# DECISIONS.md

1. **Indexes.**
On `employees`: unique `{emp_code: 1}` enforces unique employee identity and powers fast employee lookups; compound `{department: 1, joined_on: 1}` accelerates department listings and headcount filtering (`joined_on <= period_end`).
On `attendance_logs`: unique compound `{emp_code: 1, date: 1}` enforces the natural key constraint and enables race-safe punches; compound `{date: -1, emp_code: 1}` powers the primary attendance listing and cursor sorting; compound `{date: 1, late_minutes: -1}` accelerates monthly analytics and late leaderboard aggregation stages.
Rejected: A single-field index on `date` alone, because all date-scoped operations filter or sort by `emp_code` or `late_minutes`, which the compound indexes satisfy with higher selectivity and zero collection scans.

2. **Punch-in race.**
Both incoming requests validate their payload and compute the identical attendance date. Both attempt `insert_one` into `attendance_logs`. MongoDB's unique compound index `{emp_code: 1, date: 1}` ensures that exactly one write succeeds at the database engine level and returns HTTP 201. The concurrent request fails with MongoDB error 11000 (`DuplicateKeyError`), which our handler catches and converts into HTTP 409 Conflict. This eliminates check-then-insert race windows.

3. **Ties.**
Our leaderboard pipeline uses `$setWindowFields` with the `$rank` operator to implement standard competition ranking (1, 2, 2, 4), where tied employees receive the same rank and subsequent ranks are skipped. Filtering `rank <= limit` occurs strictly after rank computation. If multiple employees tie at the cutoff rank (for example, rank 10 when `limit=10`), all tied employees are returned (yielding more than 10 rows), ordered deterministically by `emp_code` ascending.

4. **Headcount.**
The department summary aggregation pipeline starts from the `employees` collection (filtering `joined_on <= end_of_month`) rather than `attendance_logs`. It joins monthly attendance logs using a `$lookup`. Employees with zero attendance logs are preserved with an empty log array. Grouping by department tallies headcount with `{ $sum: 1 }` across all employees who have joined, accurately counting zero-log staff while omitting employees joining after the month.

5. **One thing you would change**
At 100x scale (~10M attendance documents), executing on-demand aggregation pipelines across collections on read requests would bottleneck the database. I would introduce an asynchronous event-driven worker using MongoDB Change Streams to incrementally update pre-aggregated rollup collections (`daily_department_stats`, `monthly_employee_stats`) whenever punches or regularizations occur, allowing analytics endpoints to serve responses via instant O(1) indexed reads.
