"""Health gates for local providers, checked at dispatch.

A provider's `[health]` block decides what happens here:

  kind = "none"       no gate. Cloud providers: their refusals come back in
                      the child's own error.
  kind = "http"       GET `url`; healthy on 200.
  kind = "omlx"       GET `url` with the provider's key; healthy when it
                      answers 200 with status "ok". The model missing from
                      loaded_models is still healthy (the first request loads
                      it), but the result carries cold_load so the run can
                      allow for it.
  kind = "llamacpp"   the address is not fixed: `candidates` (base URLs) are
                      tried in order, after whatever `resolve_cmd` prints,
                      and the first whose /health answers 200 wins. A proxy
                      that stops llama.cpp when idle answers {"status": "ok",
                      "backend": "stopped"}: healthy, with cold_load set.
                      With `warm = true`, one request through that proxy
                      wakes the backend before a child is spawned, bounded by
                      `cold_load_seconds`.

Nothing here starts a model server itself. A provider whose server is down
fails its gate and the router moves on.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import log
from .providers.base import ProviderConfig

PROBE_TIMEOUT = 2.0
RESOLVE_TIMEOUT = 2.0
WARM_POLL_SECONDS = 2.0

KIND_NONE = "none"
KIND_HTTP = "http"
KIND_OMLX = "omlx"
KIND_LLAMACPP = "llamacpp"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True, slots=True)
class Health:
    """A gate's answer. base_url is the address the run should use."""

    ok: bool
    base_url: str | None
    message: str = ""
    cold_load: bool = False


def _http_get_once(url: str, headers: dict[str, str], timeout: float
                   ) -> tuple[int, bytes] | None:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (OSError, ValueError):
        return None


def _http_get(url: str, headers: dict[str, str] | None = None,
              timeout: float = PROBE_TIMEOUT) -> tuple[int, bytes] | None:
    """(status, body) for a GET, or None when nothing answered. No proxies.

    `timeout` bounds the whole call, name lookup and a slow-drip body
    included (urllib's own timeout is per socket operation). The request
    runs on a daemon thread that is abandoned when the bound passes.
    """
    box: list[tuple[int, bytes] | None] = []
    worker = threading.Thread(
        target=lambda: box.append(_http_get_once(url, headers or {}, timeout)),
        name="sam-health-probe", daemon=True,
    )
    worker.start()
    worker.join(timeout)
    return box[0] if box else None


def _run(argv: list[str], timeout: float = RESOLVE_TIMEOUT) -> str:
    """stdout of `argv`, or "" on any failure. Never raises."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("resolve command failed: %s", exc)
        return ""
    if out.returncode != 0:
        log.info("resolve command exited %d", out.returncode)
        return ""
    return out.stdout


def resolve_urls(cfg: ProviderConfig) -> list[str]:
    """Base URLs to probe for `cfg`, in order, without duplicates.

    `resolve_cmd` prints one base URL per line (an address that moves with
    DHCP is found this way); the configured `candidates` follow it.
    """
    spec = cfg.health
    found: list[str] = []
    if spec.resolve_cmd:
        for line in _run(list(spec.resolve_cmd)).splitlines():
            url = line.strip()
            if url:
                found.append(url.rstrip("/"))
    found.extend(url.rstrip("/") for url in spec.candidates)
    ordered: list[str] = []
    for url in found:
        if url not in ordered:
            ordered.append(url)
    return ordered


def _backend_stopped(body: bytes) -> bool:
    """True when a llama.cpp proxy says its backend is stopped (idle)."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("backend") == "stopped"


def _backend_running(body: bytes) -> bool:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("backend") == "running"


def check_llamacpp(cfg: ProviderConfig) -> Health:
    """Resolve the provider's base URL, or report that no host answered."""
    if cfg.base_url:
        # A fixed address: no resolution, but still gated.
        found = _http_get(f"{cfg.base_url}/health")
        if found is not None and found[0] == 200:
            return Health(True, cfg.base_url, cold_load=_backend_stopped(found[1]))
        return Health(False, None, f"{cfg.name}: {cfg.base_url}/health did not answer 200")
    urls = resolve_urls(cfg)
    for base in urls:
        found = _http_get(f"{base}/health")
        if found is not None and found[0] == 200:
            return Health(True, base, cold_load=_backend_stopped(found[1]))
    return Health(
        False, None,
        f"{cfg.name}: no host answered /health (tried {', '.join(urls) or 'nothing'})",
    )


def warm(cfg: ProviderConfig, base_url: str, seconds: float,
         poll: float = WARM_POLL_SECONDS) -> Health:
    """Wake a stopped backend and wait for it, at most `seconds`.

    One GET <base_url><warm_path> goes through the owner's proxy, which starts
    its own backend and holds the request until it is healthy. That request
    runs on a side thread; this polls /health until the proxy reports
    "backend": "running". ok False when the bound passes first.
    """
    key = cfg.api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    deadline = time.monotonic() + seconds
    trigger = threading.Thread(
        target=_http_get,
        args=(f"{base_url}{cfg.health.warm_path}", headers, max(1.0, seconds)),
        name="sam-warm", daemon=True,
    )
    trigger.start()
    while True:
        found = _http_get(f"{base_url}/health")
        if found is not None and found[0] == 200 and _backend_running(found[1]):
            return Health(True, base_url, cold_load=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Health(
                False, None,
                f"{cfg.name}: backend not running {seconds:g}s after the wake request",
            )
        time.sleep(min(poll, remaining))


def check_http(cfg: ProviderConfig) -> Health:
    """GET the provider's health URL with its key; healthy on 200."""
    url = cfg.health.url
    if not url:
        return Health(True, cfg.base_url)
    key = cfg.api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    found = _http_get(url, headers)
    if found is None:
        return Health(False, None, f"{cfg.name}: {url} did not answer")
    if found[0] != 200:
        return Health(False, None, f"{cfg.name}: {url} answered HTTP {found[0]}")
    return Health(True, cfg.base_url)


def check_omlx(cfg: ProviderConfig) -> Health:
    """GET the oMLX status endpoint. cold_load when no model is loaded yet."""
    url = cfg.health.url
    if not url:
        return Health(True, cfg.base_url)
    key = cfg.api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    found = _http_get(url, headers)
    if found is None:
        return Health(False, None, f"{cfg.name}: {url} did not answer")
    status, body = found
    if status != 200:
        return Health(False, None, f"{cfg.name}: {url} answered HTTP {status}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("status") != "ok":
        return Health(False, None, f"{cfg.name}: {url} status is not ok")
    return Health(True, cfg.base_url, cold_load=omlx_cold(data, cfg.model))


def omlx_cold(status: Mapping[str, Any], model: str | None) -> bool:
    """Whether `model` still has to load: absent from loaded_models when the
    server lists them (another model being loaded says nothing about this
    one), else no model loaded at all."""
    listed = status.get("loaded_models")
    if isinstance(listed, list) and model:
        return model not in [str(m) for m in listed]
    loaded = status.get("models_loaded")
    return isinstance(loaded, int) and loaded == 0


def check(cfg: ProviderConfig) -> Health:
    """The gate for `cfg`. A provider with no health block always passes."""
    kind = cfg.health.kind
    if kind == KIND_LLAMACPP:
        return check_llamacpp(cfg)
    if kind == KIND_OMLX:
        return check_omlx(cfg)
    if kind == KIND_HTTP:
        return check_http(cfg)
    return Health(True, cfg.base_url)
