# Live walkthrough cheat-sheet

Everything is in `app/main.py` (about 930 lines). Companion files: `REVIEW.md` (21 defects), `DECISIONS.md` (5 answers), `README.md`, `tests/` (225 tests).

## 60-second pitch
FastAPI + sync PyMongo. The database is the arbiter of correctness: unique indexes decide races, conditional updates decide punch-out, compare-and-set decides PATCH, and aggregation pipelines do every analytic. Business rules (R1-R5, R8) are pure functions, unit-tested against every example in the spec. Verified on a generated 91k-record database: every explain plan is IXSCAN with no COLLSCAN, and the app is ready in under 4 s with all indexes built.

## Map of app/main.py
1. Config: `client` (tz_aware, 5 s selection timeout), `employees`, `attendance`.
2. Rules engine: `attendance_date`, `calc_late_minutes`, `calc_overtime_minutes`, `calc_work_hours`, `calc_half_day`, `compute_derived`, `round_half_up`, `ms_to_dt` / `dt_to_ms`.
3. Types and models: `EpochMs` (StrictInt, range), `DateStr`, `MonthStr`, `EmployeeCreate`, `PunchInIn`, `PunchOutIn`, `RegularizeIn`.
4. Serializers: `serialize_attendance` (no _id, ms, legacy defaults), `serialize_employee`.
5. Indexes: `INDEXES` dict, `ensure_indexes()` called in the lifespan hook (and retried from /health).
6. Endpoints: employees, punch-in, `build_attendance_find`, punch-out, `plan_regularization` + PATCH, analytics builders (`build_employee_monthly`, `build_department_summary`, `build_late_leaderboard`, `build_department_trend`), `/admin/explain`.

## Likely questions, short answers
- **Punch-in race?** Unique (emp_code, date) index; insert_one; DuplicateKeyError -> 409. No read-then-write.
- **Punch-out race?** `find_one_and_update` with filter `punch_out: null` + same `_id` and `punch_in`; no match -> 409.
- **Concurrent PATCH?** The plan is a pure function. The update filter pins status, punch times and history length. A loser re-reads and re-plans (max 4 attempts), else 409. History is `$push` only. Measured: 20 parallel -> 9 x 200, 11 x 409, 9 history entries, a consistent chain.
- **Ties?** `$rank` (competition ranking), filter `rank <= limit` after ranking, sort by total desc then emp_code. A tie at the cutoff returns all tied rows.
- **Why not $densify in the trend?** It needs at least one input doc. The calendar is generated with `$range` + `$unwind`, so every day exists even with no logs.
- **Headcount with no logs?** The pipeline starts from employees; the log lookup is inside `$lookup`, so no-log employees give a zero row.
- **Average over records?** Sum work-hour hundredths and counts per employee, add them up per department, divide once. Rounding is Decimal half-up in Python (MongoDB `$round` is half-even).
- **Time zones?** Mongo returns aware UTC (`tz_aware=True`); all rule math is in IST; API instants are epoch ms truncated to seconds.
- **At 100x?** Keyset pagination (offset is O(skip): page 400 reads 40k keys), per-employee daily rollups written at punch time, possibly sharding by emp_code.

## Likely live changes and where
- Grace period / overtime minimum / half-day threshold: `GRACE_SECONDS`, `OVERTIME_MIN_MINUTES`, `HALF_DAY_THRESHOLD`; update `tests/test_rules.py`.
- New filter on GET /attendance: `build_attendance_query` + an index in `INDEXES`.
- Dense ranking instead of competition ranking: swap `$rank` for `$denseRank` in `build_late_leaderboard`.
- Different moving window: `"documents": [-6, 0]` in `build_department_trend`.
- Weekend counted as a working day: `_IS_WEEKDAY` and `count_working_days`.
- New status: `Status` / `PRESENCE_STATUSES` and the status checks in `plan_regularization`.

## Judgment calls to be upfront about
- Punch-out strictly earlier than the punch-in returns 404 (record selection rule); equal returns 422.
- PATCH "changes nothing" is judged on status / punch_in / punch_out, not on derived values.
- The moving average uses unrounded daily rates, rounded once at the end.
- The 92-day trend limit counts days inclusively.

## Commands
`uvicorn app.main:app --port 8000` | `pytest tests` (server running) | `python tests/loadcheck.py` (needs the load DB: `MONGO_DB=hrone_load python tests/loadgen.py`).
