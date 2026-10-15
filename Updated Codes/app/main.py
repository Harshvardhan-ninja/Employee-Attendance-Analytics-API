"""
Employee Attendance & Analytics API
Run from candidate_kit root:
    uvicorn app.main:app --port 8000

Environment:
    MONGO_URI=mongodb://localhost:27017
    MONGO_DB=attendance_db
"""
import os
import re
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- Validation/helpers -------------------------------
def now_ist():
    return datetime.now(IST).replace(microsecond=0)

def epoch_ms(dt: Optional[datetime]):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)

def api_dt(dt: Optional[datetime]):
    return epoch_ms(dt)

def parse_ms(value):
    if value is None:
        return now_ist()
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "Timestamp must be an integer epoch-millisecond value in the supported range")
    return datetime.fromtimestamp(value / 1000, tz=IST).replace(microsecond=0)

def parse_date(s: str, field="date"):
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s:
            raise ValueError()
        return d
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")

def parse_month(s: str):
    if not re.fullmatch(r"\d{4}-\d{2}", s):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        return date.fromisoformat(s + "-01")
    except ValueError:
        raise HTTPException(422, "month must be a valid YYYY-MM")

def parse_clock(s: str):
    if not isinstance(s, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s):
        raise HTTPException(422, "Time must be HH:MM")
    return time.fromisoformat(s)

