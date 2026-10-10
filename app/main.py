# uvicorn app.main:app --port 8000
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
VALID_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def round_half_up(value: Decimal, digits: int) -> Decimal:
    quant = Decimal("1").scaleb(-digits)
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def floor_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    if isinstance(ms, bool):
        raise HTTPException(status_code=422, detail="Invalid epoch milliseconds")
    if not isinstance(ms, int):
        raise HTTPException(status_code=422, detail="Epoch milliseconds must be an integer")
    if ms < 100000000000 or ms > 4102444800000:
        raise HTTPException(status_code=422, detail="Epoch milliseconds out of range")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_hhmm(value: str) -> time:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="Invalid time value")
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid time value: {value}") from exc
    if value != parsed.strftime("%H:%M"):
        raise HTTPException(status_code=422, detail=f"Invalid time value: {value}")
    return parsed


def is_overnight_shift(emp: Dict[str, Any]) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def to_ist_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date_for_punch(emp: Dict[str, Any], punched_at: datetime) -> str:
    local = to_ist_local(punched_at)
    if is_overnight_shift(emp):
        shift_end = parse_hhmm(emp["shift_end"])
        if local.time() < shift_end:
            return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_start_dt_for_punch(emp: Dict[str, Any], punched_at: datetime) -> datetime:
    local = to_ist_local(punched_at)
    shift_start = parse_hhmm(emp["shift_start"])
    if is_overnight_shift(emp):
        if local.time() < parse_hhmm(emp["shift_end"]):
            start_date = local.date() - timedelta(days=1)
        else:
            start_date = local.date()
    else:
        start_date = local.date()
    return datetime.combine(start_date, shift_start, tzinfo=IST)


