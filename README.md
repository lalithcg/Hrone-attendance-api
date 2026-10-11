# Employee Attendance & Analytics API

A FastAPI and MongoDB service providing punch-in/out attendance tracking, manual regularization with audit trails, and aggregation-driven analytics pipelines.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0 and 9.0)

## Setup & Running

1. **Install dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Create a `.env` file or export environment variables:
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

3. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

The service is available at `http://localhost:8000` with Swagger documentation at `http://localhost:8000/docs`.

## Key Features & Guarantees
- **Startup Index Creation**: Indexes on `employees` and `attendance_logs` are initialized idempotently on startup within the FastAPI lifespan context.
- **Concurrency & Atomicity**: Punch-in concurrency is safeguarded by MongoDB's unique compound index `{emp_code: 1, date: 1}`. Punch-out and regularizations execute atomic conditional updates to prevent race condition overrides and history loss.
- **Aggregation-Backed Analytics**: Analytics endpoints (Monthly Summary, Cross-Collection Department Summary, Late Leaderboard with `$setWindowFields` / `$rank`, and Department Trend with `$densify` gap filling) are executed directly in MongoDB aggregation pipelines.
- **Index Optimization**: Verified with `GET /admin/explain/{endpoint}`, using index scans (`IXSCAN`) with zero collection scans (`COLLSCAN`).

## Project Structure
- `app/main.py`: Full API implementation and business rule logic.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect catalog and fixes for the starter code.
- `DECISIONS.md`: Design rationale for indexes, concurrency, ranking, headcount, and scaling.
