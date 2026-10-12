"""Unit tests for the rules engine (R1 - R5, R8). Pure functions: no MongoDB needed."""
from datetime import datetime, timezone, timedelta

import pytest

from app.main import (IST, attendance_date, calc_half_day, calc_late_minutes, calc_overtime_minutes,
                      calc_work_hours, compute_derived, dt_to_ms, ms_to_dt, round_half_up)

UTC = timezone.utc


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


# ---- R2 late (spec examples, shift 09:30) -------------------------------------------------
@pytest.mark.parametrize("h,m,s,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after start -> not late
    (9, 40, 1, 10),     # strictly more -> late, counted from shift start
    (10, 15, 59, 45),
    (9, 30, 0, 0),
    (8, 0, 0, 0),       # early
    (9, 41, 0, 11),
])
def test_late(h, m, s, expected):
    assert calc_late_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30") == expected


def test_late_ignores_sub_second():
    # 09:40:00.900 counts as 09:40:00 -> not late
    assert calc_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900000), "2026-07-06", "09:30") == 0


# ---- R3 overtime --------------------------------------------------------------------------
@pytest.mark.parametrize("h,m,s,expected", [
    (18, 35, 0, 0),     # 5 min < 30
    (18, 59, 59, 0),    # 29 min 59 s -> floor 29 < 30
    (19, 0, 0, 30),     # exactly 30
    (19, 10, 0, 40),
    (19, 10, 59, 40),   # floor
    (17, 0, 0, 0),      # before shift end
])
def test_overtime(h, m, s, expected):
    assert calc_overtime_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30", "18:30") == expected


# ---- R4 / R5 ------------------------------------------------------------------------------
def test_work_hours_half_up_not_bankers():
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
    assert calc_work_hours(t0, t0 + timedelta(seconds=450)) == 0.13       # 0.125 -> 0.13 (banker's would give 0.12)
    assert calc_work_hours(t0, t0 + timedelta(seconds=16218)) == 4.51     # 4.505 -> 4.51


def test_half_day_uses_rounded_hours():
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
    wh = calc_work_hours(t0, t0 + timedelta(seconds=16182))   # 4.495 h -> rounds to 4.50 -> NOT a half day
    assert wh == 4.5 and calc_half_day(wh) is False
    wh = calc_work_hours(t0, t0 + timedelta(seconds=16181))   # 4.4947 h -> 4.49 -> half day
    assert wh == 4.49 and calc_half_day(wh) is True
    assert calc_half_day(None) is False


# ---- R1 attendance date --------------------------------------------------------------------
def test_date_day_shift():
    assert attendance_date(ist(2026, 7, 6, 9, 28), "09:30", "18:30") == "2026-07-06"
    # IST date, not UTC date: 00:30 IST is still the previous day in UTC
    assert attendance_date(ist(2026, 7, 6, 0, 30), "09:30", "18:30") == "2026-07-06"


@pytest.mark.parametrize("args,expected", [
    ((2026, 7, 6, 21, 55), "2026-07-06"),   # before shift start, same day
    ((2026, 7, 6, 22, 0), "2026-07-06"),
    ((2026, 7, 7, 0, 5), "2026-07-06"),     # after midnight -> belongs to the shift that started yesterday
    ((2026, 7, 7, 5, 59, 59), "2026-07-06"),
    ((2026, 7, 7, 6, 0), "2026-07-07"),     # exactly shift_end is NOT earlier than shift_end
    ((2026, 7, 7, 12, 0), "2026-07-07"),
])
def test_date_overnight(args, expected):
    assert attendance_date(ist(*args), "22:00", "06:00") == expected


# ---- ms <-> datetime ----------------------------------------------------------------------
def test_ms_truncates_to_seconds_and_roundtrips():
    dt = ms_to_dt(1783312500999)
    assert dt.microsecond == 0 and dt_to_ms(dt) == 1783312500000
    naive = datetime(2026, 7, 6, 4, 35)               # what PyMongo returns by default (UTC)
    assert dt_to_ms(naive) == dt_to_ms(naive.replace(tzinfo=UTC))


def test_round_half_up():
    assert round_half_up(2.675, 2) == 2.68            # float round() gives 2.67
    assert round_half_up(0.12345, 4) == 0.1235


# ---- compute_derived against the documented sample documents -----------------------------------
def d(y, mo, dd, h, mi):
    return datetime(y, mo, dd, h, mi, tzinfo=UTC)


def test_sample_normal_day():
    r = compute_derived("PRESENT", d(2026, 7, 6, 3, 58), d(2026, 7, 6, 13, 5), "2026-07-06", "09:30", "18:30")
    assert r == {"work_hours": 9.12, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_sample_late_with_overtime():
    r = compute_derived("PRESENT", d(2026, 7, 6, 4, 35), d(2026, 7, 6, 13, 40), "2026-07-06", "09:30", "18:30")
    assert r == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}


def test_sample_open_record():
    r = compute_derived("PRESENT", d(2026, 7, 7, 4, 5), None, "2026-07-07", "09:30", "18:30")
    assert r == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_sample_half_day_wfh():
    r = compute_derived("WFH", d(2026, 7, 7, 4, 0), d(2026, 7, 7, 7, 30), "2026-07-07", "09:30", "18:30")
    assert r == {"work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True}


def test_sample_overnight():
    pin, pout = d(2026, 7, 6, 16, 25), d(2026, 7, 7, 1, 10)
    date = attendance_date(pin, "22:00", "06:00")
    assert date == "2026-07-06"
    r = compute_derived("PRESENT", pin, pout, date, "22:00", "06:00")
    assert r == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


def test_overnight_late_after_midnight():
    # 22:00-06:00 shift, punch-in 22:30 -> 30 late; punch-in 00:15 next day -> 135 late (since 22:00)
    assert compute_derived("PRESENT", ist(2026, 7, 6, 22, 30), None, "2026-07-06", "22:00", "06:00")["late_minutes"] == 30
    assert compute_derived("PRESENT", ist(2026, 7, 7, 0, 15), None, "2026-07-06", "22:00", "06:00")["late_minutes"] == 135


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
def test_non_presence_all_zero(status):
    assert compute_derived(status, None, None, "2026-07-08", "09:30", "18:30") == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
