"""Live-API tests: GET /analytics/leaderboard/late and GET /analytics/departments/{department}/trend."""
import pytest
from test_api_analytics_a import add_emp, add_log

LB_MONTH = "2031-03"          # a month in which no other data exists, so the global ranking is fully controlled


def lb(api, **params):
    return api.get("/analytics/leaderboard/late", params={"month": LB_MONTH, **params})


@pytest.fixture
def lb_data(mongo_db):
    db = mongo_db
    for code, dept in (("EMP9201", "T_A"), ("EMP9202", "T_A"), ("EMP9203", "T_A"), ("EMP9204", "T_A"),
                       ("EMP9205", "T_A"), ("EMP9206", "T_A"), ("EMP9208", "T_B")):
        add_emp(db, code, dept)
    add_log(db, "EMP9201", "2031-03-03", late=60)
    add_log(db, "EMP9201", "2031-03-04", late=40)           # total 100 over 2 late records
    add_log(db, "EMP9202", "2031-03-03", late=80)
    add_log(db, "EMP9203", "2031-03-05", late=50)
    add_log(db, "EMP9203", "2031-03-08", late=30)           # a Saturday: counts. total 80, tied with EMP9202
    add_log(db, "EMP9204", "2031-03-03", late=50)
    add_log(db, "EMP9205", "2031-03-03", late=30)
    add_log(db, "EMP9206", "2031-03-03", late=0)             # not late: excluded
    add_log(db, "EMP9208", "2031-03-03", late=90)            # other department
    add_log(db, "EMP9207", "2031-03-03", late=500)           # orphan log, no such employee: ignored
    add_log(db, "EMP9201", "2031-02-28", late=999)           # outside the month
    add_log(db, "EMP9201", "2031-04-01", late=999)
    add_log(db, "EMP9205", "2031-03-10", legacy=True)        # legacy doc, fine


def rows(r):
    return [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in r.json()["items"]]


def test_leaderboard_ranking_and_fields(api, lb_data):
    r = lb(api)
    assert r.status_code == 200 and r.json()["month"] == LB_MONTH
    assert rows(r) == [(1, "EMP9201", 100), (2, "EMP9208", 90), (3, "EMP9202", 80), (3, "EMP9203", 80),
                       (5, "EMP9204", 50), (6, "EMP9205", 30)]
    first = r.json()["items"][0]
    assert first == {"rank": 1, "emp_code": "EMP9201", "name": "N EMP9201", "department": "T_A",
                     "total_late_minutes": 100, "late_count": 2}
    assert [i["late_count"] for i in r.json()["items"]] == [2, 1, 1, 2, 1, 1]


@pytest.mark.parametrize("limit,expected", [
    (1, ["EMP9201"]),
    (2, ["EMP9201", "EMP9208"]),
    (3, ["EMP9201", "EMP9208", "EMP9202", "EMP9203"]),     # tie straddles the cutoff: BOTH tied rows are returned
    (4, ["EMP9201", "EMP9208", "EMP9202", "EMP9203"]),     # rank 4 does not exist (1,2,3,3,5): nothing extra
    (5, ["EMP9201", "EMP9208", "EMP9202", "EMP9203", "EMP9204"]),
    (50, ["EMP9201", "EMP9208", "EMP9202", "EMP9203", "EMP9204", "EMP9205"]),
])
def test_leaderboard_limit_applies_after_ranking(api, lb_data, limit, expected):
    assert [i["emp_code"] for i in lb(api, limit=limit).json()["items"]] == expected


def test_leaderboard_default_limit_is_10(api, lb_data):
    assert len(lb(api).json()["items"]) == 6


def test_leaderboard_department_ranks_inside_the_department(api, lb_data):
    r = lb(api, department="T_A")
    assert rows(r) == [(1, "EMP9201", 100), (2, "EMP9202", 80), (2, "EMP9203", 80), (4, "EMP9204", 50),
                       (5, "EMP9205", 30)]                                  # 1, 2, 2, 4, 5 - the rank after a tie is skipped
    assert rows(lb(api, department="T_A", limit=2)) == [(1, "EMP9201", 100), (2, "EMP9202", 80), (2, "EMP9203", 80)]
    assert rows(lb(api, department="T_A", limit=3)) == [(1, "EMP9201", 100), (2, "EMP9202", 80), (2, "EMP9203", 80)]
    assert rows(lb(api, department="T_B")) == [(1, "EMP9208", 90)]            # rank restarts at 1
    assert lb(api, department="Nope").json()["items"] == []


def test_leaderboard_tie_order_is_emp_code(api, mongo_db):
    for code in ("EMP9213", "EMP9211", "EMP9212"):
        add_emp(mongo_db, code, "T_A")
        add_log(mongo_db, code, "2031-03-03", late=25)
    assert rows(lb(api)) == [(1, "EMP9211", 25), (1, "EMP9212", 25), (1, "EMP9213", 25)]
    assert rows(lb(api, limit=1)) == [(1, "EMP9211", 25), (1, "EMP9212", 25), (1, "EMP9213", 25)]


def test_leaderboard_empty_month(api):
    assert lb(api, month="2031-09").json() == {"month": "2031-09", "items": []}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"limit": "x"}, {"limit": -1}, {"month": "2031-13"},
                                    {"month": "202103"}])
def test_leaderboard_validation_422(api, params):
    assert lb(api, **params).status_code == 422


def test_leaderboard_missing_month_422(api):
    assert api.get("/analytics/leaderboard/late").status_code == 422


# ===================================================================================== trend
DEPT = "T_Trend"


