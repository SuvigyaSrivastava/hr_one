"""Generates a ~100k-record dataset for performance checks (NOT part of the application).
    MONGO_DB=hrone_load python tests/loadgen.py
Seeds like the grader: only the default _id index exists afterwards. Refuses to touch a database whose name lacks 'load'."""
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import main as m  # noqa: E402

assert "load" in m.MONGO_DB, f"refusing to wipe database {m.MONGO_DB!r}: its name must contain 'load'"
rnd = random.Random(20261012)
UTC, IST = timezone.utc, m.IST
DEPTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Operations", "Marketing", "Legal"]
START, END = date(2025, 10, 1), date(2026, 9, 30)
N_EMP = 420

m.db.drop_collection("employees")
m.db.drop_collection("attendance_logs")

emps = []
for i in range(N_EMP):
    code = f"EMP{10001 + i}"
    r = rnd.random()
    joined = START if r < 0.80 else START + timedelta(days=rnd.randint(1, 330))      # ~20% join mid-period
    shift = rnd.choices([("09:30", "18:30"), ("10:00", "19:00"), ("22:00", "06:00")], [90, 6, 4])[0]
    emps.append({"emp_code": code, "name": f"Employee {i}", "email": f"e{i}@example.com", "department": DEPTS[i % len(DEPTS)],
                 "shift_start": shift[0], "shift_end": shift[1], "joined_on": joined.isoformat(),
                 "created_at": datetime(2025, 10, 1, 4, 30, tzinfo=UTC), "_no_logs": i % 20 == 7})   # 5% never log
m.employees.insert_many([{k: v for k, v in e.items() if k != "_no_logs"} for e in emps])


def make_log(e, d):
    r = rnd.random()
    status = "PRESENT" if r < .80 else "WFH" if r < .90 else "ON_DUTY" if r < .93 else "ABSENT" if r < .96 else "LEAVE"
    doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": status}
    if status in m.NON_PRESENCE_STATUSES:
        doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        sh, sm = map(int, e["shift_start"].split(":"))
        offset = int(rnd.gauss(2, 18)) + (rnd.randint(15, 90) if rnd.random() < 0.18 else 0)         # ~18% clearly late
        start_ist = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST) + timedelta(minutes=offset, seconds=rnd.randint(0, 59))
        pin = m.trunc_seconds(start_ist)
        if m.attendance_date(pin, e["shift_start"], e["shift_end"]) != d.isoformat():            # keep the intended date
            pin = m.trunc_seconds(datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST))
        pout = None if rnd.random() < 0.03 else pin + timedelta(seconds=rnd.randint(4 * 3600, 11 * 3600))
        doc.update(punch_in=pin, punch_out=pout, **m.compute_derived(status, pin, pout, d.isoformat(), e["shift_start"], e["shift_end"]))
    doc["history"] = []
    if rnd.random() < 0.02:
        doc["history"] = [{"at": datetime(2026, 9, 1, 5, 0, tzinfo=UTC), "by": "hr.admin", "reason": "seeded correction",
                           "changes": {"late_minutes": {"from": 12, "to": 0}}}]
    if rnd.random() < 0.05:                                    # legacy shape
        doc.pop("history"), doc.pop("half_day", None)
    return doc


batch, total = [], 0
d = START
while d <= END:
    weekday = d.weekday() < 5
    for e in emps:
        if e["_no_logs"] or e["joined_on"] > d.isoformat():
            continue
        if rnd.random() < (0.95 if weekday else 0.03):
            batch.append(make_log(e, d))
    if len(batch) >= 4000:
        m.attendance.insert_many(batch, ordered=False)
        total += len(batch)
        batch = []
    d += timedelta(days=1)
for i in range(50):                                            # orphan logs (no such employee)
    batch.append(make_log({"emp_code": f"EMP{19000 + i}", "shift_start": "09:30", "shift_end": "18:30"}, date(2026, 7, 6 + i % 5)))
m.attendance.insert_many(batch, ordered=False)
total += len(batch)
print(f"database {m.MONGO_DB}: {m.employees.count_documents({})} employees, {m.attendance.count_documents({})} attendance_logs")
print("indexes:", {c: [i['name'] for i in m.db[c].list_indexes()] for c in ("employees", "attendance_logs")})
