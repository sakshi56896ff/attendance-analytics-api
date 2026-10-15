"""Quick checks for the rule helpers (R1 - R5, R8). Run from the repo root:  python test_helpers.py
No database needed. Every case comes from the spec text or the sample documents in DATA_MODEL.md."""
from datetime import datetime

from app.main import (IST, UTC, as_utc, attendance_date, compute_half_day, compute_late_minutes,
                      compute_overtime_minutes, compute_work_hours, derive_fields, dt_to_ms, ms_to_dt,
                      round_half_up)


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# ---- R2: late (shift 09:30, grace 10 minutes) ------------------------------------------------
D = "2026-07-06"
assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), D, "09:30") == 0      # exactly 10:00 -> not late
assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), D, "09:30") == 10     # 10:01 -> late, 10 min
assert compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), D, "09:30") == 45   # floor, from shift start
assert compute_late_minutes(ist(2026, 7, 6, 9, 0, 0), D, "09:30") == 0       # early -> never negative

# ---- R3: overtime (shift_end 18:30) ----------------------------------------------------------
ot = lambda out: compute_overtime_minutes(out, D, "09:30", "18:30")
assert ot(ist(2026, 7, 6, 18, 59, 59)) == 0       # 29 min -> below the 30 minimum
assert ot(ist(2026, 7, 6, 19, 0, 0)) == 30        # exactly 30 -> counted
assert ot(ist(2026, 7, 6, 19, 10, 0)) == 40
assert ot(ist(2026, 7, 6, 17, 0, 0)) == 0         # left early

# ---- R4 / R8: half-up rounding ---------------------------------------------------------------
assert compute_work_hours(utc(2026, 7, 6, 0, 0, 0), utc(2026, 7, 6, 0, 0, 54)) == 0.02   # 0.015 -> 0.02 (round() gives 0.01)
assert round_half_up(2.675) == 2.68               # round(2.675, 2) gives 2.67
assert round_half_up(0.285) == 0.29
assert round_half_up(0.66665, 4) == 0.6667

# ---- R5: half day is judged on the ROUNDED hours ---------------------------------------------
t0 = utc(2026, 7, 6, 4, 0, 0)
assert compute_work_hours(t0, utc(2026, 7, 6, 8, 29, 42)) == 4.5     # 16182 s = 4.495 h -> 4.50
assert compute_half_day(4.5) is False
assert compute_work_hours(t0, utc(2026, 7, 6, 8, 29, 41)) == 4.49    # 16181 s = 4.4947 h -> 4.49
assert compute_half_day(4.49) is True
assert compute_half_day(None) is False

# ---- R1: truncation to whole seconds and ms <-> datetime -------------------------------------
assert ms_to_dt(1783312500900) == ms_to_dt(1783312500000) == utc(2026, 7, 6, 4, 35, 0)
assert dt_to_ms(utc(2026, 7, 6, 4, 35, 0)) == 1783312500000
assert dt_to_ms(datetime(2026, 7, 6, 4, 35, 0)) == 1783312500000    # naive datetime is treated as UTC
assert as_utc(datetime(2026, 7, 6, 4, 35, 0)).tzinfo is UTC

# ---- R1: attendance date, normal and overnight -----------------------------------------------
assert attendance_date(ist(2026, 7, 6, 23, 30), "09:30", "18:30") == "2026-07-06"
assert attendance_date(ist(2026, 7, 6, 0, 30), "09:30", "18:30") == "2026-07-06"  # IST date, not UTC date
assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
assert attendance_date(ist(2026, 7, 7, 5, 59, 30), "22:00", "06:00") == "2026-07-06"  # before shift_end -> previous day
assert attendance_date(ist(2026, 7, 7, 6, 0, 30), "22:00", "06:00") == "2026-07-07"   # not earlier than 06:00

# ---- Sample documents from DATA_MODEL.md -----------------------------------------------------
# 1. normal full day, shift 09:30-18:30
d = derive_fields("PRESENT", utc(2026, 7, 6, 3, 58), utc(2026, 7, 6, 13, 5), "2026-07-06", "09:30", "18:30")
assert d == {"work_hours": 9.12, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}, d
# 2. late with overtime
d = derive_fields("PRESENT", utc(2026, 7, 6, 4, 35), utc(2026, 7, 6, 13, 40), "2026-07-06", "09:30", "18:30")
assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}, d
# 3. open record
d = derive_fields("PRESENT", utc(2026, 7, 7, 4, 5), None, "2026-07-07", "09:30", "18:30")
assert d == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}, d
# 4. half day, WFH
d = derive_fields("WFH", utc(2026, 7, 7, 4, 0), utc(2026, 7, 7, 7, 30), "2026-07-07", "09:30", "18:30")
assert d == {"work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True}, d
# 5/6. absent and leave
for status in ("ABSENT", "LEAVE"):
    d = derive_fields(status, None, None, "2026-07-08", "09:30", "18:30")
    assert d == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}, d
# 7. overnight 22:00-06:00: in 21:55 IST on the 6th, out 06:40 IST on the 7th
d = derive_fields("PRESENT", utc(2026, 7, 6, 16, 25), utc(2026, 7, 7, 1, 10), "2026-07-06", "22:00", "06:00")
assert d == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}, d
# overnight, punch-in at 05:30 IST on the 7th belongs to the 6th, so it is 7.5 h after 22:00 -> 450 min late
assert attendance_date(ist(2026, 7, 7, 5, 30), "22:00", "06:00") == "2026-07-06"
assert compute_late_minutes(ist(2026, 7, 7, 5, 30), "2026-07-06", "22:00") == 450

print("all helper checks passed")