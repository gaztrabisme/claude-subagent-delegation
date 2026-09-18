"""Health gates for the local lanes, checked at dispatch.

bppc: its LAN address changes with DHCP, so the base URL is resolved each time
a run is dispatched. Hosts are tried in order: the LAN address `tailscale
status --json` reports for bppc, then SAM_BPPC_LAN_HOSTS (comma-separated,
default 192.168.1.17), then the stable Tailscale address. The first whose
/health answers 200 within 2 s wins. SAM_BPPC_BASE_URL skips the resolution
and is probed as given.

omlx: GET the lane's health_url with its key. Healthy when it answers 200 with
status "ok". models_loaded 0 is still healthy (the first request loads the
model), but the result carries cold_load so the run can allow for it.

bppc's :8080 is a proxy that stops llama.cpp when idle and starts it on the
first request. Its /health then answers {"status": "ok", "backend": "stopped"}:
healthy, with cold_load set, like oMLX with no model loaded.

Cloud lanes have no gate: their refusals come back in the child's own error.

Nothing here starts a model server. A lane whose server is down fails its
gate and the router moves on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import log
from .lanes import Lane

PROBE_TIMEOUT = 2.0
BPPC_TAILSCALE_HOST = "100.106.185.34"
BPPC_LAN_HOSTS = "192.168.1.17"
BPPC_PORT = 8080
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True, slots=True)
class Health:
    """A gate's answer. base_url is the address the run should use."""

    ok: bool
    base_url: str | None
    message: str = ""
    cold_load: bool = False


def _http_get(url: str, headers: dict[str, str] | None = None,
              timeout: float = PROBE_TIMEOUT) -> tuple[int, bytes] | None:
    """(status, body) for a GET, or None when nothing answered. No proxies."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (OSError, ValueError):
        return None


def _tailscale_status() -> dict[str, Any] | None:
    """`tailscale status --json`, bounded to 2 s. None on any failure."""
    binary = shutil.which("tailscale") or (TAILSCALE_APP if os.path.exists(TAILSCALE_APP) else None)
    if binary is None:
        log.info("tailscale CLI not found; bppc LAN leg from tailscale skipped")
        return None
    try:
        out = subprocess.run(
            [binary, "status", "--json"], capture_output=True, text=True, timeout=PROBE_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        log.info("tailscale status unavailable; bppc LAN leg from tailscale skipped")
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def bppc_lan_from_tailscale(status: Mapping[str, Any] | None) -> str | None:
    """bppc's LAN IPv4 from tailscale status: the first non-100.x address in
    CurAddr, Addrs or Endpoints of the peer whose HostName is bppc."""
    if not isinstance(status, Mapping):
        return None
    peers = status.get("Peer") or {}
    if not isinstance(peers, Mapping):
        return None
    for peer in peers.values():
        if not isinstance(peer, Mapping):
            continue
        host = str(peer.get("HostName") or "").lower()
        if host != "bppc" and not host.startswith(("bppc-", "bppc.")):
            continue
        candidates = [str(peer.get("CurAddr") or "")]
        for key in ("Addrs", "Endpoints"):
            value = peer.get(key)
            if isinstance(value, list):
                candidates.extend(str(x) for x in value)
        for raw in candidates:
            ip = raw.strip()
            if not ip:
                continue
            if ":" in ip:  # ip:port or bare IPv6
                ip = ip.rsplit(":", 1)[0].strip("[]")
            parts = ip.split(".")
            if len(parts) != 4 or parts[0] == "100":
                continue
            try:
                if all(0 <= int(x) <= 255 for x in parts):
                    return ip
            except ValueError:
                continue
    return None


def bppc_hosts(env: Mapping[str, str] | None = None,
               status: Mapping[str, Any] | None = None) -> list[str]:
    """Hosts to probe for bppc, in order, without duplicates."""
    env = os.environ if env is None else env
    hosts: list[str] = []
    lan = bppc_lan_from_tailscale(status)
    if lan:
        hosts.append(lan)
    listed = env.get("SAM_BPPC_LAN_HOSTS")
    listed = BPPC_LAN_HOSTS if listed is None else listed
    hosts.extend(h.strip() for h in listed.split(",") if h.strip())
    hosts.append((env.get("SAM_BPPC_TAILSCALE_HOST") or BPPC_TAILSCALE_HOST).strip())
    seen: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.append(host)
    return seen


def _backend_stopped(body: bytes) -> bool:
    """True when bppc's proxy says its llama.cpp backend is stopped (idle)."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("backend") == "stopped"


def check_bppc(lane: Lane, env: Mapping[str, str] | None = None) -> Health:
    """Resolve bppc's base URL, or report that no host answered."""
    env = os.environ if env is None else env
    if lane.base_url:
        # SAM_BPPC_BASE_URL: no resolution, but still gated.
        found = _http_get(f"{lane.base_url}/health")
        if found is not None and found[0] == 200:
            return Health(True, lane.base_url, cold_load=_backend_stopped(found[1]))
        return Health(False, None, f"bppc: {lane.base_url}/health did not answer 200")
    port = int(env.get("SAM_BPPC_PORT") or BPPC_PORT)
    hosts = bppc_hosts(env, _tailscale_status())
    for host in hosts:
        base = f"http://{host}:{port}"
        found = _http_get(f"{base}/health")
        if found is not None and found[0] == 200:
            return Health(True, base, cold_load=_backend_stopped(found[1]))
    return Health(
        False, None, f"bppc: no host answered /health on :{port} (tried {', '.join(hosts)})"
    )


def check_omlx(lane: Lane) -> Health:
    """GET the oMLX status endpoint. cold_load when no model is loaded yet."""
    if not lane.health_url:
        return Health(True, lane.base_url)
    key = lane.api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    found = _http_get(lane.health_url, headers)
    if found is None:
        return Health(False, None, f"{lane.name}: {lane.health_url} did not answer")
    status, body = found
    if status != 200:
        return Health(False, None, f"{lane.name}: {lane.health_url} answered HTTP {status}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("status") != "ok":
        return Health(False, None, f"{lane.name}: {lane.health_url} status is not ok")
    loaded = data.get("models_loaded")
    cold = isinstance(loaded, int) and loaded == 0
    return Health(True, lane.base_url, cold_load=cold)


def check(lane: Lane, env: Mapping[str, str] | None = None) -> Health:
    """The gate for `lane`. Cloud lanes always pass."""
    if lane.resolver == "bppc":
        return check_bppc(lane, env)
    if lane.local and lane.health_url:
        return check_omlx(lane)
    return Health(True, lane.base_url)