def shift_end_dt_for_record(emp: Dict[str, Any], record_date: str) -> datetime:
    day = datetime.strptime(record_date, "%Y-%m-%d").date()
    end_time = parse_hhmm(emp["shift_end"])
    if is_overnight_shift(emp):
        day = day + timedelta(days=1)
    return datetime.combine(day, end_time, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, emp: Dict[str, Any]) -> int:
    start_dt = shift_start_dt_for_punch(emp, punch_in)
    elapsed = (punch_in - start_dt).total_seconds()
    minutes = int(elapsed / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    delta = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")
    return float(round_half_up(delta, 2))


def build_shift_end_datetime(emp: Dict[str, Any], record_date: str) -> datetime:
    day = datetime.strptime(record_date, "%Y-%m-%d").date()
    end_time = parse_hhmm(emp["shift_end"])
    if is_overnight_shift(emp):
        day = day + timedelta(days=1)
    return datetime.combine(day, end_time, tzinfo=IST)


def compute_overtime_for_employee(punch_out: datetime, emp: Dict[str, Any], record_date: str) -> int:
    end_dt = build_shift_end_datetime(emp, record_date)
    diff_seconds = (punch_out - end_dt).total_seconds()
    if diff_seconds <= 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def half_day_for_work_hours(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < Decimal("4.50")


def month_range(month: str):
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    if mon == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, mon + 1, 1)
    last = next_first - timedelta(days=1)
    return first, last


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def round_number(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    dec = Decimal(str(value))
    return float(round_half_up(dec, digits))


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def serialize_epoch_value(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return dt_to_epoch_ms(value)
    return value


def serialize_history(entries: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in entries or []:
        changes_out: Dict[str, Any] = {}
        for key, value in (entry.get("changes") or {}).items():
            before = value.get("from")
            after = value.get("to")
            if key in {"punch_in", "punch_out"}:
                before = serialize_epoch_value(before)
                after = serialize_epoch_value(after)
            changes_out[key] = {"from": before, "to": after}
        out.append({
            "at": dt_to_epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes_out,
        })
    return out


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc.get("status"),
        "punch_in": serialize_epoch_value(doc.get("punch_in")),
        "punch_out": serialize_epoch_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history") or []),
    }


def require_epoch_millis(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool) or type(raw) is not int:
        raise HTTPException(status_code=422, detail="Epoch milliseconds must be an integer")
    if raw < 100000000000 or raw > 4102444800000:
        raise HTTPException(status_code=422, detail="Epoch milliseconds out of range")
    return raw


def build_history_change(old_value: Any, new_value: Any, field_name: str) -> Optional[Dict[str, Any]]:
    if old_value == new_value:
        return None
    if field_name in {"punch_in", "punch_out"}:
        old_value = serialize_epoch_value(old_value)
        new_value = serialize_epoch_value(new_value)
    return {"from": old_value, "to": new_value}


def ensure_employee_exists(emp_code: str) -> Dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


def ensure_attendance_record(emp_code: str, date_str: str) -> Dict[str, Any]:
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    return record


def valid_status_enum(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {value}")
    return value


def field_value_for_date(date_value: Optional[str]) -> Optional[str]:
    if not isinstance(date_value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        raise HTTPException(status_code=422, detail=f"Invalid date: {date_value}")
    try:
        date.fromisoformat(date_value)
        return date_value
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {date_value}") from exc


def require_object_body(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be an object")
    return body


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_out", 1)])


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="DB Down") from exc


@app.post("/employees", status_code=201)
def create_employee(body: Dict[str, Any] = Body(...)):
    body = require_object_body(body)
    emp_code = body.get("emp_code")
    if not isinstance(emp_code, str):
        raise HTTPException(status_code=422, detail="emp_code must be a string")
    if not re.fullmatch(r"EMP\d{4,6}", emp_code):
        raise HTTPException(status_code=422, detail="emp_code format is invalid")
    name = body.get("name")
    email = body.get("email")
    department = body.get("department")
    shift_start = body.get("shift_start", "09:30")
    shift_end = body.get("shift_end", "18:30")
    joined_on = body.get("joined_on")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise HTTPException(status_code=422, detail="name is required")
    if not isinstance(email, str) or len(email) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="email is invalid")
    if not isinstance(department, str) or not department.strip() or len(department) > 50:
        raise HTTPException(status_code=422, detail="department is required")
    try:
        field_value_for_date(joined_on)
    except HTTPException as exc:
        raise HTTPException(status_code=422, detail="joined_on must be YYYY-MM-DD") from exc
    try:
        parse_hhmm(shift_start)
        parse_hhmm(shift_end)
    except HTTPException:
        raise
    if shift_start == shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    if db.employees.find_one({"emp_code": emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc = {
        "emp_code": emp_code,
        "name": name,
        "email": email,
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: Dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: Dict[str, Any] = Body(...)):
    body = require_object_body(body)
    emp_code = body.get("emp_code")
    status = body.get("status", "PRESENT")
    if not isinstance(emp_code, str):
        raise HTTPException(status_code=422, detail="emp_code must be a string")
    if not isinstance(status, str) or status not in PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail="Only PRESENT, WFH and ON_DUTY are valid for punch-in")
    emp = ensure_employee_exists(emp_code)
    if "punched_at" in body and body["punched_at"] is None:
        raise HTTPException(status_code=422, detail="punched_at must be epoch milliseconds")
    punched_at_raw = body.get("punched_at")
    if punched_at_raw is None:
        punch_dt = datetime.now(timezone.utc)
    else:
        punch_dt = floor_ms_to_dt(require_epoch_millis(punched_at_raw))
    punch_dt = punch_dt.replace(microsecond=0)
    record_date = attendance_date_for_punch(emp, punch_dt)
    if db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date}):
        raise HTTPException(status_code=409, detail="already punched in for this date")
    doc = {
        "emp_code": emp_code,
        "date": record_date,
        "status": status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_dt, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: Dict[str, Any] = Body(...)):
    body = require_object_body(body)
    emp_code = body.get("emp_code")
    if not isinstance(emp_code, str):
        raise HTTPException(status_code=422, detail="emp_code must be a string")
    emp = ensure_employee_exists(emp_code)
    if "punched_at" in body and body["punched_at"] is None:
        raise HTTPException(status_code=422, detail="punched_at must be epoch milliseconds")
    punched_at_raw = body.get("punched_at")
    if punched_at_raw is None:
        now_dt = datetime.now(timezone.utc)
    else:
        now_dt = floor_ms_to_dt(require_epoch_millis(punched_at_raw))
    now_dt = now_dt.replace(microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "punch_in": {"$lte": now_dt}, "punch_out": None}, sort=[("punch_in", -1)])
    if not record:
        # allow a punch-out on an already closed record only if there is a later open record; otherwise 404
        record = db.attendance_logs.find_one({"emp_code": emp_code, "punch_in": {"$lte": now_dt}}, sort=[("punch_in", -1)])
        if not record:
            raise HTTPException(status_code=404, detail="No punch-in found")
        if record.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="that record is already punched out")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")
    if now_dt <= record["punch_in"]:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if now_dt - record["punch_in"] > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], now_dt)
    overtime = compute_overtime_for_employee(now_dt, emp, record["date"])
    half = half_day_for_work_hours(work_hours)
    updates = {
        "punch_out": now_dt,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half,
    }
    updated_result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": updates},
    )
    if updated_result.modified_count != 1:
        raise HTTPException(status_code=409, detail="that record is already punched out")
    record.update(updates)
    return serialize_attendance_record(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        valid_status_enum(status)
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance_record(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: Dict[str, Any] = Body(...)):
    body = require_object_body(body)
    ensure_employee_exists(emp_code)
    field_value_for_date(date)
    record = ensure_attendance_record(emp_code, date)
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    new_status = body.get("status", old_status)
    new_punch_in_raw = body.get("punch_in")
    new_punch_out_raw = body.get("punch_out")
    reason = body.get("reason")
    regularized_by = body.get("regularized_by")
    if reason is None or not isinstance(reason, str) or len(reason) < 5 or len(reason) > 200:
        raise HTTPException(status_code=422, detail="reason must be 5-200 chars")
    if regularized_by is None or not isinstance(regularized_by, str) or len(regularized_by) < 1 or len(regularized_by) > 50:
        raise HTTPException(status_code=422, detail="regularized_by is invalid")
    if not isinstance(new_status, str) or new_status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in body or "punch_out" in body:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot include punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if "punch_in" in body and new_punch_in_raw is None:
            raise HTTPException(status_code=422, detail="punch_in must be epoch milliseconds")
        if "punch_out" in body and new_punch_out_raw is None:
            raise HTTPException(status_code=422, detail="punch_out must be epoch milliseconds")
        if new_punch_in_raw is not None:
            new_punch_in = floor_ms_to_dt(require_epoch_millis(new_punch_in_raw))
            if attendance_date_for_punch(ensure_employee_exists(emp_code), new_punch_in) != date:
                raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
        else:
            new_punch_in = old_punch_in
        if new_punch_out_raw is not None:
            new_punch_out = floor_ms_to_dt(require_epoch_millis(new_punch_out_raw))
        else:
            new_punch_out = old_punch_out
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if new_punch_out - new_punch_in > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours")
        else:
            if old_punch_out is None:
                pass

    final_status = new_status
    final_punch_in = new_punch_in if new_status not in {"ABSENT", "LEAVE"} else None
    final_punch_out = new_punch_out if new_status not in {"ABSENT", "LEAVE"} else None
    if final_status in {"ABSENT", "LEAVE"}:
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        final_late_minutes = compute_late_minutes(final_punch_in, ensure_employee_exists(emp_code))
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime_for_employee(final_punch_out, ensure_employee_exists(emp_code), date)
            final_half_day = final_work_hours < 4.5
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    changed_fields: Dict[str, Any] = {}
    for field_name, old_value, new_value in [
        ("status", old_status, final_status),
        ("punch_in", old_punch_in, final_punch_in),
        ("punch_out", old_punch_out, final_punch_out),
        ("work_hours", old_work_hours, final_work_hours),
        ("late_minutes", old_late_minutes, final_late_minutes),
        ("overtime_minutes", old_overtime_minutes, final_overtime_minutes),
        ("half_day", old_half_day, final_half_day),
    ]:
        if old_value != new_value:
            changed_fields[field_name] = build_history_change(old_value, new_value, field_name)
    if not changed_fields:
        raise HTTPException(status_code=422, detail="Request changes nothing")
    new_history = list(record.get("history") or [])
    new_history.append({
        "at": datetime.now(timezone.utc),
        "by": regularized_by,
        "reason": reason,
        "changes": changed_fields,
    })
    update_doc = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
        "history": new_history,
    }
    updated_result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": record.get("history") or []},
        {"$set": update_doc},
    )
    if updated_result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record was changed concurrently")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = ensure_employee_exists(emp_code)
    first_day, last_day = month_range(month)
    month_query = {"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}
    records = list(db.attendance_logs.find(month_query).sort("date", 1))
    joined_date = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    work_start = max(joined_date, first_day)
    working_days = 0
    current = work_start
    while current <= last_day:
        if is_working_day(current):
            working_days += 1
        current += timedelta(days=1)

    present_days = Decimal("0")
    leave_days = 0
    late_count = 0
    total_late = 0
    total_overtime = 0
    for rec in records:
        rec_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
        if rec.get("status") == "LEAVE":
            leave_days += 1
        if rec.get("late_minutes", 0) > 0:
            late_count += 1
        total_late += int(rec.get("late_minutes", 0) or 0)
        total_overtime += int(rec.get("overtime_minutes", 0) or 0)
        if rec.get("status") in PRESENCE_STATUSES and is_working_day(rec_date):
            present_days += Decimal("0.5") if rec.get("half_day") else Decimal("1")
    attendance_pct = None if working_days == 0 else float(round_half_up((present_days / Decimal(working_days)) * Decimal("100"), 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = month_range(month)
    filter_spec = {"joined_on": {"$lte": last_day.isoformat()}}
    if department is not None:
        filter_spec["department"] = department
    employees = list(db.employees.find(filter_spec).sort("department", 1))
    if not employees:
        return {"month": month, "items": []}
    department_names = sorted({emp["department"] for emp in employees})
    outputs = []
    for dept in department_names:
        dept_emps = [e for e in employees if e["department"] == dept]
        headcount = len(dept_emps)
        if headcount == 0:
            continue
        emp_codes = [e["emp_code"] for e in dept_emps]
        records = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}))
        present_days = Decimal("0")
        late_count = 0
        total_late = 0
        leave_count = 0
        on_duty_count = 0
        work_hours_values = []
        for rec in records:
            rec_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
            if rec.get("status") in PRESENCE_STATUSES and is_working_day(rec_date):
                present_days += Decimal("0.5") if rec.get("half_day") else Decimal("1")
            if rec.get("late_minutes", 0) > 0:
                late_count += 1
            total_late += int(rec.get("late_minutes", 0) or 0)
            if rec.get("status") == "LEAVE":
                leave_count += 1
            if rec.get("status") == "ON_DUTY":
                on_duty_count += 1
            if rec.get("status") in PRESENCE_STATUSES and rec.get("work_hours") is not None:
                work_hours_values.append(float(rec["work_hours"]))
        avg_work_hours = None if not work_hours_values else round_number(sum(work_hours_values) / len(work_hours_values), 2)
        outputs.append({
            "department": dept,
            "headcount": headcount,
            "present_days": float(present_days),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })
    outputs.sort(key=lambda x: x["department"])
    return {"month": month, "items": outputs}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = month_range(month)
    employee_filter: Dict[str, Any] = {"joined_on": {"$lte": last_day.isoformat()}}
    if department is not None:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter))
    employee_map = {emp["emp_code"]: emp for emp in employees}
    if not employee_map:
        return {"month": month, "items": []}
    records = list(db.attendance_logs.find({"emp_code": {"$in": list(employee_map.keys())}, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}))
    aggregates: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        code = rec["emp_code"]
        entry = aggregates.setdefault(code, {"total_late_minutes": 0, "late_count": 0})
        entry["total_late_minutes"] += int(rec.get("late_minutes", 0) or 0)
        if rec.get("late_minutes", 0) > 0:
            entry["late_count"] += 1
    ranked_rows = []
    for code, info in aggregates.items():
        if info["total_late_minutes"] <= 0:
            continue
        emp = employee_map.get(code)
        if not emp:
            continue
        ranked_rows.append({
            "emp_code": code,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": info["total_late_minutes"],
            "late_count": info["late_count"],
        })
    ranked_rows.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
    output_rows = []
    rank = 0
    prev_total = None
    for row in ranked_rows:
        if prev_total is None or row["total_late_minutes"] != prev_total:
            rank = len(output_rows) + 1
            prev_total = row["total_late_minutes"]
        output_rows.append({
            "rank": rank,
            **row,
        })
    filtered = [row for row in output_rows if row["rank"] <= limit]
    return {"month": month, "items": filtered}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="Department not found")
    emp_codes = [emp["emp_code"] for emp in employees]
    records = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}))
    by_date: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        day = rec["date"]
        entry = by_date.setdefault(day, {"present_count": Decimal("0"), "late_count": 0})
        if rec.get("status") in PRESENCE_STATUSES:
            entry["present_count"] += Decimal("0.5") if rec.get("half_day") else Decimal("1")
        if rec.get("late_minutes", 0) > 0:
            entry["late_count"] += 1

    current = from_date
    result_items = []
    while current <= to_date:
        day_key = current.isoformat()
        headcount = sum(1 for emp in employees if emp["joined_on"] <= day_key)
        day_record = by_date.get(day_key, {"present_count": Decimal("0"), "late_count": 0})
        present_count = float(day_record["present_count"])
        late_count = int(day_record["late_count"])
        is_working = is_working_day(current)
        attendance_rate = None
        if is_working and headcount > 0:
            attendance_rate = float(round_half_up(Decimal(str(present_count)) / Decimal(headcount), 4))
            if attendance_rate == 0:
                attendance_rate = 0.0
        result_items.append({
            "date": day_key,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        })
        current += timedelta(days=1)
    for idx, item in enumerate(result_items):
        window_values = []
        for j in range(max(0, idx - 6), idx + 1):
            candidate = result_items[j]["attendance_rate"]
            if candidate is not None:
                window_values.append(candidate)
        if window_values:
            avg = float(round_half_up(sum(Decimal(str(v)) for v in window_values) / Decimal(len(window_values)), 4))
            item["moving_avg_7d"] = avg
        else:
            item["moving_avg_7d"] = None
    return {"department": department, "items": result_items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    endpoint_name = endpoint
    if endpoint_name == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        query: Dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to:
                query["date"]["$lte"] = date_to.isoformat()
        if status:
            valid_status_enum(status)
            query["status"] = status
        explain = client[MONGO_DB].command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain}
    if endpoint_name == "employee_monthly":
        if month is None or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="month is required")
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")
        ensure_employee_exists(emp_code)
        first_day, last_day = month_range(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}}]
        explain = client[MONGO_DB].command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain}
    if endpoint_name == "department_summary":
        if month is None or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="month is required")
        first_day, last_day = month_range(month)
        query = {"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}
        if department is not None:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
            query["emp_code"] = {"$in": emp_codes} if emp_codes else {"$in": []}
        explain = client[MONGO_DB].command({
            "explain": {"find": "attendance_logs", "filter": query},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain}
    if endpoint_name == "late_leaderboard":
        if month is None or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=422, detail="month is required")
        first_day, last_day = month_range(month)
        employees = list(db.employees.find({"department": department} if department else {}, {"_id": 0, "emp_code": 1}))
        emp_codes = [e["emp_code"] for e in employees]
        query = {"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}
        if emp_codes:
            query["emp_code"] = {"$in": emp_codes}
        explain = client[MONGO_DB].command({
            "explain": {"find": "attendance_logs", "filter": query},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain}
    if endpoint_name == "department_trend":
        if department is None:
            raise HTTPException(status_code=422, detail="department is required")
        if from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="from and to are required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must be on or after from")
        employees = list(db.employees.find({"department": department}, {"emp_code": 1}))
        if not employees:
            raise HTTPException(status_code=404, detail="Department not found")
        query = {"emp_code": {"$in": [e["emp_code"] for e in employees]}, "date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}
        explain = client[MONGO_DB].command({
            "explain": {"find": "attendance_logs", "filter": query},
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain}
    raise HTTPException(status_code=422, detail="Unknown endpoint")
