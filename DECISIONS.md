# DECISIONS.md

1. **Indexes.** Unique `emp_code` and unique `(emp_code, date)` enforce identity and serve lookups and joins. `(date desc, emp_code)` matches the list sort, `(status, date, emp_code)` serves status filters, `(date, late_minutes, emp_code)` covers the leaderboard, and `(department, emp_code)` and `(joined_on, department)` serve employee lists and headcount. Rejected: a lone `status` index (unselective).

2. **Punch-in race.** Both requests try to insert the same `(emp_code, date)`. The unique index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which I return as 409. There is no read-then-write step; the database decides.

3. **Ties.** Both tied rows are returned, even if that exceeds `limit`. `$rank` gives tied employees one rank, and I filter `rank <= limit` after ranking instead of cutting a sorted list. Ties are ordered by `emp_code`.

4. **Headcount.** The summary starts from `employees`, not from the logs, and looks up each person's logs afterwards. Someone with none still yields a zero row, so they count; later joiners are filtered out first.

5. **At 100x the data.** I would replace offset pagination with keyset pagination and keep daily per-employee rollups, updated at punch time, so analytics read small summaries instead of raw logs.
