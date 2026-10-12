"""Live-API tests for employees, punch-in, health and the attendance list (starter endpoints)."""
from datetime import datetime, timezone

import pytest

from helpers import emp, ms


# ----------------------------------------------------------------------------- health
def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


# ----------------------------------------------------------------------------- employees
def test_create_employee_shape(api):
    r = api.post("/employees", json=emp())
    assert r.status_code == 201
    b = r.json()
    assert "_id" not in b and "id" not in b
    assert isinstance(b["created_at"], int) and b["created_at"] > 100_000_000_000   # epoch ms, not a string


def test_create_employee_ignores_client_created_at(api):
    b = api.post("/employees", json=emp(created_at=5)).json()
    assert b["created_at"] != 5


def test_duplicate_employee_409(api):
    assert api.post("/employees", json=emp()).status_code == 201
    assert api.post("/employees", json=emp()).status_code == 409


def test_concurrent_duplicate_employee(api, parallel):
    codes = [r.status_code for r in parallel(lambda: api.post("/employees", json=emp("EMP9002")), 12)]
    assert codes.count(201) == 1 and codes.count(409) == 11


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "emp9003"}, {"emp_code": "EMP9003X"}, {"email": "not-an-email"},
    {"name": ""}, {"department": ""}, {"shift_start": "9:30"}, {"shift_start": "24:00"},
    {"shift_start": "09:30", "shift_end": "09:30"}, {"joined_on": "2026-02-30"}, {"joined_on": "2026-2-3"},
    {"joined_on": "yesterday"},
])
def test_create_employee_validation_422(api, patch):
    assert api.post("/employees", json=emp("EMP9003", **patch)).status_code == 422


def test_list_employees_pagination_sort_and_total(api):
    for i in range(1, 6):
        api.post("/employees", json=emp(f"EMP900{i}", department="DeptA" if i <= 3 else "DeptB"))
    r = api.get("/employees", params={"department": "DeptA", "page_size": 2, "page": 1}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP9001", "EMP9002"]
    assert r["total"] == 3                                   # filtered total, not whole collection
    r = api.get("/employees", params={"department": "DeptA", "page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP9003"]


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_employees_bad_paging_422(api, params):
    assert api.get("/employees", params=params).status_code == 422


# ----------------------------------------------------------------------------- punch-in
def test_punch_in_unknown_employee_404(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.5, "1783312500000", True, -5, 0, 99_999_999_999,
                                 4_102_444_800_001])
def test_punch_in_bad_timestamp_422(api, bad):
    api.post("/employees", json=emp())
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": bad}).status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "HOLIDAY", "present", ""])
def test_punch_in_bad_status_422(api, status):
    api.post("/employees", json=emp())
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 30), "status": status})
    assert r.status_code == 422


def test_punch_in_record_shape_and_ist_date(api):
    api.post("/employees", json=emp())
    # 00:30 IST on 7 Jul == 19:00 UTC on 6 Jul: the attendance date must be the IST date
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": ms(2026, 7, 7, 0, 30)})
    assert r.status_code == 201
    b = r.json()
    assert b["date"] == "2026-07-07"
    assert "id" not in b and "_id" not in b
    assert b["punch_in"] == ms(2026, 7, 7, 0, 30)
    assert b["punch_out"] is None and b["work_hours"] is None and b["history"] == []
    assert b["overtime_minutes"] == 0 and b["half_day"] is False and b["status"] == "PRESENT"


def test_punch_in_truncates_to_seconds(api):
    api.post("/employees", json=emp())
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 40, 0, 900_000)}).json()
    assert b["punch_in"] == ms(2026, 7, 6, 9, 40, 0)
    assert b["late_minutes"] == 0                          # 09:40:00.900 counts as 09:40:00


@pytest.mark.parametrize("h,m,s,expected", [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 25, 0, 0)])
def test_punch_in_late_rule(api, h, m, s, expected):
    api.post("/employees", json=emp())
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, h, m, s)}).json()
    assert b["late_minutes"] == expected


