# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` / line 76-78 | Health check returns `{"status": "ok"}` unconditionally without checking MongoDB connectivity. | MongoDB is down or network disconnects, yet `/health` returns 200 instead of 503. | Added `client.admin.command("ping")` inside a `try/except` block, raising `HTTPException(503)` on failure. |
| 2 | Startup / lines 26-28 | Client has no `tz_aware=True`, and database indexes are never created at startup. | Inspecting `db.attendance_logs.getIndexes()` shows only `_id_`. All queries do slow COLLSCANs. | Added FastAPI `lifespan` context manager to ping MongoDB and create all required indexes idempotently with `background=False`. Added `tz_aware=True` to `MongoClient`. |
| 3 | `compute_late_minutes()` / lines 37-41 | Uses `punch_in.replace(hour=h, minute=m)` on UTC datetimes and `minutes > 10` check. Fails on timezone offset, overnight shifts, and exact grace period boundary (`09:40:01`). | Shift 09:30, punch-in at 09:40:01 IST yields 0 minutes late instead of 10. Also off by 5h30m when replacing UTC hours. | Replaced with `calculate_late_minutes()` constructing `shift_start` in IST on the record's attendance date. Applied strict `diff_seconds > 600` check and `math.floor(diff_seconds / 60)`. |
| 4 | `compute_overtime()` / lines 48-52 | Overtime returns `max(0, minutes)` without the 30-minute threshold requirement; ignores overnight shifts where shift_end falls on next day. | Punch-out 25 minutes after shift end returns 25 instead of 0. Overnight punch-out computes negative or invalid duration. | Replaced with `calculate_overtime_minutes()` enforcing `minutes >= 30` (otherwise 0) and advancing `shift_end` by 1 calendar day when `shift_end <= shift_start`. |
| 5 | `compute_work_hours()` / lines 44-46 | Uses Python standard `round(..., 2)` which performs banker's rounding (round-half-to-even) instead of round-half-up. | Raw duration of 2.525 hours rounds to 2.52 instead of 2.53. | Implemented `round_half_up()` using `Decimal(...).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)`. |
| 6 | `punch_in()` / line 107 | Attendance date `d` is computed from local/naive timestamp without IST conversion or overnight shift logic (R1). | Overnight shift (22:00-06:00), punch-in at 01:30 IST on 2024-01-15 gets assigned date `"2024-01-15"` instead of `"2024-01-14"`. | Implemented `get_attendance_date_ist()` converting punch-in to IST, detecting overnight shifts, and attributing punch-ins before `shift_end` to previous day. |
| 7 | `PunchInIn` & `punch_in()` / lines 69, 106 | `punched_at` timestamp is not validated against seconds/floats/strings, and not truncated to whole seconds. | Sending `punched_at: 1718000000` (seconds) or timestamps with millisecond fractions fails validation or corrupts boundary checks. | Implemented `parse_timestamp_ms()` rejecting non-integers and values < 1e11 (422), and truncated instants to whole seconds via `(ms // 1000) * 1000`. |
| 8 | `create_employee()` & `punch_in()` / lines 83, 108 | Check-then-insert pattern (`find_one` then `insert_one`) is vulnerable to race conditions under concurrent requests. | Two simultaneous requests with identical `emp_code` or `(emp_code, date)` can both pass check and cause duplicate insert or 500 unhandled error. | Enforced database unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` and wrapped `insert_one()` in `try ... except DuplicateKeyError: raise HTTPException(409)`. |
| 9 | `list_employees()` / line 97 | Offset pagination bug: `skip = page * page_size`. For `page = 1`, `skip = 20`, skipping the first 20 employees. | `GET /employees?page=1&page_size=20` returns employees 21 to 40; the first 20 records are never returned. | Changed to `skip = (page - 1) * page_size`. |
| 10 | `list_employees()` / line 98 | `total` counts the whole collection `db.employees.count_documents({})` regardless of applied filters. | `GET /employees?department=Engineering` returns total count of all company employees instead of Engineering headcount. | Updated to `total = db.employees.count_documents(q)`. |
| 11 | `list_employees()` / line 99 | Query results are unsorted, returning documents in arbitrary natural insertion order. | Inserting employees out of order causes `GET /employees` to return an unsorted list. | Added `.sort("emp_code", ASCENDING)`. |
| 12 | `punch_in()` & `list_attendance()` / lines 123, 153 | Exposes internal MongoDB `_id` and synthetic `id` in responses (`doc["id"] = str(res.inserted_id)`). | Responses include `"id": "..."`, violating the API specification that `_id` must never appear. | Implemented `serialize()` which strips `_id` and `id`, returning records identified purely by `emp_code` and `(emp_code, date)`. |
| 13 | Datetime serialization / lines 86, 125, 154 | Server returns native datetime objects or ISO strings in responses instead of integer epoch milliseconds. | Client receives ISO string or Python datetime representation instead of integer millisecond timestamps. | Converted all instants (`created_at`, `punch_in`, `punch_out`, `at`, and `changes`) to integer epoch milliseconds via `dt_to_ms()`. |
| 14 | `punch_in()` / lines 105, 117 | Missing check for nonexistent employee: if `emp_code` is not found, `emp["shift_start"]` raises `TypeError` (HTTP 500). | `POST /attendance/punch-in` with an unknown `emp_code` crashes with 500 Internal Server Error instead of 404. | Added explicit check `if not emp: raise HTTPException(404, detail="Employee not found")`. |
| 15 | `list_attendance()` / lines 148-151 | Loads all attendance documents into Python memory with `list(db.attendance_logs.find(q))` before sorting. | High memory consumption and severe latency when collection scales to 100,000+ records. | Moved sorting and pagination directly into MongoDB: `.sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(...).limit(...)`. |
| 16 | Missing Endpoints / lines 158-166 | Starter omitted 7 required endpoints: `punch-out`, `regularize`, `monthly`, `department_summary`, `leaderboard`, `trend`, and `explain`. | Requesting any analytics or correction endpoint yields HTTP 404 Not Found. | Implemented all 7 endpoints with exact OpenAPI schemas, business rules, and MongoDB aggregation pipelines. |

---

## Detailed Defect Reports

### Defect 1: Unchecked Health Probe
- **Location:** `health()`, lines 76-78
- **What is wrong:** The health endpoint unconditionally returns `{"status": "ok"}` without testing connectivity to MongoDB.
- **How to trigger:** Stop or disconnect the MongoDB database and call `GET /health`. The endpoint returns 200 OK instead of 503 Service Unavailable.
- **Fix:** Executed `client.admin.command("ping")` within a `try/except` block, returning 200 on success and raising `HTTPException(503, detail="MongoDB unavailable")` on failure.

### Defect 2: Missing Index Creation and Naive Client Configuration
- **Location:** Startup setup, lines 26-28
- **What is wrong:** The starter does not create any indexes at startup, nor does it pass `tz_aware=True` to PyMongo.
- **How to trigger:** Inspect collection indexes using `db.attendance_logs.getIndexes()`; only the default `_id_` index exists. All queries perform COLLSCANs.
- **Fix:** Implemented a FastAPI `lifespan` handler that pings MongoDB and creates all required indexes idempotently with `background=False`. Configured `MongoClient(..., tz_aware=True)`.

### Defect 3: Timezone and Grace Boundary Flaws in Late Minutes
- **Location:** `compute_late_minutes()`, lines 37-41
- **What is wrong:** Replaces hour/minute directly on a UTC datetime with IST times (off by 5h 30m). Furthermore, `int(diff / 60) > 10` fails the strict grace period boundary: at 09:40:01 (601 seconds), `int(601 / 60)` is 10 which is not `> 10`, returning 0 instead of 10.
- **How to trigger:** Shift 09:30, punch-in at 09:40:01 IST returns 0 minutes late instead of 10 minutes.
- **Fix:** Created `calculate_late_minutes()` constructing `shift_start` in IST on the attendance date, checking `diff_seconds > 600`, and taking `math.floor(diff_seconds / 60)`.

### Defect 4: Missing 30-Minute Threshold and Overnight Support in Overtime
- **Location:** `compute_overtime()`, lines 48-52
- **What is wrong:** Overtime is returned as `max(0, minutes)` without enforcing the rule that overtime is counted only if >= 30 minutes. In addition, overnight shifts where `shift_end` is on the next calendar day are not adjusted.
- **How to trigger:** Punch-out 25 minutes after shift end returns 25 instead of 0. Overnight shift punch-outs produce invalid negative numbers.
- **Fix:** Implemented `calculate_overtime_minutes()` checking if `shift_end <= shift_start` to add 1 day to `shift_end_ist`, and returning `minutes` only if `>= 30`, otherwise 0.

### Defect 5: Banker's Rounding in Work Hours Calculation
- **Location:** `compute_work_hours()`, lines 44-46
- **What is wrong:** Uses Python's built-in `round()`, which performs round-half-to-even rather than exact round-half-up (R4/R8).
- **How to trigger:** Pass punch intervals resulting in 2.525 hours; `round()` yields 2.52 instead of 2.53.
- **Fix:** Implemented `round_half_up()` using `Decimal.quantize(..., rounding=ROUND_HALF_UP)`.

### Defect 6: Missing IST and Overnight Shift Attendance Date Calculation
- **Location:** `punch_in()`, line 107
- **What is wrong:** Calculates attendance date `d` directly from local/naive `ts.date().isoformat()`, ignoring IST timezone and overnight shift rules.
- **How to trigger:** In shift 22:00-06:00, punch in at 01:30 IST on 2024-01-15. The starter assigns `"2024-01-15"` instead of previous day `"2024-01-14"`.
- **Fix:** Implemented `get_attendance_date_ist()` converting punch-in to IST and subtracting 1 calendar day if the shift is overnight and punch-in is earlier than `shift_end`.

### Defect 7: Lack of Timestamp Validation and Sub-Second Truncation
- **Location:** `PunchInIn`, line 69, and `punch_in()`, line 106
- **What is wrong:** `punched_at` accepts invalid types (floats, strings, timestamps in seconds < 1e11) without returning 422, and sub-second milliseconds are not truncated to whole seconds.
- **How to trigger:** Pass `punched_at = 1718000000` (seconds) or `punched_at = 1783312500900` (ending in 900ms).
- **Fix:** Added Pydantic field validator `parse_timestamp_ms()` rejecting non-integers and numbers < 1e11 (returning 422), and truncated milliseconds to whole seconds via `(punched_at // 1000) * 1000`.

### Defect 8: Race Condition on Duplicate Inserts
- **Location:** `create_employee()`, line 83, and `punch_in()`, line 108
- **What is wrong:** Uses non-atomic `find_one()` followed by `insert_one()`. Concurrent requests can both find no existing document and both attempt insertion.
- **How to trigger:** Send two concurrent POST requests with the same `emp_code` or `(emp_code, date)`.
- **Fix:** Added unique database indexes on `emp_code` and `(emp_code, date)` and caught `DuplicateKeyError`, converting it to HTTP 409.

### Defect 9: Employee Listing Pagination Offset Calculation
- **Location:** `list_employees()`, line 97
- **What is wrong:** Calculates `skip = page * page_size`. For `page = 1`, `skip = 20`, skipping the first 20 employees.
- **How to trigger:** Request `GET /employees?page=1&page_size=20`. Records 1 to 20 are skipped.
- **Fix:** Changed skip calculation to `(page - 1) * page_size`.

### Defect 10: Unfiltered Total Count in Employee Listing
- **Location:** `list_employees()`, line 98
- **What is wrong:** `total` counts the entire collection without applying the query filter.
- **How to trigger:** Request `GET /employees?department=Engineering`. `total` returns total headcount of all departments.
- **Fix:** Set `total = employees.count_documents(q)`.

### Defect 11: Unsorted Employee Listing
- **Location:** `list_employees()`, line 99
- **What is wrong:** `employees.find(q)` does not specify sorting, returning documents in arbitrary natural order.
- **How to trigger:** Insert employees out of alphabetical order; `GET /employees` returns them unsorted.
- **Fix:** Added `.sort("emp_code", ASCENDING)`.

### Defect 12: Leak of `_id` and Injection of Synthetic `id`
- **Location:** `punch_in()`, line 123, and `list_attendance()`, line 153
- **What is wrong:** Injects `doc["id"] = str(res.inserted_id)` into response documents, violating contract conventions.
- **How to trigger:** Inspect responses of `POST /attendance/punch-in` and `GET /attendance`; they contain `"id": "..."`.
- **Fix:** Removed all references to `"id"` and stripped `_id` via `serialize()`.

### Defect 13: Datetime Objects Serialized Instead of Epoch Milliseconds
- **Location:** `app/main.py`, lines 86, 125, 154
- **What is wrong:** BSON datetimes are returned directly or formatted as strings instead of integer epoch milliseconds.
- **How to trigger:** Calling `GET /employees` or `POST /attendance/punch-in` returns datetime strings or objects.
- **Fix:** Recursively converted all datetime objects to integer epoch milliseconds using `dt_to_ms()`.

### Defect 14: Unhandled TypeError for Nonexistent Employee
- **Location:** `punch_in()`, lines 105, 117
- **What is wrong:** If employee does not exist, `emp` is `None`, and `emp["shift_start"]` raises an unhandled `TypeError` (HTTP 500).
- **How to trigger:** Call `POST /attendance/punch-in` with nonexistent `emp_code`. Server returns 500.
- **Fix:** Added check `if not emp: raise HTTPException(404, detail="Employee not found")`.

### Defect 15: In-Memory Loading and Sorting of Attendance Records
- **Location:** `list_attendance()`, lines 148-151
- **What is wrong:** Loads all records into Python memory with `list(db.attendance_logs.find(q))` before sorting and slicing.
- **How to trigger:** Query a large collection (~100k records); causes high RAM consumption and slow response times.
- **Fix:** Pushed sorting and pagination into MongoDB using `.sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(...).limit(...)`.

### Defect 16: Seven Entire Endpoints Missing
- **Location:** `app/main.py`, lines 158-166
- **What is wrong:** Starter file omitted implementations for `punch-out`, `PATCH /attendance/{emp_code}/{date}`, and all analytics/admin endpoints.
- **How to trigger:** Making requests to any of these endpoints returns HTTP 404 Not Found.
- **Fix:** Implemented all 7 missing endpoints using atomic MongoDB operations, business rules R1-R9, and aggregation pipelines.

---

## What Was Considered and Decided NOT a Defect

1. **Storing Dates and Shift Times as Plain Strings:**
   `date` (`"YYYY-MM-DD"`) and shift times (`"HH:MM"`) are intentionally stored as strings rather than BSON dates or timestamps. A calendar date is not an instant in time; storing it as a BSON datetime would artificially attach a timezone to a date. Lexicographical comparison (`$gte` / `$lte`) on ISO-formatted `"YYYY-MM-DD"` strings is exact, index-friendly, and specified by the data model.

2. **Omitting `_id` from OpenAPI Schemas:**
   `_id` is MongoDB's internal primary key and is not part of the external REST contract. Addressing attendance records by their natural business composite key `(emp_code, date)` and employees by `emp_code` is the designed architecture, not a defect.