def half_up(value, places=2):
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def round_int_half_up(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

def shift_end_dt(att_date: str, shift_start: str, shift_end: str):
    d = parse_date(att_date)
    start = parse_clock(shift_start)
    end = parse_clock(shift_end)
    end_day = d + timedelta(days=1) if end <= start else d
    return datetime.combine(end_day, end, tzinfo=IST)

def attendance_date_for(ts: datetime, shift_start: str, shift_end: str):
    local = ts.astimezone(IST)
    start, end = parse_clock(shift_start), parse_clock(shift_end)
    if end <= start and local.time().replace(tzinfo=None) < end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()

def late_minutes(ts, shift_start):
    start_t = parse_clock(shift_start)
    start = datetime.combine(ts.astimezone(IST).date(), start_t, tzinfo=IST)
    elapsed = int((ts - start).total_seconds() // 60)
    # The grace rule is strictly more than 10 minutes; floor elapsed minutes.
    if (ts - start).total_seconds() <= 600:
        return 0
    return max(0, elapsed)

def derive_values(punch_in, punch_out, emp, att_date):
    late = late_minutes(punch_in, emp["shift_start"]) if punch_in else 0
    overtime = 0
    hours = None
    half = False
    if punch_out and punch_in:
        seconds = (punch_out - punch_in).total_seconds()
        hours = half_up(seconds / 3600, 2)
        half = hours < 4.50
        end = shift_end_dt(att_date, emp["shift_start"], emp["shift_end"])
        overtime_raw = int((punch_out - end).total_seconds() // 60)
        overtime = overtime_raw if overtime_raw >= 30 else 0
    return {"late_minutes": late, "overtime_minutes": max(0, overtime),
            "work_hours": hours, "half_day": half}

def serialize_doc(doc):
    if not doc:
        return doc
    out = dict(doc)
    oid = out.pop("_id", None)
    if oid is not None:
        out["id"] = str(oid)
    for key in ("created_at", "punch_in", "punch_out"):
        if isinstance(out.get(key), datetime):
            out[key] = api_dt(out[key])
    history = out.get("history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item.get("at"), datetime):
                item["at"] = api_dt(item["at"])
    return out

def validate_page(page, page_size):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")

def employee_or_404(emp_code):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    return emp

def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)], name="ix_attendance_emp_punchin")

@app.on_event("startup")
def startup():
    indexes()


# ------------------------------------ Models ------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_code(cls, v):
        if not re.fullmatch(r"EMP\d{4,6}", v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, v):
        parse_clock(v)
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class AttendanceStatusIn(BaseModel):
    emp_code: str
    date: str
    status: Literal["ABSENT", "LEAVE"]
    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("date")
    @classmethod
    def validate_attendance_date(cls, v):
        parse_date(v, "date")
        return v

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None and (type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX):
            raise ValueError("punched_at must be epoch milliseconds as an integer")
        return v

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None and (type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX):
            raise ValueError("punched_at must be epoch milliseconds as an integer")
        return v

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_times(cls, v):
        if v is not None and (type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX):
            raise ValueError("Punch times must be epoch milliseconds as integers")
        return v


# ------------------------------------- Routes -------------------------------------
@app.get("/")
def root():
    return {"message": "Employee Attendance & Analytics API is running"}

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ist()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = api_dt(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    emp_code: Optional[str] = None,
    name: Optional[str] = None,
    department: Optional[str] = None,
    shift_start: Optional[str] = None,
    shift_end: Optional[str] = None,
    joined_on: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_page(page, page_size)

    q = {}

    # Exact match filters
    if emp_code:
        q["emp_code"] = emp_code

    if department:
        q["department"] = department

    if shift_start:
        parse_clock(shift_start)
        q["shift_start"] = shift_start

    if shift_end:
        parse_clock(shift_end)
        q["shift_end"] = shift_end

    if joined_on:
        parse_date(joined_on, "joined_on")
        q["joined_on"] = joined_on

    # Case-insensitive partial match for employee name
    if name:
        q["name"] = {
            "$regex": re.escape(name.strip()),
            "$options": "i"
        }

    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = api_dt(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/status", status_code=201)
def create_attendance_status(body: AttendanceStatusIn):
    employee_or_404(body.emp_code)

    doc = {
        "emp_code": body.emp_code,
        "date": body.date,
        "status": body.status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [{
            "at": now_ist(),
            "by": body.by,
            "reason": body.reason,
            "changes": {
                "status": body.status
            }
        }]
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance record already exists for this employee and date"
        )

    doc["_id"] = result.inserted_id
    return serialize_doc(doc)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee_or_404(body.emp_code)
    ts = parse_ms(body.punched_at)
    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": []
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc["_id"] = result.inserted_id
    return serialize_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee_or_404(body.emp_code)
    ts = parse_ms(body.punched_at)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": list(PRESENCE)}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if record is None:
        already = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": ts},
                "status": {"$in": list(PRESENCE)}
            },
            sort=[("punch_in", DESCENDING)]
        )

        if already and already.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")

        raise HTTPException(404, "No matching punch-in found")

    # MongoDB returns naive datetimes in UTC by default.
    # Convert punch-in to timezone-aware IST before comparison.
    punch_in_time = record["punch_in"]

    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    punch_in_time = punch_in_time.astimezone(IST)

    # Validate punch-out time.
    if (
        ts <= punch_in_time
        or ts > punch_in_time + timedelta(hours=24)
    ):
        raise HTTPException(
            422,
            "punched_at must be after punch-in and within 24 hours"
        )

    # Calculate attendance using timezone-aware datetime values.
    derived = derive_values(
        punch_in_time,
        ts,
        emp,
        record["date"]
    )

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                **derived
            }
        }
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance record changed concurrently; retry"
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    return serialize_doc(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    status: Optional[str] = None,
                    page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100)):
    if status and status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_doc(d) for d in docs], "total": total,
            "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: str, body: RegularizeIn):
    parse_date(att_date)
    emp = employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not record:
        raise HTTPException(404, "Attendance record not found")
    changes = {}
    supplied = body.model_dump(exclude_unset=True)
    new_status = supplied.get("status", record["status"])
    if new_status in {"ABSENT", "LEAVE"} and ("punch_in" in supplied or "punch_out" in supplied):
        if supplied.get("punch_in") is not None or supplied.get("punch_out") is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    if new_status in PRESENCE and supplied.get("punch_in", api_dt(record.get("punch_in"))) is None:
        raise HTTPException(422, "Presence status requires punch_in")
    if "status" in supplied and supplied["status"] != record["status"]:
        changes["status"] = supplied["status"]
    pin = parse_ms(supplied["punch_in"]) if "punch_in" in supplied and supplied["punch_in"] is not None else record.get("punch_in")
    pout = parse_ms(supplied["punch_out"]) if "punch_out" in supplied and supplied["punch_out"] is not None else record.get("punch_out")
    if new_status in {"ABSENT", "LEAVE"}:
        pin = pout = None
    if pin and pin.date().isoformat() != att_date:
        # For overnight records, punch-in must remain on the attendance date.
        raise HTTPException(422, "punch_in must fall on the attendance date")
    if pout and (not pin or pout <= pin or pout > pin + timedelta(hours=24)):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if "punch_in" in supplied and api_dt(pin) != api_dt(record.get("punch_in")):
        changes["punch_in"] = pin
    if "punch_out" in supplied and api_dt(pout) != api_dt(record.get("punch_out")):
        changes["punch_out"] = pout
    if not changes:
        raise HTTPException(422, "No changes supplied")
    derived = derive_values(pin, pout, emp, att_date) if new_status in PRESENCE else {
        "late_minutes": 0, "overtime_minutes": 0, "work_hours": None, "half_day": False
    }
    history_item = {"at": now_ist(), "by": body.by, "reason": body.reason, "changes": changes}
    update = {"status": new_status, "punch_in": pin, "punch_out": pout, **derived}
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "status": record["status"], "punch_in": record.get("punch_in"),
         "punch_out": record.get("punch_out")},
        {"$set": update, "$push": {"history": history_item}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed concurrently; retry")
    return serialize_doc(db.attendance_logs.find_one({"_id": record["_id"]}))


# ------------------------------- Analytics helpers -------------------------------
def month_bounds(month):
    start = parse_month(month)
    next_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return start.isoformat(), next_month.isoformat()

def working_days(start: date, end_exclusive: date):
    count = 0
    d = start
    while d < end_exclusive:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count

def monthly_employee_pipeline(emp_code, start_s, end_s):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lt": end_s}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]},
                          {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                          {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]},
                {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = employee_or_404(emp_code)
    start_s, end_s = month_bounds(month)
    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    working_start = max(start, date.fromisoformat(emp["joined_on"]))
    workdays = working_days(working_start, end) if working_start < end else 0
    rows = list(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, start_s, end_s)))
    a = rows[0] if rows else {}
    present = a.get("present_days", 0)
    return {
        "emp_code": emp_code, "month": month, "working_days": workdays,
        "present_days": present, "leave_days": a.get("leave_days", 0),
        "late_count": a.get("late_count", 0),
        "total_late_minutes": a.get("total_late_minutes", 0),
        "total_overtime_minutes": a.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / workdays * 100, 2) if workdays else None
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_s, end_s = month_bounds(month)
    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    emp_match = {"joined_on": {"$lt": end_s}}
    if department:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
          "pipeline": [{"$match": {"$expr": {"$and": [
              {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lt": ["$date", end_s]}
          ]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department",
          "headcount": {"$addToSet": "$emp_code"},
          "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]},
              {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
          "work_hours_sum": {"$sum": {"$cond": [{"$and": [
              {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}
          ]}, "$logs.work_hours", 0]}},
          "work_hours_count": {"$sum": {"$cond": [{"$and": [
              {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}
          ]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"},
          "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
             {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
          "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    result = list(db.employees.aggregate(pipeline))
    for row in result:
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = half_up(row["avg_work_hours"], 2)
    return {"month": month, "departments": result}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=1000),
                     department: Optional[str] = None):
    start_s, end_s = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lt": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
          "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name",
          "department": "$employee.department", "total_late_minutes": 1, "rank": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    d1, d2 = parse_date(date_from, "date_from"), parse_date(date_to, "date_to")
    if d2 < d1:
        raise HTTPException(422, "date_to must be >= date_from")
    if (d2 - d1).days > 91:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")
    # MongoDB aggregation creates daily buckets; date spine is generated by $densify.
    pipeline = [
        {"$match": {"date": {"$gte": d1.isoformat(), "$lte": d2.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {"_id": "$date",
          "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
             {"$cond": ["$half_day", 0.5, 1]}, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
             "bounds": [datetime.combine(d1, time.min), datetime.combine(d2, time.min)]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
          "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"},
          "pipeline": [{"$match": {"$expr": {"$and": [
            {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}
          ]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
          "is_working_day": {"$and": [
             {"$ne": [{"$dayOfWeek": "$day"}, 1]}, {"$ne": [{"$dayOfWeek": "$day"}, 7]}]} }},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
             {"$round": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1},
          "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
            "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
          "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]
    return {"department": department, "from": date_from, "to": date_to,
            "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=1000),
):
    # Validate common query parameters only when supplied.
    if month is not None:
        parse_month(month)

    if date_from is not None:
        parse_date(date_from, "date_from")

    if date_to is not None:
        parse_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    if status is not None and status not in STATUSES:
        raise HTTPException(422, "Invalid status")

    if endpoint == "attendance_list":
        q = {}

        if emp_code:
            q["emp_code"] = emp_code

        if status:
            q["status"] = status

        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        return db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=400,
                detail="For employee_monthly, provide emp_code and month (YYYY-MM).",
            )

        employee_or_404(emp_code)
        start_s, end_s = month_bounds(month)

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": monthly_employee_pipeline(
                    emp_code, start_s, end_s
                ),
                "cursor": {},
            },
            verbosity="executionStats",
        )

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=400,
                detail="For department_summary, provide month (YYYY-MM).",
            )

        start_s, end_s = month_bounds(month)
        q = {"joined_on": {"$lt": end_s}}

        if department:
            q["department"] = department

        return db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": [
                    {"$match": q},
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "let": {"code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {"$eq": ["$emp_code", "$$code"]},
                                                {"$gte": ["$date", start_s]},
                                                {"$lt": ["$date", end_s]},
                                            ]
                                        }
                                    }
                                }
                            ],
                            "as": "logs",
                        }
                    },
                ],
                "cursor": {},
            },
            verbosity="executionStats",
        )

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=400,
                detail="For late_leaderboard, provide month (YYYY-MM).",
            )

        start_s, end_s = month_bounds(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_s, "$lt": end_s},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(
                status_code=400,
                detail=(
                    "For department_trend, provide department, "
                    "date_from and date_to."
                ),
            )

        if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days > 91:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if db.employees.count_documents(
            {"department": department}
        ) == 0:
            raise HTTPException(404, "Department not found")

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": date_from, "$lte": date_to}
                }
            },
            {"$sort": {"date": 1, "emp_code": 1}},
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    raise HTTPException(
        status_code=400,
        detail=(
            "Unsupported endpoint name. Supported values: "
            "attendance_list, employee_monthly, department_summary, "
            "late_leaderboard, department_trend."
        ),
    )
