"""MCP server exposing coding subagents on the configured providers."""

from __future__ import annotations

import sys
import threading
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, config
from .config import ConfigError, configure_logging, log
from .goal import GoalError, parse_goal
from .goal_runtime import goal_error_messages
from .guard.classify import protect_provider_homes
from .guard.supervisor import Supervisor
from .lane_status import probe_stale_lanes
from .providers import for_driver
from .providers.base import capabilities
from .router import FALLBACK_MODES
from .runs import TERMINAL_STATES, Registry, RegistryError, Run
from .secrets import redact_secrets

SERVER_INSTRUCTIONS = """\
Delegate self-contained engineering work to a subagent running in its own
process, on one of the backends this server is configured with ("providers";
`list` names them). delegate takes `provider` to pick one. The child reads and
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

Autoresearch is a separate MCP server that drives experiment loops and uses this
server only as a dispatcher through InProcessServer with deny_read set to its
session folder; children never call autoresearch.
"""


def _provider_instructions(settings) -> str:
    """The configured providers, named with their driver and local flag."""
    lines = ["Configured providers (`delegate` picks one by `provider`):"]
    for cfg in settings.providers.values():
        local = ", local" if cfg.local else ""
        lines.append(f"- {cfg.name} (driver {cfg.driver}{local})")
    if not settings.providers:
        lines.append("- (none configured)")
    return "\n".join(lines)


