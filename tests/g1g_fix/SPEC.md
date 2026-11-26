# G1g fix round: the promote probe skips held lanes and lanes under an unexpired closure

Reader: the non-builder test writer and the builder. Decision: the coordinator's design point for the G1g merge (S109).
Adds to `tests/g1g/SPEC.md` part 2; everything there stays.

- A provider is **held** when its config table has a non-empty string `held` (for example
  `held = "H7 until 2026-12-24"`). Config loading already keeps unknown provider keys in `ProviderConfig.extra`, so read
  it as `cfg.extra.get("held")`. Do not change `config.py`, `_PROVIDER_KEYS` or `ProviderConfig`, so that older builds
  reading the same config still load it. A `held` value that is not a non-empty string is ignored.
- A provider is **closed until later** when the stored lane closure (`LaneState(settings.session_root)`, as
  `subagent lanes --json` reports it, without probing) has a `closed_until` later than now. A lane whose stored status
  is closed with no unexpired `closed_until` (for example one closed by a failed probe) is still probed: the S103
  failure showed up only as such a closure.
- With `probe` true, a held lane and a lane closed until later are not probed. Each is printed to stdout on its own
  line, in config order, with the other skip warnings:
  `warning: lane probe skipped for <name> (held: <reason>)` and
  `warning: lane probe skipped for <name> (closed until <closed_until>)`. When both apply, the held line is printed.
  A lane in `skip_probe` keeps its existing line.
- The static route check still covers held and closed lanes.
- With `probe` false, nothing changes and nothing is printed for held or closed lanes.
