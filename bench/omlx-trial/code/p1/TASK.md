# Task: parse_duration

Implement `parse_duration(s: str) -> int` in `durations.py` (this folder).
Return the total number of seconds. Do not change the function name or signature.
Use the standard library only.

## Input format

A duration is one or more parts separated by whitespace. Each part is one or more
`<digits><unit>` pairs, with no space between a number and its unit.

Units (lowercase only):

| unit | seconds |
|------|---------|
| d    | 86400   |
| h    | 3600    |
| m    | 60      |
| s    | 1       |

Examples:

- `"45s"` -> 45
- `"2d"` -> 172800
- `"1h30m"` -> 5400
- `"1h 5m 3s"` -> 3665
- `"  10m  "` -> 600 (leading and trailing whitespace is ignored)
- `"30m1h"` -> 5400 (units may appear in any order)

## Errors

Raise `ValueError` for:

- an empty string, or a string that is only whitespace
- an unknown unit, e.g. `"5w"`, `"1H"` (units are case-sensitive)
- a negative number, e.g. `"-5m"`
- a repeated unit anywhere in the string, e.g. `"1h2h"`
- a number with no unit, a unit with no number, or a decimal, e.g. `"1.5h"`, `"h"`, `"1 h"`

## Files

- Write the implementation in `durations.py`, replacing the `NotImplementedError` stub.
- The tests are in `tests/test_durations.py`. Do not edit them.
