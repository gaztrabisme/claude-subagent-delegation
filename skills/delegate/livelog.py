"""Readable live log of worker runs, the process runner that feeds it, and live-view terminals."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

END_MARKER = "=== end"
PARTIAL_LINES_PER_TOOL = 30


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
    """Turns one worker's output (Copilot JSONL events, or plain text) into readable lines as it arrives."""

    def __init__(self, sink, raw_path, root, prefix=""):
        self.sink = sink
        self.raw = open(raw_path, "a", buffering=1)
        self.root = str(root).rstrip("/") + "/"
        self.prefix = f"[{prefix}] " if prefix else ""
        self.tools = {}     # toolCallId -> tool name
        self.partial = {}   # toolCallId -> partial output lines shown
        self.credits = None

    def write(self, text):
        self.sink.write(self.prefix + text)

    def close(self):
        self.raw.close()

    def _short(self, value, limit=160):
        text = str(value).replace(self.root, "").replace(self.root.rstrip("/"), ".").replace("\n", " ⏎ ")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _time(event):
        try:
            return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
        except (KeyError, ValueError, AttributeError):
            return datetime.now().strftime("%H:%M:%S")

    def feed(self, line):
        line = line.rstrip("\n")
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict) or "type" not in event:
            if line.strip():
                self.write(line)
            return
        self.raw.write(line + "\n")
        kind, data, ts = event["type"], event.get("data") or {}, self._time(event)
        if kind == "assistant.turn_start":
            self.write(f"{ts} ── turn {int(data.get('turnId', 0)) + 1}")
        elif kind == "assistant.message":
            for text_line in (data.get("content") or "").strip().splitlines():
                self.write(f"{ts} 💬 {text_line}")
        elif kind == "tool.execution_start":
            name = data.get("toolName", "?")
            self.tools[data.get("toolCallId")] = name
            args = data.get("arguments") or {}
            detail = args.get("command") or args.get("path") or next(
                (v for v in args.values() if isinstance(v, str)), "")
            self.write(f"{ts} ▸ {name} {self._short(detail)}")
        elif kind == "tool.execution_partial_result":
            # partialOutput is cumulative: print only the complete lines not shown yet.
            call = data.get("toolCallId")
            shown = self.partial.get(call, 0)
            lines = (data.get("partialOutput") or "").split("\n")[:-1]
            for out_line in lines[shown:]:
                if shown < PARTIAL_LINES_PER_TOOL:
                    self.write(f"         │ {self._short(out_line, 200)}")
                elif shown == PARTIAL_LINES_PER_TOOL:
                    self.write("         │ …")
                shown += 1
            self.partial[call] = max(shown, self.partial.get(call, 0))
        elif kind == "tool.execution_complete":
            name = self.tools.get(data.get("toolCallId"), "tool")
            if data.get("success"):
                self.write(f"{ts}   ✓ {name}")
            else:
                detail = data.get("error") or data.get("result") or {}
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail.get("content") or json.dumps(detail)
                self.write(f"{ts}   ✗ {name} failed: {self._short(detail, 300)}")
        elif kind == "session.usage_checkpoint":
            if data.get("totalNanoAiu") is not None:
                self.credits = data["totalNanoAiu"] / 1e9
        elif "error" in kind:
            self.write(f"{ts} ! {kind}: {self._short(json.dumps(data), 300)}")


def run_backend(argv, cwd, env, timeout, live):
    """Run the worker, feeding its output to the live log line by line. Returns (exit_code, timed_out)."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, errors="replace")
    except FileNotFoundError as exc:
        live.write(f"backend not found: {exc}")
        return 127, False
    reader = threading.Thread(target=lambda: [live.feed(line) for line in proc.stdout], daemon=True)
    reader.start()
    try:
        code, timed_out = proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        code, timed_out = None, True
    reader.join(timeout=5)
    return code, timed_out


TERMINALS = [
    ["alacritty", "--title", "delegate", "-e"],
    ["kitty", "--title", "delegate"],
    ["wezterm", "start", "--"],
    ["ghostty", "-e"],
    ["foot", "-T", "delegate"],
    ["konsole", "-e"],
    ["gnome-terminal", "--title", "delegate", "--"],
    ["xterm", "-T", "delegate", "-e"],
]


def open_live_view(cfg, root, log, script):
    """Open a terminal that follows this run's live log (config "live_view"). Returns an error or None."""
    setting = cfg.get("live_view")
    if not setting or setting == "off" or os.environ.get("DELEGATE_LIVE_VIEW") == "off":
        return None
    watch = [sys.executable, str(script), "--root", str(root), "watch", "--log", str(log), "--hold"]
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


def follow(path, stop_at_end, latest=None):
    """Print a log file as it grows. Returns when the end marker is seen (stop_at_end) or latest moves."""
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
            time.sleep(0.2)
