# G5: scripts for T4 (defects after merge), T5 (harm-3 coverage) and T10 (reviewer calibration score)

Reader: the test writer (non-builder) and the builder. Definitions come from `wiki/pilot-playbook-2026-11.md` §1 row 4,
§2 and §6, and `wiki/target-state-2026-11.md` T4, T5, T10. Each script: `python3 -I scripts/<name>.py ...`, standard
library only, no network, the only subprocess is `git`, writes nothing except the `--out` file; prints the same JSON to
stdout; exit 0 on success, 2 on invalid or unreadable input (message on stderr). JSON is written with `indent=2`,
`ensure_ascii=False` and a trailing newline. Every report has a `"warnings"` list (strings).

## 1. `scripts/post_merge_defects.py` (T4)

A defect after merge is a distinct defect found in a package's code after that package merged into the base branch
(playbook §1 row 4). Two sources:

- **Commits.** Commits reachable from `--base` (default `fuse`) in `--repo` (default the repository root) carrying the
  git trailer `Fixes-pkg: <package>`. Read trailers with `git log --format=...%(trailers:key=Fixes-pkg,valueonly)` and
  `%(trailers:key=Defect-id,valueonly)`. A commit with trailer `Defect-id: <id>` is that defect; one without is its own
  defect, id `commit:<short sha 7>`. Several commits with the same `Defect-id` are one defect. A commit with two
  `Fixes-pkg` trailers counts once for each package.
- **Register.** `--register` (default `wiki/status/post-merge-defects.md`): the first table with the exact header
  `| id | package | found | by | fixed in | note |`. One row = one defect. `found` is an ISO date. A register row and a
  commit with the same id (`Defect-id` equal to the row's `id`) are one defect; the register row's `package` wins.
  A missing register file is not an error: warning `"no register file"`, register count 0. A row with the wrong cell
  count is skipped with a warning.
- Optional `--since DATE` / `--until DATE` (ISO dates, inclusive) filter by commit author date (UTC date) or the row's
  `found` date.

Output:
```
{"base": "fuse", "window": {"since": null|DATE, "until": null|DATE},
 "packages": {"<pkg>": {"defects": N, "ids": [sorted ids]}},   # sorted by package name
 "total": N, "from_commits": N, "from_register": N, "both": N, "warnings": [...]}
```
`from_commits` counts defects seen only in commits, `from_register` only in the register, `both` in both;
`total = from_commits + from_register + both`.

## 2. `scripts/harm3_coverage.py` (T5)

T5 is the number of harm-3 rules of the audit whose mechanism is merged.

- `--audit` (default `wiki/pilot-playbook-2026-11.md`): the first table whose header row starts with
  `| Rank | Rule (source) | Breaks | Evidence | Harm |`. A row is harm 3 when its `Harm` cell, stripped, is `3`.
  Row key = its `Rank` cell.
- `--map` (default `wiki/status/t5-mechanisms.json`):
  `{"<rank>": [{"mechanism": str, "covers": "full"|"part", "paths": [repo-relative paths], "merge": "<commit>"}]}`.
- For each harm-3 row and each of its mechanisms, the script checks: `merge` resolves (`git rev-parse --verify
  <merge>^{commit}`) and is an ancestor of `--base` (`git merge-base --is-ancestor`), and every path exists in the
  tree of `--base` (`git cat-file -e <base>:<path>`). A mechanism is **merged** when all checks pass.
- A row is **covered** when at least one merged mechanism has `covers: "full"`; **part** when it has merged
  mechanisms but none full; **none** otherwise. A map entry for a rank that is not harm 3 gives a warning and is ignored.
  A missing map file is not an error: warning `"no mechanism map"`, all rows none.

Output:
```
{"base": "fuse", "harm3_rows": N, "covered": N, "part": N, "none": N,
 "rows": [{"rank": "2", "rule": "<Rule cell>", "status": "covered"|"part"|"none",
           "mechanisms": [{"mechanism": str, "covers": str, "merged": bool, "failed": [str]}]}],
 "warnings": [...]}
```
Rows in table order. `failed` lists each failed check as `"merge <x> not an ancestor of fuse"`,
`"merge <x> unknown"` or `"missing path <p>"`.

## 3. `scripts/calibration_score.py` (T10)

Scores one planted-defect calibration (playbook §2).

- `--key KEY.json`: `{"plants": [{"id": "P1", "kind": "security"|"logic"|"test"|"interface", "file": path,
  "line": int, "failure": str}], "decoys": [{"id": "D1", "file": path, "line": int}]}`.
- `--findings LANE=FILE` (repeatable): each FILE is `{"findings": [{"file": path, "line": int, "text": str}]}`.
  The finding's `file` matches a plant's when equal after stripping a leading `./` or `a/`/`b/`.
- `--qa QA.json` (optional): `{"<lane>": {"<finding index>": {"plant": "P1"|null, "real_defect": bool}}}`, the QA
  agent's judgement of cause match (playbook §2: a script does the file-and-line test, a QA agent of another family
  decides the cause).
- **Window match.** A finding window-matches a plant when the file matches and `abs(line - plant.line) <= 5`.
- **Caught.** Without QA for that lane: a plant is caught when some finding window-matches it (`"qa": false` in the
  output, marking the rate provisional). With QA: a plant is caught only when some finding window-matches it **and** the
  QA entry for that finding names that plant. A finding counts for at most one plant (the nearest line; ties to the
  lower plant id).
- **False positive.** A finding that window-matches no plant: with QA, it is a false positive when its QA entry says
  `real_defect: false` (or has no entry); without QA it is listed as `unmatched` and not counted as a false positive.
  A finding that window-matches a decoy is always a false positive.
- **Wilson interval.** 95% Wilson score interval, z = 1.96, for caught out of planted, rounded to 3 decimals;
  `[0.0, 0.0]`-style bounds are fine at n = 0 → use `null` for both when n = 0.

Output:
```
{"plants": N, "lanes": {"<lane>": {"qa": bool, "caught": N, "planted": N, "rate": float|null,
   "wilson95": [lo, hi]|null, "by_kind": {"<kind>": {"caught": N, "planted": N}},
   "security": {"caught": N, "planted": N}, "caught_ids": [...], "false_positives": N,
   "unmatched": N, "findings": N}}, "warnings": [...]}
```
Lanes sorted by name; `by_kind` sorted by kind and listing every kind present in the key.

## Records the scripts read (seeded by the manager, not by the builder)

`wiki/status/post-merge-defects.md` and `wiki/status/t5-mechanisms.json` are records; the tests use their own fixtures.
