import os
import sys
import pathlib
import pytest
from fastapi.testclient import TestClient
from bson import json_util

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.main import app, db

client = TestClient(app)

@pytest.fixture(autouse=True)
def seed_sample_data():
    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)
    yield


def test_sample_attendance_listing():
    res = client.get("/attendance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    assert len(data["items"]) == 9
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)


def test_sample_employees_listing():
    res = client.get("/employees")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 6
    assert len(data["items"]) == 6
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)


def test_sample_monthly_summary():
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23


def test_sample_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) > 0


def test_sample_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data


def test_sample_department_trend():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-15")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 15
