"""Capacity telemetry for the local lanes: what the machine looked like while a
child ran on it.

Three pieces, all for the two local lanes (omlx on this Mac, bppc's llama.cpp):

  admission  one snapshot at dispatch, stored on the hop record, checked
             against per-lane thresholds. Log-only by default: a breach
             records would_refuse and the child still runs.
             SAM_<LANE>_ADMIT_ENFORCE=1 turns a breach into a skipped hop.
  sampler    while at least one child is active on a lane, one daemon thread
             per lane takes the same snapshot every SAM_SAMPLE_SECONDS
             (default 10) and appends a "sample" row to metrics.jsonl.
  summary    when a local-lane run ends, its samples and turn records become
             one "run_summary" record in the trace: peak memory, peak swap,
             worst pressure, decode tok/s, and bppc's energy.

Probes:

  omlx  GET <base_url>/api/status with the lane's key: models_loaded,
        models_loading, active_requests, waiting_requests, model_memory_used,
        model_memory_max, avg_prefill_tps, avg_generation_tps,
        total_prompt_tokens, total_completion_tokens.
  mac   `sysctl vm.swapusage` (swap used, MB),
        `sysctl kern.memorystatus_vm_pressure_level` (1 normal, 2 warn,
        4 critical; `memory_pressure -Q` free percentage when sysctl fails),
        `pmset -g therm` (thermal warning level, CPU speed limit).
  bppc  `ssh bppc@<host> nvidia-smi --query-gpu=...` (VRAM used/total MB,
        utilisation %, power W, temperature C), llama.cpp GET :8081/slots
        (the llama.cpp server port itself; :8080 is a preset proxy), and
        GET :8081/metrics, recorded as "unavailable" on any non-200 (the
        server runs without --metrics today).

Every probe is bounded to 3 s and never raises: a probe that fails records
{"error": "..."} in place of its fields. Tests replace `_http_get` and `_run`
(or pass their own probe functions); no test reaches the network, ssh or
sysctl.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import log
from ..lanes import Lane
from .trace import Trace

PROBE_TIMEOUT = 3.0
SAMPLE_SECONDS = 10.0
LLAMA_PORT = 8081
BPPC_USER = "bppc"

OMLX_FIELDS = (
    "models_loaded",
    "models_loading",
    "active_requests",
    "waiting_requests",
    "model_memory_used",
    "model_memory_max",
    "avg_prefill_tps",
    "avg_generation_tps",
    "total_prompt_tokens",
    "total_completion_tokens",
)
GPU_FIELDS = ("memory_used_mb", "memory_total_mb", "utilization_pct", "power_w", "temperature_c")
UNAVAILABLE = "unavailable"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Probe = Callable[[Lane], dict[str, Any]]


def _http_get(url: str, headers: dict[str, str] | None = None,
              timeout: float = PROBE_TIMEOUT) -> tuple[int, bytes]:
    """(status, body). Raises on no answer; callers turn that into an error."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _run(argv: list[str], timeout: float = PROBE_TIMEOUT) -> str:
    """stdout of `argv`. Raises on a non-zero exit, a timeout or a missing binary."""
    out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()[:200]
        raise RuntimeError(f"{argv[0]} exited {out.returncode}: {detail}")
    return out.stdout


def _error(exc: BaseException) -> dict[str, Any]:
    return {"error": f"{type(exc).__name__}: {exc}"[:300]}


# --- probes -------------------------------------------------------------------


def omlx_status(lane: Lane) -> dict[str, Any]:
    """The oMLX fields admission and the summary read, or {"error": ...}."""
    try:
        base = (lane.base_url or "").rstrip("/")
        url = lane.health_url or f"{base}/api/status"
        key = lane.api_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        status, body = _http_get(url, headers)
        if status != 200:
            return {"error": f"HTTP {status} from {url}"}
        data = json.loads(body.decode("utf-8", "replace"))
        if not isinstance(data, dict):
            return {"error": "status body is not a JSON object"}
        return {name: data.get(name) for name in OMLX_FIELDS}
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        return _error(exc)


