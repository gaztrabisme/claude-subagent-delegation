"""Acceptance tests for DeepSeek Harness (`dsh`) account-login auth mode (W2b addendum).

Pins contract points L1-L6 for ``auth = "login"`` on a ``driver = "dsh"`` provider
offline using synthetic fixtures (``tests/fixtures/dsh_login/`` and
``tests/fixtures/dsh/``), ``tests/fakes/fake_dsh.py``, the in-process guard
classifier, and the DSH guard bridge harness. No real DSH binary, endpoint, or
``~/.dsh`` credential store is read or touched.

DSH Source Evidence for Login Patch Shape (from ``app.asar``)
-------------------------------------------------------------
1. ``dsh/node_modules/@deepseek-ai/dsh-headless/cordis.patch.yml:38-46``:
   Disables the interactive credential store and account services for headless runs::

     - id: credentials
       disabled: true
     - id: authorization
       disabled: true
     - id: deepseek-account
       disabled: true

2. ``dsh/node_modules/@deepseek-ai/dsh-base/cordis.patch.yml:6-10, 90-94, 117-126, 552-553``:
   Defines the base Cordis row ids and defaults:
   - ``id: agent-default-model`` (``@deepseek-ai/dsh-agent-default-model``) with
     ``config: { provider: deepseek-official, model: deepseek-flash }``.
   - ``id: authorization`` (``@deepseek-ai/dsh-authorization``).
   - ``id: deepseek-account`` (``@deepseek-ai/dsh-deepseek-account``).
   - ``id: credentials`` (``@deepseek-ai/dsh-credentials-local``).
   - ``id: llm-deepseek-account`` (``@deepseek-ai/dsh-llm-deepseek-account``) is
     already mounted in ``dsh-base`` and not disabled by ``dsh-headless``.
   - Note (lines 6-7): a patch replaces the targeted row's whole ``config`` rather
     than merging into it.

3. ``dsh/node_modules/@deepseek-ai/cordis-plugin-include/src/patch.ts:5-16, 62-106``:
   ``applyPatches`` matches non-insert patches by ``id`` and assigns every other key
   onto the target row (``target[key] = value``); ``disabled: false`` re-enables a
   disabled row, and later patches overwrite earlier ones last-write-wins.

4. ``dsh/node_modules/@deepseek-ai/dsh-credentials-local/README.md:36-47`` and
   ``dsh/node_modules/@deepseek-ai/dsh-credentials-local/src/index.ts:61-93, 532-537``:
   ``CREDENTIALS_FILENAME = '.credentials.yaml'``; ``Config`` accepts
   ``path?: string`` (absolute path to ``.credentials.yaml``), ``dshHome?: string``,
   ``watch?: boolean``, ``debounceMs?: number``; ``resolveSpec`` resolves
   ``config.path ?? join(resolveDshHome(config.dshHome), '.credentials.yaml')``.

5. ``dsh/node_modules/@deepseek-ai/dsh-deepseek-account/src/index.ts:73-88, 107`` and
   ``dsh/node_modules/@deepseek-ai/dsh-authorization/src/index.ts:191``:
   ``DeepSeekAccount.inject = ['credentials', 'authorization']`` and
   ``AuthorizationService.inject = ['credentials']``; all ``deepseek-account``
   config fields carry defaults, so re-enabling the three rows suffices.

6. ``dsh/node_modules/@deepseek-ai/dsh-llm-deepseek-account/README.md:12, 28, 32`` and
   ``dsh/node_modules/@deepseek-ai/dsh-llm-deepseek-account/src/index.ts:18, 36``:
   Route id is ``PROVIDER = 'deepseek-account'``; built-in catalog includes
   ``deepseek-flash`` and ``deepseek-v4-pro``; signed-out or ineligible requests
   throw ``LlmError(..., 'ACCOUNT_SIGN_IN_REQUIRED')`` and never fall back to an
   API key.

7. ``dsh/node_modules/@deepseek-ai/dsh-agent-default-model/src/index.ts:24-31, 51-55``:
   ``AgentDefaultModelConfig.Config`` marks BOTH ``provider`` and ``model`` as
   ``.required()`` (``z.string().required().volatile()``). Because Cordis replaces
   ``config`` wholesale, any patch targeting ``agent-default-model`` must supply
   both ``provider: deepseek-account`` and a non-empty ``model``.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from subagent import config, router
from subagent.cli import main as cli_main
from subagent.config import APPROVAL_HOOK
from subagent.guard.classify import ALLOW, DENY, ESCALATE, classify
from subagent.providers import for_driver
from subagent.providers.base import ProviderConfig, Session
from subagent.runs import COMPLETED, FAILED, Registry

from .conftest import TEST_CHILD_ENV_PASSTHROUGH, make_settings
from .test_dsh_guard_bridge import (
    APPLY_HARNESS_JS,
    _classification_socket,
    _find_node,
    _require_bridge_entry,
    _run_node,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DSH_FIXTURES = Path(__file__).parent / "fixtures" / "dsh"
LOGIN_FIXTURES = Path(__file__).parent / "fixtures" / "dsh_login"
FAKE_DSH = Path(__file__).parent / "fakes" / "fake_dsh.py"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "config.dsh.toml"

DSH_TEST_ENV_PASSTHROUGH = (
    *TEST_CHILD_ENV_PASSTHROUGH,
    "FAKE_DSH_RECORD",
    "FAKE_DSH_FIXTURE",
    "FAKE_DSH_STDERR",
    "FAKE_DSH_EXIT",
)


@pytest.fixture(autouse=True)
def _isolate_owner_home(tmp_path: Path, monkeypatch):
    """Never let any test in this module touch the real user's ``~/.dsh``."""
    synthetic_home = tmp_path / "synthetic-home"
    synthetic_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(synthetic_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)


