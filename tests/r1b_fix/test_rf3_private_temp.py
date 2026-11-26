"""RF3 (F3): verification gets a private per-call temp dir, not the shared per-user temp."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from tests.r1b_fix._helpers import HEADER, run_script

_SIBLING = HEADER + """
target = Path(T["sibling"]) / "rf3-escape.txt"
try:
    target.write_text("escaped", encoding="utf-8")
    R["sibling"] = "written"
except PermissionError as exc:
    R["sibling"] = f"PermissionError:{exc.errno}"
except OSError as exc:
    R["sibling"] = f"OSError:{exc.errno}"
done()
"""

_TMPENV = HEADER + """
import os
R["tmpdir"] = os.environ.get("TMPDIR")
R["exists_during"] = bool(R["tmpdir"]) and Path(R["tmpdir"]).is_dir()
done()
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_rf3_sibling_of_shared_temp_is_not_writable(tmp_path) -> None:
    """RF3: a verification command cannot write into a sibling folder in the shared per-user temp."""
    with tempfile.TemporaryDirectory(prefix="rf3-sibling-") as sib:
        sibling = Path(sib)
        _, result, data = run_script(tmp_path, _SIBLING, targets={"sibling": str(sibling)})
        assert result.sandboxed is True
        assert data.get("sibling", "").startswith("PermissionError"), data
        assert not (sibling / "rf3-escape.txt").exists()


def test_rf3_tmpdir_is_private_unique_and_removed(tmp_path) -> None:
    """RF3: TMPDIR is a private dir (not the shared temp root), unique per call, removed afterwards."""
    shared = Path(tempfile.gettempdir()).resolve()
    seen = [run_script(tmp_path, _TMPENV)[2] for _ in range(2)]
    for data in seen:
        tmpdir = data.get("tmpdir")
        assert tmpdir, f"no TMPDIR in child env: {data}"
        resolved = Path(tmpdir).resolve()
        assert resolved != shared, "TMPDIR is the shared temp root"
        assert data["exists_during"] is True
        assert not resolved.exists(), "private temp dir was not removed after the call"
    assert seen[0]["tmpdir"] != seen[1]["tmpdir"], "TMPDIR is not unique per call"
