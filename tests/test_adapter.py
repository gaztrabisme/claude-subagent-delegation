"""The per-provider request adapter (U-B1) and the local wake-up (U-B2)."""

from __future__ import annotations

import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from subagent import adapter, config, health

from .conftest import make_settings, provider_cfg

# The shape Claude Code 2.1.276 sends, cut down: a system entry after the
# first user message, as the last message.
CLAUDE_CODE_BODY = {
    "model": "qwen3.8-27b",
    "stream": True,
    "system": [{"type": "text", "text": "You are Claude Code."},
               {"type": "text", "text": "tools...", "cache_control": {"type": "ephemeral"}}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x"},
                                     {"type": "text", "text": "say hi"}]},
        {"role": "system", "content": [{"type": "text", "text": "# Environment\ncwd: /ws"}]},
    ],
}

SAMPLING_SETTINGS = {
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.05,
    "max_tokens": 1024,
    "stop_sequences": ["STOP"],
    "seed": 0,
    "presence_penalty": 0,
    "frequency_penalty": None,
    "repetition_penalty": 1.1,
    "thinking": {"type": "enabled", "budget_tokens": 512},
}


@pytest.fixture
def sampling_body():
    return {
        **deepcopy(CLAUDE_CODE_BODY),
        "tools": [{"name": "example", "input_schema": {
            "type": "object", "properties": {"temperature": {"type": "number"}},
        }}],
        "metadata": {"user_id": "example", "seed": 42},
        "custom": {"max_tokens": 10},
        **deepcopy(SAMPLING_SETTINGS),
    }


@pytest.fixture(autouse=True)
def _stop_adapters():
    yield
    adapter.shutdown()


def test_adapter_fold_system_moves_trailing_system_into_the_user_turn():
    out = adapter.fold_system(CLAUDE_CODE_BODY)
    assert [m["role"] for m in out["messages"]] == ["user"]
    texts = [b["text"] for b in out["messages"][0]["content"]]
    assert texts == ["<system-reminder>x", "say hi", "# Environment\ncwd: /ws"]
    assert out["system"] == CLAUDE_CODE_BODY["system"]  # top-level system first, untouched
    assert CLAUDE_CODE_BODY["messages"][1]["role"] == "system"  # input not mutated


def test_adapter_fold_system_goes_to_the_next_user_turn_after_tool_results():
    body = {"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                           "input": {}}]},
        {"role": "system", "content": "reminder"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ok"}, {"type": "text", "text": "more"}]},
    ]}
    out = adapter.fold_system(body)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in out["messages"][2]["content"]] == ["tool_result", "text", "text"]
    assert out["messages"][2]["content"][1]["text"] == "reminder"
    assert out["messages"][0]["content"] == "go"


def test_adapter_fold_system_without_a_user_turn_joins_top_level_system():
    out = adapter.fold_system({"system": "base", "messages": [
        {"role": "system", "content": "late"}]})
    assert out["messages"] == []
    assert [b["text"] for b in out["system"]] == ["base", "late"]


def test_adapter_passes_other_bodies_byte_for_byte():
    plain = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    assert adapter.adapt_body("fold_system", "/v1/messages", plain) is plain
    raw = json.dumps(CLAUDE_CODE_BODY).encode()
    assert adapter.adapt_body("fold_system", "/v1/models", raw) is raw
    assert adapter.adapt_body("fold_system", "/v1/messages", b"not json") == b"not json"
    folded = json.loads(adapter.adapt_body("fold_system", "/v1/messages?beta=true", raw))
    assert all(m["role"] != "system" for m in folded["messages"])


def test_adapter_strip_sampling_pins_limit_and_removes_other_generation_settings(sampling_body):
    original = deepcopy(sampling_body)
    expected = {key: value for key, value in original.items() if key not in SAMPLING_SETTINGS}
    expected["max_tokens"] = 262144
    out = adapter.strip_sampling(sampling_body, 262144)
    assert out == expected
    assert sampling_body == original
    for key in expected.keys() - {"max_tokens"}:
        assert out[key] is sampling_body[key]


@pytest.mark.parametrize("key", SAMPLING_SETTINGS)
def test_adapter_strip_sampling_removes_each_setting_individually(key):
    body = {**CLAUDE_CODE_BODY, key: SAMPLING_SETTINGS[key]}
    expected = {**CLAUDE_CODE_BODY, "max_tokens": 262144}
    assert adapter.strip_sampling(body, 262144) == expected


