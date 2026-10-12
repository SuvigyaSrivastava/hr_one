"""Live-API tests: POST /attendance/punch-out and PATCH /attendance/{emp_code}/{date}."""
from datetime import datetime, timezone

import pytest
from helpers import emp, ms


def punch_in(api, code="EMP9001", at=None, status=None):
    body = {"emp_code": code, "punched_at": at or ms(2026, 7, 6, 9, 28)}
    if status:
        body["status"] = status
    r = api.post("/attendance/punch-in", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def punch_out(api, code="EMP9001", at=None):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": at})


@pytest.fixture
def e1(api):
    assert api.post("/employees", json=emp()).status_code == 201


# =============================================================================== punch-out
def test_punch_out_normal_day(api, e1):
    punch_in(api)
    r = punch_out(api, at=ms(2026, 7, 6, 18, 35))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (9.12, 0, 0, False)
    assert b["punch_out"] == ms(2026, 7, 6, 18, 35) and b["history"] == [] and "id" not in b


def test_punch_out_late_with_overtime_keeps_late(api, e1):
    punch_in(api, at=ms(2026, 7, 6, 10, 5))
    b = punch_out(api, at=ms(2026, 7, 6, 19, 10)).json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"]) == (9.08, 35, 40)


def test_punch_out_half_day_wfh(api, e1):
    punch_in(api, at=ms(2026, 7, 7, 9, 30), status="WFH")
    b = punch_out(api, at=ms(2026, 7, 7, 13, 0)).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True and b["status"] == "WFH"


def test_punch_out_half_day_boundary_uses_rounded_hours(api, e1):
    punch_in(api, at=ms(2026, 7, 6, 9, 0, 0))
    b = punch_out(api, at=ms(2026, 7, 6, 9, 0, 0) + 16182 * 1000).json()   # 4.495 h -> 4.50 -> not half day
    assert b["work_hours"] == 4.5 and b["half_day"] is False


def test_punch_out_overnight_across_midnight(api):
    api.post("/employees", json=emp("EMP9005", shift_start="22:00", shift_end="06:00"))
    punch_in(api, "EMP9005", at=ms(2026, 7, 6, 21, 55))
    r = punch_out(api, "EMP9005", at=ms(2026, 7, 7, 6, 40))
    assert r.status_code == 200
    b = r.json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"], b["late_minutes"]) == ("2026-07-06", 8.75, 40, 0)


def test_punch_out_truncates_to_seconds(api, e1):
    punch_in(api)
    b = punch_out(api, at=ms(2026, 7, 6, 18, 35, 0, 900_000)).json()
    assert b["punch_out"] == ms(2026, 7, 6, 18, 35, 0)


def test_punch_out_closes_most_recent_record_only(api, e1):
    punch_in(api, at=ms(2026, 7, 6, 9, 28))
    punch_out(api, at=ms(2026, 7, 6, 18, 0))
    punch_in(api, at=ms(2026, 7, 7, 9, 28))
    r = punch_out(api, at=ms(2026, 7, 7, 18, 0))
    assert r.status_code == 200 and r.json()["date"] == "2026-07-07"
    day1 = api.get("/attendance", params={"emp_code": "EMP9001", "date_to": "2026-07-06"}).json()["items"][0]
    assert day1["punch_out"] == ms(2026, 7, 6, 18, 0)


def test_punch_out_unknown_employee_404(api):
    assert punch_out(api, "EMP9999", at=ms(2026, 7, 6, 18, 0)).status_code == 404


def test_punch_out_without_punch_in_404(api, e1):
    assert punch_out(api, at=ms(2026, 7, 6, 18, 0)).status_code == 404


def test_punch_out_time_not_after_punch_in(api, e1):
    punch_in(api, at=ms(2026, 7, 6, 9, 28))
    assert punch_out(api, at=ms(2026, 7, 6, 9, 28)).status_code == 422       # equal -> 422
    # strictly earlier: no record has punch_in <= punched_at, so there is "no punch-in found"
    assert punch_out(api, at=ms(2026, 7, 6, 8, 0)).status_code == 404


def test_punch_out_more_than_24h_after_punch_in_422(api, e1):
    punch_in(api, at=ms(2026, 7, 6, 9, 28))
    assert punch_out(api, at=ms(2026, 7, 7, 9, 28, 1)).status_code == 422
    assert punch_out(api, at=ms(2026, 7, 7, 9, 28, 0)).status_code == 200    # exactly 24 h is allowed


def test_punch_out_twice_409(api, e1):
    punch_in(api)
    assert punch_out(api, at=ms(2026, 7, 6, 18, 0)).status_code == 200
    assert punch_out(api, at=ms(2026, 7, 6, 19, 0)).status_code == 409


def test_punch_out_concurrent_exactly_one_200(api, e1, parallel):
    punch_in(api)
    codes = [r.status_code for r in parallel(lambda: punch_out(api, at=ms(2026, 7, 6, 18, 35)), 15)]
    assert codes.count(200) == 1 and codes.count(409) == 14


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.5, "1783312500000", True, 0])
def test_punch_out_bad_timestamp_422(api, e1, bad):
    punch_in(api)
    assert punch_out(api, at=bad).status_code == 422


