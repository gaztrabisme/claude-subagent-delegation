"""R1a merge: a run tripped by the reaper's `end_run` must still be killed at the
child's next event (R7a guarantee), not left to run past its deadline.

Offline and in-process. The scripted child ignores the kill that `end_run` sends
(as a process that had not really started would), so the only thing that can
kill it afterwards is the stream loop's kill-on-trip branch.
"""

from __future__ import annotations

import threading

from ..r7a import _helpers as h


class KillIgnoringChild:
    """Parks after `init`; once released, keeps emitting events until killed twice."""

    def __init__(self, argv, env):
        self.argv, self.env = argv, env
        self.parked = threading.Event()
        self.release = threading.Event()
        self.second_kill = threading.Event()
        self.kills = 0
        self.events_after_release = 0

    def events(self):
        yield h.init()
        self.parked.set()
        assert self.release.wait(timeout=10)
        for i in range(5):
            self.events_after_release += 1
            yield h.read(f"/tmp/file-{i}")
            if self.second_kill.is_set():
                return

    def kill(self) -> None:
        self.kills += 1  # never stops the child: only the stream loop's kill is counted
        if self.kills >= 2:
            self.second_kill.set()


def test_worker_kills_child_at_next_event_after_reaper_ended_the_run_on_its_deadline(
    tmp_path, monkeypatch
):
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    holder: list[KillIgnoringChild] = []

    def maker(argv, env):
        holder.append(KillIgnoringChild(argv, env))
        return holder[0]

    reg = h.registry(tmp_path, monkeypatch, [maker],
                     provider_extra={"run_timeout": 30.0, "max_agents": 1})
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        child = holder[0]
        assert child.parked.wait(5), "child never started"

        clock.tick(500)
        reg.reap_once()  # end_run on the deadline: its own kill is ignored by the child
        assert run.trip is not None and run.trip[1] == "run deadline exceeded"
        assert child.kills == 1

        child.release.set()  # the child now emits its next event
        assert child.second_kill.wait(5), (
            f"worker never killed the child at its next event after the trip "
            f"(kills={child.kills}, events after release={child.events_after_release})"
        )
        assert child.kills >= 2
    finally:
        if holder:
            holder[0].release.set()
            holder[0].second_kill.set()
        reg.shutdown()