@pytest.fixture
def trend_data(mongo_db):
    db = mongo_db
    add_emp(db, "EMP9301", DEPT, "2026-01-05")
    add_emp(db, "EMP9302", DEPT, "2026-07-08")      # joins inside the range (Wednesday)
    add_emp(db, "EMP9303", DEPT, "2026-07-20")      # joins after the range
    add_emp(db, "EMP9399", "T_Other", "2026-01-05")
    add_log(db, "EMP9301", "2026-07-01")                                       # Wed
    add_log(db, "EMP9301", "2026-07-03", "WFH", wh=3.5, half=True, late=20)    # Fri, half day
    add_log(db, "EMP9301", "2026-07-04", late=15)                              # Saturday: counts as present_count
    add_log(db, "EMP9301", "2026-07-06")                                       # Mon
    add_log(db, "EMP9301", "2026-07-07", "ABSENT")
    add_log(db, "EMP9301", "2026-07-08")
    add_log(db, "EMP9302", "2026-07-08")
    add_log(db, "EMP9301", "2026-07-09")
    add_log(db, "EMP9301", "2026-07-10", "ON_DUTY", wh=None)
    add_log(db, "EMP9302", "2026-07-10", "LEAVE")
    add_log(db, "EMP9301", "2026-07-13")
    add_log(db, "EMP9302", "2026-07-13", "WFH", wh=3.0, half=True)
    add_log(db, "EMP9399", "2026-07-01")                                       # other department: never counted
    add_log(db, "EMP9399", "2026-07-04", late=77)


def trend(api, dept=DEPT, frm="2026-07-01", to="2026-07-14"):
    return api.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})


def test_trend_full_series(api, trend_data):
    r = trend(api)
    assert r.status_code == 200 and r.json()["department"] == DEPT
    items = r.json()["items"]
    assert [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(1, 15)]       # exactly one row per day
    assert [i["is_working_day"] for i in items] == [True, True, True, False, False, True, True, True, True, True,
                                                    False, False, True, True]
    assert [i["headcount"] for i in items] == [1] * 7 + [2] * 7
    assert [i["present_count"] for i in items] == [1, 0, 0.5, 1, 0, 1, 0, 2, 1, 1, 0, 0, 1.5, 0]
    assert [i["late_count"] for i in items] == [0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert [i["attendance_rate"] for i in items] == [1.0, 0.0, 0.5, None, None, 1.0, 0.0, 1.0, 0.5, 0.5, None, None,
                                                     0.75, 0.0]
    assert [i["moving_avg_7d"] for i in items] == [1.0, 0.5, 0.5, 0.5, 0.5, 0.625, 0.5, 0.5, 0.6, 0.6, 0.6, 0.6, 0.55, 0.55]


def test_trend_row_shape(api, trend_data):
    assert trend(api).json()["items"][2] == {
        "date": "2026-07-03", "is_working_day": True, "headcount": 1, "present_count": 0.5, "late_count": 1,
        "attendance_rate": 0.5, "moving_avg_7d": 0.5}


def test_trend_window_starts_at_range_not_before(api, trend_data):
    # 01 Jul has rate 1.0, but a range that starts on 03 Jul must not look at it
    items = trend(api, frm="2026-07-03", to="2026-07-06").json()["items"]
    assert [i["moving_avg_7d"] for i in items] == [0.5, 0.5, 0.5, 0.75]


def test_trend_leading_weekend_gives_null_average(api, trend_data):
    items = trend(api, frm="2026-07-04", to="2026-07-05").json()["items"]
    assert [(i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [(None, None), (None, None)]
    assert [i["present_count"] for i in items] == [1, 0]


def test_trend_single_day(api, trend_data):
    items = trend(api, frm="2026-07-06", to="2026-07-06").json()["items"]
    assert len(items) == 1 and items[0]["attendance_rate"] == items[0]["moving_avg_7d"] == 1.0


def test_trend_before_anyone_joined(api, trend_data):
    items = trend(api, frm="2025-12-01", to="2025-12-03").json()["items"]
    assert [i["headcount"] for i in items] == [0, 0, 0]
    assert all(i["attendance_rate"] is None and i["moving_avg_7d"] is None for i in items)
    assert items[0]["is_working_day"] is True                                  # Monday, but nobody to measure


def test_trend_empty_days_still_present_across_a_month_boundary(api, trend_data):
    items = trend(api, frm="2026-06-29", to="2026-07-02").json()["items"]
    assert [i["date"] for i in items] == ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"]
    assert [i["attendance_rate"] for i in items] == [0.0, 0.0, 1.0, 0.0]       # working days with no records: 0.0


def test_trend_range_limits(api, trend_data):
    assert len(trend(api, frm="2026-07-01", to="2026-09-30").json()["items"]) == 92        # exactly 92 days: allowed
    assert trend(api, frm="2026-07-01", to="2026-10-01").status_code == 422               # 93 days
    assert trend(api, frm="2026-07-05", to="2026-07-04").status_code == 422               # to < from


def test_trend_unknown_department_404(api, trend_data):
    assert trend(api, dept="NoSuchDept").status_code == 404
    assert trend(api, dept="t_trend").status_code == 404                                   # case-sensitive


@pytest.mark.parametrize("frm,to", [("2026-13-01", "2026-07-02"), ("2026-07-01", "07/02/2026"), ("x", "y")])
def test_trend_bad_dates_422(api, trend_data, frm, to):
    assert trend(api, frm=frm, to=to).status_code == 422


def test_trend_missing_params_422(api, trend_data):
    assert api.get(f"/analytics/departments/{DEPT}/trend", params={"from": "2026-07-01"}).status_code == 422
    assert api.get(f"/analytics/departments/{DEPT}/trend", params={"to": "2026-07-01"}).status_code == 422
