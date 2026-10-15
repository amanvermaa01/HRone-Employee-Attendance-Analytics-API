# DECISIONS.md

1. **Indexes.**
On `employees`: `emp_code` (unique, lookups), `department`, and `joined_on` (headcount). On `attendance_logs`: `(emp_code, date)` (unique, single daily punch), `(date, emp_code)` (attendance listing sort/ranges), `(date, late_minutes)` (leaderboard), and `status`. Rejected single-field `work_hours` index due to low query selectivity.

2. **Punch-in race.**
Both threads pass employee lookup and attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` permits exactly one insert to succeed (returning 201 Created). The other raises a `DuplicateKeyError`, which our exception handler catches and returns 409 Conflict.

3. **Ties.**
Using MongoDB's `$rank`, tied employees share identical competition ranks. Because filtering applies `rank <= limit` after ranking, all tied employees at the cutoff return. For example, with limit=2 and minutes [A:120, B:120, C:90], both A and B share rank 1 and return.

4. **Headcount.**
The pipeline starts from `employees` matching `joined_on <= month_end`, then `$lookup`s `attendance_logs` by `emp_code` and month. Unwinding with `preserveNullAndEmptyArrays: true` and grouping with `$addToSet: "$emp_code"` ensures employees with zero attendance records are fully counted in department headcount.

5. **One thing you would change for 100x data.**
At 100x scale (10M records), I would shard `attendance_logs` using hashed `emp_code` with `date`, pre-aggregate monthly metrics into a rollup collection via change streams, and route read-heavy analytical aggregations to secondary read replicas.
