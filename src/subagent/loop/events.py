"""Readable live log of worker runs (stream-json events) and live-view terminals."""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

END_MARKER = "=== end"


def _truncate(text, limit=160):
    text = str(text).replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(event):
    """Readable lines for one stream-json event, or [] (nothing worth showing)."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "system":
        if event.get("subtype") == "init":
            sid = event.get("session_id")
            return [f"── session {sid}"] if sid else []
        return []
    if kind == "assistant":
        message = event.get("message") or {}
        content = message.get("content") or []
        lines = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and (block.get("text") or "").strip():
                lines += [f"💬 {line}" for line in str(block["text"]).strip().splitlines()]
            elif block.get("type") == "tool_use":
                name = block.get("name", "?")
                args = block.get("input") or {}
                if isinstance(args, dict):
                    detail = args.get("command") or args.get("file_path") or args.get("path") or next(
                        (v for v in args.values() if isinstance(v, str)), "")
                else:
                    detail = str(args).strip().splitlines()[0] if str(args).strip() else ""
                lines.append(f"▸ {name} {_truncate(detail)}")
        return lines
    if kind == "user":
        content = (event.get("message") or {}).get("content") or []
        lines = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                mark = "✓" if not block.get("is_error") else "✗"
                lines.append(f"  {mark} {block.get('tool_use_id') or ''}")
                text = block.get("content")
                if text is None or text == "":
                    continue
                if not isinstance(text, str):
                    text = str(text)
                # The first lines of a tool's output are what a user watching the
                # live log needs; the full output stays in the raw jsonl.
                for line in text.strip().splitlines()[:12]:
                    lines.append(f"    {_truncate(line, 300)}")
        return lines
    if kind == "result" and event.get("is_error"):
        detail = event.get("error") or event.get("result") or ""
        return [f"! {_truncate(detail, 300)}"]
    return []


class LogSink:
    """A log file several workers can write whole lines to at once."""

    def __init__(self, path):
        self.path = Path(path)
        self.fh = open(self.path, "a", buffering=1)
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.fh.write(text + "\n")

    def close(self):
        self.fh.close()


class LiveLog:
    """Turns one worker's stream-json events into readable lines as they arrive."""

    def __init__(self, sink, raw_path, root, prefix=""):
        self.sink = sink
        self.raw = open(raw_path, "a", buffering=1)
        self.prefix = f"[{prefix}] " if prefix else ""
        self.credits = None
        self.last_message = ""  # the worker's last chat message (fallback when it forgets to write a file)

    def write(self, text):
        self.sink.write(self.prefix + text)

    def close(self):
        self.raw.close()

    def _note(self, event):
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip():
                    self.last_message = str(block["text"]).strip()
        elif event.get("type") == "result":
            usage = event.get("usage") or {}
            if usage.get("credits") is not None:
                self.credits = usage["credits"]

    def feed(self, event):
        """Never raises: one malformed event must not stop the stream (usage and completion come last)."""
        try:
            if not isinstance(event, dict):
                return
            self.raw.write(json.dumps(event) + "\n")
            self._note(event)
            for line in render(event):
                if line:
                    self.write(line)
        except Exception as exc:  # noqa: BLE001
            self.write(f"(could not format an event: {type(exc).__name__}: {exc})")


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


TERMINALS = [
    ["alacritty", "--title", "subagent", "-e"],
    ["kitty", "--title", "subagent"],
    ["wezterm", "start", "--"],
    ["ghostty", "-e"],
    ["foot", "-T", "subagent"],
    ["konsole", "-e"],
    ["gnome-terminal", "--title", "subagent", "--"],
    ["xterm", "-T", "subagent", "-e"],
]


def open_live_view(cfg, root, runner, watch_args):
    """Open a terminal running `watch <watch_args> --hold` (config "live_view"). Returns an error or None."""
    setting = cfg.live_view
    if not setting or setting == "off" or os.environ.get("SUBAGENT_LIVE_VIEW") == "off":
        return None
    watch = [*runner, "--root", str(root), "watch", *[str(a) for a in watch_args], "--hold"]
    if isinstance(setting, list):
        argv = [arg for part in setting for arg in (watch if part == "{cmd}" else [part])]
    elif os.environ.get("TMUX") and shutil.which("tmux"):
        # Separate arguments: tmux runs them directly, not through the user's shell (fish, zsh, ...).
        argv = ["tmux", "split-window", "-d", "-h", *watch]
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        user_term = os.environ.get("TERMINAL")
        candidates = ([[user_term, "-e"]] if user_term else []) + TERMINALS
        base = next((t for t in candidates if shutil.which(t[0])), None)
        if not base:
            return "no supported terminal found; set live_view to an argv containing \"{cmd}\""
        argv = base + watch
    else:
        return "no display or tmux session to open a terminal in"
    try:
        subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return f"could not open terminal: {exc}"
    return None


def follow(path, stop_at_end, latest=None, stop_when=None):
    """Print a log file as it grows.

    Returns when the end marker is seen (stop_at_end), when `latest` points to a newer log, or when
    everything has been printed and stop_when() is true.
    """
    with open(path, errors="replace") as fh:
        while True:
            line = fh.readline()
            if line:
                print(line, end="", flush=True)
                if stop_at_end and line.startswith(END_MARKER):
                    return
                continue
            if latest is not None and latest.exists() and latest.resolve() != Path(path).resolve():
                return
            if stop_when is not None and stop_when():
                return
            time.sleep(0.2)
