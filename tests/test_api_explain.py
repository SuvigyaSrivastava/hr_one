"""Live-API tests: GET /admin/explain/{endpoint}. Plan checks on the 100k dataset are in tests/loadcheck.py."""
import pytest

CASES = {
    "attendance_list": {},
    "attendance_list ": {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT",
                         "page": 2, "page_size": 5},
    "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"},
    "late_leaderboard": {"month": "2026-07", "limit": 5},
    "department_trend": {"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"},
}


def plan_text(r):
    import json
    return json.dumps(r.json()["explain"])


@pytest.mark.parametrize("name", list(CASES))
def test_explain_returns_executionstats_with_index_scan_and_no_collscan(api, name):
    endpoint = name.strip()
    r = api.get(f"/admin/explain/{endpoint}", params=CASES[name])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["endpoint"] == endpoint and b["collection"] in ("employees", "attendance_logs")
    assert isinstance(b["explain"], dict) and "executionStats" in plan_text(r)
    assert "IXSCAN" in plan_text(r) and "COLLSCAN" not in plan_text(r)


def test_explain_is_plain_json_numbers(api):
    r = api.get("/admin/explain/attendance_list")
    stats = r.json()["explain"]["executionStats"]
    assert isinstance(stats["nReturned"], int) and isinstance(stats["totalDocsExamined"], int)


def test_explain_attendance_list_pagination_is_part_of_the_plan(api):
    r = api.get("/admin/explain/attendance_list", params={"page": 3, "page_size": 7})
    txt = plan_text(r)
    assert '"skip": 14' in txt or '"limit": 21' in txt or "SKIP" in txt


@pytest.mark.parametrize("endpoint,params", [
    ("employee_monthly", {"month": "2026-07"}), ("employee_monthly", {"emp_code": "EMP0001"}),
    ("department_summary", {}), ("late_leaderboard", {}),
    ("department_trend", {"department": "Sales", "from": "2026-07-01"}),
    ("department_trend", {"from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_missing_required_params_422(api, endpoint, params):
    assert api.get(f"/admin/explain/{endpoint}", params=params).status_code == 422


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {"date_from": "2026-07-10", "date_to": "2026-07-01"}),
    ("attendance_list", {"status": "BOGUS"}),
    ("late_leaderboard", {"month": "2026-13"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 99}),
    ("department_trend", {"department": "Sales", "from": "2026-07-10", "to": "2026-07-01"}),
    ("department_trend", {"department": "Sales", "from": "2026-01-01", "to": "2026-12-31"}),
    ("nope", {}),
])
def test_explain_invalid_params_422(api, endpoint, params):
    assert api.get(f"/admin/explain/{endpoint}", params=params).status_code == 422