def test_punch_in_overnight_shift_date(api):
    api.post("/employees", json=emp("EMP9005", shift_start="22:00", shift_end="06:00"))
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP9005", "punched_at": ms(2026, 7, 7, 1, 0)}).json()
    assert b["date"] == "2026-07-06"                       # belongs to the shift that started the previous day


def test_punch_in_wfh_and_on_duty_accepted(api):
    api.post("/employees", json=emp())
    api.post("/employees", json=emp("EMP9002"))
    for code, st in (("EMP9001", "WFH"), ("EMP9002", "ON_DUTY")):
        r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, 6, 9, 30), "status": st})
        assert r.status_code == 201 and r.json()["status"] == st


def test_punch_in_duplicate_409(api):
    api.post("/employees", json=emp())
    body = {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 30)}
    assert api.post("/attendance/punch-in", json=body).status_code == 201
    assert api.post("/attendance/punch-in", json={**body, "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409


def test_punch_in_concurrent_exactly_one_201(api, parallel):
    api.post("/employees", json=emp())
    body = {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 30)}
    codes = [r.status_code for r in parallel(lambda: api.post("/attendance/punch-in", json=body), 20)]
    assert codes.count(201) == 1 and codes.count(409) == 19


# ----------------------------------------------------------------------------- GET /attendance
def seed_days(api, code="EMP9001", n=3, status=None):
    api.post("/employees", json=emp(code))
    for d in range(n):
        body = {"emp_code": code, "punched_at": ms(2026, 7, 6 + d, 9, 30)}
        if status:
            body["status"] = status
        assert api.post("/attendance/punch-in", json=body).status_code == 201


def test_list_attendance_sort_filters_total(api):
    seed_days(api, "EMP9001", 3)
    seed_days(api, "EMP9002", 2, status="WFH")
    r = api.get("/attendance", params={"emp_code": "EMP9001", "page_size": 2}).json()
    assert [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07"] and r["total"] == 3
    r = api.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "emp_code": "EMP9002"}).json()
    assert r["total"] == 2 and all(i["status"] == "WFH" for i in r["items"])
    r = api.get("/attendance", params={"status": "WFH", "emp_code": "EMP9001"}).json()
    assert r["total"] == 0 and r["items"] == []


def test_list_attendance_secondary_sort_emp_code(api, mongo_db):
    # an isolated date, so the result is the same on a small database and on the 100k load database
    for code in ("EMP9002", "EMP9001", "EMP9003"):
        mongo_db.attendance_logs.insert_one({"emp_code": code, "date": "2031-05-05", "status": "PRESENT", "punch_in": None,
                                             "punch_out": None, "work_hours": None, "late_minutes": 0,
                                             "overtime_minutes": 0, "half_day": False, "history": []})
    items = api.get("/attendance", params={"date_from": "2031-05-05", "date_to": "2031-05-05"}).json()["items"]
    assert [i["emp_code"] for i in items] == ["EMP9001", "EMP9002", "EMP9003"]


def test_list_attendance_item_shape(api):
    seed_days(api, "EMP9001", 1)
    it = api.get("/attendance", params={"emp_code": "EMP9001"}).json()["items"][0]
    assert "id" not in it and "_id" not in it
    assert isinstance(it["punch_in"], int) and it["history"] == [] and it["half_day"] is False


def test_list_attendance_legacy_doc_defaults(api, mongo_db):
    mongo_db.attendance_logs.insert_one({"emp_code": "EMP9001", "date": "2026-07-14", "status": "PRESENT",
                                         "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                         "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                                         "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    it = api.get("/attendance", params={"emp_code": "EMP9001"}).json()["items"][0]
    assert it["history"] == [] and it["half_day"] is False and it["punch_out"] == 1784034060000


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-10", "date_to": "2026-07-01"}, {"date_from": "2026-13-01"}, {"date_to": "07/01/2026"},
    {"status": "BOGUS"}, {"page": 0}, {"page_size": 101}, {"page_size": 0},
])
def test_list_attendance_validation_422(api, params):
    assert api.get("/attendance", params=params).status_code == 422