_SWAP_RE = re.compile(r"used\s*=\s*([0-9.]+)([KMG])", re.IGNORECASE)
_FREE_PCT_RE = re.compile(r"free percentage:\s*([0-9]+)%", re.IGNORECASE)
_THERM_LEVEL_RE = re.compile(r"thermal warning level\s*(?:set to|=|:)?\s*(\w+)", re.IGNORECASE)
_SPEED_RE = re.compile(r"CPU_Speed_Limit\s*=\s*([0-9]+)")


def parse_swap_mb(text: str) -> float | None:
    """MB used from `sysctl vm.swapusage`: "total = 0.00M  used = 0.00M ..."."""
    match = _SWAP_RE.search(text or "")
    if match is None:
        return None
    scale = {"K": 1 / 1024, "M": 1.0, "G": 1024.0}[match.group(2).upper()]
    return round(float(match.group(1)) * scale, 2)


def parse_therm(text: str) -> dict[str, Any]:
    """`pmset -g therm`: "nominal" when no warning level has been recorded."""
    out: dict[str, Any] = {}
    if re.search(r"no thermal warning level has been recorded", text or "", re.IGNORECASE):
        out["thermal"] = "nominal"
    else:
        match = _THERM_LEVEL_RE.search(text or "")
        out["thermal"] = match.group(1).lower() if match else "unknown"
    speed = _SPEED_RE.search(text or "")
    if speed:
        out["cpu_speed_limit"] = int(speed.group(1))
    return out


def mac_status(lane: Lane | None = None) -> dict[str, Any]:
    """Swap, memory pressure and thermal state of this Mac. Each part fails alone."""
    out: dict[str, Any] = {}
    try:
        out["swap_used_mb"] = parse_swap_mb(_run(["sysctl", "vm.swapusage"]))
    except Exception as exc:  # noqa: BLE001
        out["swap_error"] = _error(exc)["error"]
    try:
        raw = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
        out["pressure_level"] = int(raw.strip().split()[-1])
    except Exception as exc:  # noqa: BLE001
        try:
            match = _FREE_PCT_RE.search(_run(["memory_pressure", "-Q"]))
            out["pressure_level"] = None
            out["memory_free_pct"] = int(match.group(1)) if match else None
        except Exception:  # noqa: BLE001
            out["pressure_error"] = _error(exc)["error"]
    try:
        out.update(parse_therm(_run(["pmset", "-g", "therm"])))
    except Exception as exc:  # noqa: BLE001
        out["thermal_error"] = _error(exc)["error"]
    return out


def parse_nvidia(text: str) -> dict[str, Any]:
    """The first GPU's line of nvidia-smi csv,noheader,nounits output."""
    line = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != len(GPU_FIELDS):
        raise ValueError(f"unexpected nvidia-smi output: {line[:120]!r}")
    values: dict[str, Any] = {}
    for name, raw in zip(GPU_FIELDS, parts, strict=True):
        try:
            values[name] = float(raw)
        except ValueError:
            values[name] = None  # "[N/A]"
    return values


def lane_host(lane: Lane) -> str | None:
    """The host a lane's base URL points at (bppc's is resolved at dispatch)."""
    return urlsplit(lane.base_url).hostname if lane.base_url else None


