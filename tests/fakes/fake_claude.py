#!/usr/bin/env python3
"""Fake Claude Code CLI for the test suite.

Same scenario protocol as fake_copilot.py (FAKE_SCRIPT, FAKE_MODEL, FAKE_PROMPT,
FAKE_FAIL_MODEL), but it speaks Claude stream-json and gates the scenario's one
Bash tool call through the PreToolUse hook named by the `--settings` file, so
the supervisor's deterministic tier is exercised exactly like the real child.

Options (a subset of the real CLI, enough for the claude driver):
  --version            print a version and exit 0 (the driver's boot probe)
  -p PROMPT            the prompt (also exported as FAKE_PROMPT)
  --model MODEL        the model (also exported as FAKE_MODEL)
  --settings FILE      Claude settings.json holding the PreToolUse hook
  --resume ID          reuse ID as the session id (echoed in system/init)
  FAKE_FAIL_MODEL=M    exit 1 with a cli_error before any work, for that model
  .subagent/fake_message.txt  if the scenario writes it, it is the final message
"""

import json
import os
import shlex
import subprocess
import sys

args = sys.argv[1:]

if "--version" in args:
    print("1.0.0 (fake)")
    sys.exit(0)


def flag(name, default=""):
    return args[args.index(name) + 1] if name in args else default


cwd = flag("-C", ".")
model = flag("--model", "default")
prompt = flag("-p", "")
settings_path = flag("--settings", "")
session_id = flag("--resume", "") or f"fake-session-{os.getpid()}"


def event(obj):
    print(json.dumps(obj), flush=True)


def hook_command():
    """The PreToolUse hook command line from the settings file, or None."""
    if not settings_path or not os.path.exists(settings_path):
        return None
    with open(settings_path) as fh:
        settings = json.load(fh)
    for entry in (settings.get("hooks") or {}).get("PreToolUse") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("type") == "command" and hook.get("command"):
                return hook["command"]
    return None


if os.environ.get("FAKE_FAIL_MODEL") == model:
    print(f"Error: model {model!r} is not available (fake)", file=sys.stderr)
    sys.exit(1)

env = {**os.environ, "FAKE_MODEL": model, "FAKE_PROMPT": prompt}

event({"type": "system", "subtype": "init", "session_id": session_id,
       "model": model, "cwd": cwd})

# One Bash tool call: the copied scenario script, relative so the classifier
# (deterministic tier) ALLOWs it. An absolute path would escalate and deny.
script_dir = os.path.join(cwd, ".subagent")
os.makedirs(script_dir, exist_ok=True)
script = os.path.join(script_dir, "fake_scenario.sh")
with open(os.environ["FAKE_SCRIPT"]) as fh:
    with open(script, "w") as out:
        out.write(fh.read())
command = "bash .subagent/fake_scenario.sh"
event({"type": "assistant", "session_id": session_id, "message": {"content": [
    {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": command}},
]}})

denied = None
hook = hook_command()
if hook:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    proc = subprocess.run(shlex.split(hook), input=json.dumps(payload), capture_output=True,
                          text=True, env=env)
    if proc.returncode != 0:
        denied = (proc.stderr or proc.stdout or "denied").strip()
else:
    denied = "no PreToolUse hook in the settings file"

if denied:
    event({"type": "user", "session_id": session_id, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "call-1", "content": denied, "is_error": True},
    ]}})
else:
    out = subprocess.run(["bash", script], cwd=cwd, env=env, capture_output=True, text=True)
    event({"type": "user", "session_id": session_id, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "call-1",
         "content": out.stdout + out.stderr, "is_error": out.returncode != 0},
    ]}})

message_file = os.path.join(cwd, ".subagent", "fake_message.txt")
if os.path.exists(message_file):
    with open(message_file) as fh:
        final = fh.read()
    os.remove(message_file)
else:
    final = f"fake worker ({model}) finished"
event({"type": "assistant", "session_id": session_id, "message": {"content": [
    {"type": "text", "text": final},
]}})
event({"type": "result", "subtype": "success", "is_error": False, "result": final,
       "session_id": session_id, "num_turns": 1,
       "usage": {"input_tokens": 100, "output_tokens": 40, "credits": 2.5}})
sys.exit(0)
