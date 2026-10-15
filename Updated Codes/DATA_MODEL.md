from pathlib import Path

content = r"""# DATA_MODEL.md: what is stored in MongoDB

This document describes the two MongoDB collections used by the Employee Attendance & Analytics API. The assessment grader seeds these collections directly, so the application must read the seeded documents exactly as described here and write documents with the same schema.

The HTTP contract and business rules R1–R10 are defined in `openapi.yaml`. This file documents the storage model and refers to those rules where relevant.

| Setting | Value |
|---|---|
| Database | The name configured by `MONGO_DB` |
| Collections | `employees`, `attendance_logs` |
| Initial indexes | Only MongoDB's default `_id` index; the application must create any additional required indexes at startup |

## 1. Types at a glance

| Kind of value | MongoDB representation | HTTP API representation |
|---|---|---|
| Instant (punch time, creation time, history time) | BSON date (`datetime`, UTC) | Integer epoch milliseconds |
| Calendar date (`date`, `joined_on`) | String in `YYYY-MM-DD` format | Same string |
| Time of day (`shift_start`, `shift_end`) | String in `HH:MM` format, 24-hour IST | Same string |
| Work duration | `work_hours` as a double; duration fields in minutes as integers | Same numeric values |

Calendar dates and times of day are strings intentionally: a calendar date is not an instant, and converting it into a timestamp would introduce a time-zone assumption. Instants are stored as real BSON dates in MongoDB. The API converts instants to and from epoch milliseconds at the HTTP boundary.

PyMongo returns naive Python datetimes by default unless configured otherwise. The application must handle these consistently as UTC when converting BSON dates to epoch milliseconds or back.

## 2. `employees` collection

One document represents one employee.

| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Internal to MongoDB. Never accepted or returned by the API and must not be used to identify employees. |
| `emp_code` | String | Employee identifier throughout the assignment, e.g. `EMP0001`. Must be unique and immutable. It is not the MongoDB `_id`. |
| `name` | String | Employee name. |
| `email` | String | Employee email address. |
| `department` | String | Department name; matching is case-sensitive. |
| `shift_start` | String (`HH:MM`) | Scheduled shift start in IST. |
| `shift_end` | String (`HH:MM`) | Scheduled shift end in IST. If `shift_end` is less than or equal to `shift_start`, the shift is overnight; see R1. |
| `joined_on` | String (`YYYY-MM-DD`) | First day of employment; see R7 and R9. |
| `created_at` | BSON date | Set by the API when creating an employee. It must not be supplied by the client. |

Example document (MongoDB Extended JSON):

```json
{
  "emp_code": "EMP0001",
  "name": "Asha Rao",
  "email": "asha@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05",
  "created_at": {
    "$date": "2026-01-05T04:30:00Z"
  }
}
```

## 3. `attendance_logs` collection

At most one document may exist for each pair of (`emp_code`, `date`). `emp_code` refers to `employees.emp_code`; there is no MongoDB ObjectId reference between the collections.

| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Internal to MongoDB. Not part of the API. A record is addressed by (`emp_code`, `date`). |
| `emp_code` | String | Employee identifier. |
| `date` | String (`YYYY-MM-DD`) | Attendance day in the IST calendar. For overnight shifts, this is the day the shift started; see R1. |
| `status` | String | Exactly one of the five statuses listed below. |
| `punch_in` | BSON date or null | Set at punch-in. Null or missing for `ABSENT` and `LEAVE`. |
| `punch_out` | BSON date or null | Null until punch-out. |
| `work_hours` | Double or null | Computed by the API, never supplied by a client; see R4. Null until punch-out exists. |
| `late_minutes` | Integer | Computed by the API at punch-in; see R2. Missing means `0`. |
| `overtime_minutes` | Integer | Computed by the API at punch-out; see R3. Missing means `0`. |
| `half_day` | Boolean | Computed by the API at punch-out; see R5. Missing means `false`. |
| `history` | Array | Audit trail of manual corrections. Missing means an empty array. See Section 4. |

### Status values

| Status | Meaning | Punch times | Counts as present (R6) | Created by |
|---|---|---|---|---|
| `PRESENT` | Worked on site | `punch_in` required | Yes | Punch-in |
| `WFH` | Worked from home | `punch_in` required | Yes | Punch-in |
| `ON_DUTY` | Official duty away from the office | `punch_in` required | Yes | Punch-in |
| `ABSENT` | Did not work and was not on approved leave | Both null | No | Seeded back-office data or a correction |
| `LEAVE` | On approved leave | Both null | No | Seeded back-office data or a correction |

No endpoint creates `ABSENT` or `LEAVE` records. A weekday with no attendance document is different from a recorded `ABSENT` day: it has simply not been recorded.

### Stored derived values are authoritative

The API writes `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` when it punches in, punches out, or corrects an attendance record. Analytics must read these stored values and must not recompute them from punch timestamps. In seeded data, the stored values define the analytics results.

For records written by the API:

| Situation | `work_hours` | `late_minutes` | `overtime_minutes` | `half_day` |
|---|---|---|---|---|
| Presence status, no punch-out yet | `null` | Calculated by R2 | `0` | `false` |
| Presence status, with punch-out | Calculated by R4 | Calculated by R2 | Calculated by R3 | Calculated by R5 |
| `ABSENT` or `LEAVE` | `null` | `0` | `0` | `false` |

## 4. `history`: audit trail

`history` records manual corrections only (regularizations). Punch-in and punch-out are original events and must not write to this array. Each correction appends exactly one entry; entries are never edited or removed.

Each history entry has this structure:

| Field | Type | Notes |
|---|---|---|
| `at` | BSON date | Time the correction was made. |
| `by` | String | Person making the correction; free text, with no authentication required by this assignment. |
| `reason` | String | Reason for the correction. |
| `changes` | Object | Contains one key per field that actually changed. Each value has `from` and `to` values. |

Allowed keys in `changes` are `status`, `punch_in`, `punch_out`, `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day`.

In MongoDB, `from` and `to` values for `punch_in` and `punch_out` are BSON dates. In HTTP API responses, these timestamps are represented as epoch milliseconds. This conversion also applies to nested history values.

Example shape:

```json
{
  "at": {
    "$date": "2026-07-08T05:00:00Z"
  },
  "by": "hr.admin",
  "reason": "biometric glitch",
  "changes": {
    "late_minutes": {
      "from": 35,
      "to": 0
    }
  }
}
```

## 5. Sample data and edge cases

The files in `sample_data/` contain MongoDB Extended JSON documents. They can be loaded using `python sample_seed.py` or an appropriate `mongoimport --jsonArray` command. If `_id` is omitted, MongoDB generates it.

Sample data illustrates document shapes only. The grader's dataset is larger and may include ties, weekend records, employees with no attendance logs, employees who join mid-month, and other edge cases. Passing tests against the small sample alone does not prove the analytics are correct.

Important cases represented by the data model include:

- **Normal full day:** timestamps are BSON dates; derived metrics are stored.
- **Late arrival and overtime:** lateness and overtime are stored as integer minutes.
- **Open attendance record:** `punch_out` and `work_hours` are `null`; derived fields use their defined defaults.
- **Half-day attendance:** `half_day` is `true` when the R5 rule says so; a half-day counts as `0.5` of a present day under R6.
- **Absent and leave:** both punch timestamps and `work_hours` are `null`; the record is not counted as present.
- **Overnight shift:** the `date` field is the IST calendar day on which the shift started, even if punch-out occurs the following day.
- **Regularized record:** derived values are updated as required and exactly one audit entry is appended.
- **Legacy record:** missing `history` means `[]`; missing `half_day` means `false`; missing `late_minutes` or `overtime_minutes` means `0`.

## 6. Indexes and data integrity

The assessment starts with no application-created indexes. Create the indexes required by the implementation at startup.

Recommended indexes for this schema are:

- Unique index on `employees.emp_code`.
- Unique compound index on `attendance_logs.emp_code` and `attendance_logs.date`, enforcing at most one attendance document per employee per attendance day.
- Additional indexes for common date, department, or status queries only where needed by the implementation.

Before creating a unique index, account for any duplicate records already present in the database. The default MongoDB `_id` index remains in place.

Data integrity requirements:

- Use `emp_code`, not `_id`, as the employee identifier.
- Preserve exact field names: `joined_on` for employee joining date and `date` for attendance day.
- Store calendar dates and shift times as strings in their specified formats.
- Store instants as BSON dates in UTC and convert them to epoch milliseconds at the API boundary.
- Preserve case-sensitive department values.
- Do not let clients supply API-computed fields such as `created_at`, `work_hours`, `late_minutes`, `overtime_minutes`, or `half_day` where the API contract says these are server-managed.
- Do not recompute stored attendance metrics in analytics; use the persisted values.
- Preserve the append-only nature of the `history` audit trail.

## 7. Related specification

This document describes the MongoDB storage model. The HTTP request/response contract, validation requirements, and detailed business rules R1–R10 are defined in `openapi.yaml`. Use that specification together with this file when implementing and testing the API.
"""