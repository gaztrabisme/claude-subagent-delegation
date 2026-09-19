# Task: cron expression scheduler

Create a Python package `cron/` (standard library only, Python 3.10+) whose `cron/__init__.py` exports:

- `parse(expression: str) -> CronExpr`: parse an expression; raise `ValueError` if it is invalid.
- `matches(expression: str, when: datetime) -> bool`: whether the minute of `when` matches
  (seconds and microseconds are ignored).
- `next_run(expression: str, after: datetime) -> datetime | None`: the earliest minute strictly after
  `after` that matches, with seconds and microseconds set to 0; `None` if there is no match within
  10 years (366 * 10 days) after `after`.
- `next_runs(expression: str, after: datetime, count: int) -> list[datetime]`: the next `count` runs
  (each strictly after the previous one; stops early if `next_run` returns `None`).

`CronExpr` can be any class; the functions above may also accept a `CronExpr` instead of a string.
Datetimes are naive (no time zones, no DST).

## Syntax
Five fields separated by whitespace: minute (0-59), hour (0-23), day of month (1-31),
month (1-12 or `JAN`-`DEC`), day of week (0-7 or `SUN`-`SAT`; both 0 and 7 are Sunday).
Names are case-insensitive. Leading/trailing whitespace is ignored.

Each field is a comma-separated list of items. An item is one of:
- `*`: every value
- `N`: a single value
- `A-B`: inclusive range (A must be <= B)
- `*/S`, `A-B/S`: every S-th value starting at the range start (`*/S` starts at the field minimum)
- `N/S`: every S-th value from N up to the field maximum

Names work anywhere a number does (`MON-FRI`, `JAN,JUL`). Step `S` must be >= 1.
In day of week, a range may end at 7 (`5-7` is Friday, Saturday, Sunday).

Macros (the whole expression): `@yearly` and `@annually` = `0 0 1 1 *`, `@monthly` = `0 0 1 * *`,
`@weekly` = `0 0 * * 0`, `@daily` and `@midnight` = `0 0 * * *`, `@hourly` = `0 * * * *`.

Invalid (raise `ValueError`): wrong number of fields, values out of range, unknown names or macros,
empty items (`1,,2`), a range with start > end, step 0 or a non-numeric step, anything else malformed.

## Day matching
If both day of month and day of week are restricted (neither field is exactly `*`), a day matches
when EITHER matches (standard cron behavior). If only one is restricted, only that one applies.

## Performance
`next_run` must return within 50 ms for any valid expression, including rare ones such as
`0 0 29 2 *` (next Feb 29) and expressions that never match such as `0 0 30 2 *` (returns `None`).
