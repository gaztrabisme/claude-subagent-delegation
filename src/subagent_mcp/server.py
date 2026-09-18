"""MCP server exposing coding subagents on several backends (lanes)."""

from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import __version__
from .config import Settings, configure_logging, log
from .lanes import FALLBACK_MODES
from .runs import TERMINAL_STATES, Registry, RegistryError, Run
from .supervisor import Supervisor

SERVER_INSTRUCTIONS = """\
Delegate self-contained engineering work to a subagent running in its own
process, on one of several backends ("lanes": codex, deepseek, glm, bppc,
omlx). delegate takes `lane` (default glm) to pick one. The child reads and
edits files and runs shell commands in the workspace you name, so give it a
task with a clear definition of done.

Typical loop: delegate -> await -> (continue to iterate) ->
cancel when finished. Runs are asynchronous; await polls.

ALWAYS call await immediately after delegate, with wait_seconds well
above your client's blocking threshold (1800 or more). A run lives inside this
server, not as a process on the machine, so nothing shows up in the caller's
task list and nothing notifies anyone when it ends. An await that exceeds the
threshold is moved to the background by the client and becomes the visible,
notifying handle for that run. Skip it and the run is invisible: the human sees
no activity, and the caller only learns the outcome by remembering to poll.
Promising to park it later is how it gets forgotten.

Every delegation needs a `verification` command -- the command that proves the
work is done, which this server runs itself after the child finishes. A run is
only reported `completed` when that command exits 0; otherwise it comes back
`completed_unverified` with the output.

The child's tool calls are gated by a policy classifier before they execute, and
anything the classifier cannot settle is escalated to you. It still works
unattended between escalations, so point it at a branch, a worktree, or a
scratch directory rather than anything you cannot afford to have edited.
"""


def _instructions() -> str:
    extra = (os.environ.get("SAM_INSTRUCTIONS") or "").strip()
    if extra:
        return SERVER_INSTRUCTIONS + "\n" + extra + "\n"
    return SERVER_INSTRUCTIONS

settings = Settings.from_env()
registry = Registry(settings)
supervisor = Supervisor(settings, registry, trace=registry.trace)


@asynccontextmanager
async def _lifespan(_app):
    """Run the approval socket for as long as the server is up."""
    async with anyio.create_task_group() as tg:
        await tg.start(supervisor.serve)
        try:
            yield {}
        finally:
            tg.cancel_scope.cancel()
            supervisor.cleanup()


app = MCPServer(
    "subagent",
    version=__version__,
    instructions=_instructions(),
    lifespan=_lifespan,
)

# How often a wait reports progress back to the client while it blocks.
PROGRESS_INTERVAL = 3.0


async def _wait_for(run: Run, seconds: float, ctx: Context | None = None) -> None:
    """Block for up to `seconds`, reporting progress while we wait.

    run.done.wait is a blocking SDK-adjacent call, so it crosses to a thread.
    Chunking it is what makes progress reporting possible at all.
    """
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        finished = await anyio.to_thread.run_sync(
            run.done.wait, min(PROGRESS_INTERVAL, remaining)
        )
        if finished:
            return
        if ctx is not None:
            await _report(ctx, run, seconds, seconds - (deadline - time.monotonic()))


async def _report(ctx: Context, run: Run, total: float, elapsed: float) -> None:
    message = run.transcript[-1] if run.transcript else run.phase
    try:
        await ctx.report_progress(round(elapsed, 1), total=total, message=f"{run.phase}: {message}")
    except Exception:  # noqa: BLE001 - the client may not have asked for progress
        log.debug("progress report failed for %s", run.run_id, exc_info=True)


def _result(run: Run) -> dict[str, Any]:
    out = run.detail()
    if run.state not in TERMINAL_STATES:
        out["hint"] = (
            f"Still working. Call await(run_id='{run.run_id}') to keep waiting, "
            f"or transcript(run_id='{run.run_id}') to see what it is doing."
        )
    elif run.state == "completed":
        out["hint"] = (
            f"Done and verified. Call continue(agent_id='{run.agent_id}', ...) to "
            "iterate in the same session, or cancel to release the runtime."
        )
    elif run.state == "completed_unverified":
        out["hint"] = (
            "The child finished but the verification command did not pass. Read "
            f"`verification.output_tail`, then continue(agent_id='{run.agent_id}', ...) "
            "with what to fix."
        )
    return out


