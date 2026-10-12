# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb+srv://..."   # or mongodb://localhost:27017
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Real environment variables take priority over a local `.env` (see `.env.example`). Indexes are created at startup,
idempotently; `GET /health` returns 200 once MongoDB answers (503 otherwise). Python 3.11+, MongoDB 6.0+.

## Status
All endpoints are implemented, including `/admin/explain/{endpoint}`. Nothing is missing.

## Interpretations worth knowing
- **Punch-out earlier than the punch-in** returns 404 ("no punch-in found"), because the record is selected by
  `punch_in <= punched_at`; exactly equal returns 422.
- **PATCH "changes nothing"** (422) is judged on `status`, `punch_in`, `punch_out`, not on recomputed derived values.
  A concurrent PATCH loser is retried on the fresh record and gets 409 if its request is no longer valid.
- **Trend:** the calendar is generated in the pipeline with `$range` (a `$densify` needs at least one input document);
  the moving average uses unrounded daily rates and is rounded half-up at the end.
- `present_days` only counts Mon-Fri records; late and overtime totals include weekend records.

## Tests (optional)
`pip install -r requirements-dev.txt`, start the server, then `pytest tests`. The tests talk to the running API and only
touch `EMP9xxx` data. `tests/loadgen.py` and `tests/loadcheck.py` generate ~90k records in a database whose name contains
"load" and check that every explain plan uses an index.
