VERDICT: PASS
TESTS: 16
RED_ON_BASE: 10/16

Run: `uv run --no-sync pytest -q -p no:cacheprovider tests/r1a_agy2` on 6111c52: 10 failed, 6 passed, about 6 s. ruff on tests/r1a_agy2: clean.
Files: `test_r1_real_stream.py` (R1, 4 tests), `test_r2_bypass_audit.py` (R2, 12 cases), `_common2.py` (synthetic conversation db builder). Fixture: `tests/fixtures/antigravity/real-bypass.stdout.jsonl`.

| test node id | clause | reason it fails on base |
|---|---|---|
| test_r2_bypass_audit.py::test_r2_3_exactly_the_distinct_true_objects_are_recorded | R2.3 | no audit exists: zero sandbox_bypass records |
| test_r2_bypass_audit.py::test_r2_3_command_is_clipped_to_500_characters | R2.3 | no audit: zero records |
| test_r2_bypass_audit.py::test_r2_4_run_state_and_result_are_unchanged | R2.4 | asserts the audit ran (2 records) before comparing state; none written |
| test_r2_bypass_audit.py::test_r2_5_unreadable_record_fails_open_with_one_unavailable_record[_missing] | R2.5 | no bypass_audit_unavailable record |
| ...[_not_sqlite] | R2.5 | same |
| ...[_no_steps_table] | R2.5 | same |
| ...[_no_payload_column] | R2.5 | same |
| ...[_db_path_is_a_directory] | R2.5 (reading raises) | same |
| test_r2_bypass_audit.py::test_r2_2_only_the_runs_own_conversation_is_read | R2.2 | no sandbox_bypass records and the own db is never opened |
| test_r2_bypass_audit.py::test_r2_2_record_is_opened_read_only | R2.2 | audit never reads the record (no records, no mode=ro connect) |

Green on base (6):
- test_r1_real_stream.py (4 tests, regression): the Translator already handles the real stream (no BypassSandbox key, so no early stop); the fixture-equals-raw test passes because the fixture was built from it.
- test_r2_6_audit_off_opens_nothing[None] and [False]: guards, green by construction on base. They keep a builder from opening the file when the flag is off (spies on sqlite3.connect, builtins.open, os.open; the db is chmod 0, so a read would also record an unavailable record).

Ambiguities:
1. The fixture still holds the streamed final-answer `text_delta` fragments that split the scratch path across deltas (for example `ate/var/folders/yy/...`, `n/T/subagent-agy-smoke-n`). They cannot be matched as a path, so they are kept byte for byte, as the brief says. The README paragraph records this. They leak only the temp-dir name; say so if you want them scrubbed too.
2. The spy tests assume the builder calls `sqlite3.connect(<str URI containing "mode=ro">)` through the `sqlite3` module attribute. A builder using `from sqlite3 import connect`, or a non-string target, is not seen by the spies. R2.2 read-only is therefore also checked by bytes, mtime and folder listing.
3. "After each agy turn ends" is read as: the records exist by the time `run.done` is set and the registry is shut down. No polling.
4. A directory at the db path is counted as "reading raises" under R2.5.
5. The trace-schema autouse check did not raise on base (the new kinds are never written). It will raise once the builder writes them; that is the expected TCR.
6. The R2 tests skip off darwin, like tests/r1a_agy.
7. Record field `ts` is checked as a number only; "kind" is not asserted.
