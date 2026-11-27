"""Child of test_state_dir_modes_stress: one full-run scenario; prints a JSON verdict line.

    python -m tests.graph.n1._modes_child <scratch-dir>
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path


async def scenario(tmp_path: Path) -> dict:
    from subagent.graph.dispatch import FakeDispatcher
    from subagent.graph.server import DevServer
    from subagent.graph.service import GraphService
    from tests.graph.e_crash._cluster import free_loopback_port
    from tests.graph.f_tools_cli.conftest import until
    from tests.graph.h_fix1.conftest import TraceSink, boot, traced_worker, two_task_doc

    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    os.chmod(state, 0o755)
    server = DevServer(state, binary=Path(os.environ["TEMPORAL_BIN"]),
                       address=f"127.0.0.1:{free_loopback_port()}", startup_timeout_s=60.0)
    info = server.start()
    try:
        settings, gsettings, workspace = boot(None, tmp_path, address=info.address)
        service = GraphService(settings, gsettings=gsettings)
        async with traced_worker(settings, gsettings, FakeDispatcher(), TraceSink()):
            started = await service.start(two_task_doc(workspace), request_id="n1-perm")
            root = started["root_run_id"]

            async def done():
                res = await service.status(root)
                return res if res.get("state") in ("completed", "failed", "cancelled") else None

            final = await until(done, timeout=80, what="a finished run")
    finally:
        server.stop(timeout_s=10)
    wider, narrower = [], []
    for path in [state, *sorted(state.rglob("*"))]:
        if path != state and (path.name.startswith("temporal.db") or path.is_symlink()):
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        want = 0o700 if path.is_dir() else 0o600
        name = "." if path == state else str(path.relative_to(state))
        if mode & ~want:
            wider.append(f"{name} {oct(mode)} want {oct(want)}")
        elif mode != want:
            narrower.append(f"{name} {oct(mode)} want {oct(want)}")
    return {"state": final["state"], "wider": wider, "narrower": narrower}


if __name__ == "__main__":
    out = asyncio.run(scenario(Path(sys.argv[1])))
    print("RESULT " + json.dumps(out), flush=True)
