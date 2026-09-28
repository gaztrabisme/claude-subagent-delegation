"""Per-agent configuration and environment for the Oh My Pi driver."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..config import APPROVAL_HOOK, Settings
from ..guard.classify import protect
from .base import ProviderConfig

OMP_HOOK = Path(__file__).parent.parent / "guard" / "omp_hook.ts"


def _yaml_string(value: str) -> str:
    """JSON strings are valid YAML scalars and safely quote generated values."""
    return json.dumps(value, ensure_ascii=False)


def _model_id(provider: str, model: str) -> str:
    prefix = f"{provider}/"
    return model[len(prefix):] if model.startswith(prefix) else model


def _model_reference(provider: str, model: str) -> str:
    return model if model.startswith(f"{provider}/") else f"{provider}/{model}"


def _key_binding(cfg: ProviderConfig) -> tuple[str | None, str | None]:
    """Return an env name and key value, keeping every key out of YAML."""
    for name in cfg.api_key_envs:
        value = os.environ.get(name)
        if value and value.strip():
            return name, value.strip()
    if cfg.api_key_default:
        return "SUBAGENT_OMP_API_KEY", cfg.api_key_default
    return None, None


def write_agent_config(
    settings: Settings, agent_id: str, cfg: ProviderConfig, model: str | None = None
) -> Path:
    """Write omp's models.yml and settings overlay outside the workspace."""
    selected = model or cfg.model
    if not cfg.base_url or not selected:
        raise ValueError(f"omp provider {cfg.name!r} requires base_url and model")
    home = settings.session_root / "agents" / agent_id / "omp-agent"
    home.mkdir(parents=True, exist_ok=True)
    protect(home)
    key_name, _ = _key_binding(cfg)
    reference = _model_reference(cfg.name, selected)
    model_id = _model_id(cfg.name, selected)

    provider_lines = [
        "providers:",
        f"  {_yaml_string(cfg.name)}:",
        "    api: openai-completions",
        f"    baseUrl: {_yaml_string(cfg.base_url)}",
        f"    auth: {_yaml_string('apiKey' if key_name else 'none')}",
    ]
    if key_name:
        provider_lines.append(f"    apiKey: {_yaml_string(key_name)}")
    provider_lines.extend([
        "    models:",
        f"      - id: {_yaml_string(model_id)}",
        f"        name: {_yaml_string(model_id)}",
        "        supportsTools: true",
    ])
    if not cfg.send_sampling:
        provider_lines.extend(["        reasoning: false", "        omitMaxOutputTokens: true"])
    (home / "models.yml").write_text("\n".join(provider_lines) + "\n", encoding="utf-8")

    settings_lines = ["modelRoles:", f"  default: {_yaml_string(reference)}"]
    if not cfg.send_sampling:
        settings_lines.extend([
            "temperature: -1",
            "topP: -1",
            "topK: -1",
            "minP: -1",
            "presencePenalty: -1",
            "repetitionPenalty: -1",
        ])
    config_path = home / "config.yml"
    config_path.write_text("\n".join(settings_lines) + "\n", encoding="utf-8")
    return config_path


def child_env(settings: Settings, agent_id: str, cfg: ProviderConfig) -> dict[str, str]:
    """Build omp's allowlisted child environment with only its own API key."""
    env = settings.base_child_env()
    key_name, key_value = _key_binding(cfg)
    if key_name and key_value:
        env[key_name] = key_value
    home = settings.session_root / "agents" / agent_id / "omp-agent"
    env.update({
        "PI_CODING_AGENT_DIR": str(home),
        "SUBAGENT_APPROVAL_SOCKET": settings.approval_socket,
        "SUBAGENT_HOOK_TIMEOUT": str(settings.supervisor_timeout + 20),
        "SUBAGENT_GUARD_PYTHON": sys.executable,
        "SUBAGENT_GUARD_HOOK": str(APPROVAL_HOOK),
        "SUBAGENT_GUARD_AGENT_ID": agent_id,
    })
    return env
