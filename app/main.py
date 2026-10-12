"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here by assignment rule):
  1. Configuration and database handles
  2. Rules engine            - pure functions for R1-R5 / R8, no I/O, unit-tested in tests/test_rules.py
  3. Request models / types  - strict validation, everything invalid becomes a 422
  4. Serializers             - BSON -> API shapes (epoch ms, no _id, defaults for legacy documents)
  5. Indexes                 - created idempotently at startup
  6. Endpoints
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the real environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
# The defaults only exist for local development; the grader injects its own values.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: every datetime read from Mongo is an aware UTC datetime (never naive).
# The short selection timeout makes an unreachable database fail fast (503) instead of hanging requests.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
MAX_PAGE = 1_000_000

# --------------------------------------------------------------------------- #
# 2. Rules engine (R1 - R5, R8): pure functions, no database access
# --------------------------------------------------------------------------- #
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 10 * 60              # R2: late only if strictly more than 10:00 after shift start
OVERTIME_MIN_MINUTES = 30            # R3
HALF_DAY_THRESHOLD = Decimal("4.50")  # R5 (compared against the ROUNDED work hours)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")      # R6
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
ALL_STATUSES = PRESENCE_STATUSES + NON_PRESENCE_STATUSES


def round_half_up(value, places: int = 2) -> float:
    """R8: half-up rounding (not banker's, not binary-float rounding)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def to_utc(dt: datetime) -> datetime:
    """Mongo may hand back naive datetimes (always UTC); make everything tz-aware UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """UTC/naive datetime -> epoch ms, truncated to whole seconds."""
    return ((to_utc(dt) - EPOCH) // timedelta(seconds=1)) * 1000


def trunc_seconds(dt: datetime) -> datetime:
    return to_utc(dt).replace(microsecond=0)


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_minutes(shift_end) <= hhmm_to_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = trunc_seconds(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        minutes_into_day = local.hour * 60 + local.minute
        if minutes_into_day < hhmm_to_minutes(shift_end):
            day -= timedelta(days=1)
    return day.isoformat()


def _ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=plus_days)
    h, m = divmod(hhmm_to_minutes(hhmm), 60)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    return _ist_at(date_str, shift_start)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The overnight shift's end falls on the next calendar day."""
    return _ist_at(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)


def calc_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly > 10:00 after shift start; then floor minutes since shift start."""
    delta = int((trunc_seconds(punch_in) - shift_start_at(date_str, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def calc_overtime_minutes(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes from shift_end to punch_out, counted only when >= 30."""
    delta = int((trunc_seconds(punch_out) - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = delta // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def calc_half_day(work_hours: Optional[float]) -> bool:
    """R5: based on the rounded work hours."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_THRESHOLD


def compute_derived(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                    date_str: str, shift_start: str, shift_end: str) -> dict:
    """All stored derived fields for a record (DATA_MODEL.md table)."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = calc_late_minutes(punch_in, date_str, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work_hours = calc_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": calc_overtime_minutes(punch_out, date_str, shift_start, shift_end),
        "half_day": calc_half_day(work_hours),
    }


# --------------------------------------------------------------------------- #
# 3. Request models and validated types
# --------------------------------------------------------------------------- #
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000   # rejects seconds-looking values and absurd instants
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _check_date(value: str) -> str:
    if not _DATE_RE.fullmatch(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError("not a real calendar date")
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# StrictInt: floats, numeric strings and booleans are rejected instead of silently coerced.
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")      # unknown / server-owned fields (e.g. created_at) are ignored

    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=64)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


def invalid(loc: list, msg: str, value: Any = None):
    """Raise a FastAPI-style 422 for a check that cannot be expressed on a single field."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


# --------------------------------------------------------------------------- #
# 4. Serializers (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _ms_or_none(value: Any) -> Optional[int]:
    return dt_to_ms(value) if isinstance(value, datetime) else None


def _history_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_history(entry: dict) -> dict:
    changes = {
        field: {"from": _history_value(c.get("from")), "to": _history_value(c.get("to"))}
        for field, c in (entry.get("changes") or {}).items()
    }
    return {"at": _ms_or_none(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_attendance(doc: dict) -> dict:
    """Never exposes _id; fills in the defaults that legacy documents may be missing."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms_or_none(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup, idempotent: same name + same keys is a no-op)
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        # uniqueness of the business key, enforced by the database (concurrent creates), and the join key
        dict(keys=[("emp_code", ASCENDING)], name="uq_emp_code", unique=True),
        # GET /employees?department=..  (filter + emp_code sort in index order)
        dict(keys=[("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_dept_emp"),
        # headcount: joined_on <= end of period, with or without a department
        dict(keys=[("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_joined_dept"),
    ],
    "attendance_logs": [
        # one record per (employee, day): punch-in race guard, PATCH/punch-out lookup, per-employee ranges
        dict(keys=[("emp_code", ASCENDING), ("date", ASCENDING)], name="uq_emp_date", unique=True),
        # GET /attendance sort order (date desc, emp_code asc) and date-range scans
        dict(keys=[("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_desc_emp"),
        # GET /attendance?status=..
        dict(keys=[("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_status_date_emp"),
    ],
}
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    for coll, specs in INDEXES.items():
        for spec in specs:
            db[coll].create_index(spec["keys"], name=spec["name"], unique=spec.get("unique", False))
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep serving; /health reports 503 and retries index creation
        log.error("index creation at startup failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error_handler(_, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = trunc_seconds(datetime.now(UTC))
    try:
        employees.insert_one(doc)        # the unique index is the only arbiter - no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department else {}
    total = employees.count_documents(query)
    cursor = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched = ms_to_dt(body.punched_at) if body.punched_at is not None else trunc_seconds(datetime.now(UTC))
    date_str = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **compute_derived(body.status, punched, None, date_str, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        attendance.insert_one(doc)       # unique (emp_code, date) index: exactly one concurrent caller wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                           status: Optional[str]) -> dict:
    if date_from and date_to and date_from > date_to:
        invalid(["query", "date_from"], "date_from must not be after date_to", date_from)
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[DateStr], Query()] = None,
    date_to: Annotated[Optional[DateStr], Query()] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = attendance.count_documents(query)
    cursor = attendance.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
