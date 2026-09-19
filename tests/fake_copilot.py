#!/usr/bin/env python3
"""Fake Copilot CLI for the test suite.

Prints Copilot-style JSON events and runs the bash scenario $FAKE_SCRIPT in the -C directory, with
the model in $FAKE_MODEL and the prompt in $FAKE_PROMPT, so a scenario can act as worker, test
writer or reviewer. Options:
  FAKE_FAIL_MODEL=<model>   exit with an "unavailable model" error for that model
  .delegate/fake_message.txt  if the scenario writes it, it becomes the final chat message
"""

import json
import os
import subprocess
import sys

args = sys.argv[1:]
cwd = args[args.index("-C") + 1] if "-C" in args else "."
model = args[args.index("--model") + 1] if "--model" in args else "default"
prompt = args[args.index("-p") + 1] if "-p" in args else ""


def event(kind, data):
    print(json.dumps({"type": kind, "data": data, "timestamp": "2026-09-19T05:00:00Z"}), flush=True)


if os.environ.get("FAKE_FAIL_MODEL") == model:
    print(f'Error: Model "{model}" from --model flag is not available.', flush=True)
    sys.exit(1)

event("assistant.turn_start", {"turnId": "0"})
event("tool.execution_start", {"toolCallId": "1", "toolName": "bash", "arguments": {"command": "fake work"}})
env = {**os.environ, "FAKE_MODEL": model, "FAKE_PROMPT": prompt}
out = subprocess.run(["bash", os.environ["FAKE_SCRIPT"]], cwd=cwd, env=env, capture_output=True, text=True)
event("tool.execution_partial_result", {"toolCallId": "1", "partialOutput": out.stdout + out.stderr})
event("tool.execution_complete", {"toolCallId": "1", "success": out.returncode == 0})
message_file = os.path.join(cwd, ".delegate", "fake_message.txt")
if os.path.exists(message_file):
    with open(message_file) as fh:
        event("assistant.message", {"content": fh.read()})
    os.remove(message_file)
else:
    event("assistant.message", {"content": f"fake worker ({model}) finished"})
event("session.usage_checkpoint", {"totalNanoAiu": 2500000000})
sys.exit(0)
