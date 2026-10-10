# Starter Code Review

The starter was compared with `openapi.yaml`, `DATA_MODEL.md`, and the business rules R1-R10.

| # | Where | What was wrong | How to notice | How it was fixed |
|---|---|---|---|---|
| 1 | Configuration and startup | Mongo configuration and readiness/index behavior did not fully follow the contract. | Change `MONGO_URI`, stop MongoDB, or inspect startup indexes. | Environment variables are loaded through `python-dotenv`; `/health` pings MongoDB and returns 503 on failure; required indexes are created idempotently at startup and startup errors are not silently swallowed. |
| 2 | Employee creation | Validation was incomplete, including the required employee-code, email, date, shift-format, length, and unequal-shift rules. | Submit malformed codes, dates, emails, shift values, or equal shift times. | Added contract-aligned validation and preserved the database unique index for concurrent duplicate protection. |
| 3 | Punch-in | The attendance-date and derived-value rules were missing or incomplete. | Test IST boundaries, overnight shifts, grace-period boundaries, and fractional epoch milliseconds. | Added epoch-millisecond validation/truncation, IST/overnight date resolution, and stored `late_minutes` calculation. |
| 4 | Punch-out | Selection and update behavior was not safe for simultaneous requests. | Send two punch-outs at the same time or punch out across midnight. | Selects the most recent eligible open record, validates the 24-hour limit, computes R3-R5, and atomically updates only an open record so one concurrent loser receives 409. |
| 5 | Attendance listing | Filtering and pagination needed to match the exact ordering and total semantics. | Combine date, employee, status, page, and page-size filters. | Added inclusive filters, contract ordering, bounded pagination, and filtered totals. |
| 6 | Regularization | Corrections needed stricter transition validation and history preservation. | Try status/time conflicts, no-op updates, invalid dates, or simultaneous corrections. | Recomputes all derived fields, appends only actual changes, and uses an optimistic history predicate to return 409 instead of losing another correction. |
| 7 | Analytics | Monthly, department, leaderboard, and trend calculations needed R5-R9 handling. | Use employees with no logs, mid-month joins, weekend logs, ties, and empty trend days. | Analytics read stored derived values, count headcount from employees, apply half-days and weekdays correctly, use standard competition ranks, and gap-fill trend rows. |
| 8 | Explain endpoint | The original MongoDB command shape caused an internal error, and `from`/`to` aliases were not wired. | Call `/admin/explain/attendance_list` or `/admin/explain/department_trend`. | Uses the PyMongo database command form with `executionStats` and accepts the documented query parameter names. |

Items deliberately retained: no root `/` route was added because it is not in the contract; `/health`, `/docs`, and `/openapi.json` are the supported entry points. No authentication was added because the assignment explicitly provides no authentication mechanism.
