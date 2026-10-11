"""
Employee Attendance & Analytics API
Full implementation adhering to openapi.yaml, DATA_MODEL.md, and rules R1-R10.
"""
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Annotated, Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# --------------------------------------------------------------------------- #
# MongoDB connection & Timezone setup
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    indexes_to_create = [
        (db.employees, [("emp_code", 1)], {"unique": True}),
        (db.employees, [("department", 1), ("joined_on", 1)], {}),
        (db.employees, [("joined_on", 1), ("department", 1)], {}),
        (db.attendance_logs, [("emp_code", 1), ("date", 1)], {"unique": True}),
        (db.attendance_logs, [("date", -1), ("emp_code", 1)], {}),
        (db.attendance_logs, [("date", 1), ("late_minutes", -1)], {}),
        (db.attendance_logs, [("emp_code", 1), ("punch_in", -1)], {}),
        (db.attendance_logs, [("date", 1), ("emp_code", 1)], {}),
    ]
    for coll, keys, kwargs in indexes_to_create:
        try:
            coll.create_index(keys, **kwargs)
        except Exception:
            pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Time & Business Rule Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def epoch_ms_to_dt(ms: int) -> datetime:
    """Converts epoch milliseconds to timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts datetime to epoch milliseconds int."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def truncate_to_seconds(ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (ms // 1000) * 1000


def to_ist(dt: datetime) -> datetime:
    """Converts a datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means overnight shift."""
    return shift_end <= shift_start


def compute_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts: a punch-in whose IST time is earlier than shift_end
    belongs to the shift that started the previous day.
    """
    h_end, m_end = map(int, shift_end.split(":"))
    end_time = time(h_end, m_end)
    if is_overnight_shift(shift_start, shift_end) and punch_in_ist.time() < end_time:
        prev_day = punch_in_ist.date() - timedelta(days=1)
        return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def get_shift_start_dt(date_str: str, shift_start: str) -> datetime:
    """Returns shift start datetime in IST for the attendance date."""
    d = dt_date.fromisoformat(date_str)
    h, m = map(int, shift_start.split(":"))
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def get_shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """
    Returns shift end datetime in IST.
    For overnight shifts, shift end is on the next calendar day.
    """
    d = dt_date.fromisoformat(date_str)
    if is_overnight_shift(shift_start, shift_end):
        d = d + timedelta(days=1)
    h, m = map(int, shift_end.split(":"))
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start;
    then whole minutes (floor) since shift_start.
    """
    punch_in_ist = to_ist(punch_in_dt)
    start_ist = get_shift_start_dt(attendance_date, shift_start)
    elapsed_seconds = (punch_in_ist - start_ist).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    hours_dec = (Decimal(str(diff_seconds)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours_dec)


def compute_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    punch_out_ist = to_ist(punch_out_dt)
    end_ist = get_shift_end_dt(attendance_date, shift_start, shift_end)
    diff_seconds = (punch_out_ist - end_ist).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is not None and work_hours < 4.50:
        return True
    return False


def round_half_up(value: float, decimals: int = 2) -> float:
    """R8: Rounding half-up."""
    q = Decimal("10") ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = dt_to_epoch_ms(created_at)
    elif isinstance(created_at, (int, float)):
        created_at_ms = int(created_at)
    else:
        created_at_ms = 0

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_attendance(doc: dict) -> dict:
    history_out = []
    for h in doc.get("history") or []:
        at_val = h.get("at")
        at_ms = dt_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
        changes_out = {}
        for k, v in (h.get("changes") or {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = dt_to_epoch_ms(to_val)
            changes_out[k] = {"from": from_val, "to": to_val}
        history_out.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    punch_in_ms = dt_to_epoch_ms(doc.get("punch_in")) if doc.get("punch_in") else None
    punch_out_ms = dt_to_epoch_ms(doc.get("punch_out")) if doc.get("punch_out") else None

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(ge=100000000000, le=4102444800000)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: 200 only if MongoDB responds to ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Creates a new employee with race-safe uniqueness on emp_code."""
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists employees paginated, filtered by department, sorted by emp_code asc."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Records punch-in, enforces R1/R2, race-safe on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ms = truncate_to_seconds(body.punched_at)
    else:
        ms = truncate_to_seconds(dt_to_epoch_ms(datetime.now(timezone.utc)))

    punch_in_dt = epoch_ms_to_dt(ms)
    punch_in_ist = to_ist(punch_in_dt)
    att_date = compute_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(409, "already punched in for this date")

    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Closes employee's most recent record with punch_in <= punched_at.
    Computes R3, R4, R5. Race-safe concurrency.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ms = truncate_to_seconds(body.punched_at)
    else:
        ms = truncate_to_seconds(dt_to_epoch_ms(datetime.now(timezone.utc)))

    punch_out_dt = epoch_ms_to_dt(ms)

    # Find the most recent record whose punch_in <= punch_out_dt
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not candidate:
        raise HTTPException(404, "no punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    punch_in_dt = candidate["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    att_date = candidate["date"]
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], att_date)
    is_half_day = compute_half_day(work_hrs)

    res = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "record already punched out")

    return serialize_attendance(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists attendance records, fast at 100k docs, sorted by date desc, emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularizes a record, recomputes derived fields, appends audit trail to history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "No record found for date")

    # Rule: setting ABSENT or LEAVE clears both punch times; supplying punches with them is 422
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
    else:
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_dt(truncate_to_seconds(body.punch_in))
        else:
            target_punch_in = rec.get("punch_in")

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_dt(truncate_to_seconds(body.punch_out))
        else:
            target_punch_out = rec.get("punch_out")

    # Presence status requires punch_in
    if target_status in ("PRESENT", "WFH", "ON_DUTY") and target_punch_in is None:
        raise HTTPException(422, "Presence status requires punch_in")

    # punch_in must stay on record's attendance date per R1
    if target_punch_in is not None:
        punch_in_ist = to_ist(target_punch_in)
        derived_date = compute_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(422, f"punch_in belongs to attendance date {derived_date}, not {date}")

    # punch_out must be after punch_in and within 24 hours
    if target_punch_out is not None:
        if target_punch_in is None:
            raise HTTPException(422, "punch_out cannot exist without punch_in")
        diff_sec = (target_punch_out - target_punch_in).total_seconds()
        if diff_sec <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if diff_sec > 24 * 3600:
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime_minutes(target_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check differences across all fields
    changes = {}
    if target_status != rec.get("status"):
        changes["status"] = {"from": rec.get("status"), "to": target_status}
    if target_punch_in != rec.get("punch_in"):
        changes["punch_in"] = {"from": rec.get("punch_in"), "to": target_punch_in}
    if target_punch_out != rec.get("punch_out"):
        changes["punch_out"] = {"from": rec.get("punch_out"), "to": target_punch_out}
    if new_work_hours != rec.get("work_hours"):
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != rec.get("late_minutes", 0):
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != rec.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != rec.get("half_day", False):
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, "Request made no changes")

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = rec.get("history") or []
    update_set = {
        "status": target_status,
        "punch_in": target_punch_in,
        "punch_out": target_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    res = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": current_history,
        },
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "Concurrent modification conflict")

    return serialize_attendance(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Aggregation Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m_num = map(int, month.split("-"))
    # Determine days in month
    if m_num == 12:
        next_month_first = dt_date(year + 1, 1, 1)
    else:
        next_month_first = dt_date(year, m_num + 1, 1)
    last_day_date = next_month_first - timedelta(days=1)
    first_day_date = dt_date(year, m_num, 1)

    joined_date = dt_date.fromisoformat(emp["joined_on"])

    # Count Mon-Fri working days from max(first_day, joined_date) to last_day
    working_days = 0
    start_date = max(first_day_date, joined_date)
    if start_date <= last_day_date:
        curr = start_date
        while curr <= last_day_date:
            if curr.weekday() < 5:  # Mon-Fri
                working_days += 1
            curr += timedelta(days=1)

    month_start_str = first_day_date.isoformat()
    month_end_str = last_day_date.isoformat()

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start_str, "$lte": month_end_str},
            }
        },
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date"}},
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "is_mon_fri": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_mon_fri"]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": ["$is_leave", 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": ["$is_late", 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_results = list(db.attendance_logs.aggregate(pipeline))
    if agg_results:
        res = agg_results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via cross-collection aggregation.
    Headcount per R9 (includes people with zero logs).
    """
    year, m_num = map(int, month.split("-"))
    if m_num == 12:
        next_month_first = dt_date(year + 1, 1, 1)
    else:
        next_month_first = dt_date(year, m_num + 1, 1)
    last_day_date = next_month_first - timedelta(days=1)
    first_day_date = dt_date(year, m_num, 1)

    month_start_str = first_day_date.isoformat()
    month_end_str = last_day_date.isoformat()

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "date": "$$log.date",
                            "status": "$$log.status",
                            "half_day": {"$ifNull": ["$$log.half_day", False]},
                            "work_hours": "$$log.work_hours",
                            "late_minutes": {"$ifNull": ["$$log.late_minutes", 0]},
                            "day_of_week": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}
                            },
                        },
                    }
                },
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "unique_employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs.day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$unique_employees"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        avg_wh = r["avg_work_hours"]
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks late-comers using standard competition ranking ($rank) with ties.
    limit applies after ranking.
    """
    year, m_num = map(int, month.split("-"))
    if m_num == 12:
        next_month_first = dt_date(year + 1, 1, 1)
    else:
        next_month_first = dt_date(year, m_num + 1, 1)
    last_day_date = next_month_first - timedelta(days=1)
    first_day_date = dt_date(year, m_num, 1)

    month_start_str = first_day_date.isoformat()
    month_end_str = last_day_date.isoformat()

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
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
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with $densify gap filling
    and 7-day moving average using $setWindowFields in MongoDB.
    """
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")

    d_from = dt_date.fromisoformat(from_)
    d_to = dt_date.fromisoformat(to)
    if d_to < d_from or (d_to - d_from).days > 92:
        raise HTTPException(422, "Invalid date range or range exceeds 92 days")

    dt_bound_start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    d_to_next = d_to + timedelta(days=1)
    dt_bound_end = datetime(d_to_next.year, d_to_next.month, d_to_next.day, tzinfo=timezone.utc)

    # Get employee codes for this department
    emp_docs = list(db.employees.find({"department": department}, {"emp_code": 1}))
    emp_codes = [e["emp_code"] for e in emp_docs]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_, "$lte": to},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "dt": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        {
            "$densify": {
                "field": "dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_bound_start, dt_bound_end],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        rate = r.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)
        m_avg = r.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(m_avg, 4)
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: Optional[int] = 1,
    page_size: Optional[int] = 20,
):
    """
    Runs MongoDB explain with executionStats verbosity for the main query or pipeline
    of the named endpoint, built exactly as that endpoint builds it.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        p = page or 1
        ps = page_size or 20
        if p < 1 or ps < 1 or ps > 100:
            raise HTTPException(422, "Invalid pagination")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (p - 1) * ps,
            "limit": ps,
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        clean_explain = json_util.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(422, "emp_code and valid month are required")

        year, m_num = map(int, month.split("-"))
        if m_num == 12:
            next_month_first = dt_date(year + 1, 1, 1)
        else:
            next_month_first = dt_date(year, m_num + 1, 1)
        last_day_date = next_month_first - timedelta(days=1)
        first_day_date = dt_date(year, m_num, 1)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day_date.isoformat(), "$lte": last_day_date.isoformat()},
                }
            },
            {
                "$addFields": {
                    "date_obj": {"$dateFromString": {"dateString": "$date"}},
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "is_leave": {"$eq": ["$status", "LEAVE"]},
                    "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                }
            },
            {
                "$addFields": {
                    "is_mon_fri": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_presence", "$is_mon_fri"]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": ["$is_leave", 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": ["$is_late", 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        clean_explain = json_util.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "department_summary":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(422, "valid month is required")

        year, m_num = map(int, month.split("-"))
        if m_num == 12:
            next_month_first = dt_date(year + 1, 1, 1)
        else:
            next_month_first = dt_date(year, m_num + 1, 1)
        last_day_date = next_month_first - timedelta(days=1)
        first_day_date = dt_date(year, m_num, 1)

        emp_match = {"joined_on": {"$lte": last_day_date.isoformat()}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp_code"]},
                                        {"$gte": ["$date", first_day_date.isoformat()]},
                                        {"$lte": ["$date", last_day_date.isoformat()]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        clean_explain = json_util.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": clean_explain,
        }

    elif endpoint == "late_leaderboard":
        if not month or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(422, "valid month is required")

        year, m_num = map(int, month.split("-"))
        if m_num == 12:
            next_month_first = dt_date(year + 1, 1, 1)
        else:
            next_month_first = dt_date(year, m_num + 1, 1)
        last_day_date = next_month_first - timedelta(days=1)
        first_day_date = dt_date(year, m_num, 1)

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day_date.isoformat(), "$lte": last_day_date.isoformat()},
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
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": "$total_late_minutes",
                    "late_count": "$late_count",
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit or 10}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        clean_explain = json_util.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to parameters are required")

        d_from = dt_date.fromisoformat(from_)
        d_to = dt_date.fromisoformat(to)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(422, "Invalid date range or range exceeds 92 days")

        dt_bound_start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
        d_to_next = d_to + timedelta(days=1)
        dt_bound_end = datetime(d_to_next.year, d_to_next.month, d_to_next.day, tzinfo=timezone.utc)

        emp_docs = list(db.employees.find({"department": department}, {"emp_code": 1}))
        emp_codes = [e["emp_code"] for e in emp_docs]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_, "$lte": to},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$addFields": {
                    "dt": {"$dateFromString": {"dateString": "$_id"}}
                }
            },
            {
                "$densify": {
                    "field": "dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [dt_bound_start, dt_bound_end],
                    },
                }
            },
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        raw_explain = db.command("explain", cmd, verbosity="executionStats")
        clean_explain = json_util.loads(json_util.dumps(raw_explain))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": clean_explain,
        }

    raise HTTPException(422, "Unknown endpoint")