@app.tool(name="delegate")
async def delegate(
    task: str,
    verification: str,
    workspace: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    name: str | None = None,
    wait_seconds: float = 0,
    lane: str | None = None,
    fallback: str = "full",
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Start a new subagent on a task.

    Returns immediately with an agent_id and run_id unless wait_seconds is set.
    Each call creates a fresh agent with its own runtime process and session;
    use continue to give more work to an agent that already exists.

    Args:
        task: What to do, with a clear definition of done. The child cannot ask
            you clarifying questions, so state the acceptance criteria.
        verification: The shell command that proves the task is done, run by
            this server in the workspace after the child finishes — e.g.
            "pytest -q" or "npm test && npm run lint". Its exit code decides
            whether the run is reported completed or completed_unverified. Pass
            "true" if there is genuinely nothing to check.
        workspace: Directory the child reads and writes. Relative paths resolve
            against the server's configured workspace. Defaults to that workspace.
        instructions: Optional standing guidance prepended to the task, e.g.
            coding conventions or files to leave alone.
        model: Model id on the chosen lane. Defaults to the lane's model.
        name: Human label for this agent, shown in list.
        wait_seconds: Block up to this long for the run to finish. 0 returns at once.
        lane: Backend to run on: codex, deepseek, glm, bppc or omlx. Defaults
            to the server's default lane (SAM_DEFAULT_LANE, glm unless set).
        fallback: Which other lanes may take the work if this one refuses
            before doing any work (quota spent, balance empty, usage limit,
            local server down): "full" tries the other cloud lanes then bppc
            and omlx, "local" only bppc and omlx, "none" no other lane. A
            failure after work started is never moved. The result's `lane`
            names the lane that ran and `hops` lists every lane tried.
            continue always stays on that lane.
    """
    try:
        chosen = registry.select_lane(lane, fallback)
    except RegistryError as exc:
        # Refused before anything is spawned.
        return {
            "state": "rejected",
            "error": str(exc),
            "lane": lane or settings.default_lane,
            "fallback": fallback,
            "known_lanes": list(settings.lanes),
            "fallback_modes": list(FALLBACK_MODES),
        }
    if ctx is not None:
        supervisor.bind(ctx.session)
    try:
        resolved = settings.resolve_workspace(workspace)
    except OSError as exc:
        raise RegistryError(f"cannot resolve workspace {workspace!r}: {exc}") from exc
    if not resolved.is_dir():
        raise RegistryError(f"workspace is not an existing directory: {resolved}")
    if not (verification or "").strip():
        raise RegistryError(
            "verification is required: give the command that proves the task is done, "
            'or "true" if there is genuinely nothing to check.'
        )

    agent = registry.create_agent(
        name=name,
        workspace=resolved,
        model=model or chosen.model,
        lane=chosen.name,
        fallback=fallback,
    )
    start_error = await anyio.to_thread.run_sync(agent.wait_ready, 60.0)
    if start_error is not None:
        raise RegistryError(f"Claude Code runtime failed to start: {start_error}")
    prompt = f"{instructions.strip()}\n\n---\n\n{task}" if instructions else task
    run = agent.delegate(prompt, verification)
    await _wait_for(run, wait_seconds, ctx)
    out = _result(run)
    out["workspace"] = str(resolved)
    out["model"] = agent.model
    out["lane"] = run.lane or agent.lane.name
    out["fallback"] = agent.fallback
    return out


@app.tool(name="await")
async def await_run(
    run_id: str,
    wait_seconds: float = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Wait for a run to finish and return its result.

    Safe to call repeatedly. If the run is still going when wait_seconds
    elapses, this returns the current state rather than an error. Runs whose
    agent has since been reaped are still readable — their results are archived.

    Args:
        run_id: The run to wait on, from delegate or continue.
        wait_seconds: Maximum time to block. Use a longer value for big tasks.
    """
    run = registry.find_run(run_id)
    await _wait_for(run, wait_seconds, ctx)
    return _result(run)


@app.tool(name="continue")
async def continue_agent(
    agent_id: str,
    message: str,
    verification: str | None = None,
    wait_seconds: float = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Send follow-up work to an existing subagent, in its original session.

    The child keeps the full context of its earlier turns, so refer to prior
    work directly ("the migration you just wrote"). Work is queued: if the agent
    is mid-run, this message runs after it.

    Args:
        agent_id: Agent to continue, from delegate or list.
        message: The follow-up instruction.
        verification: Command proving this follow-up is done. Omit it to reuse
            the command this agent's delegate was given (the result's
            `verification_note` says so). Pass "" to skip verification; the run
            then reports completed_unverified.
        wait_seconds: Block up to this long for the run to finish. 0 returns at once.
    """
    if ctx is not None:
        supervisor.bind(ctx.session)
    agent = registry.agent(agent_id)
    run = agent.follow_up(message, verification)
    await _wait_for(run, wait_seconds, ctx)
    return _result(run)


@app.tool(name="list")
async def list_agents(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
    """List every subagent this server owns, with its state, cost, and run history."""
    # Bind here too, so the reported supervisor tier is the real one on the
    # first call rather than a placeholder that reads like a resolved answer.
    if ctx is not None:
        supervisor.bind(ctx.session)
    agents = [a.info() for a in registry.agents()]
    spent = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0, "steps": 0}
    for info in agents:
        for key in spent:
            spent[key] += info["usage"][key]
    return {
        "agents": agents,
        "live": sum(1 for a in agents if a["state"] != "closed"),
        "limit": settings.max_agents,
        "default_lane": settings.default_lane,
        "lanes": [lane.as_dict() for lane in settings.lanes.values()],
        "default_workspace": str(settings.workspace),
        "tokens_spent": spent,
        "archived_runs": len(registry.archived_runs()),
        "trace": str(registry.trace.path) if registry.trace.enabled else None,
        "limits": {
            "run_timeout_seconds": settings.lane().run_timeout,
            "idle_timeout_seconds": settings.lane().idle_timeout,
            "max_steps": settings.lane().max_steps,
            "turn_token_budget": settings.turn_token_budget,
            "result_cap_chars": settings.result_cap_chars,
        },
        "supervisor": {
            "tier": supervisor.tier,
            "recent_decisions": supervisor.decisions[-10:],
        },
    }


@app.tool(name="cancel")
async def cancel(agent_id: str) -> dict[str, Any]:
    """Stop a subagent and release its runtime process.

    The harness protocol has no mid-turn cancel, so this kills the child
    process. Any in-flight run is reported as cancelled, and file edits it
    already made stay on disk. Cancelling ends the session: its context cannot
    be resumed, so start a new agent rather than continuing this one.

    Args:
        agent_id: Agent to stop.
    """
    agent = registry.agent(agent_id)
    was_busy = agent.busy
    await anyio.to_thread.run_sync(agent.close)
    return {
        "agent_id": agent_id,
        "closed": True,
        "interrupted_running_work": was_busy,
        "usage": agent.usage().as_dict(),
        "runs": [r.summary() for r in agent.runs()],
    }


@app.tool(name="transcript")
async def transcript(run_id: str, limit: int = 60, raw: bool = False) -> dict[str, Any]:
    """Show what a subagent actually did during a run.

    Returns the tail of its activity log — tool calls, assistant messages, turn
    endings. Use this to check progress on a long run, or to understand a
    failure. Returns live data while the run is still going.

    Args:
        run_id: The run to inspect.
        limit: How many of the most recent activity lines to return.
        raw: Also return the child's full uncapped response. delegate
            returns a distilled version when the answer is large; this is where
            the original text lives.
    """
    run = registry.find_run(run_id)
    lines = list(run.transcript)
    tail = lines[-limit:] if limit > 0 else lines
    out = {
        "run_id": run_id,
        "agent_id": run.agent_id,
        "state": run.state,
        "phase": run.phase,
        "usage": run.usage.as_dict(),
        "activity_count": run.event_count,
        "showing": len(tail),
        "truncated": len(tail) < len(lines),
        "activity": tail,
    }
    if raw:
        out["raw_response"] = run.final_response
    return out


def main() -> None:
    configure_logging(settings.log_level)
    log.info(
        "starting: default_lane=%s workspace=%s max_agents=%d supervisor=%s",
        settings.default_lane,
        settings.workspace,
        settings.max_agents,
        settings.supervisor,
    )
    if registry.trace.enabled:
        log.info("tracing decisions and runs to %s", registry.trace.path)
    for lane in settings.lanes.values():
        reason = lane.unavailable()
        if reason is not None:
            log.info("lane %s: %s", lane.name, reason)
        elif lane.api_key_envs and not lane.api_key():
            # Names the variables only; key values are never logged.
            print(
                f"warning: lane {lane.name} has no API key in this server's environment; "
                f"children on it will fail until one of {', '.join(lane.api_key_envs)} is set.",
                file=sys.stderr,
            )
    try:
        app.run()
    finally:
        registry.shutdown()


if __name__ == "__main__":
    main()