def bppc_gpu(host: str) -> dict[str, Any]:
    try:
        text = _run([
            "ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", f"{BPPC_USER}@{host}",
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu",
            "--format=csv,noheader,nounits",
        ])
        return parse_nvidia(text)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def llama_slots(host: str) -> dict[str, Any]:
    try:
        status, body = _http_get(f"http://{host}:{LLAMA_PORT}/slots")
        if status != 200:
            return {"error": f"HTTP {status} from /slots"}
        data = json.loads(body.decode("utf-8", "replace"))
        if not isinstance(data, list):
            return {"error": "slots body is not a JSON list"}
        busy = sum(1 for s in data if isinstance(s, dict) and s.get("is_processing"))
        return {"slots_total": len(data), "slots_busy": busy}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def llama_metrics(host: str) -> dict[str, Any] | str:
    """Prometheus gauges from :8081/metrics, or "unavailable" on any non-200."""
    try:
        status, body = _http_get(f"http://{host}:{LLAMA_PORT}/metrics")
    except Exception:  # noqa: BLE001
        return UNAVAILABLE
    if status != 200:
        return UNAVAILABLE
    values: dict[str, Any] = {}
    for line in body.decode("utf-8", "replace").splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, raw = line.rpartition(" ")
        if name.startswith("llamacpp:"):
            try:
                values[name.split("{")[0][len("llamacpp:"):]] = float(raw)
            except ValueError:
                continue
    return values


def probe_omlx(lane: Lane) -> dict[str, Any]:
    return {"omlx": omlx_status(lane), "mac": mac_status(lane)}


def probe_bppc(lane: Lane) -> dict[str, Any]:
    host = lane_host(lane)
    if host is None:
        return {"error": "bppc host not resolved"}
    return {"gpu": bppc_gpu(host), "slots": llama_slots(host), "metrics": llama_metrics(host)}


DEFAULT_PROBES: dict[str, Probe] = {"omlx": probe_omlx, "bppc": probe_bppc}


# --- admission ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Thresholds:
    """SAM_<LANE>_ADMIT_*; None means that check is off."""

    max_swap_mb: float | None = None
    max_pressure: int | None = None
    max_waiting: int | None = None
    min_vram_free_mb: float | None = None
    enforce: bool = False

    @classmethod
    def from_env(cls, lane: str, env: Mapping[str, str]) -> Thresholds:
        def num(knob: str) -> float | None:
            raw = env.get(f"SAM_{lane.upper()}_ADMIT_{knob}")
            if raw is None or not raw.strip():
                return None
            return float(raw)

        pressure = num("MAX_PRESSURE")
        waiting = num("MAX_WAITING")
        flag = (env.get(f"SAM_{lane.upper()}_ADMIT_ENFORCE") or "").strip().lower()
        return cls(
            max_swap_mb=num("MAX_SWAP_MB"),
            max_pressure=int(pressure) if pressure is not None else None,
            max_waiting=int(waiting) if waiting is not None else None,
            min_vram_free_mb=num("MIN_VRAM_FREE_MB"),
            enforce=flag in ("1", "true", "yes", "on"),
        )


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def breaches(snapshot: Mapping[str, Any], limits: Thresholds) -> list[str]:
    """Which thresholds `snapshot` exceeds. A value a probe could not read is no breach."""
    mac = snapshot.get("mac") if isinstance(snapshot.get("mac"), dict) else {}
    omlx = snapshot.get("omlx") if isinstance(snapshot.get("omlx"), dict) else {}
    gpu = snapshot.get("gpu") if isinstance(snapshot.get("gpu"), dict) else {}
    found = []
    swap = _num(mac.get("swap_used_mb"))
    if limits.max_swap_mb is not None and swap is not None and swap > limits.max_swap_mb:
        found.append(f"swap {swap:g} MB > {limits.max_swap_mb:g}")
    level = _num(mac.get("pressure_level"))
    if limits.max_pressure is not None and level is not None and level > limits.max_pressure:
        found.append(f"memory pressure {level:g} > {limits.max_pressure}")
    waiting = _num(omlx.get("waiting_requests"))
    if limits.max_waiting is not None and waiting is not None and waiting > limits.max_waiting:
        found.append(f"waiting requests {waiting:g} > {limits.max_waiting}")
    used, total = _num(gpu.get("memory_used_mb")), _num(gpu.get("memory_total_mb"))
    if limits.min_vram_free_mb is not None and used is not None and total is not None:
        free = total - used
        if free < limits.min_vram_free_mb:
            found.append(f"VRAM free {free:g} MB < {limits.min_vram_free_mb:g}")
    return found


