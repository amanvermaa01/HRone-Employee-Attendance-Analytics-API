"""
Employee Attendance & Analytics API
Stack: Python 3.11+, FastAPI, PyMongo (sync), MongoDB 6.0+

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""

import calendar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import os
from pathlib import Path
import time
from typing import Any, List, Literal, Optional

import json
from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as FPath
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Environment & MongoDB Setup
# --------------------------------------------------------------------------- #
base_dir = Path(__file__).resolve().parent
for p in [Path(".env"), base_dir / ".env", base_dir.parent / ".env"]:
    if p.exists():
        load_dotenv(p)
load_dotenv()

if "MONGO_URI" not in os.environ:
    os.environ["MONGO_URI"] = "mongodb://localhost:27017"
if "MONGO_DB" not in os.environ:
    os.environ["MONGO_DB"] = "hrattendance"

MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.environ["MONGO_DB"]

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Lifespan: Ping & Idempotent Index Creation
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify MongoDB connection at startup
    client.admin.command("ping")

    # Create indexes idempotently with background=False
    employees.create_index([("emp_code", ASCENDING)], unique=True, background=False)
    employees.create_index([("department", ASCENDING)], background=False)
    employees.create_index([("joined_on", ASCENDING)], background=False)
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], background=False)

    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, background=False)
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], background=False)
    attendance.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], background=False)
    attendance.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], background=False)
    attendance.create_index([("status", ASCENDING)], background=False)
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], background=False)

    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds (truncated to whole seconds) to UTC datetime."""
    truncated = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Convert datetime to integer epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float, decimals: int) -> float:
    """Round half-up to given decimal places using Decimal."""
    d = Decimal(str(value)).quantize(
        Decimal("0." + "0" * decimals if decimals > 0 else "1"),
        rounding=ROUND_HALF_UP
    )
    return float(d)


def parse_timestamp_ms(value: Any) -> int:
    """
    Validate and parse epoch milliseconds.
    Rejects: booleans, floats, strings, numbers not in [100000000000, 4102444800000].
    """
    if isinstance(value, bool):
        raise ValueError("Timestamp must not be boolean")
    if isinstance(value, (float, str)):
        raise ValueError("Timestamp must be integer milliseconds, not float or string")
    if not isinstance(value, int):
        raise ValueError("Timestamp must be integer")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise ValueError(f"Timestamp {value} out of range [100000000000, 4102444800000]")
    return value


