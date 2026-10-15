# Design Decisions

**Indexes.** I use a unique index on `employees.emp_code` so two requests cannot create the same employee, and a unique compound index on `attendance_logs.emp_code` plus `date` so duplicate daily punch-ins are blocked by MongoDB itself. I also index department lookups and attendance date/status filters and sorts. I would avoid adding an index for every possible filter combination without checking query plans, because extra indexes slow writes and use storage.

**Punch-in race.** Both requests may initially check the employee and try to insert. The unique `(emp_code, date)` index is the final guard: MongoDB accepts one insert, and the other raises a duplicate-key error that the API converts to 409. A check before insert alone would not be safe.

**Ties.** The leaderboard uses competition ranking: equal totals share a rank, and the next rank is skipped (for example, 1, 2, 2, 4). The limit is applied to rank, so every employee tied at the cutoff is included.

**Headcount.** The department summary starts from employees who joined by the end of the month, then looks up their attendance. This keeps employees with no logs in the headcount.

**At 100x scale.** I would inspect real query plans and latency first, then consider pre-aggregated daily/monthly summaries and background refreshes for expensive analytics. I would keep the source attendance records as the audit trail.
