import os
import sys
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient

os.environ["MONGO_DB"] = "attendance_edge_db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    yield


def test_leaderboard_competition_ranking_with_ties():
    """
    Ranks employees by total late_minutes (highest first).
    Ties share a rank and next rank is skipped (1, 2, 2, 4).
    limit applies after ranking.
    Tied rows ordered by emp_code ascending.
    """
    employees = [
        {"emp_code": "EMP0001", "name": "Emp 1", "email": "e1@test.com", "department": "Tech", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "Emp 2", "email": "e2@test.com", "department": "Tech", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0003", "name": "Emp 3", "email": "e3@test.com", "department": "Tech", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0004", "name": "Emp 4", "email": "e4@test.com", "department": "Tech", "joined_on": "2026-01-01"},
    ]
    for emp in employees:
        client.post("/employees", json=emp)

    # EMP0001: 50 late minutes (Rank 1)
    # EMP0002: 30 late minutes (Rank 2)
    # EMP0003: 30 late minutes (Rank 2) - tie with EMP0002
    # EMP0004: 10 late minutes (Rank 4) - skips rank 3!
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 50, "overtime_minutes": 0},
        {"emp_code": "EMP0002", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 30, "overtime_minutes": 0},
        {"emp_code": "EMP0003", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 30, "overtime_minutes": 0},
        {"emp_code": "EMP0004", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 10, "overtime_minutes": 0},
    ])

    # Case A: limit = 2
    # Both tied employees have rank 2 <= 2.
    # So response must contain 3 rows! (Rank 1, Rank 2, Rank 2)
    res_lim2 = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res_lim2.status_code == 200
    items2 = res_lim2.json()["items"]
    assert len(items2) == 3
    assert items2[0]["rank"] == 1 and items2[0]["emp_code"] == "EMP0001"
    assert items2[1]["rank"] == 2 and items2[1]["emp_code"] == "EMP0002"
    assert items2[2]["rank"] == 2 and items2[2]["emp_code"] == "EMP0003"

    # Case B: limit = 10
    # All 4 rows returned: rank 1, rank 2, rank 2, rank 4
    res_all = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res_all.status_code == 200
    items_all = res_all.json()["items"]
    assert len(items_all) == 4
    assert items_all[0]["rank"] == 1
    assert items_all[1]["rank"] == 2
    assert items_all[2]["rank"] == 2
    assert items_all[3]["rank"] == 4 and items_all[3]["emp_code"] == "EMP0004"


def test_zero_log_employees_in_headcount_and_future_joiners():
    """
    R9: Headcount includes employees with joined_on on or before the end of the period,
    whether or not they have logs. Excludes employees who joined after.
    """
    # 3 employees in Sales:
    # EMP0010: joined 2026-06-01 (has 1 log)
    # EMP0011: joined 2026-07-15 (0 logs)
    # EMP0012: joined 2026-08-01 (future joiner, 0 logs)
    client.post("/employees", json={"emp_code": "EMP0010", "name": "E10", "email": "e10@t.com", "department": "Sales", "joined_on": "2026-06-01"})
    client.post("/employees", json={"emp_code": "EMP0011", "name": "E11", "email": "e11@t.com", "department": "Sales", "joined_on": "2026-07-15"})
    client.post("/employees", json={"emp_code": "EMP0012", "name": "E12", "email": "e12@t.com", "department": "Sales", "joined_on": "2026-08-01"})

    db.attendance_logs.insert_one({
        "emp_code": "EMP0010",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 8.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False
    })

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    sales = items[0]
    # Headcount must be 2 (EMP0010 and EMP0011), EMP0012 excluded!
    assert sales["headcount"] == 2
    assert sales["present_days"] == 1.0
    assert sales["avg_work_hours"] == 8.0


def test_weekend_records_handling():
    """
    R7: present_days counts only Mon - Fri records.
    Weekend records still add to late and overtime totals.
    """
    client.post("/employees", json={"emp_code": "EMP0020", "name": "E20", "email": "e20@t.com", "department": "Finance", "joined_on": "2026-07-01"})

    # 2026-07-04 is Saturday, 2026-07-05 is Sunday
    # 2026-07-06 is Monday
    db.attendance_logs.insert_many([
        # Saturday record with late and overtime
        {"emp_code": "EMP0020", "date": "2026-07-04", "status": "PRESENT", "work_hours": 9.0, "late_minutes": 25, "overtime_minutes": 40, "half_day": False},
        # Sunday record with overtime
        {"emp_code": "EMP0020", "date": "2026-07-05", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "overtime_minutes": 50, "half_day": False},
        # Monday record
        {"emp_code": "EMP0020", "date": "2026-07-06", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 10, "overtime_minutes": 0, "half_day": False},
    ])

    res = client.get("/analytics/employees/EMP0020/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    # present_days should only count Monday (1.0), not Saturday or Sunday!
    assert data["present_days"] == 1.0
    # total_late_minutes includes Saturday (25) + Monday (10) = 35!
    assert data["total_late_minutes"] == 35
    # total_overtime_minutes includes Saturday (40) + Sunday (50) = 90!
    assert data["total_overtime_minutes"] == 90


def test_mid_month_joiner_working_days():
    """
    R7: For an employee who joined mid-month, working days are counted from joined_on.
    """
    # July 2026:
    # 2026-07-20 is Monday
    # Week of July 20-24: 5 days
    # Week of July 27-31: 5 days
    # Total working days from 2026-07-20 through 2026-07-31 is 10 days!
    client.post("/employees", json={"emp_code": "EMP0030", "name": "E30", "email": "e30@t.com", "department": "HR", "joined_on": "2026-07-20"})

    res = client.get("/analytics/employees/EMP0030/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 10