@dataclass(slots=True)
class Admission:
    """The dispatch snapshot and what the thresholds made of it."""

    snapshot: dict[str, Any]
    would_refuse: bool
    reason: str | None
    enforce: bool

    @property
    def refuse(self) -> bool:
        return self.would_refuse and self.enforce


# --- summary math -------------------------------------------------------------


def _peak(samples: list[tuple[float, dict[str, Any]]], source: str, name: str) -> float | None:
    values = [
        _num(snap.get(source, {}).get(name))
        for _, snap in samples
        if isinstance(snap.get(source), dict)
    ]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _integral(points: list[tuple[float, float]]) -> float | None:
    """Trapezoid integral of (t, y) over t. None with fewer than two points."""
    points = sorted(points)
    if len(points) < 2:
        return None
    pairs = zip(points, points[1:], strict=False)
    return sum((y0 + y1) / 2 * (t1 - t0) for (t0, y0), (t1, y1) in pairs)


def decode_rates(turns: list[Mapping[str, Any]]) -> list[float]:
    """Per-turn output tokens / (ts_end - ts_first_token), for turns where both exist."""
    rates = []
    for turn in turns:
        first, end = turn.get("ts_first_token"), turn.get("ts_end")
        output = turn.get("output") or 0
        if first is None or end is None or output <= 0 or end - first <= 0:
            continue
        rates.append(output / (end - first))
    return rates


