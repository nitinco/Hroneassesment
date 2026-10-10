# Implementation Decisions

1. **Indexes.** I created a unique `employees.emp_code` index for the immutable business identifier and a unique compound `attendance_logs.(emp_code, date)` index for the natural attendance key and punch-in race. Date ordering, employee/date lookups, employee punch-time selection, department membership, join-date headcount, and status filtering have supporting indexes. I considered a large compound index covering every analytics filter, but rejected it because it would increase write cost and the focused indexes already produce indexed plans for the required queries.

2. **Punch-in race.** Each request calculates the attendance date and attempts an insert. The unique `(emp_code, date)` index is the authority; there is no check-then-insert guarantee. One insert succeeds with 201, while a concurrent duplicate raises `DuplicateKeyError` and is translated to 409.

3. **Ties.** Rows are sorted by total late minutes descending and employee code ascending. Rank is standard competition ranking. `limit` is applied to rank, not row count, so all employees tied at the cutoff are returned.

4. **Headcount.** Department summaries start from employees joined on or before the month end, then attach attendance records. Employees with zero logs therefore remain in the department headcount.

5. **At 100x scale.** I would move analytics to carefully indexed aggregation pipelines (and materialized daily summaries where appropriate) so large result sets are not assembled in Python.