# =============================================================================== PATCH
def patch(api, code="EMP9001", date="2026-07-06", **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return api.patch(f"/attendance/{code}/{date}", json=body)


@pytest.fixture
def day(api, e1):
    """Late day with overtime: in 10:05, out 19:10 -> late 35, ot 40, 9.08 h."""
    punch_in(api, at=ms(2026, 7, 6, 10, 5))
    punch_out(api, at=ms(2026, 7, 6, 19, 10))


def test_patch_recomputes_and_appends_history(api, day):
    r = patch(api, punch_in=ms(2026, 7, 6, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert (b["late_minutes"], b["work_hours"], b["overtime_minutes"], b["half_day"]) == (0, 9.7, 40, False)
    assert b["punch_in"] == ms(2026, 7, 6, 9, 28)
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {                      # only fields that actually changed, instants as epoch ms
        "punch_in": {"from": ms(2026, 7, 6, 10, 5), "to": ms(2026, 7, 6, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 9.08, "to": 9.7},
    }
    stored = api.get("/attendance", params={"emp_code": "EMP9001"}).json()["items"][0]
    assert stored["history"] == b["history"]


def test_patch_appends_never_overwrites(api, day):
    first = patch(api, punch_in=ms(2026, 7, 6, 9, 28)).json()["history"][0]
    b = patch(api, punch_out=ms(2026, 7, 6, 18, 0), reason="left early", regularized_by="boss").json()
    assert len(b["history"]) == 2 and b["history"][0] == first
    assert b["history"][1]["changes"]["punch_out"] == {"from": ms(2026, 7, 6, 19, 10), "to": ms(2026, 7, 6, 18, 0)}
    assert b["history"][1]["changes"]["overtime_minutes"] == {"from": 40, "to": 0}


def test_patch_status_only_change(api, day):
    b = patch(api, status="WFH").json()
    assert b["status"] == "WFH" and list(b["history"][0]["changes"]) == ["status"]


def test_patch_absent_clears_punches(api, day):
    b = patch(api, status="ABSENT").json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"]) == ("ABSENT", None, None, None)
    assert (b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (0, 0, False)
    ch = b["history"][0]["changes"]
    assert ch["punch_in"] == {"from": ms(2026, 7, 6, 10, 5), "to": None}
    assert ch["late_minutes"] == {"from": 35, "to": 0}
    assert ch["work_hours"] == {"from": 9.08, "to": None}


@pytest.mark.parametrize("extra", [{"punch_in": ms(2026, 7, 6, 9, 0)}, {"punch_out": ms(2026, 7, 6, 18, 0)}])
@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
def test_patch_absent_leave_with_punch_times_422(api, day, status, extra):
    assert patch(api, status=status, **extra).status_code == 422


def test_patch_absent_to_present_requires_punch_in(api, e1, mongo_db):
    mongo_db.attendance_logs.insert_one({"emp_code": "EMP9001", "date": "2026-07-08", "status": "ABSENT",
                                         "punch_in": None, "punch_out": None, "work_hours": None,
                                         "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})
    assert patch(api, date="2026-07-08", status="PRESENT").status_code == 422
    assert patch(api, date="2026-07-08", status="PRESENT", punch_out=ms(2026, 7, 8, 18, 0)).status_code == 422
    r = patch(api, date="2026-07-08", status="PRESENT", punch_in=ms(2026, 7, 8, 9, 30), punch_out=ms(2026, 7, 8, 13, 30))
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["half_day"], b["late_minutes"]) == (4.0, True, 0)


def test_patch_legacy_record_without_history(api, e1, mongo_db):
    mongo_db.attendance_logs.insert_one({"emp_code": "EMP9001", "date": "2026-07-14", "status": "PRESENT",
                                         "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                         "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                                         "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    r = patch(api, date="2026-07-14", status="WFH")
    assert r.status_code == 200 and len(r.json()["history"]) == 1


def test_patch_punch_in_must_stay_on_attendance_date(api, day):
    assert patch(api, punch_in=ms(2026, 7, 7, 9, 28)).status_code == 422
    assert patch(api, punch_in=ms(2026, 7, 5, 9, 28)).status_code == 422


def test_patch_overnight_punch_in_date_rule(api):
    api.post("/employees", json=emp("EMP9005", shift_start="22:00", shift_end="06:00"))
    punch_in(api, "EMP9005", at=ms(2026, 7, 6, 22, 5))
    assert patch(api, "EMP9005", punch_in=ms(2026, 7, 7, 1, 0)).status_code == 200    # still the 6th's shift
    assert patch(api, "EMP9005", punch_in=ms(2026, 7, 7, 7, 0)).status_code == 422    # belongs to the 7th


def test_patch_punch_out_ordering_and_24h(api, day):
    assert patch(api, punch_out=ms(2026, 7, 6, 10, 5)).status_code == 422      # equal to punch_in
    assert patch(api, punch_out=ms(2026, 7, 6, 9, 0)).status_code == 422       # before punch_in
    assert patch(api, punch_out=ms(2026, 7, 7, 10, 5, 1)).status_code == 422   # > 24 h
    assert patch(api, punch_in=ms(2026, 7, 6, 20, 0)).status_code == 422       # existing punch_out (19:10) now before it


def test_patch_noop_is_422(api, day):
    assert patch(api, punch_in=ms(2026, 7, 6, 10, 5)).status_code == 422
    assert patch(api, status="PRESENT").status_code == 422


def test_patch_ignores_derived_fields(api, day):
    assert patch(api, work_hours=1.0, late_minutes=99).status_code == 422      # nothing editable changed
    b = patch(api, punch_in=ms(2026, 7, 6, 9, 28), work_hours=1.0, late_minutes=99, half_day=True).json()
    assert (b["work_hours"], b["late_minutes"], b["half_day"]) == (9.7, 0, False)


def test_patch_404s(api, e1, day):
    assert patch(api, code="EMP9999", status="WFH").status_code == 404
    assert patch(api, date="2026-07-20", status="WFH").status_code == 404


@pytest.mark.parametrize("bad", [
    {"reason": "abc"}, {"reason": "     "}, {"reason": "x" * 201}, {"regularized_by": ""}, {"regularized_by": "y" * 51},
    {"status": "HOLIDAY"}, {"punch_in": None}, {"punch_out": None}, {"punch_in": 1783312500},
    {"punch_in": 1783312500000.5}, {"punch_out": "1783312500000"}, {"status": None},
])
def test_patch_validation_422(api, day, bad):
    assert patch(api, **{"status": "WFH", **bad}).status_code == 422


def test_patch_missing_required_fields_422(api, day):
    assert api.patch("/attendance/EMP9001/2026-07-06", json={"status": "WFH"}).status_code == 422
    assert api.patch("/attendance/EMP9001/2026-07-06", json={"reason": "valid reason"}).status_code == 422


def test_patch_bad_date_path_422(api, e1):
    assert patch(api, date="2026-13-45", status="WFH").status_code == 422
    assert patch(api, date="2026-7-6", status="WFH").status_code == 422


def test_patch_reason_with_dollar_sign_stored_literally(api, day):
    b = patch(api, status="WFH", reason="$100 correction").json()
    assert b["history"][0]["reason"] == "$100 correction"


def test_patch_concurrent_edits_lose_no_history(api, day, parallel):
    minutes = iter(range(0, 60))
    import threading
    lock = threading.Lock()

    def fire():
        with lock:
            m = next(minutes)
        return patch(api, punch_in=ms(2026, 7, 6, 8, m), reason=f"edit number {m}", regularized_by=f"u{m}")

    results = parallel(fire, 12)
    codes = [r.status_code for r in results]
    assert set(codes) <= {200, 409}, codes
    assert codes.count(200) >= 1
    final = api.get("/attendance", params={"emp_code": "EMP9001"}).json()["items"][0]
    hist = final["history"]
    assert len(hist) == codes.count(200)                                     # every success is in the trail, nothing lost
    prev_to = ms(2026, 7, 6, 10, 5)                                          # the trail is a consistent chain
    for h in hist:
        assert h["changes"]["punch_in"]["from"] == prev_to
        prev_to = h["changes"]["punch_in"]["to"]
    assert final["punch_in"] == prev_to
