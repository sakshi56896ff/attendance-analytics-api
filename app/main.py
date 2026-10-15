
"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import logging
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (the default): real environment variables win over .env

log = logging.getLogger("attendance")

# tz_aware=True makes PyMongo return timezone-aware (UTC) datetimes instead of naive ones.
# serverSelectionTimeoutMS keeps /health from hanging for 30 s when MongoDB is down.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60        # R2: late only if strictly MORE than 10:00 after shift start
MIN_OVERTIME_MINUTES = 30      # R3
HALF_DAY_HOURS = 4.5           # R5
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
EPOCH_MS_MIN = 100_000_000_000      # below this a value looks like seconds -> 422
EPOCH_MS_MAX = 4_102_444_800_000    # 2100-01-01
MAX_WORK_SECONDS = 24 * 3600        # punch-out / correction may be at most 24 h after punch-in


def ensure_indexes() -> None:
    """Created at every startup; create_index is idempotent (a no-op when the index exists)."""
    # emp_code is the business key: unique, and the sort key of GET /employees.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # GET /employees?department=X sorted by emp_code; also the department lookups in analytics.
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # One record per (emp_code, date): the guarantee behind race-safe punch-in, and the PATCH lookup.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    # GET /attendance sorts by date DESC then emp_code ASC. An index only serves a sort if the
    # directions match exactly, so this one is (date -1, emp_code 1), not (date 1, emp_code 1).
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )
    # Same sort when filtering by status (equality field first, then the sort fields).
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_desc_emp"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Do not crash: /health will answer 503 until MongoDB is reachable.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time conversion (API <-> MongoDB)
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Always work with timezone-aware UTC datetimes. A naive datetime (what PyMongo returns
    by default) is UTC in this system, so we attach the UTC tzinfo to it."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Aware/naive datetime -> epoch milliseconds, using integer maths only (no float error)."""
    delta = as_utc(dt) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Helpers: shifts and the attendance date (R1)
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> tuple:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    # (hour, minute) tuples compare the way clock times do.
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in, except that on an overnight shift a punch-in
    EARLIER than shift_end belongs to the shift that started the previous day."""
    ist = as_utc(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute) < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_at(date_str: str, hhmm: str) -> datetime:
    """The IST instant at which clock time `hhmm` occurs on attendance date `date_str`."""
    d = date.fromisoformat(date_str)
    hour, minute = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The shift end of an overnight shift falls on the NEXT calendar day (R1)."""
    end = shift_at(date_str, shift_end)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


# --------------------------------------------------------------------------- #
# Helpers: derived fields (R2 - R5)
# --------------------------------------------------------------------------- #
def round_half_up(value, places: int = 2) -> float:
    """R8: round half UP. Python's round() rounds half to even and works on the binary float,
    e.g. round(2.675, 2) == 2.67. Going through str() and Decimal gives the intended 2.68."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes (floor)
    since shift start, not since the end of the grace period. Measured from the shift start
    of the ATTENDANCE date, so overnight shifts work."""
    seconds = int((as_utc(punch_in) - shift_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) after shift end, counted only when that is at least 30."""
    seconds = int((as_utc(punch_out) - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half up."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: judged on the ROUNDED work_hours; null (no punch-out yet) is not a half day."""
    return work_hours is not None and work_hours < HALF_DAY_HOURS


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  date_str: str, shift_start: str, shift_end: str) -> dict:
    """One place that produces the four derived fields, following the table in DATA_MODEL.md.
    Punch-in, punch-out and the PATCH endpoint all call this, so they cannot disagree."""
    if status not in PRESENT_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, date_str, shift_start, shift_end),
        "half_day": compute_half_day(work_hours),
    }




# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# StrictInt rejects floats, strings and booleans (pydantic would otherwise coerce "123" or 1.0 to 123).
# The bounds reject values that look like seconds (R: "below 100000000000") and absurd far-future ones.
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


