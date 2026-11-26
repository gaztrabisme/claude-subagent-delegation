"""`subagent graph ...` against a dev Temporal server; the CLI runs as a real subprocess."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import make_settings

pytest.importorskip("temporalio")

from .conftest import (  # noqa: E402
    FakeApp,
    call,
    cli_json,
    in_process_worker,
    local_temporal,
    run_cli_async,
    until,
)

INPUT_DOC = {
    "schema": "subagent.graph/1", "id": "inputs-demo", "version": 1,
    "inputs": {"n": 0, "s": "", "j": {}, "flag": False, "raw": ""},
    "body": [{"id": "g", "kind": "gate",
              "condition": {"path": "inputs.n", "op": "exists"}}],
    "output": {"n": "inputs.n", "s": "inputs.s", "j": "inputs.j", "flag": "inputs.flag",
               "raw": "inputs.raw"},
}

ASK_DOC = {
    "schema": "subagent.graph/1", "id": "ask-demo", "version": 1,
    "body": [{"id": "ask", "kind": "owner_decision", "question": "Go?",
              "options": ["go", "stop"]}],
    "output": {"answer": "nodes.ask.output.answer"},
}


def test_cli_verbs_print_the_tool_json_and_exit_with_the_documented_codes(tmp_path, graph_cfg):
    async def scenario():
        from subagent.graph.mcp_tools import register

        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.server import GraphSettings
        from subagent.graph.service import GraphService

        async with local_temporal(tmp_path) as env:
            settings = make_settings(tmp_path)
            address = env.client.service_client.config.target_host
            graph_cfg(address=address, admit_poll_s=0.2)
            gsettings = GraphSettings(state_dir=tmp_path / "state", address=address,
                                      autostart=False, admit_poll_s=0.2)
            service = GraphService(settings, gsettings=gsettings)
            app = FakeApp()
            register(app, service_factory=lambda: service)

            async def tool(name, **kwargs):
                return await call(app.tools[name], **kwargs)

            async def cli(*args):
                return await run_cli_async(["graph", *args], tmp_path)

            input_file = tmp_path / "inputs.json"
            input_file.write_text(json.dumps(INPUT_DOC), encoding="utf-8")
            ask_file = tmp_path / "ask.json"
            ask_file.write_text(json.dumps(ASK_DOC), encoding="utf-8")

            async with in_process_worker(settings, gsettings, FakeDispatcher()):
                # -- start with --input K=V (JSON when it parses, else a string) -------
                proc = await cli("start", str(input_file), "--input", "n=3", "--input", "s=hello",
                                 "--input", 'j={"a": [1, 2]}', "--input", "flag=true",
                                 "--input", "raw=not json {", "--request-id", "cli-1", "--json")
                assert proc.returncode == 0, proc.stdout + proc.stderr
                started = cli_json(proc)
                assert started["ok"] is True and started["attached"] is False
                inputs_root = started["root_run_id"]

                async def finished():
                    res = await tool("graph_status", root_run_id=inputs_root)
                    return res if res["state"] == "completed" else None

                done = await until(finished, timeout=60, what="the inputs graph to complete")
                assert done["result"]["output"] == {
                    "n": 3, "s": "hello", "j": {"a": [1, 2]}, "flag": True, "raw": "not json {"}

                # -- status / list print exactly the tool's dict ----------------------
                for extra, kwargs in (([], {}), (["--detail"], {"detail": True})):
                    proc = await cli("status", inputs_root, *extra, "--json")
                    assert proc.returncode == 0, proc.stdout + proc.stderr
                    expected = json.loads(json.dumps(
                        await tool("graph_status", root_run_id=inputs_root, **kwargs)))
                    assert cli_json(proc) == expected

                proc = await cli("list", "--json")
                assert proc.returncode == 0
                assert [r["root_run_id"] for r in cli_json(proc)["runs"]] == [inputs_root]
                table = await cli("list")
                assert table.returncode == 0 and table.stdout.strip()

                # -- an owner decision, answered through the CLI ---------------------
                proc = await cli("start", str(ask_file), "--request-id", "cli-2", "--json")
                assert proc.returncode == 0, proc.stdout + proc.stderr
                ask_root = cli_json(proc)["root_run_id"]

                async def one_exception():
                    res = await tool("graph_exceptions", root_run_id=ask_root)
                    return res if res["exceptions"] else None

                listed = await until(one_exception, timeout=30, what="the owner decision")
                (rec,) = listed["exceptions"]
                proc = await cli("exceptions", "--root", ask_root, "--json")
                assert proc.returncode == 0
                assert cli_json(proc)["exceptions"] == json.loads(json.dumps(listed["exceptions"]))

                bad = await cli("answer", rec["exception_id"], "maybe", "--json")
                assert bad.returncode == 2
                assert cli_json(bad)["error"]["code"] == "invalid_answer"
                missing = await cli("answer", "gx-000000000000", "go", "--json")
                assert missing.returncode == 4
                assert cli_json(missing)["error"]["code"] == "not_found"
                good = await cli("answer", rec["exception_id"], "go", "--note", "ok",
                                 "--by", "cli-tester", "--json")
                assert good.returncode == 0, good.stdout + good.stderr
                body = cli_json(good)
                assert body["ok"] is True and body["exception_id"] == rec["exception_id"]
                assert isinstance(body["delivered"], bool)
                clash = await cli("answer", rec["exception_id"], "stop", "--json")
                assert clash.returncode == 4
                assert cli_json(clash)["error"]["code"] == "already_answered"

                async def ask_done():
                    res = await tool("graph_status", root_run_id=ask_root)
                    return res if res["state"] == "completed" else None

                await until(ask_done, timeout=60, what="the answered graph to complete")
                answered = (await tool("graph_exceptions", root_run_id=ask_root,
                                       state="all"))["exceptions"][0]
                assert answered["answered_by"] == "cli-tester"
                assert answered["note"] == "ok"

                # -- closed and unknown roots, bad arguments ---------------------------
                for verb in (["pause", ask_root], ["resume", ask_root],
                             ["cancel", ask_root, "--reason", "late"]):
                    proc = await cli(*verb, "--json")
                    assert proc.returncode == 4, (verb, proc.stdout, proc.stderr)
                    assert cli_json(proc)["error"]["code"] == "conflict"
                proc = await cli("status", "gr-ffffffffffff", "--json")
                assert proc.returncode == 4
                assert cli_json(proc)["error"]["code"] == "not_found"
                proc = await cli("list", "--limit", "0", "--json")
                assert proc.returncode == 2
                assert cli_json(proc)["error"]["code"] == "invalid_argument"
                proc = await cli("cancel", ask_root, "--json")  # --reason missing
                assert proc.returncode == 2

                # -- pause, resume, cancel on a live root --------------------------------
                proc = await cli("start", str(ask_file), "--request-id", "cli-3", "--json")
                live = cli_json(proc)["root_run_id"]
                proc = await cli("pause", live, "--reason", "lunch", "--json")
                assert proc.returncode == 0, proc.stdout + proc.stderr
                assert cli_json(proc) == {"ok": True, "root_run_id": live, "state": "paused",
                                          "accepted": True}
                proc = await cli("resume", live, "--json")
                assert proc.returncode == 0
                assert cli_json(proc)["state"] == "running"
                proc = await cli("cancel", live, "--reason", "enough", "--json")
                assert proc.returncode == 0
                assert cli_json(proc) == {"ok": True, "root_run_id": live, "state": "cancelling",
                                          "accepted": True}

                async def cancelled():
                    res = await tool("graph_status", root_run_id=live)
                    return res if res["state"] == "cancelled" else None

                await until(cancelled, timeout=30, what="state cancelled")

    asyncio.run(scenario())