def _instructions() -> str:
    parts = [SERVER_INSTRUCTIONS, _provider_instructions(settings)]
    extra = (settings.instructions or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(parts) + "\n"


settings = config.load(Path.cwd())
protect_provider_homes(settings)
registry = Registry(settings)
supervisor = Supervisor(settings, registry, trace=registry.trace)


@asynccontextmanager
async def _lifespan(_app):
    """Run the approval socket for as long as the server is up."""
    probe_stop = threading.Event()
    probe_thread = None
    if settings.lane_status_ttl_seconds > 0:
        probe_thread = threading.Thread(
            target=_lane_probe_loop,
            args=(probe_stop,),
            name="sam-lane-probe",
            daemon=True,
        )
        probe_thread.start()
    async with anyio.create_task_group() as tg:
        await tg.start(supervisor.serve)
        try:
            yield {}
        finally:
            probe_stop.set()
            if probe_thread is not None:
                probe_thread.join(timeout=8.0)
            tg.cancel_scope.cancel()
            supervisor.cleanup()


def _lane_probe_loop(stop: threading.Event) -> None:
    """Refresh stale statuses off the MCP request path."""
    interval = settings.lane_status_ttl_seconds
    while not stop.wait(interval):
        try:
            from .cli import _prompt_turn

            live_by_lane: dict[str, int] = {}
            for agent in registry.agents():
                if not agent.closed:
                    live_by_lane[agent.cfg.name] = live_by_lane.get(agent.cfg.name, 0) + 1
            probe_stale_lanes(
                settings, registry.lane_status, _prompt_turn,
                live_by_lane=live_by_lane, stop=stop,
            )
        except Exception:  # noqa: BLE001 - an observation must not stop the server
            log.warning("background lane probe failed", exc_info=True)


app = MCPServer(
    "subagent",
    version=__version__,
    instructions=_instructions(),
    lifespan=_lifespan,
)

# How often a wait reports progress back to the client while it blocks.
PROGRESS_INTERVAL = 3.0

LANE_DEPRECATED = "`lane` is deprecated; use `provider`"


def _parent_context(ctx: Context | None) -> dict[str, Any] | None:
    """The caller's request `_meta` (Claude Code sends its tool-use id there),
    scalars only, for the trace's run record. None when the caller sent none."""
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
        if meta is None:
            return None
        if hasattr(meta, "model_dump"):
            data = meta.model_dump(exclude_none=True, by_alias=True)
        elif isinstance(meta, dict):
            data = dict(meta)
        else:
            return None
    except Exception:  # noqa: BLE001 - context is a nicety, never a failure
        return None
    out = {
        str(k): (v[:200] if isinstance(v, str) else v)
        for k, v in data.items()
        if k != "progressToken" and isinstance(v, (str, int, float, bool))
    }
    return out or None


def _goal_guard_hooks(agent):
    """Create per-run hooks that lock and verify this agent's goal file."""
    goal = getattr(agent, "goal", None)
    if goal is None or not goal.path:
        return None, None

    from .loop.testguard import TestGuard

    active = {}

    def lock_goal():
        guard = TestGuard(agent.workspace, (), protected_paths=(goal.path,))
        guard.lock()
        active["guard"] = guard
        agent.guard_context["protected"] = list(guard.protected)

    def release_goal():
        guard = active.pop("guard", None)
        if guard is None:
            return {"violations": [], "changed_files": []}
        violations, changed = guard.release()
        return {"violations": violations, "changed_files": changed}

    return lock_goal, release_goal


async def _wait_for(run: Run, seconds: float, ctx: Context | None = None) -> None:
    """Block for up to `seconds`, reporting progress while we wait.

    run.await_ready.wait is a blocking call, so it crosses to a thread.
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
            run.await_ready.wait, min(PROGRESS_INTERVAL, remaining)
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
    if run.state not in TERMINAL_STATES and run.needs_human:
        out["hint"] = "Waiting for a human. Follow human_action outside MCP, then call await again."
    elif run.state not in TERMINAL_STATES:
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
            "One or more server-side acceptance checks did not pass. Read "
            "`verification.output_tail` and `uat`, then continue "
            f"(agent_id='{run.agent_id}') with what to fix."
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
    provider: str | None = None,
    lane: str | None = None,
    fallback: str | None = None,
    goal: str | None = None,
    request_id: str | None = None,
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
        model: Model id on the chosen provider. Defaults to its model.
        name: Human label for this agent, shown in list.
        wait_seconds: Block up to this long for the run to finish. 0 returns at once.
        provider: Backend to run on, by its configured name (`list` reports
            them). Defaults to the server's default provider.
        lane: Deprecated alias for `provider`.
        fallback: Which other providers may take the work if this one refuses
            before doing any work (quota spent, balance empty, usage limit,
            local server down): "full" tries the configured fallback chain,
            "local" only the local providers in it, "none" no other provider.
            A failure after work started is never moved. The result's `lane`
            names the provider that ran and `hops` lists every one tried.
            continue always stays on that provider.
        goal: Optional inline goal block or workspace-relative GOAL.md path.
            Command rows run on the server; prose rows go to the configured reviewer.
        request_id: Optional caller key for providers supporting run context.
            Identical retries return the original run; conflicting work raises.
            Epi waits up to five seconds for admission even with wait_seconds=0.
    """
    wanted = provider or lane
    deprecated = provider is None and lane is not None
    try:
        chosen = registry.select_provider(wanted, fallback)
    except RegistryError as exc:
        # Refused before anything is spawned.
        result = {
            "state": "rejected",
            "error": str(exc),
            "lane": wanted or settings.default_provider,
            "provider": wanted or settings.default_provider,
            "fallback": fallback or "full",
            "known_providers": list(settings.providers),
            "fallback_modes": list(FALLBACK_MODES),
        }
        if deprecated:
            result["warning"] = LANE_DEPRECATED
        return result
    if ctx is not None:
        supervisor.bind(ctx.session)
    try:
        resolved = settings.resolve_workspace(workspace)
    except OSError as exc:
        raise RegistryError(f"cannot resolve workspace {workspace!r}: {exc}") from exc
    if not resolved.is_dir():
        raise RegistryError(f"workspace is not an existing directory: {resolved}")
    parsed_goal = None
    if goal is not None:
        try:
            parsed_goal = parse_goal(goal, resolved)
        except GoalError as exc:
            raise RegistryError("\n".join(goal_error_messages(exc))) from None
    if not (verification or "").strip():
        raise RegistryError(
            "verification is required: give the command that proves the task is done, "
            'or "true" if there is genuinely nothing to check.'
        )

    goal_review = None
    if parsed_goal is not None and any(row.kind == "prose" for row in parsed_goal.rows):
        from .loop.loop import goal_reviewer

        goal_review = goal_reviewer(
            resolved, SimpleNamespace(settings=settings, registry=registry)
        )

    from .providers import for_driver
    from .providers.base import capabilities

    caller_key = request_id if capabilities(for_driver(chosen.driver)).run_context else None
    arguments = {"tool": "delegate", "task": task, "verification": verification,
                 "workspace": str(resolved), "instructions": instructions, "model": model,
                 "name": name, "fallback": fallback or "full", "goal": goal}
    agent = None

    def submit():
        nonlocal agent
        agent = registry.create_agent(
            name=name, workspace=resolved, model=model, provider=chosen.name,
            fallback=fallback, goal=parsed_goal, goal_reviewer=goal_review,
            task=task, instructions=instructions,
        )
        start_error = agent.wait_ready(60.0)
        if start_error is not None:
            raise RegistryError(f"Claude Code runtime failed to start: {start_error}")
        prompt = f"{instructions.strip()}\n\n---\n\n{task}" if instructions else task
        pre_run, pre_verify = _goal_guard_hooks(agent)
        try:
            return agent.delegate(prompt, verification, parent=_parent_context(ctx),
                                  pre_run=pre_run, pre_verify=pre_verify, request_id=caller_key)
        except Exception:
            if pre_verify is not None:
                pre_verify()
            raise

    try:
        if caller_key is None:
            run = await anyio.to_thread.run_sync(submit)
        else:
            run = await anyio.to_thread.run_sync(partial(
                registry.request_once, (chosen.name, str(resolved)), caller_key, arguments, submit,
            ))
            agent = registry.find_agent(run.agent_id)
        if (agent is not None
                and capabilities(for_driver(chosen.driver)).admission_barrier):
            await anyio.to_thread.run_sync(agent.wait_admission, run)
        elif getattr(run, "admission_error", None):
            raise RegistryError(run.admission_error)
    except RegistryError as exc:
        if caller_key is not None or capabilities(for_driver(chosen.driver)).admission_barrier:
            raise ToolError(str(exc)) from None
        raise
    await _wait_for(run, wait_seconds, ctx)
    out = _result(run)
    out["workspace"] = str(resolved)
    out["model"] = agent.model if agent is not None else (model or chosen.model)
    out["lane"] = run.lane or chosen.name
    out["fallback"] = agent.fallback if agent is not None else run.fallback
    if deprecated:
        out["warning"] = LANE_DEPRECATED
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
    request_id: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Send follow-up work to an existing subagent, in its original session.

    The child keeps the full context of its earlier turns, so refer to prior
    work directly ("the migration you just wrote"). Work is queued: if the agent
    is mid-run, this message runs after it. Providers with synchronous admission
    (epi) must admit within five seconds, even with wait_seconds=0. A queued
    request that cannot start by then raises a tool error before submission;
    the active run continues.

    Args:
        agent_id: Agent to continue, from delegate or list.
        message: The follow-up instruction.
        verification: Command proving this follow-up is done. Omit it to reuse
            the command this agent's delegate was given (the result's
            `verification_note` says so). Pass "" to skip verification; the run
            then reports completed_unverified.
        wait_seconds: Block up to this long for the run to finish. 0 returns at once.
        request_id: Optional caller key. Repeating it with identical work arguments
            returns the original run; conflicting arguments raise a tool error.
    """
    if ctx is not None:
        supervisor.bind(ctx.session)
    arguments = {"tool": "continue", "agent_id": agent_id, "message": message,
                 "verification": verification}
    admission_barrier = False
    caller_key = None
    try:
        run = registry.replay_continue(agent_id, request_id, arguments)
        if run is None:
            agent = registry.agent(agent_id)
            from .providers.base import capabilities

            caller_key = request_id if capabilities(agent.driver).run_context else None
            admission_barrier = capabilities(agent.driver).admission_barrier

            def submit():
                pre_run, pre_verify = _goal_guard_hooks(agent)
                return agent.follow_up(
                    message, verification, parent=_parent_context(ctx), pre_run=pre_run,
                    pre_verify=pre_verify, request_id=caller_key,
                )

            run = await anyio.to_thread.run_sync(partial(
                registry.request_once, (agent.cfg.name, str(agent.workspace.resolve())),
                caller_key, arguments, submit,
            ))
        agent = registry.find_agent(agent_id)
        if agent is not None:
            await anyio.to_thread.run_sync(agent.wait_admission, run)
        elif run.admission_error:
            raise RegistryError(run.admission_error)
    except RegistryError as exc:
        if request_id is not None or admission_barrier:
            raise ToolError(str(exc)) from None
        raise
    await _wait_for(run, wait_seconds, ctx)
    return _result(run)


@app.tool(name="list")
async def list_agents(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
    """List every subagent this server owns, with its state, cost, and run history."""
    # Bind here too, so the reported supervisor tier is the real one on the
    # first call rather than a placeholder that reads like a resolved answer.
    if ctx is not None:
        supervisor.bind(ctx.session)
    live_agents = await anyio.to_thread.run_sync(registry.agents)
    agents = await anyio.to_thread.run_sync(lambda: [a.info() for a in live_agents])
    spent = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0, "steps": 0}
    for info in agents:
        for key in spent:
            spent[key] += info["usage"][key]
    provider_views = []
    live_by_lane: dict[str, int] = {}
    for agent in live_agents:
        if not agent.closed:
            live_by_lane[agent.cfg.name] = live_by_lane.get(agent.cfg.name, 0) + 1
    statuses = await anyio.to_thread.run_sync(
        partial(registry.lane_status.snapshot, settings, live_by_lane=live_by_lane)
    )
    for cfg in settings.providers.values():
        view = cfg.as_dict()
        driver = for_driver(cfg.driver)
        if capabilities(driver).metadata:
            try:
                extra = await anyio.to_thread.run_sync(partial(driver.metadata, settings, cfg))
            except Exception:  # noqa: BLE001 - metadata is optional list decoration
                log.warning("provider metadata unavailable for %s", cfg.name, exc_info=True)
            else:
                if isinstance(extra, dict):
                    view["extra"] = extra
        view["status"] = statuses[cfg.name]
        provider_views.append(view)
    result = {
        "agents": agents,
        "live": sum(1 for a in agents if a["state"] != "closed"),
        "limit": settings.max_agents,
        "default_provider": settings.default_provider,
        "providers": provider_views,
        "default_workspace": str(settings.workspace),
        "tokens_spent": spent,
        "archived_runs": len(registry.archived_runs()),
        "trace": str(registry.trace.path) if registry.trace.enabled else None,
        "limits": {
            "run_timeout_seconds": settings.provider().run_timeout,
            "idle_timeout_seconds": settings.provider().idle_timeout,
            "max_steps": settings.provider().max_steps,
            "turn_token_budget": settings.turn_token_budget,
            "result_cap_chars": settings.result_cap_chars,
        },
        "supervisor": {
            "tier": supervisor.tier,
            "recent_decisions": supervisor.decisions[-10:],
        },
    }
    return _sanitize_mcp_output(result)


def _sanitize_mcp_output(value: Any) -> Any:
    """Scrub credentials and replace the caller home prefix in list output."""
    if isinstance(value, str):
        text = redact_secrets(value, settings.leaked_keys)
        home = str(Path.home())
        if home and home != "/":
            text = text.replace(home, "~")
        return text
    if isinstance(value, list):
        return [_sanitize_mcp_output(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_mcp_output(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_mcp_output(item) for key, item in value.items()}
    return value


@app.tool(name="cancel")
async def cancel(agent_id: str) -> dict[str, Any]:
    """Stop a subagent and release its runtime process.

    Providers with a cancel hook cancel their service job before this kills
    the child process. Any in-flight run is reported as cancelled, and file edits it
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
        "closed": agent.closed,
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
    from .providers import for_driver
    from .providers.base import capabilities

    agent = registry.find_agent(run.agent_id)
    cfg = settings.providers.get(run.lane)
    driver = agent.driver if agent is not None else (for_driver(cfg.driver) if cfg else None)
    if capabilities(driver).transcript and run.native_outcome:
        native_id = run.native_outcome.get("extra", {}).get("epi", {}).get("run_id")
        if native_id:
            if agent is not None:
                session = agent.session()
            else:
                error, session = driver.boot(settings, run.agent_id, cfg)
                if error:
                    raise RegistryError(error)
                session.session_id = run.native_outcome["extra"]["epi"].get("agent_id")
            native = await anyio.to_thread.run_sync(partial(
                driver.transcript, session, run_id=native_id, limit=limit, raw=raw,
            ))
            return {**native, "run_id": run_id, "agent_id": run.agent_id,
                    "state": run.state, "phase": run.phase, "usage": run.usage.as_dict()}
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


def main() -> int:
    configure_logging(settings.log_level)
    try:
        settings.require_providers()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    log.info(
        "starting: default_provider=%s workspace=%s max_agents=%d supervisor=%s",
        settings.default_provider,
        settings.workspace,
        settings.max_agents,
        settings.supervisor,
    )
    if registry.trace.enabled:
        log.info("tracing decisions and runs to %s", registry.trace.path)
    for cfg in settings.providers.values():
        reason = cfg.unavailable()
        if reason is not None:
            log.info("provider %s: %s", cfg.name, reason)
    # Names the variables only; key values are never logged.
    for warning in settings.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    try:
        app.run()
    finally:
        registry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