def p10(values: list[float]) -> float | None:
    """The 10th percentile by nearest rank: the slowest 10%'s upper edge."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.1 * len(ordered)) - 1)]


def summarize(
    samples: list[tuple[float, dict[str, Any]]], turns: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """The run_summary fields for one run's samples and turn records.

    energy_wh is the trapezoid integral of bppc's power draw over the samples;
    gpu_seconds the same integral of utilisation (fraction busy x seconds).
    """
    rates = decode_rates(turns)
    power = [(t, v) for t, s in samples
             if (v := _num((s.get("gpu") or {}).get("power_w"))) is not None]
    util = [(t, v / 100) for t, s in samples
            if (v := _num((s.get("gpu") or {}).get("utilization_pct"))) is not None]
    energy = _integral(power)
    busy = _integral(util)
    return {
        "samples": len(samples),
        "peak_model_memory_used": _peak(samples, "omlx", "model_memory_used"),
        "peak_swap_mb": _peak(samples, "mac", "swap_used_mb"),
        "max_pressure": _peak(samples, "mac", "pressure_level"),
        "peak_vram_used_mb": _peak(samples, "gpu", "memory_used_mb"),
        "decode_turns": len(rates),
        "decode_tps_mean": round(sum(rates) / len(rates), 3) if rates else None,
        "decode_tps_p10": round(p10(rates), 3) if rates else None,
        "energy_wh": round(energy / 3600, 4) if energy is not None else None,
        "gpu_seconds": round(busy, 3) if busy is not None else None,
    }


# --- the telemetry hub --------------------------------------------------------


@dataclass
class _Sampler:
    lane: Lane
    runs: set[str] = field(default_factory=set)
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class Telemetry:
    """Admission, sampling and summaries for the lanes that have a probe."""

    def __init__(
        self,
        metrics: Trace | None = None,
        *,
        probes: Mapping[str, Probe] | None = None,
        env: Mapping[str, str] | None = None,
        interval: float | None = None,
    ):
        source = os.environ if env is None else env
        self.metrics = metrics
        self.probes = dict(DEFAULT_PROBES if probes is None else probes)
        if interval is None:
            raw = source.get("SAM_SAMPLE_SECONDS")
            interval = float(raw) if raw and raw.strip() else SAMPLE_SECONDS
        if interval <= 0:
            raise ValueError(f"SAM_SAMPLE_SECONDS must be positive, got {interval!r}")
        self.interval = interval
        self.thresholds = {name: Thresholds.from_env(name, source) for name in self.probes}
        self._lock = threading.Lock()
        self._samplers: dict[str, _Sampler] = {}
        self._samples: dict[str, list[tuple[float, dict[str, Any]]]] = {}

    def covers(self, lane: Lane) -> bool:
        return lane.local and lane.name in self.probes

    def snapshot(self, lane: Lane) -> dict[str, Any]:
        """One probe pass. Never raises."""
        probe = self.probes.get(lane.name)
        if probe is None:
            return {}
        try:
            snap = probe(lane)
            return snap if isinstance(snap, dict) else {"error": "probe returned no fields"}
        except Exception as exc:  # noqa: BLE001
            return _error(exc)

    def admission(self, lane: Lane) -> Admission:
        snap = self.snapshot(lane)
        limits = self.thresholds.get(lane.name) or Thresholds()
        found = breaches(snap, limits)
        return Admission(
            snapshot=snap,
            would_refuse=bool(found),
            reason="; ".join(found) or None,
            enforce=limits.enforce,
        )

    # sampler ---------------------------------------------------------------

    def begin(self, lane: Lane, run_id: str) -> None:
        """A child started on `lane`: start the lane's sampler if it is idle."""
        if not self.covers(lane):
            return
        with self._lock:
            self._samples.setdefault(run_id, [])
            sampler = self._samplers.get(lane.name)
            if sampler is None:
                sampler = _Sampler(lane=lane)
                self._samplers[lane.name] = sampler
                sampler.runs.add(run_id)
                sampler.thread = threading.Thread(
                    target=self._sample_loop, args=(sampler,),
                    name=f"sam-sampler-{lane.name}", daemon=True,
                )
                sampler.thread.start()
            else:
                sampler.runs.add(run_id)

    def end(self, lane_name: str, run_id: str) -> list[tuple[float, dict[str, Any]]]:
        """A child stopped. Returns its samples; the sampler exits when the lane is idle."""
        with self._lock:
            sampler = self._samplers.get(lane_name)
            if sampler is not None:
                sampler.runs.discard(run_id)
                if not sampler.runs:
                    sampler.wake.set()
            return self._samples.pop(run_id, [])

    def active(self, lane_name: str) -> bool:
        with self._lock:
            return lane_name in self._samplers

    def _sample_loop(self, sampler: _Sampler) -> None:
        while True:
            with self._lock:
                if not sampler.runs:
                    # Removed under the lock, so a begin() that races this
                    # exit starts a fresh sampler instead of joining a dead one.
                    self._samplers.pop(sampler.lane.name, None)
                    return
                ids = sorted(sampler.runs)
            try:
                snap = self.snapshot(sampler.lane)
                ts = round(time.time(), 3)
                with self._lock:
                    for run_id in ids:
                        if run_id in self._samples:
                            self._samples[run_id].append((ts, snap))
                if self.metrics is not None:
                    self.metrics.write("sample", lane=sampler.lane.name, run_ids=ids,
                                       snapshot=snap)
            except Exception:  # noqa: BLE001 - a sampler never takes the server down
                log.warning("sampler for %s failed a pass", sampler.lane.name, exc_info=True)
            sampler.wake.wait(self.interval)
            sampler.wake.clear()

    def shutdown(self) -> None:
        with self._lock:
            samplers = list(self._samplers.values())
            for sampler in samplers:
                sampler.runs.clear()
                sampler.wake.set()
        for sampler in samplers:
            if sampler.thread is not None:
                sampler.thread.join(timeout=5)


def open_metrics(trace: Trace, session_root: Any) -> Trace | None:
    """metrics.jsonl beside the trace, under the session root; off when the trace is."""
    if not trace.enabled:
        return None
    return Trace(session_root / "metrics.jsonl")
