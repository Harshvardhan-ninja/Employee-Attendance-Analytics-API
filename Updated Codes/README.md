# Employee Attendance & Analytics API

A REST API built with **Python, FastAPI, and MongoDB** to manage employee attendance and provide monthly, departmental, and late-arrival analytics.

## Project Structure

```text
candidate_kit/
├── app/
│   └── main.py
├── sample_data/
├── .env.example
├── .gitignore
├── requirements.txt
├── sample_seed.py
├── openapi.yaml
├── DATA_MODEL.md
├── REVIEW.md
├── DECISIONS.md
└── README.md
```

## Prerequisites

- Python 3.10 or newer
- MongoDB 6.0 or newer (MongoDB 7.0 is used by the grader)
- pip
- Git

## Local Setup

### 1. Create and activate a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example environment file:

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
```

**Linux/macOS:**

```bash
cp .env.example .env
```

Configure the following variables in `.env`:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Ensure MongoDB is running before starting the application.

### 4. Load sample data

```bash
python sample_seed.py
```

This loads the supplied sample attendance data into MongoDB. Ensure the configured database and collections match the sample seeder's configuration.

### 5. Start the API

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## API Documentation

Once the application is running, open:

- **Swagger UI:** http://127.0.0.1:8000/docs
- **ReDoc:** http://127.0.0.1:8000/redoc
- **Health endpoint:** http://127.0.0.1:8000/health

Refer to `openapi.yaml` for the API contract and business rules R1–R10.

## Main Features

- Employee record management
- Attendance status and punch-in/punch-out operations
- Attendance record listing and correction
- Monthly employee attendance analytics
- Department-level attendance summaries
- Late-arrival leaderboard
- Department attendance trends
- Endpoint explanation and validation
- MongoDB persistence

## Database

The application uses two main collections:

- `employees` — employee details and shift information
- `attendance_logs` — daily attendance records, calculated attendance metrics, and correction history

The application reads its database configuration from environment variables. MongoDB stores timestamps as BSON dates, while API timestamp representations follow the specified contract.

## Testing

Use Swagger UI at `/docs` to test the endpoints.

Test cases should cover:

- Valid and invalid requests
- Duplicate employee codes and attendance records
- Punch-in and punch-out operations
- Overnight shifts and late arrivals
- Employees with no attendance logs
- Attendance corrections and history
- Department summaries and leaderboard ties

The provided sample dataset is intended for initial testing. The grader may include additional cases not present in the samples.

## Technology Stack

- Python
- FastAPI
- Uvicorn
- MongoDB
- PyMongo
- python-dotenv
- Swagger UI / OpenAPI

## Important Files

- `PROBLEM_STATEMENT.docx` — assessment instructions and evaluation criteria
- `openapi.yaml` — API contract and business rules
- `DATA_MODEL.md` — database schema and sample documents
- `app/main.py` — API implementation
- `sample_seed.py` — sample data loader
- `REVIEW.md` — implementation review
- `DECISIONS.md` — design decisions and trade-offs

## Security and Configuration

- Never commit `.env` or credentials.
- Keep `.env.example` free of real secrets.
- Do not commit virtual environments or generated cache files.
- Validate input and handle database errors appropriately.

## Submission

Submit a public Git repository containing the required implementation and documentation:

- `app/main.py`
- `requirements.txt`
- `README.md`
- `REVIEW.md`
- `DECISIONS.md`

Include other assessment files as required by the evaluator. Follow the original submission instructions, which explicitly prohibit including a Dockerfile.

## Future Improvements

- Docker-based development environment, if permitted by the assessment
- Automated unit and integration tests
- Authentication and role-based access control
- Query performance optimization and monitoring
- Deployment with production configuration and logging
