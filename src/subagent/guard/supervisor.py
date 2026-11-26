"""The middle manager: a verdict server the child's PreToolUse hook calls.

The child cannot reach the MCP client directly — the SDK wire carries three
methods and no approval message. Hooks, however, run as ordinary local
processes, so the hook connects to a unix socket here and blocks on a verdict.

Four tiers, in order of preference, chosen once at startup and logged:

  agent         a local reviewer process -- a model decides, nobody is asked
  sampling      ctx.session.create_message -- the MCP client's model decides
  elicitation   ctx.session.elicit_form   -- the operator decides
  deterministic no escalation path        -- escalate becomes deny

The `agent` tier exists because the two automated rungs are not always
available. Claude Code advertises elicitation and not sampling, so on that
client every escalation would interrupt a person -- which is the wrong price for
a delegated agent that is supposed to run unattended. Setting [guard].supervisor to
`agent` runs a reviewer locally instead: same prompt, same structured facts, no
human in the loop. It costs a subprocess and a few seconds per escalation, and
it spends tokens on whatever account the reviewer CLI is logged into.

The ladder is walked, not picked from once. Sampling is deprecated as of the
2026-07-28 spec revision (SEP-2577) and works today, but a client that drops it
should degrade to asking the operator rather than to denying everything, so a
tier that *errors* falls through to the next one. A tier that *times out* does
not: an unanswered question is a no, and asking again on another channel would
only double the wait.

Whatever the tier, `guard.classify` runs first and settles the clear cases, so
the dangerous ones never depend on a model and the routine ones cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import time
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from mcp.types import ClientCapabilities, ElicitationCapability, SamplingCapability

from ..config import Settings, _configured_approval_socket_path, log
from .approval_auth import sign, valid_nonce, verify
from .classify import ALLOW, DENY, ESCALATE, Verdict, classify, provider_guard_context


def _classify(tool_name, tool_input, workspace, cwd=None, guard_context=None):
    """Classify a request using the agent's protected-path context."""
    return classify(tool_name, tool_input, workspace, cwd, context=guard_context)

TIER_UNRESOLVED = "unresolved"
TIER_AGENT = "agent"
TIER_SAMPLING = "sampling"
TIER_ELICITATION = "elicitation"
TIER_DETERMINISTIC = "deterministic"

SUPERVISOR_SYSTEM_PROMPT = """\
You are a security supervisor for a delegated coding agent. The agent runs \
unattended in a workspace directory and has proposed a tool call that an \
automated policy could not classify.

Answer with exactly one word on the first line, ALLOW or DENY, then one short \
sentence of reason on the second line.

Rules:
- Default to DENY when uncertain. A denied call costs the agent one retry; an \
allowed destructive call cannot be undone.
- DENY anything that deletes or overwrites outside the workspace, touches \
credentials or SSH keys, installs software system-wide, changes system \
configuration, or sends data to a network endpoint.
- ALLOW ordinary development work: running the workspace's own code and tests, \
reading files, inspecting version control, writing inside the workspace.
- You are shown structured facts only. There is no justification from the agent \
to weigh, and you should not infer intent beyond what the facts state.
"""


