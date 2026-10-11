import os
import sys
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from bson import json_util

# Set test environment
os.environ["MONGO_DB"] = "attendance_test_db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app, db, IST

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    # Clean test collections before each test
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    # Ensure indexes are created
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    yield


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud():
    # Valid creation
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000

    # Duplicate creation -> 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Validation failure: shift_start == shift_end -> 422
    bad_payload = payload.copy()
    bad_payload["emp_code"] = "EMP0002"
    bad_payload["shift_start"] = "10:00"
    bad_payload["shift_end"] = "10:00"
    res_bad = client.post("/employees", json=bad_payload)
    assert res_bad.status_code == 422

    # Validation failure: bad emp_code format -> 422
    bad_payload["emp_code"] = "INVALID"
    res_bad2 = client.post("/employees", json=bad_payload)
    assert res_bad2.status_code == 422

    # Listing & Pagination
    res_list = client.get("/employees?page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] == 1
    assert len(list_data["items"]) == 1
    assert list_data["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_and_late_rules():
    # Create employee with 09:30 shift
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Case 1: Punch in at 09:40:00 IST (exactly 10 min grace) -> late_minutes = 0
    # 2026-07-06 09:40:00 IST = 2026-07-06 04:10:00 UTC
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_grace = int(dt_grace.timestamp() * 1000)

    res1 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_grace,
        "status": "PRESENT"
    })
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0
    assert res1.json()["date"] == "2026-07-06"

    # Duplicate punch in for same day -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_grace,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Case 2: Punch in at 09:40:01 IST -> strictly > 10:00 -> late_minutes = 10
    # 2026-07-07 09:40:01 IST = 2026-07-07 04:10:01 UTC
    dt_late = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc)
    ms_late = int(dt_late.timestamp() * 1000)

    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_late,
        "status": "PRESENT"
    })
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10

    # Case 3: Punch in at 10:15:59 IST -> 45 minutes elapsed -> late_minutes = 45
    # 2026-07-08 10:15:59 IST = 2026-07-08 04:45:59 UTC
    dt_late45 = datetime(2026, 7, 8, 4, 45, 59, tzinfo=timezone.utc)
    ms_late45 = int(dt_late45.timestamp() * 1000)

    res3 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ms_late45,
        "status": "PRESENT"
    })
    assert res3.status_code == 201
    assert res3.json()["late_minutes"] == 45


def test_overnight_shift_punch_in():
    # Overnight shift: 22:00 to 06:00
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Overnight Worker",
        "email": "night@example.com",
        "department": "Operations",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch-in at 01:10 IST on 2026-07-07 (earlier than 06:00 shift end)
    # 2026-07-06 19:40 UTC = 2026-07-07 01:10 IST
    dt_night = datetime(2026, 7, 6, 19, 40, 0, tzinfo=timezone.utc)
    ms_night = int(dt_night.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": ms_night,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    # Must belong to previous calendar day (2026-07-06)
    assert res.json()["date"] == "2026-07-06"


def test_punch_out_rules():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in at 09:28 IST (2026-07-06 03:58 UTC)
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })

    # Punch out at 18:35 IST (2026-07-06 13:05 UTC)
    # Overtime = 5 min < 30 min -> overtime_minutes = 0
    # Work hours = (18:35 - 09:28) = 9h 7m = 547 min = 32820 s / 3600 = 9.1166... -> 9.12
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res.status_code == 200
    data = res.json()
    assert data["overtime_minutes"] == 0
    assert data["work_hours"] == 9.12
    assert data["half_day"] is False

    # Second punch out -> 409
    res_dup = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res_dup.status_code == 409


def test_punch_out_overtime_and_half_day():
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Bob",
        "email": "bob@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in 09:30 IST (04:00 UTC)
    in_dt = datetime(2026, 7, 0o7, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })

    # Punch out at 13:00 IST (07:30 UTC) -> 3.5 hours worked (< 4.50) -> half_day = True
    out_dt = datetime(2026, 7, 0o7, 7, 30, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0002",
        "punched_at": int(out_dt.timestamp() * 1000)
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 3.50
    assert data["half_day"] is True


def test_regularize_endpoint():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in at 10:05 IST (late_minutes = 35) on 2026-07-06
    in_dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp() * 1000),
        "status": "PRESENT"
    })
    out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(out_dt.timestamp() * 1000)
    })

    # Regularize: adjust punch_in to 09:28 IST (late_minutes becomes 0)
    new_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    new_in_ms = int(new_in_dt.timestamp() * 1000)

    patch_payload = {
        "punch_in": new_in_ms,
        "reason": "Biometric device sensor glitch",
        "regularized_by": "hr.manager"
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=patch_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.manager"
    assert h["reason"] == "Biometric device sensor glitch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0

    # Request that changes nothing -> 422
    res_noop = client.patch("/attendance/EMP0001/2026-07-06", json=patch_payload)
    assert res_noop.status_code == 422


def test_analytics_endpoints_and_explain():
    # Setup test employee and attendance records
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Record on Monday 2026-07-06
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)})

    # Test monthly endpoint
    res_monthly = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_monthly.status_code == 200
    data_monthly = res_monthly.json()
    assert data_monthly["working_days"] == 23  # Mon-Fri count in July 2026
    assert data_monthly["present_days"] == 1.0
    assert data_monthly["attendance_pct"] is not None

    # Test department summary
    res_dept = client.get("/analytics/departments/summary?month=2026-07")
    assert res_dept.status_code == 200
    data_dept = res_dept.json()
    assert len(data_dept["items"]) == 1
    assert data_dept["items"][0]["department"] == "Engineering"
    assert data_dept["items"][0]["headcount"] == 1

    # Test leaderboard
    res_lb = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res_lb.status_code == 200

    # Test department trend
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res_trend.status_code == 200
    data_trend = res_trend.json()
    assert len(data_trend["items"]) == 7

    # Test explain endpoints
    endpoints_to_test = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-07"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 10}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}),
    ]

    for ep_name, params in endpoints_to_test:
        res_exp = client.get(f"/admin/explain/{ep_name}", params=params)
        assert res_exp.status_code == 200, f"Explain failed for {ep_name}: {res_exp.text}"
        exp_data = res_exp.json()
        assert exp_data["endpoint"] == ep_name
        assert "collection" in exp_data
        exp_dump = json_util.dumps(exp_data["explain"])
        assert "IXSCAN" in exp_dump, f"{ep_name} missing IXSCAN"
        assert "COLLSCAN" not in exp_dump, f"{ep_name} has COLLSCAN"
