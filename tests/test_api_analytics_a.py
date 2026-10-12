"""Live-API tests: GET /analytics/employees/{emp_code}/monthly and GET /analytics/departments/summary.
Data is inserted straight into MongoDB (like the grader's seed) so stored derived values are fully controlled.
July 2026: 1 Jul is a Wednesday; 6, 13, 20, 27 are Mondays; 11 and 18 are Saturdays; 23 working days (Mon-Fri)."""
from datetime import datetime, timezone

import pytest


def utc(d, h=4, m=0):
    return datetime.strptime(d, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=timezone.utc)


def add_emp(db, code, dept="T_Alpha", joined="2026-01-05"):
    db.employees.insert_one({"emp_code": code, "name": f"N {code}", "email": f"{code}@e.com", "department": dept,
                             "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                             "created_at": utc("2026-01-05")})


def add_log(db, code, d, status="PRESENT", wh=8.0, late=0, ot=0, half=False, legacy=False):
    doc = {"emp_code": code, "date": d, "status": status}
    if status in ("PRESENT", "WFH", "ON_DUTY"):
        doc.update(punch_in=utc(d), punch_out=utc(d, 13) if wh is not None else None, work_hours=wh)
    else:
        doc.update(punch_in=None, punch_out=None, work_hours=None)
    doc.update(late_minutes=late, overtime_minutes=ot)
    if not legacy:
        doc.update(half_day=half, history=[])
    db.attendance_logs.insert_one(doc)


def monthly(api, code, month="2026-07"):
    return api.get(f"/analytics/employees/{code}/monthly", params={"month": month})


# ================================================================================= employee monthly
def test_monthly_full_example(api, mongo_db):
    db = mongo_db
    add_emp(db, "EMP9001")
    add_log(db, "EMP9001", "2026-07-06")                                    # Mon present
    add_log(db, "EMP9001", "2026-07-07", "WFH", wh=3.5, half=True)          # Tue half day = 0.5
    add_log(db, "EMP9001", "2026-07-08", "ON_DUTY", wh=None)                # Wed on duty (open) = 1
    add_log(db, "EMP9001", "2026-07-09", "ABSENT")                          # Thu absent = 0
    add_log(db, "EMP9001", "2026-07-10", "LEAVE")                           # Fri leave
    add_log(db, "EMP9001", "2026-07-11", late=20, ot=60)                    # SATURDAY: only late/ot totals
    add_log(db, "EMP9001", "2026-07-13", late=35)                           # Mon late
    add_log(db, "EMP9001", "2026-07-14", legacy=True)                       # Tue, no half_day/history fields
    add_log(db, "EMP9001", "2026-06-30", late=99, ot=99)                    # outside the month
    add_log(db, "EMP9001", "2026-08-01", late=99, ot=99)                    # outside the month
    r = monthly(api, "EMP9001")
    assert r.status_code == 200
    assert r.json() == {"emp_code": "EMP9001", "month": "2026-07", "working_days": 23, "present_days": 4.5,
                        "leave_days": 1, "late_count": 2, "total_late_minutes": 55, "total_overtime_minutes": 60,
                        "attendance_pct": 19.57}                            # 4.5 / 23 * 100 = 19.5652


def test_monthly_no_logs_is_zero_not_error(api, mongo_db):
    add_emp(mongo_db, "EMP9002")
    assert monthly(api, "EMP9002").json() == {
        "emp_code": "EMP9002", "month": "2026-07", "working_days": 23, "present_days": 0, "leave_days": 0,
        "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0}


@pytest.mark.parametrize("joined,working", [
    ("2026-07-15", 13),     # Wed: 15,16,17 + 20-24 + 27-31
    ("2026-07-18", 10),     # Saturday: first working day is Mon 20th
    ("2026-07-31", 1),      # last day of month (Friday)
    ("2026-07-01", 23),
    ("2026-08-03", 0),      # joins after the month
])
def test_monthly_working_days_counted_from_joined_on(api, mongo_db, joined, working):
    add_emp(mongo_db, "EMP9003", joined=joined)
    b = monthly(api, "EMP9003").json()
    assert b["working_days"] == working
    assert b["attendance_pct"] is None if working == 0 else b["attendance_pct"] == 0.0


def test_monthly_mid_month_joiner_full_attendance_is_100(api, mongo_db):
    add_emp(mongo_db, "EMP9004", joined="2026-07-20")
    for d in ("20", "21", "22", "23", "24", "27", "28", "29", "30", "31"):
        add_log(mongo_db, "EMP9004", f"2026-07-{d}")
    b = monthly(api, "EMP9004").json()
    assert (b["working_days"], b["present_days"], b["attendance_pct"]) == (10, 10, 100.0)


def test_monthly_present_days_only_weekdays_but_weekend_late_counts(api, mongo_db):
    add_emp(mongo_db, "EMP9005")
    add_log(mongo_db, "EMP9005", "2026-07-18", late=40, ot=33)
    add_log(mongo_db, "EMP9005", "2026-07-19", late=0, ot=0)
    b = monthly(api, "EMP9005").json()
    assert (b["present_days"], b["late_count"], b["total_late_minutes"], b["total_overtime_minutes"]) == (0, 1, 40, 33)


def test_monthly_rate_rounding_half_up(api, mongo_db):
    add_emp(mongo_db, "EMP9006")
    for d in ("06", "07", "08"):                                             # 3 / 23 = 13.0434... -> 13.04
        add_log(mongo_db, "EMP9006", f"2026-07-{d}")
    assert monthly(api, "EMP9006").json()["attendance_pct"] == 13.04


def test_monthly_february_leap_and_non_leap(api, mongo_db):
    add_emp(mongo_db, "EMP9007")
    assert monthly(api, "EMP9007", "2026-02").json()["working_days"] == 20   # Feb 2026 = 28 days, Mon 2 Feb start
    assert monthly(api, "EMP9007", "2028-02").json()["working_days"] == 21   # leap year, 29 days


def test_monthly_december_boundary(api, mongo_db):
    add_emp(mongo_db, "EMP9007")
    add_log(mongo_db, "EMP9007", "2026-12-31")
    add_log(mongo_db, "EMP9007", "2027-01-01")
    b = monthly(api, "EMP9007", "2026-12").json()
    assert (b["working_days"], b["present_days"]) == (23, 1)


def test_monthly_404_unknown_employee(api):
    assert monthly(api, "EMP9999").status_code == 404


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "202607", "2026-7", "July", "", "0000-01", "2026-07-01"])
def test_monthly_bad_month_422(api, mongo_db, month):
    add_emp(mongo_db, "EMP9001")
    assert monthly(api, "EMP9001", month).status_code == 422


def test_monthly_missing_month_422(api, mongo_db):
    add_emp(mongo_db, "EMP9001")
    assert api.get("/analytics/employees/EMP9001/monthly").status_code == 422


# ================================================================================= department summary
@pytest.fixture
def dept_data(mongo_db):
    db = mongo_db
    for code, joined in (("EMP9101", "2026-01-05"), ("EMP9102", "2026-01-05"), ("EMP9103", "2026-07-31"),
                         ("EMP9104", "2026-08-01"), ("EMP9105", "2026-03-01")):
        add_emp(db, code, "T_Alpha", joined)
    add_emp(db, "EMP9111", "T_Beta")
    add_emp(db, "EMP9121", "T_Empty", "2026-08-05")
    # EMP9102 (no logs) and EMP9103 (joins on the last day, no logs) must still count in the headcount
    add_log(db, "EMP9101", "2026-07-06", wh=9.0)
    add_log(db, "EMP9101", "2026-07-07", wh=9.0, late=15, ot=30)
    add_log(db, "EMP9101", "2026-07-08", "WFH", wh=4.0, half=True)
    add_log(db, "EMP9101", "2026-07-09", "ON_DUTY", wh=None)                 # open record: no hours
    add_log(db, "EMP9101", "2026-07-11", wh=6.0, late=12, ot=45)             # Saturday
    add_log(db, "EMP9105", "2026-07-06", wh=5.0, late=15)
    add_log(db, "EMP9105", "2026-07-10", "LEAVE")
    add_log(db, "EMP9105", "2026-07-13", "ABSENT")
    add_log(db, "EMP9199", "2026-07-06", wh=100.0, late=500)                 # orphan log: no such employee
    add_log(db, "EMP9111", "2026-07-06", wh=8.12)
    add_log(db, "EMP9111", "2026-07-07", wh=8.13)


def summary(api, month="2026-07", **params):
    return api.get("/analytics/departments/summary", params={"month": month, **params})


def test_summary_alpha_numbers(api, dept_data):
    r = summary(api, department="T_Alpha")
    assert r.status_code == 200 and r.json()["month"] == "2026-07"
    assert r.json()["items"] == [{
        "department": "T_Alpha",
        "headcount": 4,                 # 9101, 9102 (no logs), 9103 (joins 31 Jul), 9105; 9104 joins in August
        "present_days": 4.5,            # 9101: 1+1+0.5+1 (Sat excluded) ; 9105: 1
        "avg_work_hours": 6.6,          # (9+9+4+6+5)/5 records - NOT an average of per-employee averages
        "late_count": 3,                # 15, 12 (Saturday), 15
        "total_late_minutes": 42,
        "leave_count": 1,
        "on_duty_count": 1,
    }]


def test_summary_average_is_over_records_not_employees(api, dept_data):
    # per-employee averages would be (28/4 + 5/1) / 2 = 6.0 ; over records it is 33 / 5 = 6.6
    assert summary(api, department="T_Alpha").json()["items"][0]["avg_work_hours"] == 6.6


def test_summary_average_rounds_half_up(api, dept_data):
    b = summary(api, department="T_Beta").json()["items"][0]
    assert b["avg_work_hours"] == 8.13           # (8.12 + 8.13) / 2 = 8.125 -> 8.13 (banker's would give 8.12)
    assert b["headcount"] == 1 and b["present_days"] == 2


def test_summary_department_with_zero_headcount_is_omitted(api, dept_data):
    assert summary(api, department="T_Empty").json()["items"] == []
    assert summary(api, department="NoSuchDept").json()["items"] == []


def test_summary_headcount_follows_the_month(api, dept_data):
    # in August EMP9104 (joined 1 Aug) and the T_Empty employee have joined; there are no logs at all
    a = summary(api, "2026-08", department="T_Alpha").json()["items"][0]
    assert a["headcount"] == 5 and a["present_days"] == 0 and a["avg_work_hours"] is None
    e = summary(api, "2026-08", department="T_Empty").json()["items"][0]
    assert e["headcount"] == 1 and e["avg_work_hours"] is None
    assert summary(api, "2025-12", department="T_Alpha").json()["items"] == []     # before anyone joined


def test_summary_sorted_by_department_and_no_filter(api, dept_data):
    items = [i for i in summary(api).json()["items"] if i["department"].startswith("T_")]
    assert [i["department"] for i in items] == ["T_Alpha", "T_Beta"]
    all_depts = [i["department"] for i in summary(api).json()["items"]]
    assert all_depts == sorted(all_depts)


@pytest.mark.parametrize("month", ["2026-13", "202607", "", "2026-7"])
def test_summary_bad_month_422(api, month):
    assert summary(api, month).status_code == 422


def test_summary_missing_month_422(api):
    assert api.get("/analytics/departments/summary").status_code == 422