class Supervisor:
    """Owns the socket, the tier decision, and the escalation path."""

    def __init__(self, settings: Settings, registry: Any = None, trace: Any = None):
        self.settings = settings
        self.default_guard_context = provider_guard_context(settings)
        self.trace = trace
        # The registry is how a verdict resolves the workspace it is judging
        # against. The hook reports one too, but that is a field the supervised
        # side supplies, and the security boundary should not read it.
        self.registry = registry
        self.socket_path = Path(settings.approval_socket)
        self._session: Any = None
        # Unresolved until a tool call brings a session to read capabilities
        # from. Reporting DETERMINISTIC before that reads as "escalation will
        # deny", which is a claim about the client nobody has checked yet.
        self.tier: str = TIER_UNRESOLVED
        self._ladder: list[str] = []
        self.decisions: list[dict[str, Any]] = []
        # Denials keyed by agent id, drained by the loop after each run.
        self.denials: dict[str, list[dict[str, Any]]] = {}
        self._used_nonces: dict[str, set[str]] = {}
        register = getattr(registry, "register_agent_cleanup", None)
        if callable(register):
            register(self._drop_agent_memory)

    def _drop_agent_memory(self, agent_id: str) -> None:
        self._used_nonces.pop(agent_id, None)

    # -- session binding ------------------------------------------------

    def bind(self, session: Any) -> None:
        """Capture the MCP session on the first tool call and pick a tier.

        Escalation happens on a worker thread's timeline, outside any tool
        call, so the session has to be captured rather than passed in.
        """
        if self._session is not None:
            return
        self._session = session
        self._ladder = self._resolve_ladder(session)
        self.tier = self._ladder[0] if self._ladder else TIER_DETERMINISTIC
        log.info("supervisor tier = %s (ladder: %s)",
                 self.tier, " -> ".join([*self._ladder, TIER_DETERMINISTIC]))

    def _resolve_ladder(self, session: Any) -> list[str]:
        """Every escalation channel available, best first."""
        if self.settings.supervisor == "off":
            return []
        # An explicitly configured reviewer outranks the client's channels: it
        # was asked for precisely so nothing has to interrupt a person.
        if self.settings.supervisor == "agent":
            return [TIER_AGENT]
        check = getattr(session, "check_client_capability", None)
        if check is None:
            return []
        ladder: list[str] = []
        try:
            if self.settings.supervisor in ("auto", "sampling") and check(
                ClientCapabilities(sampling=SamplingCapability())
            ):
                ladder.append(TIER_SAMPLING)
            if self.settings.supervisor in ("auto", "elicitation") and check(
                ClientCapabilities(elicitation=ElicitationCapability())
            ):
                ladder.append(TIER_ELICITATION)
        except Exception:  # noqa: BLE001 - a capability probe must never block startup
            log.warning("client capability probe failed; escalation will deny", exc_info=True)
            return []
        return ladder

    # -- decisions ------------------------------------------------------

    def workspace_for(self, agent_id: str | None, fallback: str | None) -> tuple[Path, dict]:
        """The workspace a verdict is judged against, resolved server-side,
        plus the agent's guard context (protected paths, .subagent allowlist).
        """
        del fallback  # Caller supplied workspaces are never an authority.
        if not isinstance(agent_id, str) or not agent_id or self.registry is None:
            raise ValueError("approval request does not name a live agent")
        agent = self.registry.find_agent(agent_id)
        if agent is None or getattr(agent, "closed", False):
            raise ValueError("approval request does not name a live agent")
        guard_context = getattr(agent, "guard_context", None)
        if not isinstance(guard_context, dict) or not guard_context:
            raise ValueError("live agent has no guard context")
        return agent.workspace, guard_context

    async def decide(
        self,
        tool_name: str,
        tool_input: dict,
        workspace: Path,
        agent_id: str | None = None,
        cwd: str | None = None,
        guard_context: dict | None = None,
    ) -> Verdict:
        started = time.monotonic()
        verdict = _classify(tool_name, tool_input, workspace, cwd, guard_context=guard_context)
        if verdict.action != ESCALATE:
            self._record(verdict, tier="policy", tool=tool_name, agent_id=agent_id,
                         started=started)
            return verdict
        if self.settings.supervisor == "allow-escalations":
            resolved = Verdict(ALLOW, f"escalation auto-allowed: {verdict.reason}", verdict.facts)
            self._record(resolved, tier="policy", tool=tool_name, agent_id=agent_id,
                         started=started)
            return resolved
        resolved, tier = await self._escalate(verdict, tool_name, workspace)
        self._record(resolved, tier=tier, tool=tool_name, agent_id=agent_id, started=started)
        return resolved

    async def _escalate(
        self, verdict: Verdict, tool_name: str, workspace: Path
    ) -> tuple[Verdict, str]:
        """Walk the ladder. Returns the verdict and the tier that produced it."""
        if self._session is None or not self._ladder:  # unresolved or no channel
            return Verdict(
                DENY,
                f"denied: {verdict.reason} (no supervisor available; escalation fails closed)",
                verdict.facts,
            ), TIER_DETERMINISTIC
        record = {
            "tool": tool_name,
            "workspace": str(workspace),
            "policy_reason": verdict.reason,
            "facts": verdict.facts,
        }
        failure: Exception | None = None
        for tier in self._ladder:
            try:
                with anyio.fail_after(self.settings.supervisor_timeout):
                    if tier == TIER_AGENT:
                        return await self._ask_agent(record, verdict), tier
                    if tier == TIER_SAMPLING:
                        return await self._ask_model(record, verdict), tier
                    return await self._ask_human(record, verdict), tier
            except TimeoutError:
                # An unanswered question is a no. Do not re-ask on another channel.
                return Verdict(
                    DENY,
                    f"denied: supervisor did not answer within "
                    f"{self.settings.supervisor_timeout:g}s",
                    verdict.facts,
                ), tier
            except Exception as exc:  # noqa: BLE001 - try the next tier, then fail closed
                failure = exc
                log.warning("escalation via %s failed; trying the next tier", tier, exc_info=True)
        name = type(failure).__name__ if failure else "unknown"
        return Verdict(
            DENY, f"denied: supervisor unavailable ({name})", verdict.facts
        ), TIER_DETERMINISTIC

    async def _ask_model(self, record: dict, verdict: Verdict) -> Verdict:
        from mcp.types import SamplingMessage, TextContent

        body = json.dumps(record, indent=2, default=str)
        result = await self._session.create_message(
            messages=[SamplingMessage(
                role="user",
                content=TextContent(type="text", text=f"Proposed tool call:\n{body}"),
            )],
            system_prompt=SUPERVISOR_SYSTEM_PROMPT,
            max_tokens=200,
            temperature=0,
        )
        return self._read_answer(
            getattr(result.content, "text", "") or "", verdict, "supervisor"
        )

    async def _ask_agent(self, record: dict, verdict: Verdict) -> Verdict:
        """Hand the facts to a local reviewer process and read its first word."""
        body = json.dumps(record, indent=2, default=str)
        prompt = f"{SUPERVISOR_SYSTEM_PROMPT}\n\nProposed tool call:\n{body}"
        argv = shlex.split(self.settings.supervisor_cmd)
        answer = await anyio.to_thread.run_sync(
            partial(self._run_reviewer, argv, prompt)
        )
        return self._read_answer(answer, verdict, "reviewer")

    def _run_reviewer(self, argv: list[str], prompt: str) -> str:
        """Blocking subprocess call. Runs on a worker thread, never the loop."""
        completed = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.settings.supervisor_timeout,
        )
        if completed.returncode != 0:
            # Raise rather than deny: the ladder then tries the next tier, and
            # a reviewer that will not start is a broken channel, not a verdict.
            raise RuntimeError(
                f"reviewer exited {completed.returncode}: {completed.stderr.strip()[:200]}"
            )
        return completed.stdout.strip()

    def _read_answer(self, text: str, verdict: Verdict, who: str) -> Verdict:
        """ALLOW on the first line allows. Anything else, including noise, denies."""
        head, _, tail = (text or "").strip().partition("\n")
        reason = tail.strip() or verdict.reason
        if head.strip().upper().startswith("ALLOW"):
            return Verdict(ALLOW, f"{who} allowed: {reason}", verdict.facts)
        return Verdict(DENY, f"{who} denied: {reason}", verdict.facts)

    async def _ask_human(self, record: dict, verdict: Verdict) -> Verdict:
        body = json.dumps(record, indent=2, default=str)
        # elicit() is kept for compatibility and forwards to elicit_form().
        ask = getattr(self._session, "elicit_form", None) or self._session.elicit
        result = await ask(
            message=(
                "A delegated Claude Code agent proposed a tool call that policy "
                f"could not classify ({verdict.reason}). Allow it?\n\n{body}"
            ),
            requested_schema={
                "type": "object",
                "properties": {"allow": {"type": "boolean",
                                         "description": "Allow this tool call"}},
                "required": ["allow"],
            },
        )
        if getattr(result, "action", None) == "accept" and (result.content or {}).get("allow"):
            return Verdict(ALLOW, "operator allowed", verdict.facts)
        return Verdict(DENY, "operator denied", verdict.facts)

    def pop_denials(self, agent_id: str) -> list[dict[str, Any]]:
        """Denials recorded for one agent, drained (one worker run consumes them)."""
        return self.denials.pop(agent_id, [])

    def _record(
        self,
        verdict: Verdict,
        tier: str,
        tool: str = "",
        agent_id: str | None = None,
        started: float | None = None,
    ) -> None:
        self.decisions.append({
            "action": verdict.action,
            "reason": verdict.reason,
            "tier": tier,
            "tool": tool,
            "agent_id": agent_id,
        })
        if agent_id and verdict.action != ALLOW:
            self.denials.setdefault(agent_id, []).append({
                "action": verdict.action,
                "reason": verdict.reason,
                "tier": tier,
                "tool": tool,
            })
        del self.decisions[:-200]
        log.info("verdict %s [%s] agent=%s tool=%s: %s",
                 verdict.action, tier, agent_id or "-", tool or "-", verdict.reason)
        if self.trace is not None:
            self.trace.verdict(
                agent_id=agent_id,
                tool=tool,
                action=verdict.action,
                tier=tier,
                reason=verdict.reason,
                facts=verdict.facts,
                latency_ms=(time.monotonic() - started) * 1000 if started else 0.0,
            )

    # -- socket ---------------------------------------------------------

    async def serve(self, *, task_status=anyio.TASK_STATUS_IGNORED) -> None:
        _configured_approval_socket_path(self.socket_path)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise RuntimeError(
                    f"refusing to replace non-owned approval socket path {self.socket_path}"
                )
            self.socket_path.unlink()
        previous_umask = os.umask(0o177)
        try:
            listener = await anyio.create_unix_listener(self.socket_path)
        finally:
            os.umask(previous_umask)
        os.chmod(self.socket_path, 0o600)
        task_status.started()
        async with listener:
            await listener.serve(self._handle)

    def cleanup(self) -> None:
        """Remove the socket file. The listener is closed by its task group."""
        try:
            info = self.socket_path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                self.socket_path.unlink()
        except OSError:
            log.warning("could not remove %s", self.socket_path, exc_info=True)

    async def _handle(self, stream) -> None:
        async with stream:
            agent = None
            request: dict[str, Any] | None = None
            try:
                raw = await _read_line(stream)
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                agent_id = request.get("agent_id")
                if isinstance(agent_id, str) and agent_id and self.registry is not None:
                    agent = self.registry.find_agent(agent_id)
                if agent is not None and getattr(agent, "closed", False):
                    self._drop_agent_memory(agent_id)
                nonce = request.get("nonce")
                known_key = getattr(agent, "approval_key", None) if agent is not None else None
                if (agent is None or getattr(agent, "closed", False) or not valid_nonce(nonce)
                        or not verify(known_key, request)):
                    reply = {"action": DENY, "reason": "denied: invalid approval request"}
                    if agent is not None:
                        reply["nonce"] = nonce
                        reply = sign(agent.approval_key, reply)
                    await stream.send((json.dumps(reply) + "\n").encode())
                    return
                used = self._used_nonces.setdefault(agent_id, set())
                normalized_nonce = nonce.lower()
                if normalized_nonce in used:
                    reply = sign(agent.approval_key, {
                        "action": DENY,
                        "reason": "denied: replayed approval nonce",
                        "nonce": nonce,
                    })
                    await stream.send((json.dumps(reply) + "\n").encode())
                    return
                if len(used) >= 100_000:
                    reply = sign(agent.approval_key, {
                        "action": DENY,
                        "reason": "denied: approval nonce memory is full",
                        "nonce": nonce,
                    })
                    await stream.send((json.dumps(reply) + "\n").encode())
                    return
                used.add(normalized_nonce)

                # Authentication and replay checks complete before any caller
                # data is interpreted for classification or escalation.
                workspace, guard_context = self.workspace_for(agent_id, None)
                if self._dsh_tampered(agent_id, agent, guard_context):
                    reply = sign(agent.approval_key, {
                        "action": DENY,
                        "reason": "denied: DSH guard bootstrap changed after readiness",
                        "nonce": nonce,
                    })
                    await stream.send((json.dumps(reply) + "\n").encode())
                    return
                verdict = await self.decide(
                    request.get("tool_name") or "",
                    request.get("tool_input") or {},
                    workspace,
                    agent_id,
                    request.get("cwd") or None,
                    guard_context,
                )
                reply = sign(agent.approval_key, {
                    "action": verdict.action,
                    "reason": verdict.reason,
                    "nonce": nonce,
                })
            except Exception as exc:  # noqa: BLE001 - a broken request denies
                log.warning("supervisor request failed", exc_info=True)
                reply = {"action": DENY, "reason": f"supervisor error: {type(exc).__name__}"}
                if agent is not None:
                    reply["nonce"] = request.get("nonce") if request is not None else None
                    reply = sign(agent.approval_key, reply)
            await stream.send((json.dumps(reply) + "\n").encode())

    def _dsh_tampered(self, agent_id: str, agent: Any, context: dict) -> bool:
        path = context.get("dsh_cordis_path")
        expected = context.get("dsh_cordis_sha256")
        if not isinstance(path, str) or not isinstance(expected, str):
            return bool(context.get("dsh_guard_tampered"))
        if context.get("dsh_guard_tampered"):
            return True
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            digest = ""
        if digest == expected:
            return False
        context["dsh_guard_tampered"] = True
        if not context.get("dsh_guard_tamper_traced"):
            context["dsh_guard_tamper_traced"] = True
            if self.trace is not None:
                self.trace.guard_tamper(
                    agent_id=agent_id,
                    run_id=str(context.get("dsh_cordis_run_id") or ""),
                    path=path,
                )
        return True


async def _read_line(stream, limit: int = 1_048_576) -> str:
    buffer = b""
    while b"\n" not in buffer:
        chunk = await stream.receive(65536)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > limit:
            raise ValueError("request too large")
    return buffer.split(b"\n", 1)[0].decode()
