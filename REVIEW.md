# REVIEW.md

| # | Where (function / endpoint) | What is wrong | How to notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `/health` | Health check does not verify database connectivity. | Stop or misconfigure MongoDB and call `/health`. | Ping MongoDB and return an unavailable response when the database cannot be reached. |
| 2 | Employee creation | Checking for duplicates before insertion is vulnerable to concurrent requests. | Submit the same employee code concurrently. | Add a unique employee-code index and handle `DuplicateKeyError`. |
| 3 | Timestamp helpers | Using naive local time can produce inconsistent attendance dates. | Compare timestamps near midnight with IST dates. | Use timezone-aware timestamps and explicit IST conversion. |
| 4 | Employee listing | Pagination offset and total count may be incorrect, especially with filters. | Request page 2 and compare filtered totals. | Apply the correct skip/limit and count using the same filters. |
| 5 | Punch-in | Local time and a check-then-insert sequence can produce incorrect dates or duplicate records. | Submit simultaneous punch-ins or timestamps near midnight. | Validate timestamps, use IST dates, enforce a unique `(emp_code, date)` index, and handle duplicate-key conflicts. |
| 6 | Attendance listing | Loading all records and sorting in Python is inefficient; database IDs should not leak into API responses. | Query a large date range and inspect response fields. | Filter, sort, and paginate in MongoDB; return only public response fields. |
| 7 | Punch-out | Concurrent requests must not overwrite one another. | Submit two punch-out requests simultaneously. | Use a conditional update that only succeeds when `punch_out` is still null. |
| 8 | Regularization | Concurrent edits must preserve the original values and history. | Submit overlapping regularization updates. | Use a conditional update and append a history entry atomically; retry or report a conflict when the record changed. |
| 9 | Analytics endpoints | Missing or incomplete implementations cannot satisfy the required reports. | Exercise monthly analytics, department summaries, late leaderboard, and trend endpoints. | Implement the required filters, aggregation calculations, ranking, and date handling in MongoDB. |
| 10 | Explain endpoint | Query plans must be inspectable to assess index usage. | Call each supported explain endpoint and inspect execution statistics. | Run explain with execution statistics for the relevant database query. |

## Items reviewed that are not necessarily defects

- An empty attendance page is valid when the requested page is beyond the available records.
- A `null` attendance rate on a non-working day is reasonable when the metric is not applicable.
- A `409 Conflict` for a duplicate punch-in is expected behavior, not an error in itself.
