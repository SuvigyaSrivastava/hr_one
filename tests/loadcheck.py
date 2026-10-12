"""Plan and timing check against a loaded server (default http://localhost:8000):
    MONGO_DB=hrone_load uvicorn app.main:app --port 8000   (data from tests/loadgen.py)
    python tests/loadcheck.py
Fails (exit 1) if any explain output contains COLLSCAN or lacks IXSCAN."""
import json
import re
import statistics
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=60)

CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP10050"}),
    ("attendance_list", {"emp_code": "EMP10050", "date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"date_from": "2026-03-01"}),
    ("attendance_list", {"date_to": "2026-03-31"}),
    ("attendance_list", {"status": "WFH"}),
    ("attendance_list", {"status": "LEAVE", "date_from": "2026-03-01", "date_to": "2026-06-30"}),
    ("attendance_list", {"emp_code": "EMP10050", "status": "PRESENT"}),
    ("attendance_list", {"page": 400, "page_size": 100}),
    ("employee_monthly", {"emp_code": "EMP10050", "month": "2026-03"}),
    ("employee_monthly", {"emp_code": "EMP10400", "month": "2025-12"}),
    ("department_summary", {"month": "2026-03"}),
    ("department_summary", {"month": "2026-09", "department": "Sales"}),
    ("department_summary", {"month": "2025-10"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "limit": 50, "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 3}),
    ("department_trend", {"department": "Sales", "from": "2026-01-01", "to": "2026-03-31"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
    ("department_trend", {"department": "Legal", "from": "2026-09-30", "to": "2026-09-30"}),
]
REAL = {   # explain endpoint -> real endpoint (path, translate params)
    "attendance_list": lambda p: ("/attendance", p),
    "employee_monthly": lambda p: (f"/analytics/employees/{p['emp_code']}/monthly", {"month": p["month"]}),
    "department_summary": lambda p: ("/analytics/departments/summary", p),
    "late_leaderboard": lambda p: ("/analytics/leaderboard/late", p),
    "department_trend": lambda p: (f"/analytics/departments/{p['department']}/trend", {"from": p["from"], "to": p["to"]}),
}


def stats_of(explain: dict) -> dict:
    txt = json.dumps(explain)
    es = explain.get("executionStats") or {}
    stage_stats = explain.get("stages", [{}])[0].get("$cursor", {}).get("executionStats", es) if "stages" in explain else es
    return {
        "collscan": "COLLSCAN" in txt, "ixscan": "IXSCAN" in txt,
        "indexes": sorted(set(re.findall(r'"indexName": "([a-z_]+)"', json.dumps(explain.get("queryPlanner", explain))))),
        "docs": stage_stats.get("totalDocsExamined"), "keys": stage_stats.get("totalKeysExamined"),
        "ms": es.get("executionTimeMillis", stage_stats.get("executionTimeMillis")),
    }


bad = 0
print(f"{'endpoint':20s} {'params':72s} {'IXSCAN':6s} {'COLLSCAN':8s} {'docs':>6s} {'keys':>6s} {'ms':>5s}  winning/candidate indexes")
for ep, params in CASES:
    r = c.get(f"/admin/explain/{ep}", params=params)
    if r.status_code != 200:
        print("HTTP", r.status_code, ep, params, r.text[:200]); bad += 1; continue
    s = stats_of(r.json()["explain"])
    ok = s["ixscan"] and not s["collscan"]
    bad += not ok
    print(f"{ep:20s} {json.dumps(params)[:72]:72s} {str(s['ixscan']):6s} {str(s['collscan']):8s} {s['docs']!s:>6s} {s['keys']!s:>6s} {s['ms']!s:>5s}  {','.join(s['indexes'])} {'' if ok else '  <-- FAIL'}")

print("\nend-to-end latency (7 runs each, ms, includes network to Atlas):")
for ep, params in CASES:
    path, q = REAL[ep](params)
    ts = []
    for _ in range(7):
        t = time.perf_counter(); r = c.get(path, params=q); ts.append((time.perf_counter() - t) * 1000)
        assert r.status_code == 200, (path, q, r.status_code, r.text[:200])
    print(f"  {ep:20s} {json.dumps(params)[:60]:60s} median {statistics.median(ts):6.0f}  max {max(ts):6.0f}")
print("\nRESULT:", "ALL PLANS USE INDEXES, NO COLLSCAN" if not bad else f"{bad} PROBLEM(S)")
sys.exit(1 if bad else 0)
