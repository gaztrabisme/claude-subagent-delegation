"""RR-12 M0 fix round 2: a symlink under --projects-dir must not reach files outside it."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.m0._synth import assistant_record, make_usage

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "coord_meter.py"
START = "2026-11-25T09:00:00Z"
END = "2026-11-25T11:00:00Z"
SECRET = 777_000  # distinctive output-token count in transcripts outside projects-dir
NORMAL = 123


@pytest.fixture
def cm() -> Any:
    spec = importlib.util.spec_from_file_location("coord_meter", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, out_tokens: int, msg_id: str = "m") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = assistant_record(uuid=msg_id, timestamp="2026-11-25T10:00:00Z", msg_id=msg_id,
                           usage=make_usage(input_tokens=1, output_tokens=out_tokens))
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _argv(projects: Path, sid: str) -> list[str]:
    return ["--projects-dir", str(projects), "--session", sid, "--start", START, "--end", END]


def _run(cm: Any, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    try:
        code = int(cm.main(argv))
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def _link(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def test_control_normal_session_counts(cm: Any, tmp_path: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    projects = tmp_path / "projects"
    _write(projects / "s1.jsonl", NORMAL)
    code, out = _run(cm, _argv(projects, "s1"), capsys)
    assert code == 0
    assert json.loads(out)["total"]["tokens"]["output"] == NORMAL


def test_session_file_symlink_outside_is_refused(cm: Any, tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    outside = tmp_path / "outside" / "secret.jsonl"
    _write(outside, SECRET)
    _link(outside, projects / "s1.jsonl")
    code, out = _run(cm, _argv(projects, "s1"), capsys)
    assert code == 2
    assert str(SECRET) not in out


def test_subagents_dir_symlink_outside_is_refused(cm: Any, tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    projects = tmp_path / "projects"
    _write(projects / "s1.jsonl", NORMAL)
    outside_dir = tmp_path / "outside_subagents"
    _write(outside_dir / "agent-a1.jsonl", SECRET, msg_id="x")
    _link(outside_dir, projects / "s1" / "subagents")
    code, out = _run(cm, _argv(projects, "s1"), capsys)
    assert code == 2
    assert str(SECRET) not in out


def test_linked_agent_file_in_real_subagents_dir_is_ignored(
        cm: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    projects = tmp_path / "projects"
    _write(projects / "s1.jsonl", NORMAL)
    _write(projects / "s1" / "subagents" / "agent-real.jsonl", 5, msg_id="r")
    outside = tmp_path / "outside" / "agent-evil.jsonl"
    _write(outside, SECRET, msg_id="x")
    _link(outside, projects / "s1" / "subagents" / "agent-evil.jsonl")
    code, out = _run(cm, _argv(projects, "s1"), capsys)
    assert code == 0
    assert str(SECRET) not in out
    assert json.loads(out)["total"]["tokens"]["output"] == NORMAL + 5