def test_adapter_strip_sampling_without_limit_preserves_client_max_tokens(sampling_body):
    expected = {key: value for key, value in sampling_body.items()
                if key not in SAMPLING_SETTINGS or key == "max_tokens"}
    assert adapter.strip_sampling(sampling_body) == expected
    body = {**CLAUDE_CODE_BODY, "max_tokens": 1024}
    assert adapter.strip_sampling(body) is body
    assert adapter.strip_sampling(body, 1024) is body


def test_adapter_strip_sampling_without_settings_preserves_identity_and_bytes():
    assert adapter.strip_sampling(CLAUDE_CODE_BODY) is CLAUDE_CODE_BODY
    raw = json.dumps(CLAUDE_CODE_BODY, indent=2).encode() + b"\n"
    assert adapter.adapt_body("strip_sampling", "/v1/messages", raw) is raw
    raw = b'{ "messages": [] }\n'
    assert adapter.adapt_body("fold_system,strip_sampling", "/v1/messages", raw) is raw


def test_adapter_strip_sampling_preserves_escaped_surrogates():
    raw = b'{"temperature":0.7,"metadata":{"user_id":"\\ud800"}}'
    assert json.loads(adapter.adapt_body("strip_sampling", "/v1/messages", raw)) == {
        "metadata": {"user_id": "\ud800"},
    }


@pytest.mark.parametrize("raw", [b"not json", b"", b"null", b"[]", b'"text"', b"\xff"])
def test_adapter_strip_sampling_passes_non_object_bodies_through(raw):
    assert adapter.adapt_body("strip_sampling", "/v1/messages", raw) is raw


@pytest.mark.parametrize("path", [
    "/v1/models", "/v1/chat/completions", "/v1/messages/count_tokens",
    "/other/v1/messages", "/v1/messages/other",
])
def test_adapter_strip_sampling_passes_other_paths_through(path, sampling_body):
    raw = json.dumps(sampling_body).encode()
    assert adapter.adapt_body("strip_sampling", path, raw) is raw


def test_adapter_sequence_folds_system_then_strips_sampling(sampling_body):
    raw = json.dumps(sampling_body).encode()
    out = json.loads(adapter.adapt_body(
        "fold_system, strip_sampling", "/v1/messages?beta=true", raw, 262144))
    expected = adapter.fold_system({key: value for key, value in sampling_body.items()
                                    if key not in SAMPLING_SETTINGS})
    expected["max_tokens"] = 262144
    assert out == expected


def test_adapter_sequence_preserves_fold_system_on_count_tokens(sampling_body):
    raw = json.dumps(sampling_body).encode()
    out = json.loads(adapter.adapt_body(
        "fold_system,strip_sampling", "/v1/messages/count_tokens", raw, 262144))
    assert out == adapter.fold_system(sampling_body)


def test_adapter_sequence_applies_transforms_in_listed_order(monkeypatch):
    monkeypatch.setitem(adapter.ADAPTERS, "first", lambda body: {**body, "step": "first"})
    monkeypatch.setitem(adapter.ADAPTERS, "second", lambda body: {**body, "step": body["step"] + ",second"})
    assert json.loads(adapter.adapt_body("first,second", "/v1/messages", b"{}")) == {
        "step": "first,second",
    }


@pytest.mark.parametrize("name", ["unknown", "fold_system,unknown", "unknown,strip_sampling"])
def test_adapter_proxy_refuses_unknown_names(name):
    with pytest.raises(ValueError, match="unknown adapter 'unknown'; known:"):
        adapter.AdapterProxy(name, "http://127.0.0.1")


@pytest.mark.parametrize("name", ["strip_sampling", "fold_system,strip_sampling"])
def test_adapter_provider_config_accepts_adapter_names(tmp_path, name):
    settings = make_settings(tmp_path, providers={"omlx": {
        "driver": "claude", "base_url": "http://127.0.0.1", "adapter": name,
    }})
    assert settings.provider("omlx").adapter == name


