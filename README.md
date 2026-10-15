# Employee Attendance & Analytics API

A FastAPI application backed by MongoDB for employee management, attendance tracking, and analytics. The project includes punch-in/punch-out, attendance regularization, monthly attendance analytics, department summaries, a late-arrival leaderboard, department trends, and MongoDB query-plan inspection.

## Features

- Employee management and attendance APIs
- Punch-in and punch-out functionality
- Attendance listing and regularization
- Monthly employee attendance analytics
- Department-wise attendance summary
- Late-arrival leaderboard with ranking
- Department attendance trends with daily metrics
- MongoDB query-plan inspection using execution statistics
- Input validation and error handling
- MongoDB indexes for commonly queried fields

## Tech Stack

- Python 3.10+
- FastAPI
- MongoDB
- PyMongo
- Uvicorn

## Project Structure

```text
attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── DECISIONS.md
├── REVIEW.md
├── README.md
├── requirements.txt
├── openapi.yaml
├── DATA_MODEL.md
├── sample_seed.py
├── test_helpers.py
└── .gitignore
```

The exact files may vary depending on which optional sample-data and testing files are included in the repository.

## Prerequisites

- Python 3.10 or newer
- MongoDB 6.0 or newer, or a compatible MongoDB Atlas deployment
- Git (optional, for cloning the repository)

## Installation

Clone the repository and enter the project directory:

```bash
git clone https://github.com/sakshi56896ff/attendance-analytics-api.git
cd attendance-analytics-api
```

Create and activate a virtual environment.

**Windows PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Configuration

Configure the following environment variables:

- `MONGO_URI` — MongoDB connection URI
- `MONGO_DB` — MongoDB database name

Set them in your local environment or a local `.env` file if the application supports loading it.

**Never commit `.env` files, database passwords, API keys, or other credentials to GitHub.**

## Run the Application

From the project root directory, run:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open the interactive API documentation:

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc

The health endpoint is available at:

```text
GET /health
```

The health endpoint should indicate readiness only when MongoDB is reachable, according to the application's implementation.

## Analytics and Performance

The application includes analytics endpoints for:

- Employee monthly attendance
- Department attendance summaries
- Late-arrival leaderboard
- Department attendance trends
- MongoDB query-plan inspection through `/admin/explain/{endpoint}`

Use the interactive Swagger documentation at `/docs` to inspect the available parameters and test requests.

For query-plan inspection, examine MongoDB's `executionStats`, including:

- `nReturned`
- `totalKeysExamined`
- `totalDocsExamined`
- `executionTimeMillis`

Check the winning query plan for index usage such as `IXSCAN`. A `COLLSCAN` may be appropriate for some queries, but should be investigated for large datasets when an index is expected to help.

## Testing Checklist

Before submitting, verify the following:

- [ ] The application starts successfully.
- [ ] MongoDB connection and health check work.
- [ ] Required API endpoints return the expected responses.
- [ ] Input validation and error cases behave correctly.
- [ ] Analytics calculations match the defined business rules.
- [ ] Query-plan inspection returns execution statistics.
- [ ] Relevant indexes are present and used where expected.
- [ ] The GitHub repository contains the latest version of the code.

An HTTP `200 OK` response alone does not guarantee that the returned calculations are correct. Verify response bodies and edge cases as well.

## Documentation

- `openapi.yaml` — API contract, when included.
- `DATA_MODEL.md` — data-model documentation, when included.
- `REVIEW.md` — review of starter-code defects and fixes.
- `DECISIONS.md` — implementation decisions and trade-offs.

The supplied problem statement and API contract are the source of truth for required behavior and business rules.

## Security

- Do not commit secrets or credentials.
- Do not upload virtual environments or Python cache files.
- Keep `.env` excluded through `.gitignore`.
- Keep the repository public for the requested review period.

## License

No license has been specified for this repository.
