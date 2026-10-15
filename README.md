# Employee Attendance & Analytics API

This project implements an employee attendance API backed by MongoDB. It includes employee management, punch-in/punch-out, attendance regularization, monthly and department analytics, a late leaderboard, a department trend, and query-plan inspection.

## Run locally

Use Python 3.10+ and MongoDB 6.0 or newer.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your local environment (or a local `.env` file). Do not commit `.env` or credentials.

Start the API from the project directory:

```bash
uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` to try the endpoints. `/health` should report readiness only when MongoDB is reachable.

## Before submitting

- Confirm the final `app/main.py` is saved and starts cleanly.
- Test normal responses and validation/error cases, not just HTTP 200.
- For `/admin/explain/{endpoint}`, inspect `executionStats` and confirm the relevant queries use indexes where expected.
- Keep the repository public as requested, but do not commit `.env`, database credentials, API keys, virtual environments, or generated local data.

The API contract and business rules are defined in `openapi.yaml` and the supplied problem statement. Use them as the source of truth when checking behavior.