def _driver():
    return for_driver("dsh")


def _dsh_login_cfg(**overrides) -> ProviderConfig:
    values: dict[str, Any] = {
        "name": "deepseek-login",
        "driver": "dsh",
        "vendor": "deepseek",
        "binary": "/fake/bin/dsh",
        "api_key_envs": (),
        "max_agents": 1,
        "experimental": True,
        "extra": {"auth": "login"},
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _dsh_settings(tmp_path: Path, providers: dict | None = None, **overrides):
    overrides.setdefault("child_env_passthrough", list(DSH_TEST_ENV_PASSTHROUGH))
    return make_settings(tmp_path, providers=providers, **overrides)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _patch_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--patch"]


def _parse_yaml_scalar(raw: str) -> Any:
    """Parse a scalar or inline JSON/YAML value without external dependencies."""
    val = raw.strip()
    if not val:
        return ""
    if val[0] in ("{", "["):
        return json.loads(val)
    if (len(val) >= 2 and val[0] == '"' and val[-1] == '"'):
        return json.loads(val)
    if (len(val) >= 2 and val[0] == "'" and val[-1] == "'"):
        return val[1:-1].replace("''", "'")
    # Strip trailing unquoted comment (` # ...`).
    if " #" in val:
        val = val.split(" #", 1)[0].rstrip()
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d+\.\d+", val):
        return float(val)
    return val


def _parse_cordis_patch(text: str) -> list[dict[str, Any]]:
    """Parse a Cordis patch YAML/JSON document into a list of patch-entry dicts.

    Accepts both JSON (valid YAML 1.2) and standard block YAML lists of mappings
    with nested mappings (matching ``dsh-headless/cordis.patch.yml``).
    """
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        assert isinstance(parsed, list)
        return parsed

    entries: list[dict[str, Any]] = []
    # Stack of (indent, mapping) for the current list item.
    stack: list[tuple[int, dict[str, Any]]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line[indent:]

        if content.startswith("- ") or content == "-":
            item: dict[str, Any] = {}
            entries.append(item)
            item_indent = indent + 2
            stack = [(item_indent, item)]
            rest = content[1:].lstrip(" ")
            if not rest:
                continue
            key, sep, value = rest.partition(":")
            assert sep == ":", f"expected mapping key in YAML patch line: {raw_line!r}"
            key = key.strip()
            value_str = value.strip()
            if value_str == "":
                child: dict[str, Any] = {}
                item[key] = child
                stack.append((item_indent + 1, child))
            else:
                item[key] = _parse_yaml_scalar(value_str)
            continue

        assert stack, f"unexpected YAML content before list item: {raw_line!r}"
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        target = stack[-1][1]
        key, sep, value = content.partition(":")
        assert sep == ":", f"expected mapping key in YAML patch line: {raw_line!r}"
        key = key.strip()
        value_str = value.strip()
        if value_str == "":
            child = {}
            target[key] = child
            stack.append((indent + 1, child))
        else:
            target[key] = _parse_yaml_scalar(value_str)

    return entries


def _effective_patch_rows(argv: list[str]) -> dict[str, dict[str, Any]]:
    """Apply generated ``--patch`` files in argv order using Cordis last-write-wins rules.

    Mirrors ``cordis-plugin-include/src/patch.ts:62-106``: each non-insert entry
    targets a row by ``id`` and replaces every provided top-level key (including
    replacing ``config`` wholesale).
    """
    rows: dict[str, dict[str, Any]] = {}
    for raw_path in _patch_values(argv):
        if "dsh-guard" in raw_path:
            continue
        patch_path = Path(raw_path)
        assert patch_path.is_file(), f"generated patch does not exist: {patch_path}"
        for entry in _parse_cordis_patch(patch_path.read_text(encoding="utf-8")):
            if "insert" in entry or not isinstance(entry.get("id"), str):
                continue
            row_id = entry["id"]
            target = rows.setdefault(row_id, {"id": row_id})
            for key, value in entry.items():
                if key == "id":
                    continue
                target[key] = value
    return rows


@pytest.fixture
def fake_dsh(tmp_path: Path, monkeypatch):
    """Install ``tests/fakes/fake_dsh.py`` as an executable ``dsh`` binary."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "dsh"
    binary.write_text(f"#!{sys.executable}\n{FAKE_DSH.read_text(encoding='utf-8')}", encoding="utf-8")
    binary.chmod(0o755)

    record = tmp_path / "dsh-calls.jsonl"
    monkeypatch.setenv("FAKE_DSH_RECORD", str(record))
    monkeypatch.setenv("FAKE_DSH_FIXTURE", str(DSH_FIXTURES / "success_tools_usage.jsonl"))
    monkeypatch.delenv("FAKE_DSH_STDERR", raising=False)
    monkeypatch.setenv("FAKE_DSH_EXIT", "0")

    class FakeDsh:
        path = str(binary)

        def use(
            self,
            fixture: Path | str | None,
            *,
            exit_code: int = 0,
            stderr: str = "",
        ) -> None:
            if not fixture:
                monkeypatch.setenv("FAKE_DSH_FIXTURE", "")
            else:
                path = Path(fixture)
                if not path.is_absolute():
                    path = LOGIN_FIXTURES / path
                monkeypatch.setenv("FAKE_DSH_FIXTURE", str(path))
            monkeypatch.setenv("FAKE_DSH_EXIT", str(exit_code))
            if stderr:
                monkeypatch.setenv("FAKE_DSH_STDERR", stderr)
            else:
                monkeypatch.delenv("FAKE_DSH_STDERR", raising=False)

        def calls(self) -> list[dict]:
            if not record.exists():
                return []
            return [
                json.loads(line)
                for line in record.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    return FakeDsh()


def _make_synthetic_owner_home(base: Path) -> tuple[Path, Path, Path]:
    """Create a synthetic owner DSH home with `.credentials.yaml` and `storages/`."""
    owner_home = base / "owner-dsh-home"
    storages = owner_home / "storages"
    storages.mkdir(parents=True, exist_ok=True)
    cred_file = owner_home / ".credentials.yaml"
    cred_file.write_text(
        "version: 1\nrecords:\n  deepseek-account-platform/default:\n"
        "    kind: grant\n    payload:\n      version: 1\n"
        "      token: synthetic-login-token\n      issuer: https://platform.deepseek.com\n",
        encoding="utf-8",
    )
    cred_file.chmod(0o600)
    storage_file = storages / "state.json"
    storage_file.write_text('{"synthetic": true}\n', encoding="utf-8")
    return owner_home, cred_file, storage_file


def _registry_with_dsh_login(
    tmp_path: Path,
    fake_dsh,
    *,
    provider_name: str = "deepseek-login",
    provider_patch: dict | None = None,
    **settings_overrides,
) -> Registry:
    provider: dict[str, Any] = {
        "driver": "dsh",
        "auth": "login",
        "vendor": "deepseek",
        "binary": fake_dsh.path,
        "max_agents": 1,
        "experimental": True,
    }
    if provider_patch:
        provider.update(provider_patch)
    settings = _dsh_settings(
        tmp_path,
        providers={provider_name: provider},
        run_timeout=30,
        **settings_overrides,
    )
    return Registry(settings, start_reaper=False)


# --- Config opt-in & L1: generated login patch -------------------------------


def test_dsh_auth_login_config_loads_without_api_key_and_rejects_api_key_env(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.toml"
    valid.write_text(
        '[core]\ndefault_provider = "dsh_login"\n\n'
        '[providers.dsh_login]\n'
        'driver = "dsh"\n'
        'auth = "login"\n'
        'max_agents = 1\n'
        'experimental = true\n',
        encoding="utf-8",
    )
    settings = config.load(extra=valid)
    cfg = settings.provider("dsh_login")
    assert cfg.driver == "dsh"
    assert cfg.extra.get("auth") == "login"
    assert cfg.api_key_envs == ()
    assert cfg.api_key_default is None
    assert cfg.api_key() is None
    assert cfg.unavailable() is None
    assert cfg.as_dict()["auth"] == "login"
    assert cfg.as_dict()["has_api_key"] is False
    assert not any("API key" in w for w in settings.warnings)

    invalid = tmp_path / "invalid-key-login.toml"
    invalid.write_text(
        '[providers.dsh_login]\n'
        'driver = "dsh"\n'
        'auth = "login"\n'
        'api_key_env = "DEEPSEEK_API_KEY"\n',
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="cannot set api_key_env"):
        config.load(extra=invalid)


def test_login_patch_generated_in_agent_dir_with_real_cordis_ids_and_keys(
    tmp_path: Path, monkeypatch
) -> None:
    """L1: login patch lives in the agent dir, re-enables the 3 headless-disabled
    rows with the owner credential file path, and selects `deepseek-account`."""
    owner_home, cred_file, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_login_cfg(model="deepseek-v4-pro")

    process = driver.spawn(
        cfg, settings, "agent-login-1", "do the task", ws, Session(provider="deepseek-login"), None
    )

    assert "--model" not in process.argv
    patches = _patch_values(process.argv)
    assert any("dsh-guard" in p for p in patches), f"guard patch missing from {process.argv}"
    generated = [Path(p).resolve() for p in patches if "dsh-guard" not in p]
    assert generated, f"expected generated login patch in --patch flags: {process.argv}"

    agent_dir = (settings.session_root / "agents" / "agent-login-1").resolve()
    for patch_file in generated:
        assert patch_file.is_file(), f"generated patch does not exist: {patch_file}"
        assert patch_file.is_relative_to(agent_dir), (
            f"login patch {patch_file} must live inside agent dir {agent_dir}"
        )
        assert not patch_file.is_relative_to(ws.resolve())

    # Verify effective Cordis row overrides (citations in module docstring):
    # - dsh-headless/cordis.patch.yml:38-46 disables credentials, authorization, deepseek-account
    # - dsh-credentials-local/README.md:36-47 & src/index.ts:65,90 uses config.path
    # - dsh-base/cordis.patch.yml:90-94 & dsh-llm-deepseek-account/src/index.ts:18 uses
    #   agent-default-model with config.provider = 'deepseek-account' and config.model
    rows = _effective_patch_rows(process.argv)
    assert "credentials" in rows, f"login patch must re-enable 'credentials': {rows}"
    assert rows["credentials"].get("disabled") is False
    cred_cfg = rows["credentials"].get("config")
    assert isinstance(cred_cfg, dict), f"'credentials' row must set config: {rows['credentials']}"
    assert "path" in cred_cfg, (
        f"'credentials' config must point `path` at owner .credentials.yaml: {cred_cfg}"
    )
    assert Path(str(cred_cfg["path"])).expanduser().resolve() == cred_file.resolve()

    assert "authorization" in rows, f"login patch must re-enable 'authorization': {rows}"
    assert rows["authorization"].get("disabled") is False

    assert "deepseek-account" in rows, f"login patch must re-enable 'deepseek-account': {rows}"
    assert rows["deepseek-account"].get("disabled") is False

    assert "agent-default-model" in rows, (
        f"login patch must target 'agent-default-model' for deepseek-account route: {rows}"
    )
    model_cfg = rows["agent-default-model"].get("config")
    assert isinstance(model_cfg, dict)
    assert model_cfg.get("provider") == "deepseek-account"
    assert model_cfg.get("model") == "deepseek-v4-pro"


def test_login_patch_per_call_model_overrides_config_model_and_keeps_account_route(
    tmp_path: Path, monkeypatch
) -> None:
    """L1: per-call model override must not clobber `provider: deepseek-account`
    (Cordis replaces `agent-default-model.config` wholesale per cordis-plugin-include/src/patch.ts:102-105)."""
    owner_home, _, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_login_cfg(model="deepseek-flash")

    process = driver.spawn(
        cfg,
        settings,
        "agent-login-2",
        "task",
        ws,
        Session(provider="deepseek-login"),
        "deepseek-v4-pro",
    )

    assert "--model" not in process.argv
    rows = _effective_patch_rows(process.argv)
    model_cfg = rows["agent-default-model"]["config"]
    assert model_cfg["provider"] == "deepseek-account"
    assert model_cfg["model"] == "deepseek-v4-pro"


def test_login_patch_without_configured_model_still_sets_account_route_and_required_model(
    tmp_path: Path, monkeypatch
) -> None:
    """L1: `dsh-agent-default-model/src/index.ts:51-55` requires both `provider`
    and `model`, so even with `model=None` the patch must supply both."""
    owner_home, _, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_login_cfg(model=None)

    process = driver.spawn(
        cfg, settings, "agent-login-3", "task", ws, Session(provider="deepseek-login"), None
    )

    rows = _effective_patch_rows(process.argv)
    assert rows["credentials"]["disabled"] is False
    assert rows["authorization"]["disabled"] is False
    assert rows["deepseek-account"]["disabled"] is False
    model_cfg = rows["agent-default-model"]["config"]
    assert model_cfg["provider"] == "deepseek-account"
    assert isinstance(model_cfg.get("model"), str) and model_cfg["model"].strip()


# --- L2: child env has no DEEPSEEK_API_KEY and no key value ------------------


def test_login_env_omits_deepseek_api_key_and_all_secret_values(
    tmp_path: Path, monkeypatch
) -> None:
    """L2: login mode exports no `DEEPSEEK_API_KEY` (not even empty) and no key
    value of any kind, while sibling API-key providers keep their own key."""
    owner_home, _, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    secret_vars = {
        "DEEPSEEK_API_KEY": "sk-ambient-deepseek-secret",
        "OTHER_DS_KEY": "sk-sibling-deepseek-secret",
        "GLM_API_KEY": "sk-parent-glm-secret",
        "ANTHROPIC_AUTH_TOKEN": "sk-parent-anthropic-token",
        "ANTHROPIC_API_KEY": "sk-parent-anthropic-api",
        "OPENAI_API_KEY": "sk-parent-openai-secret",
        "AWS_SECRET_ACCESS_KEY": "parent-aws-secret-value",
        "GH_TOKEN": "ghp_parent_secret_token",
        "DB_PASSWORD": "parent-db-password-value",
    }
    for key, value in secret_vars.items():
        monkeypatch.setenv(key, value)

    settings = _dsh_settings(
        tmp_path,
        providers={
            "deepseek-login": {
                "driver": "dsh",
                "auth": "login",
                "vendor": "deepseek",
                "binary": "/fake/bin/dsh",
                "max_agents": 1,
                "experimental": True,
            },
            "deepseek-api": {
                "driver": "dsh",
                "vendor": "deepseek",
                "binary": "/fake/bin/dsh",
                "api_key_env": "OTHER_DS_KEY",
                "experimental": True,
            },
        },
    )
    driver = _driver()
    login_cfg = settings.provider("deepseek-login")
    api_cfg = settings.provider("deepseek-api")

    login_env = driver.env(settings, "a-login", login_cfg, Session(provider="deepseek-login"))
    api_env = driver.env(settings, "a-api", api_cfg, Session(provider="deepseek-api"))

    assert "DEEPSEEK_API_KEY" not in login_env
    for secret_name, secret_val in secret_vars.items():
        assert secret_name not in login_env, f"secret env {secret_name!r} leaked into login env"
        assert all(secret_val not in str(v) for v in login_env.values()), (
            f"secret value for {secret_name!r} leaked into login env values"
        )

    # Sibling API-key mode still receives its mapped key unchanged.
    assert api_env["DEEPSEEK_API_KEY"] == "sk-sibling-deepseek-secret"


def test_login_spawned_child_receives_no_deepseek_api_key_in_env(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """L2 (end-to-end): spawned login child has no `DEEPSEEK_API_KEY` at all."""
    owner_home, _, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-never-reach-login-child")
    monkeypatch.setenv("GLM_API_KEY", "sk-must-never-reach-login-child-glm")

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_login_cfg(binary=fake_dsh.path)

    process = driver.spawn(
        cfg, settings, "a-login-run", "hello", ws, Session(provider="deepseek-login"), None
    )
    events = list(process.events())
    assert any(e.get("type") == "result" and not e.get("is_error") for e in events)

    calls = fake_dsh.calls()
    assert len(calls) == 1
    assert "DEEPSEEK_API_KEY" not in calls[0]["env_keys"]
    assert calls[0]["env"]["DEEPSEEK_API_KEY"] is None
    assert "GLM_API_KEY" not in calls[0]["env_keys"]


# --- L3: owner DSH home vs isolated child DSH_HOME ---------------------------


def test_owner_dsh_home_uses_env_override_while_child_dsh_home_stays_in_session_root(
    tmp_path: Path, monkeypatch
) -> None:
    """L3: `DSH_OWNER_HOME` selects the owner credential store while the child's
    own `DSH_HOME` stays isolated under `settings.session_root`."""
    owner_home, cred_file, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_login_cfg()

    process = driver.spawn(
        cfg, settings, "a-home-env", "task", ws, Session(provider="deepseek-login"), None
    )

    child_dsh_home = Path(process.env["DSH_HOME"]).resolve()
    assert child_dsh_home.is_relative_to(settings.session_root.resolve())
    assert not child_dsh_home.is_relative_to(owner_home.resolve())
    assert child_dsh_home != owner_home.resolve()
    assert not child_dsh_home.is_relative_to(ws.resolve())
    assert "DSH_OWNER_HOME" not in process.env

    rows = _effective_patch_rows(process.argv)
    assert Path(str(rows["credentials"]["config"]["path"])).resolve() == cred_file.resolve()


def test_owner_dsh_home_defaults_to_home_dot_dsh_when_env_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """L3: when `DSH_OWNER_HOME` is unset, owner DSH home defaults to `~/.dsh`,
    and child `DSH_HOME` still stays isolated under `settings.session_root`."""
    fake_home = tmp_path / "user-home"
    default_owner_dsh = fake_home / ".dsh"
    default_owner_dsh.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    ws = _workspace(tmp_path)
    cfg = _dsh_login_cfg()

    process = driver.spawn(
        cfg, settings, "a-home-default", "task", ws, Session(provider="deepseek-login"), None
    )

    child_dsh_home = Path(process.env["DSH_HOME"]).resolve()
    assert child_dsh_home.is_relative_to(settings.session_root.resolve())
    assert child_dsh_home != default_owner_dsh.resolve()
    assert not child_dsh_home.is_relative_to(default_owner_dsh.resolve())

    rows = _effective_patch_rows(process.argv)
    expected_cred = (default_owner_dsh / ".credentials.yaml").resolve()
    assert Path(str(rows["credentials"]["config"]["path"])).resolve() == expected_cred


def test_login_run_keeps_owner_dsh_home_isolated_and_untouched(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """L3 (end-to-end): child process sees session-root `DSH_HOME` and never
    modifies synthetic files in the owner DSH home."""
    owner_home, cred_file, storage_file = _make_synthetic_owner_home(tmp_path)
    orig_cred = cred_file.read_text(encoding="utf-8")
    orig_storage = storage_file.read_text(encoding="utf-8")
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))

    ws = _workspace(tmp_path)
    reg = _registry_with_dsh_login(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d-login", ws, provider="deepseek-login")
        assert agent.wait_ready(5) is None
        run = agent.submit("check status", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == COMPLETED, run.error
    finally:
        reg.shutdown()

    calls = fake_dsh.calls()
    assert len(calls) == 1
    child_dsh_home = Path(calls[0]["env"]["DSH_HOME"]).resolve()
    assert child_dsh_home.is_relative_to((tmp_path / "sessions").resolve())
    assert child_dsh_home != owner_home.resolve()
    assert cred_file.read_text(encoding="utf-8") == orig_cred
    assert storage_file.read_text(encoding="utf-8") == orig_storage


# --- L4: guard protects owner DSH home during run & releases after -----------


def test_login_protects_owner_dsh_home_in_classifier_during_run_and_releases_after(
    tmp_path: Path, monkeypatch
) -> None:
    """L4 (classifier path, mirroring `tests/test_claude_auth.py`): for the whole
    run, worker reads and writes under the owner DSH home (`.credentials.yaml`
    and `storages/`) are denied; protection is released after the run even on error."""
    owner_home, cred_file, storage_file = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    ws = _workspace(tmp_path)
    readme = ws / "README.md"
    readme.write_text("# ws\n", encoding="utf-8")

    # Sanity check baseline before the login run starts: `storages/state.json`
    # under a custom `DSH_OWNER_HOME` is not yet protected.
    assert classify("Read", {"file_path": str(storage_file)}, ws).action == ALLOW
    assert classify("Write", {"file_path": str(storage_file)}, ws).action == ESCALATE

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_login_cfg()
    process = driver.spawn(
        cfg, settings, "guard-login-1", "prompt", ws, Session(provider="deepseek-login"), None
    )

    checked = False

    def fail_after_protection(*args, **kwargs):
        nonlocal checked
        assert classify("Read", {"file_path": str(cred_file)}, ws).action == DENY
        assert classify("Write", {"file_path": str(cred_file)}, ws).action == DENY
        assert classify("Read", {"file_path": str(storage_file)}, ws).action == DENY
        assert classify("Write", {"file_path": str(storage_file)}, ws).action == DENY
        assert (
            classify(
                "Edit",
                {"file_path": str(storage_file), "old_string": "true", "new_string": "false"},
                ws,
            ).action
            == DENY
        )
        assert classify("Bash", {"command": f"cat {cred_file}"}, ws).action == DENY
        assert classify("Bash", {"command": f"cat {storage_file}"}, ws).action == DENY
        assert classify("Grep", {"pattern": "synthetic", "path": str(owner_home)}, ws).action == DENY
        assert classify("Read", {"file_path": str(readme)}, ws).action == ALLOW
        checked = True
        raise RuntimeError("fake dsh process start")

    monkeypatch.setattr(process, "_start", fail_after_protection)
    with pytest.raises(RuntimeError, match="fake dsh process start"):
        next(process.events())

    assert checked, "process.events() never reached _start() with protection active"
    # Protection is released once the run ends.
    assert classify("Write", {"file_path": str(storage_file)}, ws).action == ESCALATE
    assert classify("Read", {"file_path": str(storage_file)}, ws).action == ALLOW


def test_login_default_home_dot_dsh_is_protected_during_run(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """L4 (default `~/.dsh` path): when `DSH_OWNER_HOME` is unset, `~/.dsh`
    (`.credentials.yaml` and `storages/`) is denied during a normal run."""
    fake_home = tmp_path / "user-home"
    default_owner_dsh = fake_home / ".dsh"
    storages = default_owner_dsh / "storages"
    storages.mkdir(parents=True)
    cred_file = default_owner_dsh / ".credentials.yaml"
    cred_file.write_text("version: 1\n", encoding="utf-8")
    cred_file.chmod(0o600)
    storage_file = storages / "state.json"
    storage_file.write_text('{"synthetic": true}\n', encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DSH_OWNER_HOME", raising=False)

    ws = _workspace(tmp_path)
    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_login_cfg(binary=fake_dsh.path)
    process = driver.spawn(
        cfg, settings, "guard-login-default", "prompt", ws, Session(provider="deepseek-login"), None
    )

    real_start = process._start
    saw_protected = False

    def spy_start(*args, **kwargs):
        nonlocal saw_protected
        assert classify("Read", {"file_path": str(cred_file)}, ws).action == DENY
        assert classify("Write", {"file_path": str(cred_file)}, ws).action == DENY
        assert classify("Read", {"file_path": str(storage_file)}, ws).action == DENY
        assert classify("Write", {"file_path": str(storage_file)}, ws).action == DENY
        assert classify("Bash", {"command": f"cat {storage_file}"}, ws).action == DENY
        saw_protected = True
        return real_start(*args, **kwargs)

    monkeypatch.setattr(process, "_start", spy_start)
    events = list(process.events())

    assert saw_protected
    assert any(e.get("type") == "result" and not e.get("is_error") for e in events)


def test_login_protects_owner_dsh_home_through_bridge_during_run_and_releases_after(
    fake_dsh, tmp_path: Path, monkeypatch
) -> None:
    """L4 (bridge path): during a login run, the real DSH guard bridge ->
    `approval_hook.py` -> in-process `classify()` denies reads and writes under
    the owner DSH home (`.credentials.yaml` and `storages/`) while allowing
    ordinary workspace reads, and releases protection after the run finishes."""
    _find_node()
    entry = _require_bridge_entry()

    owner_home, cred_file, storage_file = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    ws = _workspace(tmp_path)
    readme = ws / "README.md"
    readme.write_text("# workspace readme\n", encoding="utf-8")

    sock_path = Path(f"/tmp/dsh-login-guard-{uuid.uuid4().hex[:8]}.sock")
    hook_cmd = " ".join(
        shlex.quote(p) for p in (sys.executable, str(APPROVAL_HOOK), "--agent", "dsh-login-bridge")
    )

    def bridge_eval(dsh_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return _run_node(
            APPLY_HARNESS_JS,
            cwd=ws,
            env={
                "TEST_BRIDGE_ENTRY": str(entry),
                "TEST_CWD": str(ws),
                "TEST_CALL_JSON": json.dumps({
                    "name": dsh_tool,
                    "arguments": arguments,
                    "sessionId": "session-dsh-login-bridge",
                }),
                "SUBAGENT_GUARD_HOOK": hook_cmd,
                "SUBAGENT_APPROVAL_SOCKET": str(sock_path),
            },
        )

    driver = _driver()
    settings = _dsh_settings(tmp_path)
    cfg = _dsh_login_cfg(binary=fake_dsh.path)
    process = driver.spawn(
        cfg, settings, "dsh-login-bridge", "task", ws, Session(provider="deepseek-login"), None
    )

    real_start = process._start
    during_run: dict[str, dict[str, Any]] = {}

    with _classification_socket(sock_path, ws, context={}):
        def check_bridge_while_active(*args, **kwargs):
            during_run["read_cred"] = bridge_eval("read", {"file_path": str(cred_file)})
            during_run["write_cred"] = bridge_eval(
                "write", {"file_path": str(cred_file), "content": "tampered"}
            )
            during_run["read_storage"] = bridge_eval("read", {"file_path": str(storage_file)})
            during_run["write_storage"] = bridge_eval(
                "write", {"file_path": str(storage_file), "content": "{}"}
            )
            during_run["bash_storage"] = bridge_eval("bash", {"command": f"cat {storage_file}"})
            during_run["read_workspace"] = bridge_eval("read", {"file_path": "README.md"})
            return real_start(*args, **kwargs)

        monkeypatch.setattr(process, "_start", check_bridge_while_active)
        events = list(process.events())
        after_run_storage = bridge_eval("read", {"file_path": str(storage_file)})

    assert any(e.get("type") == "result" and not e.get("is_error") for e in events)
    for label in ("read_cred", "write_cred", "read_storage", "write_storage", "bash_storage"):
        assert during_run[label]["nextCalled"] is False, f"{label} should be denied during run"
        assert during_run[label]["decision"]["kind"] == "deny", (
            f"{label} expected deny, got {during_run[label]}"
        )
    assert during_run["read_workspace"]["nextCalled"] is True
    assert during_run["read_workspace"]["decision"]["kind"] == "allow"

    # Once the run completes, temporary owner DSH home protection is released.
    assert after_run_storage["nextCalled"] is True
    assert after_run_storage["decision"]["kind"] == "allow"


# --- L5: ACCOUNT_SIGN_IN_REQUIRED is an auth refusal closing the lane --------


@pytest.mark.parametrize(
    ("fixture", "stderr_text"),
    [
        (
            "account_sign_in_required_error.jsonl",
            "dsh: ACCOUNT_SIGN_IN_REQUIRED: Sign in to DeepSeek to use the account provider. "
            "The request destination must allow account authentication.",
        ),
        (
            "account_sign_in_required_final.jsonl",
            "dsh: ACCOUNT_SIGN_IN_REQUIRED: Sign in to DeepSeek to use the account provider. "
            "The request destination must allow account authentication.",
        ),
    ],
    ids=["error-event", "final-turn-error"],
)
def test_account_sign_in_required_is_auth_refusal_that_closes_lane(
    fake_dsh, tmp_path: Path, monkeypatch, fixture: str, stderr_text: str
) -> None:
    """L5: `ACCOUNT_SIGN_IN_REQUIRED` (from `dsh-llm-deepseek-account/src/index.ts:36`)
    is classified as an auth refusal and closes the lane like `MISSING_CREDENTIAL`."""
    owner_home, _, _ = _make_synthetic_owner_home(tmp_path)
    monkeypatch.setenv("DSH_OWNER_HOME", str(owner_home))
    fake_dsh.use(fixture, exit_code=1, stderr=stderr_text)
    ws = _workspace(tmp_path)

    reg = _registry_with_dsh_login(tmp_path, fake_dsh)
    try:
        agent = reg.create_agent("d-login", ws, provider="deepseek-login", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.delegate("do it", verification="true")
        assert run.done.wait(10), f"run did not finish: {run.state} {run.error}"
        assert run.state == FAILED
        assert run.finish_reason == "refused"

        closed = reg.lane_state.closed("deepseek-login")
        assert closed is not None, f"expected ACCOUNT_SIGN_IN_REQUIRED to close lane; hops={run.hops}"
        assert "auth" in str(closed.get("code") or "").lower()
    finally:
        reg.shutdown()

    # Direct refusal() + router.close_until() contract.
    driver = _driver()
    cfg = _dsh_login_cfg()
    refusal = driver.refusal(
        cfg,
        [{"type": "result", "is_error": True, "subtype": "error", "error": stderr_text}],
    )
    assert refusal is not None
    assert "auth" in refusal.code.lower()
    assert router.close_until(refusal, balance_close_hours=6, throttle_close_minutes=15) is not None


# --- L6: example config `max_agents = 1` & `subagent doctor` warning ---------


def test_example_config_dsh_toml_has_login_provider_defaulting_to_max_agents_one() -> None:
    """L6: `examples/config.dsh.toml` declares a login provider alongside the
    API-key provider, defaulting to `max_agents = 1`."""
    assert EXAMPLE_CONFIG.is_file(), f"missing example config {EXAMPLE_CONFIG}"
    raw_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert not re.search(r"sk-[A-Za-z0-9]{10,}", raw_text)

    settings = config.load(extra=EXAMPLE_CONFIG)
    dsh_providers = [p for p in settings.providers.values() if p.driver == "dsh"]
    api_providers = [p for p in dsh_providers if p.extra.get("auth") != "login"]
    login_providers = [p for p in dsh_providers if p.extra.get("auth") == "login"]

    assert api_providers, f"expected an API-key dsh provider in {EXAMPLE_CONFIG}"
    assert login_providers, f"expected a login dsh provider in {EXAMPLE_CONFIG}"

    login_cfg = login_providers[0]
    assert login_cfg.driver == "dsh"
    assert login_cfg.experimental is True
    assert login_cfg.api_key_envs == ()
    assert login_cfg.api_key_default is None
    assert login_cfg.max_agents == 1


def test_doctor_warns_when_dsh_login_provider_has_max_agents_gt_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """L6: `subagent doctor` warns when a `dsh` login provider has `max_agents > 1`."""
    xdg = tmp_path / "xdg"
    config_path = xdg / "subagent" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[core]\ndefault_provider = "dsh_login"\n\n'
        '[providers.dsh_login]\n'
        'driver = "dsh"\n'
        'auth = "login"\n'
        'max_agents = 2\n'
        'experimental = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert cli_main(["doctor", "--provider", "dsh_login", "--no-probe", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["providers"][0]
    assert row.get("warning"), f"expected doctor warning for dsh login max_agents > 1: {row}"
    assert "max_agents" in row["warning"].lower()

    assert cli_main(["doctor", "--provider", "dsh_login", "--no-probe"]) == 0
    captured = capsys.readouterr()
    assert "max_agents" in captured.err.lower()


def test_doctor_does_not_warn_about_max_agents_when_dsh_login_is_one_or_api_key(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """L6: no `max_agents` warning when a `dsh` login provider has `max_agents = 1`
    or when an API-key `dsh` provider has `max_agents > 1`."""
    xdg = tmp_path / "xdg"
    config_path = xdg / "subagent" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[core]\ndefault_provider = "dsh_login_one"\n\n'
        '[providers.dsh_login_one]\n'
        'driver = "dsh"\n'
        'auth = "login"\n'
        'max_agents = 1\n'
        'experimental = true\n\n'
        '[providers.dsh_api]\n'
        'driver = "dsh"\n'
        'api_key_env = "DEEPSEEK_API_KEY"\n'
        'max_agents = 4\n'
        'experimental = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert cli_main(["doctor", "--no-probe", "--json"]) == 0
    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)["providers"]}

    assert "max_agents" not in (rows["dsh_login_one"].get("warning") or "").lower()
    assert "max_agents" not in (rows["dsh_api"].get("warning") or "").lower()