def _post_body(url, body, *, path="/v1/messages", headers=None):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("POST", path, body=json.dumps(body), headers={
            "content-type": "application/json", **(headers or {}),
        })
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read()) == {"type": "message"}
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["strip_sampling", "fold_system,strip_sampling"])
@pytest.mark.parametrize("auth_headers", [
    {"Authorization": "Bearer local"}, {"x-api-key": "local"},
    {"aUtHoRiZaTiOn": "Bearer local", "X-API-Key": "local"},
])
def test_adapter_proxy_delivers_pinned_body_using_omlx_example(
    mock_endpoint, sampling_body, name, auth_headers,
):
    path = "/omlx/v1/messages?beta=true"
    mock_endpoint.routes[path] = (200, {"type": "message"})
    mock_endpoint.routes["/omlx/v1/models"] = (200, {"data": [
        {"id": sampling_body["model"], "max_model_len": 262144},
    ]})
    example = Path(__file__).resolve().parents[1] / "examples" / "config.omlx.toml"
    cfg = config.load(extra=example).provider("omlx")
    assert cfg.adapter == "strip_sampling"
    # Preserve the example's upstream path while substituting only the origin.
    cfg = replace(cfg, adapter=name,
                  base_url=mock_endpoint.url("omlx") + urlsplit(cfg.base_url).path)
    _post_body(adapter.child_base_url(cfg), sampling_body, path="/v1/messages?beta=true",
               headers={**auth_headers, "X-Unrelated": "message-only"})
    expected = {key: value for key, value in sampling_body.items() if key not in SAMPLING_SETTINGS}
    expected["max_tokens"] = 262144
    if name.startswith("fold_system"):
        expected = adapter.fold_system(expected)
    assert json.loads(mock_endpoint.bodies[path]) == expected
    model_headers = {key.lower(): value
                     for key, value in mock_endpoint.headers["/omlx/v1/models"].items()}
    for key, value in auth_headers.items():
        assert model_headers[key.lower()] == value
    assert "x-unrelated" not in model_headers
    assert mock_endpoint.hits["/omlx/v1/models"] == 1


@pytest.mark.parametrize("models_response", [
    (200, {"data": [{"id": "another-model", "max_model_len": 262144}]}),
    (503, {"error": "unavailable"}),
    (200, {"data": [{"id": CLAUDE_CODE_BODY["model"]}]}),
    (200, b"not json"),
    (200, {"data": None}),
    (200, {"data": [None, {"id": CLAUDE_CODE_BODY["model"], "max_model_len": 0}]}),
])
def test_adapter_proxy_preserves_client_limit_when_discovery_fails(
    mock_endpoint, sampling_body, models_response, caplog,
):
    mock_endpoint.routes["/omlx/v1/models"] = models_response
    mock_endpoint.routes["/omlx/v1/messages"] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    _post_body(url, sampling_body, headers={"Authorization": "Bearer local"})
    expected = {key: value for key, value in sampling_body.items()
                if key not in SAMPLING_SETTINGS or key == "max_tokens"}
    assert json.loads(mock_endpoint.bodies["/omlx/v1/messages"]) == expected
    assert mock_endpoint.hits["/omlx/v1/models"] == 1
    assert len(caplog.records) == 1
    assert sampling_body["model"] in caplog.text
    assert "Bearer local" not in caplog.text
    assert "You are Claude Code." not in caplog.text


@pytest.mark.parametrize("error", [OSError, http.client.HTTPException])
def test_adapter_proxy_forwards_message_after_discovery_transport_error(
    mock_endpoint, sampling_body, monkeypatch, caplog, error,
):
    request = http.client.HTTPConnection.request

    def broken_discovery(self, method, url, *args, **kwargs):
        if url == "/omlx/v1/models":
            raise error("sensitive upstream detail")
        return request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "request", broken_discovery)
    mock_endpoint.routes["/omlx/v1/messages"] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    _post_body(url, sampling_body)
    expected = {key: value for key, value in sampling_body.items()
                if key not in SAMPLING_SETTINGS or key == "max_tokens"}
    assert json.loads(mock_endpoint.bodies["/omlx/v1/messages"]) == expected
    assert len(caplog.records) == 1 and sampling_body["model"] in caplog.text
    assert "sensitive upstream detail" not in caplog.text


