# Scale-gate audit log contract

The harness writes one append-only JSONL file (`audit.jsonl`). Every line is one JSON
object with at least:

| key | type | meaning |
|---|---|---|
| `ts` | float | epoch seconds when the event happened (one clock: all processes run on one machine) |
| `type` | string | one of the types below |

Any other key is optional unless listed. Unknown types are ignored by the audit. A line
that is not a JSON object counts as a malformed line and fails condition 6. No record may
hold a key value, a token or an environment value.

`audit_gate.py report --log audit.jsonl --trace <subagent trace.jsonl>` reads this file
and the subagent trace (`kind == "run"` records: `run_id`, `cost.provider_usd`).

## Identifiers

- `request_id`: one planned role step. Stable across retries and restarts.
- `attempt`: integer, 1 for the first dispatch of a request, +1 for each re-dispatch.
  The pair (`request_id`, `attempt`) names one child interval.
- `root_id`: the root workflow a request belongs to (one per client submission).
- `workflow_id`: a path. A child workflow's id is `<parent workflow_id>/<name>`; a root's
  workflow_id equals its `root_id`.
- `depth`: workflow level of the step, 1 (root) to 3.
- `run_id`: the subagent Registry run id; it is the `run_id` of the run record in the trace.

## Record types

| type | required fields |
|---|---|
| `config` | `lane_caps` {lane: int}, `budget_cap_usd` float, `planned_children` int, `planned_roots` int. Exactly one. |
| `plan` | `requests`: list of {`request_id`, `lane`, `root_id`, `workflow_id`, `depth`, `gated_by_owner` bool}. One or more records; a `request_id` may appear in only one entry overall. `gated_by_owner` is true for a request that can start only after the owner decision. |
| `client_submit` | `client_pid` int, `root_id` |
| `child_start` | `request_id`, `attempt`, `lane`, `provider`, `root_id`, `workflow_id`, `depth`, `worker_pid` |
| `child_end` | the `child_start` fields plus `outcome` (`completed`, `failed`, `dead`, `budget_exhausted`), `run_id` (string or null), `cost_usd` float, `verified` bool |
| `request_end` | `request_id`, `outcome` (same set), `run_id` (the completed child's run_id, else null). Exactly one per planned request: its terminal outcome. A request rejected by the budget before dispatch has a `request_end` with `budget_exhausted` and no child interval. |
| `budget_reserve` | `reservation_id`, `request_id`, `amount_usd` |
| `budget_settle` | `reservation_id`, `request_id`, `run_id` (null only if no subagent run started), `actual_usd`. Closes the reservation; `actual_usd` is the committed spend. |
| `budget_reject` | `request_id`, `requested_usd`, `committed_usd` |
| `owner_paused` | `workflow_id`, `root_id` |
| `client_killed` | `client_pid`, `signal` (`SIGKILL`) |
| `server_stopped` | `pid` |
| `server_ready` | `cold_start_seconds` float, `restart` bool (false for the first start) |
| `client_restarted` | `client_pid`, `root_id` |
| `owner_signal` | `workflow_id`, `decision` |
| `owner_resumed` | `workflow_id` |
| `root_done` | `root_id`, `status` (`completed`, `failed`, `budget_exhausted`) |
| `runtime_error` | `source`, `line`, `detail` (ingested server/worker log line, trimmed) |
| `shutdown` | `clean` bool, `orphan_pids` list of int |

## Ordering the gate expects

`config`, `plan`, `server_ready` (restart false), `client_submit` x2, child events,
`owner_paused`, (at least 30 completed `child_end`), `client_killed`, `server_stopped`,
`server_ready` (restart true), `client_restarted`, `owner_signal`, `owner_resumed`,
gated children, `request_end` for every request, `root_done` x2, `shutdown`.

## Conditions (audit_gate.py)

1. Requests: `planned_children` == 200 == distinct planned request_ids; exactly one
   `request_end` per planned request and none for others; no child for an unplanned
   request; at most one completed `child_end` per request; a completed `request_end` has
   exactly one completed `child_end` with the same non-null run_id; completed run_ids are
   distinct.
2. Admission: per lane, max overlapping (`child_start.ts`, `child_end.ts`) intervals
   (ends sort before starts at equal ts) <= `lane_caps[lane]`; every child lane has a cap
   and matches its planned lane; each (request_id, attempt) has one start and one end.
3. Budget: sum of `budget_settle.actual_usd` <= `budget_cap_usd`; every reservation
   settled exactly once, no settle without reserve; at least one `budget_reject`; every
   `child_end` with a run_id is settled under that run_id; every `budget_exhausted`
   request has a `budget_reject`; a settle with null run_id has `actual_usd` 0; ledger
   total equals the sum of `cost.provider_usd` over trace run records of the settled
   run_ids within $0.001, and every settled run_id has a priced run record.
4. Owner pause: first `owner_paused` < `client_killed` (with >= 30 completed `child_end`
   before it) < `server_stopped` < next `server_ready` (restart true) < `client_restarted`
   < `owner_signal` < `owner_resumed`; signal and resume name the paused workflow; at least
   one `gated_by_owner` request, every one of them under the paused workflow, none
   started before `owner_resumed`, all with a `request_end` after it; the paused root's
   `root_done` comes after `owner_resumed`.
5. Shape: two roots planned and submitted; max completed depth 3 in each; two distinct
   client pids; completed children: omlx >= 100, deepseek >= 1; every completed child has
   `verified` true.
6. Health: zero `runtime_error`, zero malformed lines; exactly one `root_done` per root
   with a terminal status; exactly one `shutdown`, clean, `orphan_pids` empty; a first
   `server_ready` with restart false; every `cold_start_seconds` <= 5.
