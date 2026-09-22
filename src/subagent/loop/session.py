"""The loop's session record: what a `--continue` must reopen.

One worker run stores a record in `.subagent/session.json` so a later CLI
process can `Registry.adopt` the same agent (same id, session id and agent
home) instead of starting a fresh one.
"""

from .common import STATE_DIR, read_json, write_json

SESSION_FILE = "session.json"


def load(root):
    return read_json(root / STATE_DIR / SESSION_FILE) or {}


def save(root, session):
    write_json(root / STATE_DIR / SESSION_FILE, session)


def record_for(agent, base_checkpoint=None):
    """One worker's record, enough to adopt the agent again via `Registry.adopt`."""
    home = agent.session().home
    return {
        "agent_id": agent.agent_id,
        "provider": agent.cfg.name,
        "driver": agent.driver.name,
        "model": agent.model,
        "session_id": agent.session_id,
        "agent_home": str(home) if home else None,
        "base_checkpoint": base_checkpoint,
    }