def test_adapter_proxy_uses_cached_model_map_and_refreshes_once_for_new_id(
    mock_endpoint, sampling_body,
):
    models_path = "/omlx/v1/models"
    messages_path = "/omlx/v1/messages"
    models = [{"id": sampling_body["model"], "max_model_len": 262144},
              {"id": "other-cached-model", "max_model_len": 131072}]
    mock_endpoint.routes[models_path] = (200, {"data": models})
    mock_endpoint.routes[messages_path] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    for model, limit in [(sampling_body["model"], 262144), (sampling_body["model"], 262144),
                         ("other-cached-model", 131072)]:
        _post_body(url, {**sampling_body, "model": model})
        assert json.loads(mock_endpoint.bodies[messages_path])["max_tokens"] == limit
        assert mock_endpoint.hits[models_path] == 1
    models.append({"id": "new-model", "max_model_len": 65536})
    for _ in range(2):
        _post_body(url, {**sampling_body, "model": "new-model"})
        assert json.loads(mock_endpoint.bodies[messages_path])["max_tokens"] == 65536
        assert mock_endpoint.hits[models_path] == 2
    # Each proxy owns its own map, even when the backend is shared.
    other_url = adapter.proxy_url("fold_system,strip_sampling", mock_endpoint.url("omlx"))
    _post_body(other_url, sampling_body)
    assert mock_endpoint.hits[models_path] == 3


def test_adapter_proxy_keeps_cached_limits_after_a_failed_refresh(mock_endpoint, sampling_body):
    models_path = "/omlx/v1/models"
    messages_path = "/omlx/v1/messages"
    mock_endpoint.routes[models_path] = (200, {"data": [
        {"id": sampling_body["model"], "max_model_len": 262144},
    ]})
    mock_endpoint.routes[messages_path] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    _post_body(url, sampling_body)
    assert mock_endpoint.hits[models_path] == 1
    mock_endpoint.routes[models_path] = (503, {"error": "unavailable"})
    _post_body(url, {**sampling_body, "model": "not-listed"})
    assert json.loads(mock_endpoint.bodies[messages_path])["max_tokens"] == 1024
    assert mock_endpoint.hits[models_path] == 2
    _post_body(url, sampling_body)
    assert json.loads(mock_endpoint.bodies[messages_path])["max_tokens"] == 262144
    assert mock_endpoint.hits[models_path] == 2


def test_adapter_proxy_shares_failed_discovery_between_concurrent_requests(
    mock_endpoint, sampling_body,
):
    models_path = "/omlx/v1/models"
    mock_endpoint.routes[models_path] = (503, {"error": "unavailable"})
    mock_endpoint.on_hit[models_path] = lambda: time.sleep(0.15)
    mock_endpoint.routes["/omlx/v1/messages"] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    start = threading.Barrier(4)

    def post():
        start.wait(timeout=5)
        _post_body(url, sampling_body)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(post) for _ in range(4)]
        for future in futures:
            future.result(timeout=5)
    assert mock_endpoint.hits[models_path] == 1
    assert mock_endpoint.hits["/omlx/v1/messages"] == 4


def test_adapter_proxy_cache_hit_does_not_wait_for_other_model_discovery(
    mock_endpoint, sampling_body,
):
    models_path = "/omlx/v1/models"
    mock_endpoint.routes[models_path] = (200, {"data": [
        {"id": sampling_body["model"], "max_model_len": 262144},
    ]})
    mock_endpoint.routes["/omlx/v1/messages"] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    _post_body(url, sampling_body)
    entered, release = threading.Event(), threading.Event()

    def pending_discovery():
        entered.set()
        release.wait(timeout=5)

    mock_endpoint.routes[models_path] = (503, {"error": "unavailable"})
    mock_endpoint.on_hit[models_path] = pending_discovery
    with ThreadPoolExecutor(max_workers=2) as pool:
        missing = pool.submit(_post_body, url, {**sampling_body, "model": "new-model"})
        try:
            assert entered.wait(timeout=5)
            pool.submit(_post_body, url, sampling_body).result(timeout=2)
        finally:
            release.set()
        missing.result(timeout=5)
    assert mock_endpoint.hits[models_path] == 2


def test_adapter_proxy_count_tokens_never_discovers_or_strips_sampling(
    mock_endpoint, sampling_body,
):
    path = "/omlx/v1/messages/count_tokens"
    mock_endpoint.routes[path] = (200, {"type": "message"})
    url = adapter.proxy_url("strip_sampling", mock_endpoint.url("omlx"))
    _post_body(url, sampling_body, path="/v1/messages/count_tokens")
    assert json.loads(mock_endpoint.bodies[path]) == sampling_body
    assert mock_endpoint.hits["/omlx/v1/models"] == 0


