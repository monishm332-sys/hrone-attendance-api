"""Employee Attendance & Analytics API."""
import calendar
import os
import re
import json
from bson import json_util
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson.timestamp import Timestamp
from fastapi.encoders import jsonable_encoder
from pydantic import AliasChoices

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("Configure MONGO_URI and MONGO_DB in your environment.")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_status_date")


def fail(code: int, message: str):
    raise HTTPException(status_code=code, detail=message)


def valid_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except (ValueError, TypeError):
        fail(422, "Date must use YYYY-MM-DD format.")


def valid_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail(422, "month must use YYYY-MM format.")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    return first, date(year, month, calendar.monthrange(year, month)[1])


def parse_shift(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        fail(422, "Shift times must use HH:MM.")
    return time.fromisoformat(value)


def epoch_ms_to_utc(value: Optional[int]) -> datetime:
    if value is None:
        result = datetime.now(UTC)
    else:
        if type(value) is not int or value < 100_000_000_000:
            fail(422, "Timestamps must be integer epoch milliseconds.")
        try:
            result = datetime.fromtimestamp(value / 1000, UTC)
        except (ValueError, OverflowError, OSError):
            fail(422, "Timestamp is outside the supported range.")
    return result.replace(microsecond=0)


def utc_naive(value: Optional[datetime]):
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def to_epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    return value


def api_value(value):
    if isinstance(value, datetime):
        return to_epoch_ms(value)
    if isinstance(value, dict):
        return {k: api_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [api_value(v) for v in value]
    return value


def output_doc(doc):
    if not doc:
        return None
    result = dict(doc)
    result.pop("_id", None)
    if "history" in result:
        result.setdefault("history", [])
    return api_value(result)


def round_half_up(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def employee_or_404(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        fail(404, "Employee not found.")
    return emp


def attendance_date(punch_in: datetime, employee: dict) -> date:
    local = punch_in.astimezone(IST)
    start = parse_shift(employee["shift_start"])
    end = parse_shift(employee["shift_end"])
    day = local.date()
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day


def shift_end_datetime(punch_in: datetime, employee: dict) -> datetime:
    local = punch_in.astimezone(IST)
    start = parse_shift(employee["shift_start"])
    end = parse_shift(employee["shift_end"])
    day = local.date()
    if end <= start:
        if local.time().replace(tzinfo=None) < end:
            day -= timedelta(days=1)
        day += timedelta(days=1)
    return datetime.combine(day, end, IST)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = punch_in.astimezone(IST)
    shift = parse_shift(shift_start)
    start = datetime.combine(local.date(), shift, IST)
    seconds = (local - start).total_seconds()
    return max(0, int(seconds // 60)) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, employee: dict, punch_in: datetime) -> int:
    minutes = int((punch_out - shift_end_datetime(punch_in, employee)).total_seconds() // 60)
    return max(0, minutes) if minutes >= 30 else 0


def derived_fields(employee: dict, status: str, punch_in, punch_out):
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"])
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": compute_overtime(punch_out, employee, punch_in), "half_day": hours < 4.5}


def working_days(first: date, last: date) -> int:
    if first > last:
        return 0
    count = 0
    while first <= last:
        if first.weekday() < 5:
            count += 1
        first += timedelta(days=1)
    return count


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift(cls, value):
        parse_shift(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        valid_date(value)
        return value

    @field_validator("email")
    @classmethod
    def check_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address.")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    timestamp: Optional[int] = Field(default=None, validation_alias=AliasChoices("timestamp", "punched_at"))
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    timestamp: Optional[int] = Field(default=None, validation_alias=AliasChoices("timestamp", "punched_at"))


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=1)
    regularized_by: str = Field(min_length=1)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        fail(503, "MongoDB unavailable.")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        fail(422, "shift_start must differ from shift_end.")
    doc = body.model_dump()
    doc["created_at"] = utc_naive(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "emp_code already exists.")
    return output_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [output_doc(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    stamp = epoch_ms_to_utc(body.timestamp)
    day = attendance_date(stamp, employee).isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": utc_naive(stamp), "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(stamp, employee["shift_start"]), "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "Already punched in for this attendance date.")
    return output_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    stamp = epoch_ms_to_utc(body.timestamp)
    stored = utc_naive(stamp)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": stored}, "punch_out": None, "status": {"$in": PRESENCE}}, sort=[("punch_in", DESCENDING)])
    if not record:
        closed = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": stored}, "punch_out": {"$ne": None}, "status": {"$in": PRESENCE}}, sort=[("punch_in", DESCENDING)])
        if closed:
            fail(409, "This attendance record is already punched out.")
        fail(404, "No punch-in found.")
    punch_in_at = record["punch_in"].replace(tzinfo=UTC)
    if stamp <= punch_in_at or stamp - punch_in_at > timedelta(hours=24):
        fail(422, "Punch-out must be after punch-in and within 24 hours.")
    fields = derived_fields(employee, record["status"], punch_in_at, stamp)
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": {"punch_out": stored, **fields}})
    if result.modified_count != 1:
        fail(409, "This attendance record was punched out concurrently.")
    return output_doc(db.attendance_logs.find_one({"_id": record["_id"]}))


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from:
        valid_date(date_from)
    if date_to:
        valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        fail(422, "date_from cannot be after date_to.")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [output_doc(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    valid_date(date)
    employee = employee_or_404(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        fail(404, "Attendance record not found.")
    status = body.status if body.status is not None else old.get("status")
    if status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        fail(422, "ABSENT and LEAVE cannot have punch times.")
    old_in, old_out = old.get("punch_in"), old.get("punch_out")
    new_in = epoch_ms_to_utc(body.punch_in) if body.punch_in is not None else (old_in.replace(tzinfo=UTC) if old_in else None)
    new_out = epoch_ms_to_utc(body.punch_out) if body.punch_out is not None else (old_out.replace(tzinfo=UTC) if old_out else None)
    if status in PRESENCE and new_in is None:
        fail(422, "Presence statuses require punch_in.")
    if status in ("ABSENT", "LEAVE"):
        new_in = new_out = None
    if new_in and attendance_date(new_in, employee).isoformat() != date:
        fail(422, "punch_in must belong to the record's attendance date.")
    if new_out and (new_in is None or new_out <= new_in or new_out - new_in > timedelta(hours=24)):
        fail(422, "punch_out must be after punch_in and within 24 hours.")
    new_in_db, new_out_db = utc_naive(new_in), utc_naive(new_out)
    fields = {"status": status, "punch_in": new_in_db, "punch_out": new_out_db, **derived_fields(employee, status, new_in, new_out)}
    old_fields = {key: old.get(key) for key in fields}
    changes = {key: {"from": old_fields.get(key), "to": value} for key, value in fields.items() if old_fields.get(key) != value}
    if not changes:
        fail(422, "The request does not change this record.")
    history_entry = {"at": utc_naive(datetime.now(UTC)), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.update_one({"_id": old["_id"], "history": old.get("history", []), "status": old.get("status"), "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}, {"$set": fields, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        fail(409, "Record changed concurrently; retry your correction.")
    return output_doc(db.attendance_logs.find_one({"_id": old["_id"]}))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = employee_or_404(emp_code)
    first, last = valid_month(month)
    joined = valid_date(employee["joined_on"])
    effective_first = max(first, joined)
    days = working_days(effective_first, last)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    results = list(db.attendance_logs.aggregate(pipeline))
    data = results[0] if results else {}
    present = float(data.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": round_half_up(present), "leave_days": int(data.get("leave_days", 0)), "late_count": int(data.get("late_count", 0)), "total_late_minutes": int(data.get("total_late_minutes", 0)), "total_overtime_minutes": int(data.get("total_overtime_minutes", 0)), "attendance_pct": round_half_up(present / days * 100, 4) if days else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = valid_month(month)
    employee_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]}, {"$lte": ["$date", last.isoformat()]}, {"$gte": ["$date", "$$joined"]}]}}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, 6]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$let": {"vars": {"valid_hours": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$valid_hours"}, 0]}, {"$avg": "$$valid_hours"}, None]}}},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = valid_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    first, last = valid_date(from_date), valid_date(to_date)
    if last < first:
        fail(422, "to must not be before from.")
    if (last - first).days + 1 > 92:
        fail(422, "Trend range cannot exceed 92 days.")
    if not db.employees.find_one({"department": department}):
        fail(404, "Department not found.")
    start_dt = datetime.combine(first, time.min, UTC)
    end_dt = datetime.combine(last, time.min, UTC)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "count"},
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        for key in ("attendance_rate", "moving_avg_7d"):
            if item.get(key) is not None:
                item[key] = round_half_up(item[key], 4)
        item["present_count"] = round_half_up(item["present_count"], 2)
    return {"department": department, "items": items}


def make_json_safe(value):
    """Convert BSON values into JSON-compatible values for API responses."""
    if isinstance(value, Timestamp):
        return {"time": value.time, "increment": value.inc}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        fail(422, "Unsupported endpoint.")
    if endpoint == "attendance_list":
        date_from = date_from or from_date
        date_to = date_to or to_date
        if date_from:
            valid_date(date_from)
        if date_to:
            valid_date(date_to)
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            if status not in STATUSES:
                fail(422, "Invalid status.")
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        command = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            fail(422, "emp_code and month are required.")
        first, last = valid_month(month)
        command = {"aggregate": "attendance_logs", "pipeline": [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, {"$sort": {"date": 1}}], "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            fail(422, "month is required.")
        first, last = valid_month(month)
        match = {"joined_on": {"$lte": last.isoformat()}}
        if department:
            match["department"] = department
        command = {"aggregate": "employees", "pipeline": [{"$match": match}], "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            fail(422, "month is required.")
        first, last = valid_month(month)
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}}]
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            fail(422, "department, from, and to are required.")
        first, last = valid_date(from_date), valid_date(to_date)
        if last < first or (last - first).days + 1 > 92:
            fail(422, "Invalid trend date range.")
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}, {"$sort": {"date": 1}}]
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        collection = "attendance_logs"
    try:
        explanation = client[db.name].command({"explain": command, "verbosity": "executionStats"})
    except PyMongoError as exc:
        fail(503, f"Could not explain query: {exc.__class__.__name__}")

    return json.loads(json_util.dumps({
    "endpoint": endpoint,
    "collection": collection,
    "explain": explanation,
}))
