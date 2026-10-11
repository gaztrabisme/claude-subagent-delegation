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

import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketAttribute
from mcp.types import ClientCapabilities, ElicitationCapability, SamplingCapability

from ..config import Settings, log
from .classify import ALLOW, DENY, ESCALATE, POWERSHELL, Verdict, classify

# Windows uses loopback TCP plus a token; the hook (approval_hook.py) makes the same choice.
USE_UNIX_SOCKET = hasattr(socket, "AF_UNIX") and sys.platform != "win32"


def _classify(tool_name, tool_input, workspace, cwd=None, guard_context=None, shell=None):
    """`classify()` has no guard-context parameter yet (P2b adds the rules);
    this seam is where the supervisor passes it, so tests can observe it."""
    return classify(tool_name, tool_input, workspace, cwd, shell=shell)

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
        self.trace = trace
        # The registry is how a verdict resolves the workspace it is judging
        # against. The hook reports one too, but that is a field the supervised
        # side supplies, and the security boundary should not read it.
        self.registry = registry
        if registry is not None and settings.supervisor != "off":
            # Agents created from now on compare their tool calls with the hook
            # calls counted here (saw_hook) and stop when the hook goes silent.
            registry.hooks_counted = True
        self.socket_path = Path(settings.approval_socket)
        # Set only on the loopback-TCP transport (no AF_UNIX).
        self._token: str | None = None
        self._session: Any = None
        # Unresolved until a tool call brings a session to read capabilities
        # from. Reporting DETERMINISTIC before that reads as "escalation will
        # deny", which is a claim about the client nobody has checked yet.
        self.tier: str = TIER_UNRESOLVED
        self._ladder: list[str] = []
        self.decisions: list[dict[str, Any]] = []
        # Denials keyed by agent id, drained by the loop after each run.
        self.denials: dict[str, list[dict[str, Any]]] = {}

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
        if agent_id and self.registry is not None:
            agent = self.registry.find_agent(agent_id)
            if agent is not None:
                return agent.workspace, getattr(agent, "guard_context", None) or {}
            log.warning("verdict for unknown agent %r; using the server workspace", agent_id)
            return self.settings.workspace, {}
        return (Path(fallback) if fallback else self.settings.workspace), {}

    def shell_for(self, agent_id: str | None) -> str | None:
        """The shell an agent's commands run in, from its driver: Codex on
        Windows runs them in PowerShell. None means bash, the default reading."""
        if sys.platform != "win32" or not agent_id or self.registry is None:
            return None
        agent = self.registry.find_agent(agent_id)
        driver = getattr(getattr(agent, "driver", None), "name", None)
        return POWERSHELL if driver == "codex" else None

    def saw_hook(self, agent_id: str | None) -> None:
        """Count a hook request against its agent, before it is judged.

        The run loop compares this count with the child's tool calls and stops
        a run whose hook went silent (Agent.hook_calls).
        """
        if not agent_id or self.registry is None:
            return
        agent = self.registry.find_agent(agent_id)
        if agent is not None and hasattr(agent, "saw_hook"):
            agent.saw_hook()

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
        shell = self.shell_for(agent_id)
        verdict = _classify(tool_name, tool_input, workspace, cwd, guard_context=guard_context,
                            **({"shell": shell} if shell else {}))
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
            text=True, encoding="utf-8", errors="replace",
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
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if USE_UNIX_SOCKET:
            listener = await anyio.create_unix_listener(self.socket_path)
            os.chmod(self.socket_path, 0o600)
        else:
            # No unix sockets (Windows): listen on loopback and leave the address
            # and a per-server token in the socket file. A request without the
            # token is answered with a deny.
            listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
            port = listener.listeners[0].extra(SocketAttribute.local_port)
            self._token = secrets.token_hex(16)
            self.socket_path.write_text(
                json.dumps({"host": "127.0.0.1", "port": port, "token": self._token}),
                encoding="utf-8",
            )
        task_status.started()
        async with listener:
            await listener.serve(self._handle)

    def cleanup(self) -> None:
        """Remove the socket file. The listener is closed by its task group."""
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", self.socket_path, exc_info=True)

    async def _handle(self, stream) -> None:
        async with stream:
            try:
                raw = await _read_line(stream)
                request = json.loads(raw)
                if self._token is not None and not secrets.compare_digest(
                    str(request.get("token") or ""), self._token
                ):
                    raise PermissionError("approval request without the server token")
                agent_id = request.get("agent_id") or None
                # One hook invocation may ask several questions (one per file
                # of a patch); only its first counts as a hook call.
                if not request.get("part"):
                    self.saw_hook(agent_id)
                workspace, guard_context = self.workspace_for(agent_id, request.get("workspace"))
                verdict = await self.decide(
                    request.get("tool_name") or "",
                    request.get("tool_input") or {},
                    workspace,
                    agent_id,
                    request.get("cwd") or None,
                    guard_context,
                )
                reply = {"action": verdict.action, "reason": verdict.reason}
            except Exception as exc:  # noqa: BLE001 - a broken request denies
                log.warning("supervisor request failed", exc_info=True)
                reply = {"action": DENY, "reason": f"supervisor error: {type(exc).__name__}"}
            await stream.send((json.dumps(reply) + "\n").encode())


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