@pytest.mark.parametrize("name,status", [
    ("fold_system", 200),
    ("fold_system,strip_sampling", 502),
    ("strip_sampling,fold_system", 502),
])
def test_adapter_proxy_never_forwards_sampling_when_a_transform_fails(
    mock_endpoint, sampling_body, monkeypatch, name, status,
):
    def broken_transform(body):
        raise ValueError("transform failed")

    monkeypatch.setitem(adapter.ADAPTERS, "fold_system", broken_transform)
    path = "/omlx/v1/messages"
    mock_endpoint.routes[path] = (200, {"type": "message"})
    parts = urlsplit(adapter.proxy_url(name, mock_endpoint.url("omlx")))
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("POST", "/v1/messages", body=json.dumps(sampling_body))
        resp = conn.getresponse()
        assert resp.status == status
        resp.read()
    finally:
        conn.close()
    assert mock_endpoint.hits[path] == (1 if status == 200 else 0)


def test_adapter_proxy_rewrites_and_forwards_headers(mock_endpoint):
    mock_endpoint.routes["/gpubox/v1/messages?beta=true"] = (200, {"type": "message"})
    url = adapter.proxy_url("fold_system", mock_endpoint.url("gpubox"))
    assert url.startswith("http://127.0.0.1:")
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("POST", "/v1/messages?beta=true", body=json.dumps(CLAUDE_CODE_BODY),
                 headers={"x-api-key": "local", "content-type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200 and json.loads(resp.read()) == {"type": "message"}
    conn.close()
    sent = json.loads(mock_endpoint.bodies["/gpubox/v1/messages?beta=true"])
    assert [m["role"] for m in sent["messages"]] == ["user"]
    headers = {k.lower(): v for k, v in mock_endpoint.headers["/gpubox/v1/messages?beta=true"].items()}
    assert headers["x-api-key"] == "local"
    # One proxy per (adapter, upstream).
    assert adapter.proxy_url("fold_system", mock_endpoint.url("gpubox") + "/") == url


def test_adapter_gpubox_provider_defaults_to_fold_system_and_can_be_turned_off(tmp_path: Path):
    settings = make_settings(tmp_path)
    assert settings.provider("gpubox").adapter == "fold_system"
    assert provider_cfg("gpubox", adapter="none").adapter is None
    assert all(cfg.adapter is None for name, cfg in settings.providers.items()
               if name != "gpubox")


def test_adapter_child_gets_the_proxy_url_while_the_lane_keeps_the_backend(tmp_path: Path):
    settings = make_settings(tmp_path)
    lane = replace(settings.provider("gpubox"), base_url="http://192.0.2.10:8080")
    env = settings.child_env("a1", lane)
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    written = json.loads(settings.hooks_config("a1", lane).read_text())
    assert written["env"]["ANTHROPIC_BASE_URL"] == env["ANTHROPIC_BASE_URL"]
    assert lane.base_url == "http://192.0.2.10:8080"  # health and telemetry still see gpubox
    glm = settings.provider("glm")
    assert settings.child_env("a2", glm)["ANTHROPIC_BASE_URL"] == glm.base_url


def test_adapter_warm_gpubox_wakes_through_the_proxy(mock_endpoint):
    base = mock_endpoint.url("gpubox")
    mock_endpoint.routes["/gpubox/health"] = (200, {"status": "ok", "backend": "stopped"})
    mock_endpoint.on_hit["/gpubox/v1/models"] = lambda: mock_endpoint.routes.__setitem__(
        "/gpubox/health", (200, {"status": "ok", "backend": "running"}))
    gate = health.warm(provider_cfg("gpubox"), base, 10.0, poll=0.05)
    assert gate.ok and gate.base_url == base
    assert mock_endpoint.hits["/gpubox/v1/models"] == 1


def test_adapter_warm_gpubox_is_bounded(mock_endpoint):
    base = mock_endpoint.url("gpubox")
    mock_endpoint.routes["/gpubox/health"] = (200, {"status": "ok", "backend": "stopped"})
    gate = health.warm(provider_cfg("gpubox"), base, 0.3, poll=0.05)
    assert not gate.ok and "not running" in gate.message
