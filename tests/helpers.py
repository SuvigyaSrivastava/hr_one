from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ms(y, mo, d, h, mi, s=0, us=0):
    """Epoch milliseconds of an IST wall-clock time."""
    return int(round(datetime(y, mo, d, h, mi, s, us, tzinfo=IST).timestamp() * 1000))


def emp(code="EMP9001", **kw):
    body = {"emp_code": code, "name": "Test User", "email": "t@example.com", "department": "QA",
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"}
    body.update(kw)
    return body
