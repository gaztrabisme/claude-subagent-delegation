"""Health gates for the local lanes, checked at dispatch.

bppc: its LAN address changes with DHCP, so the base URL is resolved each time
a run is dispatched. Hosts are tried in order: the RFC1918 address `tailscale
status --json` reports for the peer holding bppc's tailnet IP, then
SAM_BPPC_LAN_HOSTS (comma-separated, default 192.168.1.17), then the stable
Tailscale address. The first whose /health answers 200 within 2 s wins.
SAM_BPPC_BASE_URL skips the resolution and is probed as given.

omlx: GET the lane's health_url with its key. Healthy when it answers 200 with
status "ok". The lane's model missing from loaded_models is still healthy (the
first request loads it), but the result carries cold_load so the run can
allow for it.

bppc's :8080 is a proxy that stops llama.cpp when idle and starts it on the
first request. Its /health then answers {"status": "ok", "backend": "stopped"}:
healthy, with cold_load set, like oMLX with no model loaded. Before a child
is spawned on a cold bppc, warm_bppc sends one GET /v1/models through that
proxy (the owner's proxy then starts its own backend) and polls /health until
it says "running", bounded by SAM_BPPC_COLD_LOAD_SECONDS.

Cloud lanes have no gate: their refusals come back in the child's own error.

Nothing here starts a model server itself. A lane whose server is down fails its
gate and the router moves on.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import threading
import time
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


def _rfc1918(ip: str) -> bool:
    """A private LAN IPv4 (10/8, 172.16/12, 192.168/16) in dotted-quad form."""
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(address in net for net in _PRIVATE_NETS)


_PRIVATE_NETS = tuple(ipaddress.IPv4Network(n) for n in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def bppc_lan_from_tailscale(status: Mapping[str, Any] | None,
                            tailnet_ip: str = BPPC_TAILSCALE_HOST) -> str | None:
    """bppc's LAN IPv4 from tailscale status.

    The peer is the one whose TailscaleIPs include `tailnet_ip` (a HostName
    can be claimed by any node). Its address is the first RFC1918 one in
    CurAddr, Addrs or Endpoints; a public endpoint (STUN) is never used, since
    the prompt and key go to it over plain HTTP.
    """
    if not isinstance(status, Mapping):
        return None
    peers = status.get("Peer") or {}
    if not isinstance(peers, Mapping):
        return None
    for peer in peers.values():
        if not isinstance(peer, Mapping):
            continue
        ips = peer.get("TailscaleIPs")
        if not isinstance(ips, list) or tailnet_ip not in [str(x) for x in ips]:
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
            if _rfc1918(ip):
                return ip
    return None


def bppc_hosts(env: Mapping[str, str] | None = None,
               status: Mapping[str, Any] | None = None) -> list[str]:
    """Hosts to probe for bppc, in order, without duplicates."""
    env = os.environ if env is None else env
    hosts: list[str] = []
    tailnet = (env.get("SAM_BPPC_TAILSCALE_HOST") or BPPC_TAILSCALE_HOST).strip()
    lan = bppc_lan_from_tailscale(status, tailnet)
    if lan:
        hosts.append(lan)
    listed = env.get("SAM_BPPC_LAN_HOSTS")
    listed = BPPC_LAN_HOSTS if listed is None else listed
    hosts.extend(h.strip() for h in listed.split(",") if h.strip())
    hosts.append(tailnet)
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


def _backend_running(body: bytes) -> bool:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("backend") == "running"


WARM_POLL_SECONDS = 2.0


def warm_bppc(base_url: str, seconds: float, key: str | None = None,
              poll: float = WARM_POLL_SECONDS) -> Health:
    """Wake bppc's backend and wait for it, at most `seconds`.

    One GET <base_url>/v1/models goes through the owner's proxy, which starts
    its own llama.cpp container and holds the request until it is healthy.
    That request runs on a side thread; this polls /health until the proxy
    reports "backend": "running". ok False when the bound passes first.
    """
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    deadline = time.monotonic() + seconds
    trigger = threading.Thread(
        target=_http_get, args=(f"{base_url}/v1/models", headers, max(1.0, seconds)),
        name="sam-bppc-warm", daemon=True,
    )
    trigger.start()
    while True:
        found = _http_get(f"{base_url}/health")
        if found is not None and found[0] == 200 and _backend_running(found[1]):
            return Health(True, base_url, cold_load=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Health(False, None,
                          f"bppc: backend not running {seconds:g}s after the wake request")
        time.sleep(min(poll, remaining))


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
    return Health(True, lane.base_url, cold_load=omlx_cold(data, lane.model))


def omlx_cold(status: Mapping[str, Any], model: str | None) -> bool:
    """Whether `model` still has to load: absent from loaded_models when the
    server lists them (another model being loaded says nothing about this
    one), else no model loaded at all."""
    listed = status.get("loaded_models")
    if isinstance(listed, list) and model:
        return model not in [str(m) for m in listed]
    loaded = status.get("models_loaded")
    return isinstance(loaded, int) and loaded == 0


def check(lane: Lane, env: Mapping[str, str] | None = None) -> Health:
    """The gate for `lane`. Cloud lanes always pass."""
    if lane.resolver == "bppc":
        return check_bppc(lane, env)
    if lane.local and lane.health_url:
        return check_omlx(lane)
    return Health(True, lane.base_url)