def _check_date_string(value: str) -> str:
    """Strict YYYY-MM-DD that is a real calendar date. The regex matters: from Python 3.11,
    date.fromisoformat() also accepts forms like '20260706', which the contract does not allow."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("must look like YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("is not a real calendar date")
    return value


DateStr = Annotated[str, AfterValidator(_check_date_string)]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]  # R10

HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def unprocessable(loc: tuple, message: str, value=None):
    """Raise a 422 in FastAPI's own error format, for rules that span several fields."""
    raise RequestValidationError(
        [{"type": "value_error", "loc": loc, "msg": message, "input": value}]
    )


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """Only these fields can be sent. Derived fields are not declared, so a client that sends
    work_hours, late_minutes, ... has them silently ignored, as the contract says."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation: MongoDB document -> API response (datetimes become epoch milliseconds)
# --------------------------------------------------------------------------- #
def _ms_if_datetime(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms_if_datetime(doc.get("created_at")),
    }


def history_entry_out(entry: dict) -> dict:
    """from/to values that are datetimes (punch_in, punch_out) become milliseconds too."""
    changes = {
        field: {"from": _ms_if_datetime(c.get("from")), "to": _ms_if_datetime(c.get("to"))}
        for field, c in (entry.get("changes") or {}).items()
    }
    return {"at": _ms_if_datetime(entry.get("at")), "by": entry.get("by"),
            "reason": entry.get("reason"), "changes": changes}


def attendance_out(doc: dict) -> dict:
    """Never exposes _id. Older records may lack half_day / history / the minute counters,
    so missing means false / [] / 0 (DATA_MODEL.md, 'Gaps in the data')."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_if_datetime(doc.get("punch_in")),
        "punch_out": _ms_if_datetime(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_entry_out(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # No find_one() first: check-then-insert lets two concurrent requests both pass the check.
        # The unique index on emp_code makes the database the single judge.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)  # counts the FILTERED set
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derive_fields(body.status, punched_at, None, date_str, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        # Race-safe: of N simultaneous inserts for the same (emp_code, date) the unique index lets
        # exactly one succeed; the others raise DuplicateKeyError and become 409.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and (later) the explain endpoint so both build the query identically."""
    if date_from and date_to and date_from > date_to:  # ISO dates compare correctly as strings
        unprocessable(("query", "date_from"), "date_from must not be after date_to", date_from)
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    out_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # The most recent record whose punch_in is at or before the punch-out. Not "the record for
    # today's date": an overnight shift punches out on the day AFTER its attendance date.
    # Records with punch_in null (ABSENT / LEAVE) never match a $lte comparison with a date.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_at}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    in_at = as_utc(rec["punch_in"])
    if out_at <= in_at or (out_at - in_at).total_seconds() > MAX_WORK_SECONDS:
        unprocessable(("body", "punched_at"), "must be after punch_in and within 24 hours of it", body.punched_at)

    derived = derive_fields(rec["status"], in_at, out_at, rec["date"], emp["shift_start"], emp["shift_end"])
    # Atomic close: the filter requires punch_out to still be empty (null also matches a missing field),
    # and that punch_in / status are still what we read. Of two simultaneous punch-outs only one
    # matches; the other gets None back and becomes a 409. late_minutes was fixed at punch-in.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None,
         "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": out_at, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


def _same(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return as_utc(a) == as_utc(b)
    return a == b


CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # ---- the record as it will look after the correction --------------------------------------
    new_status = body.status or rec["status"]
    if new_status in PRESENT_STATUSES:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else (
            as_utc(rec["punch_in"]) if rec.get("punch_in") else None)
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else (
            as_utc(rec["punch_out"]) if rec.get("punch_out") else None)
        if new_in is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None:
            if new_out <= new_in or (new_out - new_in).total_seconds() > MAX_WORK_SECONDS:
                unprocessable(("body", "punch_out"), "must be after punch_in and within 24 hours of it", body.punch_out)
    else:  # ABSENT / LEAVE clear both punch times, so supplying one is contradictory
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(("body",), "ABSENT and LEAVE cannot carry punch times")
        new_in = new_out = None

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, date, emp["shift_start"], emp["shift_end"])}
    old = {
        "status": rec["status"],
        "punch_in": as_utc(rec["punch_in"]) if rec.get("punch_in") else None,
        "punch_out": as_utc(rec["punch_out"]) if rec.get("punch_out") else None,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    # "A request that changes nothing is 422" is judged on what the CLIENT asked to change.
    if all(_same(old[f], new[f]) for f in ("status", "punch_in", "punch_out")):
        unprocessable(("body",), "the request changes nothing")
    # history lists every field that actually changed, derived ones included.
    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if not _same(old[f], new[f])}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- optimistic concurrency ---------------------------------------------------------------
    # The update only applies if the record is exactly as we read it (same status, punches and
    # history length). If another request changed it meanwhile nothing matches: 409 for the loser,
    # so no history entry is ever overwritten or lost. Missing/null history counts as length 0.
    n_history = len(rec.get("history") or [])
    history_is_unchanged = (
        {"history": {"$size": n_history}} if n_history
        else {"$or": [{"history": None}, {"history": {"$size": 0}}]}
    )
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date, "status": rec["status"],
         "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"), **history_is_unchanged},
        {"$set": new, "$push": {"history": entry}},   # $push appends; it never replaces the array
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by someone else; reload and try again")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
from bson.json_util import dumps, loads

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$",
    ),
):
    # 1. Find the employee. Unknown employees must receive 404.
    emp = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "joined_on": 1},
    )

    if emp is None:
        raise HTTPException(404, "employee not found")

    # 2. Get the first and last dates of the requested month.
    year, month_num = map(int, month.split("-"))

    if year < 1:
        unprocessable(
            ("query", "month"),
            "month must be a valid YYYY-MM month",
            month,
        )

    month_start = date(year, month_num, 1)
    last_day = calendar.monthrange(year, month_num)[1]
    month_end = date(year, month_num, last_day)

    # 3. Calculate weekdays from the later of joined_on
    # and the first day of the month.
    joined_on = date.fromisoformat(emp["joined_on"])
    first_working_date = max(joined_on, month_start)

    working_days = 0
    current_day = first_working_date

    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # 4. Aggregate attendance metrics inside MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$set": {
                "_parsed_date": {
                    "$dateFromString": {"dateString": "$date"}
                },
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_overtime": {
                    "$ifNull": ["$overtime_minutes", 0]
                },
            }
        },
        {
            "$set": {
                "_is_weekday": {
                    "$in": [
                        {"$dayOfWeek": "$_parsed_date"},
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_is_weekday",
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$_late", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_overtime"},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    # 5. No attendance records is different from an unknown employee.
    metrics = result[0] if result else {}

    present_days = metrics.get("present_days", 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 4)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": attendance_pct,
    }



from datetime import date
from fastapi import Query


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
):
    # Validate month and determine its calendar boundaries.
    try:
        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1)
        if month_num == 12:
            month_end = date(year + 1, 1, 1).replace(
                day=1
            )
        else:
            month_end = date(year, month_num + 1, 1)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must be a valid YYYY-MM month",
        )

    # Exclusive next-month boundary, represented as ISO date strings.
    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    employee_match = {"joined_on": {"$lt": month_end_str}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        # Start with employees so people with no attendance logs count.
        {"$match": employee_match},

        # Fetch only attendance records in the requested month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lt": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },

        # Count weekdays from the later of joined_on and month start.
        {
            "$addFields": {
                "effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", month_start_str]},
                        "$joined_on",
                        month_start_str,
                    ]
                }
            }
        },
        {
            "$addFields": {
                "start_date_obj": {
                    "$dateFromString": {"dateString": "$effective_start"}
                },
                "end_date_obj": {
                    "$dateFromString": {"dateString": month_end_str}
                },
            }
        },
        {
            "$addFields": {
                "working_date_offsets": {
                    "$range": [
                        0,
                        {
                            "$max": [
                                0,
                                {
                                    "$dateDiff": {
                                        "startDate": "$start_date_obj",
                                        "endDate": "$end_date_obj",
                                        "unit": "day",
                                    }
                                },
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$addFields": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": "$working_date_offsets",
                                    "as": "offset",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$start_date_obj",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                            "as": "day",
                            "cond": {
                                "$and": [
                                    {"$gte": [{"$dayOfWeek": "$$day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$$day"}, 6]},
                                ]
                            },
                        }
                    }
                }
            }
        },

        # Present days count only on weekdays; half days count as 0.5.
        {
            "$addFields": {
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    2,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    6,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "valid_work_hours": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$$log.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$ne": ["$$log.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "log",
                        "in": "$$log.work_hours",
                    }
                },
                "late_count_employee": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_employee": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count_employee": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count_employee": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },

        # Aggregate employee metrics into one row per department.
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count_employee"},
                "total_late_minutes": {"$sum": "$total_late_employee"},
                "leave_count": {"$sum": "$leave_count_employee"},
                "on_duty_count": {"$sum": "$on_duty_count_employee"},
                "work_hours_arrays": {"$push": "$valid_work_hours"},
            }
        },

        # Flatten work-hour arrays so the average is per record, not per employee.
        {
            "$addFields": {
                "all_work_hours": {
                    "$reduce": {
                        "input": "$work_hours_arrays",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                }
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$all_work_hours"}, 0]},
                        {
                            "$round": [
                                {"$avg": "$all_work_hours"},
                                2,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }



from datetime import date
from fastapi import Query


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, month_num = map(int, month.split("-"))
    month_start = date(year, month_num, 1).isoformat()

    if month_num == 12:
        month_end = date(year + 1, 1, 1).isoformat()
    else:
        month_end = date(year, month_num + 1, 1).isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lt": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Apply department filter before ranking.
    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend([
        # Rank only by total minutes, so ties receive the same rank.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # Deterministic display order for employees with equal totals.
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Limit applies AFTER ranking; tied ranks are all included.
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


from datetime import date, timedelta
from fastapi import Query, HTTPException


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate dates and requested range.
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must be valid YYYY-MM-DD dates",
        )

    if start.isoformat() != from_date or end.isoformat() != to_date:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if (end - start).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Check whether this department exists.
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    end_exclusive = (end + timedelta(days=1)).isoformat()
    start_str = start.isoformat()
    end_str = end.isoformat()

    pipeline = [
        # Employees joined by the end of the requested period.
        {
            "$match": {
                "department": department,
                "joined_on": {"$lt": end_exclusive},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_str]},
                                    {"$lt": ["$date", end_exclusive]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "all_logs": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", ["$$this"]]},
                    }
                }
            }
        },
        # Build the requested calendar dates inside MongoDB.
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "joined_on": "$joined_on",
                        "logs": "$all_logs",
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                (end - start).days + 1,
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": start_str,
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
                "employees": 1,
            }
        },
        {"$unwind": "$days"},
        {
            "$addFields": {
                "day_date": {
                    "$dateFromString": {"dateString": "$days"}
                },
                "day_logs": {
                    "$reduce": {
                        "input": "$employees",
                        "initialValue": [],
                        "in": {
                            "$concatArrays": [
                                "$$value",
                                {
                                    "$filter": {
                                        "input": "$$this.logs",
                                        "as": "log",
                                        "cond": {
                                            "$eq": ["$$log.date", "$days"]
                                        },
                                    }
                                },
                            ]
                        },
                    }
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {
                                "$lte": [
                                    "$$employee.joined_on",
                                    "$days",
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day_date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day_date"}, 6]},
                    ]
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {
                                            "$divide": [
                                                "$present_count",
                                                "$headcount",
                                            ]
                                        },
                                        100,
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"days": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"days": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$days",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                },
            }
        },
    ]

    rows = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "from": start_str,
        "to": end_str,
        "items": rows,
    }


from typing import Literal
from bson.json_util import dumps, loads
from fastapi import Query, HTTPException
from fastapi.responses import JSONResponse
from bson.json_util import dumps
from fastapi.responses import Response
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def require(value, name):
        if not value:
            raise HTTPException(
                status_code=422,
                detail=f"{name} is required for endpoint {endpoint}",
            )

    def month_bounds(value):
        require(value, "month")
        try:
            y, m = map(int, value.split("-"))
            if not 1 <= m <= 12 or len(value) != 7:
                raise ValueError
            start = date(y, m, 1).isoformat()
            end = (
                date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
            ).isoformat()
            return start, end
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")

    skip = (page - 1) * page_size

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status

        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1},
            "skip": skip,
            "limit": page_size,
        }
        collection = "attendance_logs"
        explain = db.command(
            "explain", command, verbosity="executionStats"
        )

    elif endpoint == "employee_monthly":
        require(emp_code, "emp_code")
        start, end = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": end}}},
            {"$sort": {"date": 1}},
            {"$skip": skip},
            {"$limit": page_size},
        ]
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        start, end = month_bounds(month)
        employee_filter = {"joined_on": {"$lt": end}}
        if department:
            employee_filter["department"] = department

        pipeline = [
            {"$match": employee_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", start]},
                                        {"$lt": ["$date", end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$skip": skip},
            {"$limit": page_size},
        ]
        collection = "employees"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        start, end = month_bounds(month)
        pipeline = [
            {"$match": {
                "date": {"$gte": start, "$lt": end},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit},
        ]
        if department:
            pipeline.extend([
                {"$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
            ])

        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    else:  # department_trend
        from_date = from_date or date_from
        to_date = to_date or date_to

        require(from_date, "from")
        require(to_date, "to")
        
        require(department, "department")
        # require(from_date, "from")
        # require(to_date, "to")

        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "from and to must be YYYY-MM-DD")

        if end_date < start_date or (end_date - start_date).days + 1 > 92:
            raise HTTPException(422, "Invalid date range; maximum is 92 days")

        pipeline = [
            {"$match": {
                "department": department,
                "joined_on": {"$lte": to_date},
            }},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$emp_code", "$$code"]},
                            {"$gte": ["$date", from_date]},
                            {"$lte": ["$date", to_date]},
                        ]}}},
                    ],
                    "as": "logs",
                }
            },
            {"$skip": skip},
            {"$limit": page_size},
        ]
        collection = "employees"
        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
    return Response(
        content=dumps({
            "endpoint": endpoint,
            "collection": collection,
            "explain": explain,
        }),
        media_type="application/json",
    )
