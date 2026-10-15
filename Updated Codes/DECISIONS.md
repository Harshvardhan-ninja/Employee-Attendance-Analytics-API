# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
I use a unique index on employees.emp_code so an employee code cannot be duplicated. I also use a unique compound index on attendance_logs.emp_code and attendance_logs.date so an employee can have only one attendance record per day. I considered adding indexes to every field used in filters, but rejected that initially because unnecessary indexes increase storage and slow down writes. I would add more based on measured query performance.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
If two identical punch-in requests arrive simultaneously, both may initially find no attendance record. The unique compound index allows only one record for that employee and date. The other insert fails with a duplicate-key error. The API should handle that error cleanly rather than create duplicate attendance. The exact HTTP response depends on the error handling in the code.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
At the leaderboard cutoff, I would include employees tied on late minutes if the requirement is to preserve ties. This can return more than limit employees. If the implementation simply sorts and limits records, tied employees may be excluded; deterministic tie-breaking would be needed.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The department summary starts from employee records and matches attendance logs, so employees with no logs can still be counted in department headcount.

5. **One thing you would change** if this had to serve 100x the data.
I would measure slow queries, add appropriate indexes, and review aggregation pipelines before scaling further.