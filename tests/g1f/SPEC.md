# G1f: T12 and T3 on the owner dashboard; `.sock/` ignored

Route-around of G1e (S97): parts A and C of `tests/g1e/SPEC.md`, unchanged, with one added rule. The route check in
`subagent promote` (G1e part B) is held. Decisions: S95, S91, S96, S97.

**Added rule for the tests:** the dashboard tests run on their own fixtures, in which the ten existing rows emit no
warning (an 8-column Holds table with review dates after `--now`, and a meter whose roles carry `tokens.input` and
`tokens.output`). The shared `tests/owner_dash/fixtures` emit two correct pre-existing warnings (t8, t2) and must not
be used where a test asserts an exact warning list.

## A. Scorecard rows in `scripts/owner_dashboard.py`

Two new optional flags:

- `--overhead PATH`: a JSON report written by `scripts/overhead_meter.py --out`.
- `--report-json PATH`: the JSON printed by `subagent report --json`.

Three new rows are appended **after** `spend`, in this order: `t12`, `t12trip`, `t3`. The existing ten rows, their order
and their content do not change. Every row keeps the existing key order
`id, measure, value, unit, display, baseline, target, status, note`; `status` is `"reading"` when `value` is not null,
else `"no reading yet"`, and `display` is `"no reading yet"` when `value` is null (the existing `row()` rule).

| id | measure | value | unit | display | baseline | target |
|---|---|---|---|---|---|---|
| `t12` | `Machinery share of commits` | `machinery.machinery_share` (number) | `%` | `f"{v}%"` | `100.0% on 2026-11-26` | `under 50%` |
| `t12trip` | `T12 trip-wire: machinery share above the threshold two windows running` | `tripwire.tripped` (bool) | `""` | `"tripped"` / `"not tripped"` | `not tripped on 2026-11-26` | `not tripped` |
| `t3` | `Builder runs that pass their own verification` | `round(100 * verified_passed / runs, 1)` from `run_roles.builder` | `%` | `f"{v}%"` | `25.7% on 2026-11-26 (before G1d)` | `80%` |

Notes (exact strings when the value is a reading):

- `t12`: `f"records-only {a}%; rulebook {w} words, {m} methods; owner questions {q}; {start} to {end}"` with
  `a = commits.records_only_share`, `w = rulebook.words`, `m = rulebook.methods`, `q = owner_questions.total`,
  `start`/`end` from `window`. A part whose source value is missing or not a number is written as `n/a` in its place.
- `t12trip`: `f"current {c}%, previous {p}, threshold {t}%; cut list: {cuts}"` with `c = tripwire.current`,
  `p = f"{tripwire.previous}%"` or `no reading` when null, `t = tripwire.threshold`, `cuts` = the cut list's item names
  joined by `", "` (each item is a dict: use its `"name"` key if present, else `"path"`, else `str(item)`; a list of
  strings is joined as is), or `none` when the list is empty.
- `t3`: `f"{vp} of {n} builder runs; {per}; {u} runs unclassified"` where `per` is `by_provider` as
  `"<name> <vp>/<n>"` joined by `", "` in sorted name order, and `u = run_roles.unclassified.runs` (`n/a` if missing).

Rules:

- A flag not given: that row (or both T12 rows) has value null, `note` = `"no --overhead given"` or
  `"no --report-json given"`, and **no warning** is added (the locked tests expect no warnings when only the old flags
  are passed).
- A flag given whose file is missing, not JSON, not an object, or lacks the needed field or has it of the wrong type
  (bool is not a number; a number is not a bool; `runs` must be an int above 0): the row's value is null, note `""`, and
  one warning per failed row: `"t12: overhead report missing or unreadable"`, `"t12trip: overhead report missing or
  unreadable"` (or `"... lacks tripwire.tripped"`), `"t3: report lacks run_roles.builder"` (or `"t3: report file
  missing or unreadable"`). The run still exits 0 and writes all seven datasets.
- `runs == 0`: t3 is null with warning `"t3: no builder runs in the report"`.
- No other dataset changes. No network, no subprocess other than the existing git calls.

## C. `.gitignore`

Add the line `.sock/` (tests leave an untracked `.sock/` folder, S91). `git check-ignore -q .sock/x` exits 0.

## Locked test this changes

- `tests/owner_dash/test_scorecard.py::IDS` gains `"t12", "t12trip", "t3"` at the end (TCR-G1e-1, both passes fail-closed).