def get_attendance_date_ist(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Determine the IST attendance date for a punch-in.
    Overnight shift: shift_end <= shift_start (e.g. 22:00 - 06:00).
    In an overnight shift, punch-in earlier than shift_end in IST belongs to
    the shift starting the PREVIOUS calendar day.
    """
    punch_ist = punch_in_dt.astimezone(IST)
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end.split(":"))

    is_overnight = (sh_end_h * 60 + sh_end_m) <= (sh_start_h * 60 + sh_start_m)

    if is_overnight:
        punch_minutes = punch_ist.hour * 60 + punch_ist.minute
        end_minutes = sh_end_h * 60 + sh_end_m
        if punch_minutes < end_minutes:
            prev_day = (punch_ist - timedelta(days=1)).date()
            return prev_day.strftime("%Y-%m-%d")

    return punch_ist.date().strftime("%Y-%m-%d")


def calculate_late_minutes(punch_in_dt: datetime, shift_start: str, date_ist: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if strictly more than 10:00 (600 seconds) after shift_start.
    late_minutes = floor of (punch_in - shift_start) in minutes, NOT from grace end.
    """
    sh_h, sh_m = map(int, shift_start.split(":"))
    date_obj = datetime.strptime(date_ist, "%Y-%m-%d").date()
    shift_start_ist = datetime(
        date_obj.year, date_obj.month, date_obj.day,
        sh_h, sh_m, 0,
        tzinfo=IST
    )
    punch_ist = punch_in_dt.astimezone(IST)
    diff_seconds = (punch_ist - shift_start_ist).total_seconds()

    GRACE_SECONDS = 10 * 60
    if diff_seconds <= GRACE_SECONDS:
        return 0

    return math.floor(diff_seconds / 60)


def calculate_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, date_ist: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out.
    Counted only if >= 30 minutes; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end is on the NEXT calendar day.
    """
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end.split(":"))
    is_overnight = (sh_end_h * 60 + sh_end_m) <= (sh_start_h * 60 + sh_start_m)

    date_obj = datetime.strptime(date_ist, "%Y-%m-%d").date()
    if is_overnight:
        date_obj = date_obj + timedelta(days=1)

    shift_end_ist = datetime(
        date_obj.year, date_obj.month, date_obj.day,
        sh_end_h, sh_end_m, 0,
        tzinfo=IST
    )
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_seconds = (punch_out_ist - shift_end_ist).total_seconds()

    if diff_seconds < 0:
        return 0

    minutes = math.floor(diff_seconds / 60)
    return minutes if minutes >= 30 else 0


def calculate_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    raw = diff_seconds / 3600.0
    return round_half_up(raw, 2)


def is_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def serialize(doc: dict) -> dict:
    """Serialize document for HTTP responses, stripping _id and converting datetimes to epoch ms."""
    if not doc:
        return doc
    clean = dict(doc)
    clean.pop("_id", None)
    clean.pop("id", None)

    for field in ["punch_in", "punch_out", "created_at"]:
        if field in clean and clean[field] is not None:
            if isinstance(clean[field], datetime):
                clean[field] = dt_to_ms(clean[field])

    if "history" in clean:
        hist_list = []
        for entry in clean.get("history") or []:
            ent = dict(entry)
            if "at" in ent and isinstance(ent["at"], datetime):
                ent["at"] = dt_to_ms(ent["at"])
            if "changes" in ent and isinstance(ent["changes"], dict):
                changes = {}
                for f, c in ent["changes"].items():
                    c_dict = dict(c)
                    for k in ["from", "to"]:
                        if k in c_dict and isinstance(c_dict[k], datetime):
                            c_dict[k] = dt_to_ms(c_dict[k])
                    changes[f] = c_dict
                ent["changes"] = changes
            hist_list.append(ent)
        clean["history"] = hist_list
    elif "date" in clean:
        clean["history"] = []

    if "date" in clean:
        clean.setdefault("half_day", False)
        clean.setdefault("overtime_minutes", 0)
        clean.setdefault("late_minutes", 0)
        clean.setdefault("work_hours", None)

    return clean


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def check_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        return parse_timestamp_ms(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        return parse_timestamp_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_punches(cls, v):
        if v is None:
            return v
        return parse_timestamp_ms(v)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", status_code=200)
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now = now_utc()
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [serialize(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = (body.punched_at // 1000) * 1000
        punch_in_dt = ms_to_dt(ts_ms)
    else:
        now_ts = int(time.time()) * 1000
        punch_in_dt = ms_to_dt(now_ts)

    date_ist = get_attendance_date_ist(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = calculate_late_minutes(punch_in_dt, emp["shift_start"], date_ist)

    doc = {
        "emp_code": body.emp_code,
        "date": date_ist,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = (body.punched_at // 1000) * 1000
        punch_out_dt = ms_to_dt(ts_ms)
    else:
        now_ts = int(time.time()) * 1000
        punch_out_dt = ms_to_dt(now_ts)

    # Find the most recent record whose punch_in is at or before punch_out_dt
    record = attendance.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt}
        },
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    overtime_minutes = calculate_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    work_hours = calculate_work_hours(punch_in_dt, punch_out_dt)
    half_day = is_half_day(work_hours)

    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day
        }},
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    return serialize(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
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

    total = attendance.count_documents(q)
    cursor = attendance.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    items = [serialize(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest
):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = attendance.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record.get("status")

    if body.status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Setting status ABSENT or LEAVE cannot be combined with punch times")

    if body.status in ["ABSENT", "LEAVE"]:
        new_punch_in_dt = None
        new_punch_out_dt = None
    else:
        if body.punch_in is not None:
            new_punch_in_dt = ms_to_dt(body.punch_in)
        else:
            new_punch_in_dt = record.get("punch_in")
            if new_punch_in_dt is not None and new_punch_in_dt.tzinfo is None:
                new_punch_in_dt = new_punch_in_dt.replace(tzinfo=timezone.utc)

        if body.punch_out is not None:
            new_punch_out_dt = ms_to_dt(body.punch_out)
        else:
            new_punch_out_dt = record.get("punch_out")
            if new_punch_out_dt is not None and new_punch_out_dt.tzinfo is None:
                new_punch_out_dt = new_punch_out_dt.replace(tzinfo=timezone.utc)

    if new_status in ["PRESENT", "WFH", "ON_DUTY"]:
        if new_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        calc_date = get_attendance_date_ist(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if new_punch_in_dt is not None and new_punch_out_dt is not None:
        diff_sec = (new_punch_out_dt - new_punch_in_dt).total_seconds()
        if diff_sec <= 0 or diff_sec > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Compute derived fields
    if new_status in ["ABSENT", "LEAVE"]:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = calculate_late_minutes(new_punch_in_dt, emp["shift_start"], date)
        if new_punch_out_dt is not None:
            new_overtime_minutes = calculate_overtime_minutes(new_punch_out_dt, emp["shift_start"], emp["shift_end"], date)
            new_work_hours = calculate_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_half_day = is_half_day(new_work_hours)
        else:
            new_overtime_minutes = 0
            new_work_hours = None
            new_half_day = False

    # Check what actually changed
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in_dt != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in_dt}
    if new_punch_out_dt != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out_dt}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    set_doc = {
        "status": new_status,
        "punch_in": new_punch_in_dt,
        "punch_out": new_punch_out_dt,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day
    }

    updated = attendance.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": set_doc,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    return serialize(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    y, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    joined_on = emp["joined_on"]
    working_days = 0
    for d in range(1, days_in_month + 1):
        cur_date = f"{month}-{d:02d}"
        dt_obj = datetime(y, m, d)
        if dt_obj.weekday() < 5 and cur_date >= joined_on:
            working_days += 1

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end}
        }},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]

    res = list(attendance.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = data["present_days"]
        leave_days = data["leave_days"]
        late_count = data["late_count"]
        total_late_minutes = data["total_late_minutes"]
        total_overtime_minutes = data["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    y, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    match_filter = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_set": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}}}, 5]}
                        ]},
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "work_hours_list": {
                "$push": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$logs", None]},
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        "$logs.work_hours",
                        "$$REMOVE"
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]},
                        1,
                        0
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$cond": [
                        {"$ne": ["$logs", None]},
                        {"$ifNull": ["$logs.late_minutes", 0]},
                        0
                    ]
                }
            },
            "leave_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]},
                        1,
                        0
                    ]
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                        1,
                        0
                    ]
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount_set"},
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$work_hours_list"}, 0]},
                    {"$avg": "$work_hours_list"},
                    None
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

    res = list(employees.aggregate(pipeline))
    items = []
    for row in res:
        if row["headcount"] == 0:
            continue
        row["present_days"] = round_half_up(row["present_days"], 2)
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
        items.append(row)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    y, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(y, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": {"path": "$emp", "preserveNullAndEmptyArrays": False}}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$match": {"rank": {"$lte": limit}}}
    ])

    res = list(attendance.aggregate(pipeline))
    return {"month": month, "items": res}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    num_days = (to_dt - from_dt).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    dept_emp_count = employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    to_plus_one = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "date_iso": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
            "present_count": 1,
            "late_count": 1
        }},
        {"$densify": {
            "field": "date_iso",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_plus_one]
            }
        }},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$date_iso", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_iso"}, 5]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"cur_date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$cur_date"]}
                        ]
                    }
                }},
                {"$count": "count"}
            ],
            "as": "hc"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{'$first': "$hc.count"}, 0]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": [
                        {"$eq": ["$is_working_day", True]},
                        {"$gt": ["$headcount", 0]}
                    ]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date_iso": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, 0]
                    }
                }
            }
        }},
        {"$sort": {"date_iso": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }}
    ]

    res = list(attendance.aggregate(pipeline))
    for row in res:
        if row["attendance_rate"] is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)

    return {"department": department, "items": res}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        coll_name = "attendance_logs"
        y, m = map(int, month.split("-"))
        days_in_month = calendar.monthrange(y, m)[1]
        match_filter = {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-{days_in_month:02d}"}}
        pipeline = [{"$match": match_filter}, {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
        try:
            explain_res = db.command("aggregate", coll_name, pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = db.command({"explain": {"find": coll_name, "filter": match_filter}, "verbosity": "executionStats"})
        clean_exp = json.loads(json_util.dumps(explain_res))
        return {"endpoint": endpoint, "collection": coll_name, "explain": clean_exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        coll_name = "employees"
        y, m = map(int, month.split("-"))
        days_in_month = calendar.monthrange(y, m)[1]
        match_filter = {"joined_on": {"$lte": f"{month}-{days_in_month:02d}"}}
        if department:
            match_filter["department"] = department
        pipeline = [{"$match": match_filter}]
        try:
            explain_res = db.command("aggregate", coll_name, pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = db.command({"explain": {"find": coll_name, "filter": match_filter}, "verbosity": "executionStats"})
        clean_exp = json.loads(json_util.dumps(explain_res))
        return {"endpoint": endpoint, "collection": coll_name, "explain": clean_exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        coll_name = "attendance_logs"
        y, m = map(int, month.split("-"))
        days_in_month = calendar.monthrange(y, m)[1]
        match_filter = {"date": {"$gte": f"{month}-01", "$lte": f"{month}-{days_in_month:02d}"}, "late_minutes": {"$gt": 0}}
        pipeline = [{"$match": match_filter}]
        try:
            explain_res = db.command("aggregate", coll_name, pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = db.command({"explain": {"find": coll_name, "filter": match_filter}, "verbosity": "executionStats"})
        clean_exp = json.loads(json_util.dumps(explain_res))
        return {"endpoint": endpoint, "collection": coll_name, "explain": clean_exp}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        coll_name = "attendance_logs"
        match_filter = {"date": {"$gte": from_date, "$lte": to_date}}
        pipeline = [{"$match": match_filter}]
        try:
            explain_res = db.command("aggregate", coll_name, pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = db.command({"explain": {"find": coll_name, "filter": match_filter}, "verbosity": "executionStats"})
        clean_exp = json.loads(json_util.dumps(explain_res))
        return {"endpoint": endpoint, "collection": coll_name, "explain": clean_exp}

    elif endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        coll_name = "attendance_logs"
        q = {}
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
        explain_res = db.command({
            "explain": {
                "find": coll_name,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            "verbosity": "executionStats"
        })
        clean_exp = json.loads(json_util.dumps(explain_res))
        return {"endpoint": endpoint, "collection": coll_name, "explain": clean_exp}
