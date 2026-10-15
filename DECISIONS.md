# DECISIONS.md

1. **Indexes.** I created a unique index on `emp_code` to prevent duplicate employees and a unique compound index on `(emp_code, date)` to enforce one attendance record per employee per day. The `(date, emp_code)` index supports attendance date-range queries and sorting. The `(status, date)` index supports status-based attendance queries. The department/code and joined-date/department indexes help employee filtering and department analytics. I would review index usage with `explain("executionStats")` before adding more indexes, because unnecessary indexes increase write costs.

2. **Punch-in race.** Two requests can arrive for the same employee and date. Both may initially find no record, but the unique compound index allows only one insert. The duplicate-key error is caught and converted into a conflict response, so the second request does not create a duplicate record.

3. **Ties.** The leaderboard should use competition ranking, so tied employees share a rank and the next rank skips accordingly. The limit should be applied after determining the cutoff tie group if the assignment requires all tied employees to be returned.

4. **Headcount.** Department headcount must come from eligible employees, not only attendance records. This ensures employees with no logs are still counted.

5. **Scaling.** For 100 times more data, I would measure query plans and latency, add only justified indexes, and consider pre-aggregated analytics for expensive repeated reports.