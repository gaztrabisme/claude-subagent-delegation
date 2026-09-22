"""Test framework detection, test-count parsing, and test-related config fragments."""

import json
import os
import re
import subprocess
import time

from .common import STATE_DIR, read_json, tail

JS_TEST_GLOBS = [
    "**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/test/**", "**/tests/**",
    "**/jest.config.*", "**/jest.setup.*", "**/vitest.config.*", "**/vitest.setup.*", "**/.mocharc*",
]
PY_TEST_GLOBS = ["**/test_*.py", "**/*_test.py", "**/conftest.py", "**/tests/**", "pytest.ini"]


def _detect_js(root):
    pkg = read_json(root / "package.json")
    if not pkg:
        return None
    script = (pkg.get("scripts") or {}).get("test", "")
    if not script or "no test specified" in script:
        return None
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    framework = next((f for f in ("vitest", "jest", "mocha", "ava") if f in deps), None)
    if framework is None:
        framework = "node:test" if "node --test" in script else "npm-script"
    if (root / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"
    elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        pm = "bun"
    else:
        pm = "npm"
    return {"framework": framework, "test_cmd": f"{pm} test", "test_globs": list(JS_TEST_GLOBS)}


def _has_pytest():
    return subprocess.run(["python3", "-c", "import pytest"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _detect_python(root):
    pyproject = root / "pyproject.toml"
    setup_cfg = root / "setup.cfg"
    signals = [
        pyproject.exists() and "pytest" in pyproject.read_text(errors="ignore"),
        (root / "pytest.ini").exists(),
        (root / "conftest.py").exists(),
        setup_cfg.exists() and "tool:pytest" in setup_cfg.read_text(errors="ignore"),
        any(root.glob("test_*.py")),
        any(root.glob("tests/**/test_*.py")) or any(root.glob("tests/**/*_test.py")),
    ]
    if not any(signals):
        return None
    if (root / "uv.lock").exists():
        runner = "uv run pytest"
    elif (root / "poetry.lock").exists():
        runner = "poetry run pytest"
    elif (root / ".venv/bin/pytest").exists():
        runner = ".venv/bin/pytest"
    elif _has_pytest():
        runner = "python3 -m pytest"
    else:
        # No pytest available: the standard library runner handles unittest-style tests.
        start = "-s tests " if (root / "tests").is_dir() else ""
        return {"framework": "unittest", "test_cmd": f"python3 -m unittest discover {start}-v",
                "test_globs": list(PY_TEST_GLOBS)}
    return {"framework": "pytest", "test_cmd": f"{runner} -q", "test_globs": list(PY_TEST_GLOBS)}


def _detect_go(root):
    if not (root / "go.mod").exists():
        return None
    return {"framework": "go test", "test_cmd": "go test -v ./...",
            "test_globs": ["**/*_test.go", "**/testdata/**"]}


def _detect_rust(root):
    if not (root / "Cargo.toml").exists():
        return None
    # Inline #[cfg(test)] modules live in src/ and cannot be protected by path.
    return {"framework": "cargo test", "test_cmd": "cargo test", "test_globs": ["**/tests/**"]}


def _detect_make(root):
    makefile = root / "Makefile"
    if makefile.exists() and any(l.startswith("test:") for l in makefile.read_text(errors="ignore").splitlines()):
        return {"framework": "make", "test_cmd": "make test", "test_globs": ["**/tests/**", "**/test/**"]}
    return None


DETECTORS = [_detect_js, _detect_python, _detect_go, _detect_rust, _detect_make]


def detect(root, cfg):
    info = {"framework": None, "test_cmd": None, "test_globs": []}
    for detector in DETECTORS:
        found = detector(root)
        if found:
            info = found
            break
    if cfg.get("test_cmd"):
        info["test_cmd"] = cfg["test_cmd"]
        info["framework"] = info["framework"] or "custom"
    if cfg.get("test_globs"):
        info["test_globs"] = list(cfg["test_globs"])
    info["test_globs"] = info["test_globs"] + list(cfg.get("extra_protected") or [])
    # Files written by the test writer (test outline mode) are tests, wherever they live.
    written = (read_json(root / STATE_DIR / "test_writer.json") or {}).get("files", [])
    info["test_globs"] += [f for f in written if f not in info["test_globs"]]
    return info


# ---------------------------------------------------------------- running tests

def _num(pattern, text, flags=0):
    found = re.findall(pattern, text, flags)
    return sum(int(n) for n in found) if found else None


def parse_test_counts(output):
    """Best-effort {total, passed, failed, skipped} from common test runners' output, or None."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)

    # node:test (TAP summary)
    if re.search(r"^# tests \d+", text, re.M):
        total = _num(r"^# tests (\d+)", text, re.M)
        skipped = (_num(r"^# skipped (\d+)", text, re.M) or 0) + (_num(r"^# todo (\d+)", text, re.M) or 0)
        return {"total": total, "passed": _num(r"^# pass (\d+)", text, re.M) or 0,
                "failed": _num(r"^# fail (\d+)", text, re.M) or 0, "skipped": skipped}
    # jest: "Tests:       1 failed, 2 skipped, 5 passed, 8 total"
    m = re.search(r"^Tests:\s+(.*\d+ total)", text, re.M)
    if m:
        part = m.group(1)
        get = lambda k: int((re.search(rf"(\d+) {k}", part) or [0, 0])[1])
        return {"total": get("total"), "passed": get("passed"), "failed": get("failed"),
                "skipped": get("skipped") + get("todo")}
    # vitest: "Tests  1 failed | 5 passed | 1 skipped (7)"
    m = re.search(r"^\s*Tests\s+(.*)\((\d+)\)\s*$", text, re.M)
    if m:
        part = m.group(1)
        get = lambda k: int((re.search(rf"(\d+) {k}", part) or [0, 0])[1])
        return {"total": int(m.group(2)), "passed": get("passed"), "failed": get("failed"),
                "skipped": get("skipped") + get("todo")}
    # unittest: "Ran 12 tests in 0.01s" + "OK (skipped=2)" / "FAILED (failures=1, errors=1)"
    m = re.search(r"^Ran (\d+) tests? in", text, re.M)
    if m:
        total = int(m.group(1))
        verdict = (re.search(r"^(OK|FAILED)\b.*$", text[m.end():], re.M) or [""])[0]
        get = lambda k: int((re.search(rf"{k}=(\d+)", verdict) or [0, 0])[1])
        failed, skipped = get("failures") + get("errors"), get("skipped")
        return {"total": total, "passed": max(0, total - failed - skipped - get("expected failures")),
                "failed": failed, "skipped": skipped}
    # pytest: "3 failed, 10 passed, 2 skipped in 0.12s"
    m = re.search(r"^[=\s]*((?:\d+ \w+(?: \w+)?, )*\d+ \w+(?: \w+)?) in [\d.]+s", text, re.M)
    if m and re.search(r"\d+ (passed|failed|error)", m.group(1)):
        part = m.group(1)
        get = lambda k: int((re.search(rf"(\d+) {k}", part) or [0, 0])[1])
        passed, failed = get("passed"), get("failed") + get("errors?")
        skipped = get("skipped") + get("xfailed") + get("deselected")
        return {"total": passed + failed + skipped + get("xpassed"), "passed": passed,
                "failed": failed, "skipped": skipped}
    # mocha: "5 passing", "2 failing", "1 pending"
    if re.search(r"^\s+\d+ passing", text, re.M):
        passed = _num(r"^\s+(\d+) passing", text, re.M) or 0
        failed = _num(r"^\s+(\d+) failing", text, re.M) or 0
        skipped = _num(r"^\s+(\d+) pending", text, re.M) or 0
        return {"total": passed + failed + skipped, "passed": passed, "failed": failed, "skipped": skipped}
    # cargo: "test result: ok. 5 passed; 0 failed; 1 ignored;" (one line per test binary)
    if re.search(r"^test result:", text, re.M):
        passed = _num(r"^test result:.*? (\d+) passed", text, re.M) or 0
        failed = _num(r"^test result:.*? (\d+) failed", text, re.M) or 0
        skipped = _num(r"^test result:.*? (\d+) ignored", text, re.M) or 0
        return {"total": passed + failed + skipped, "passed": passed, "failed": failed, "skipped": skipped}
    # go test -v: "--- PASS: TestX", "--- FAIL: TestX", "--- SKIP: TestX"
    if re.search(r"^\s*--- (PASS|FAIL|SKIP):", text, re.M):
        passed = len(re.findall(r"^\s*--- PASS:", text, re.M))
        failed = len(re.findall(r"^\s*--- FAIL:", text, re.M))
        skipped = len(re.findall(r"^\s*--- SKIP:", text, re.M))
        return {"total": passed + failed + skipped, "passed": passed, "failed": failed, "skipped": skipped}
    return None


def run_tests(root, test_cmd, log_dir=None, label="test"):
    """Run the test command; returns {passed, exit_code, cmd, counts, output_tail?, log?}."""
    env = {**os.environ, "CI": "1"}  # keeps vitest/jest out of watch mode
    proc = subprocess.run(test_cmd, shell=True, cwd=root, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    result = {"passed": proc.returncode == 0, "exit_code": proc.returncode, "cmd": test_cmd,
              "counts": parse_test_counts(proc.stdout)}
    if log_dir is not None:
        log = log_dir / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        log.write_text(proc.stdout)
        result["log"] = str(log.relative_to(root))
    if proc.returncode != 0:
        result["output_tail"] = tail(proc.stdout)
    return result


# ---------------------------------------------------------------- test config fragments

PACKAGE_JSON_TEST_KEYS = ["jest", "mocha", "ava", "c8", "nyc"]


def _ini_section(text, header_regex):
    """Text of INI/TOML sections whose header matches, up to the next unrelated header."""
    out, keep = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*\[", line):
            keep = bool(re.match(header_regex, line.strip()))
        if keep:
            out.append(line)
    return "".join(out)


def test_config_fragments(root):
    """Test-related settings that live inside otherwise editable files: {'file#key': text}."""
    fragments = {}
    pkg = read_json(root / "package.json")
    if isinstance(pkg, dict):
        fragments["package.json#scripts.test"] = json.dumps((pkg.get("scripts") or {}).get("test"))
        for key in PACKAGE_JSON_TEST_KEYS:
            fragments[f"package.json#{key}"] = json.dumps(pkg.get(key))
    for name, header in (("pyproject.toml", r"^\[tool\.pytest"), ("setup.cfg", r"^\[tool:pytest\]"),
                         ("tox.ini", r"^\[pytest\]")):
        path = root / name
        if path.exists():
            fragments[f"{name}#section"] = _ini_section(path.read_text(errors="ignore"), header)
    return fragments


def restore_config_fragment(root, key, original):
    """Put one fragment back as it was, keeping the rest of the file's changes."""
    name, part = key.split("#", 1)
    path = root / name
    if name == "package.json":
        text = path.read_text()
        pkg = json.loads(text)
        value = json.loads(original)
        if part == "scripts.test":
            pkg.setdefault("scripts", {})
            if value is None:
                pkg["scripts"].pop("test", None)
            else:
                pkg["scripts"]["test"] = value
        elif value is None:
            pkg.pop(part, None)
        else:
            pkg[part] = value
        indent = len(re.match(r"\{\s*\n(\s*)", text).group(1)) if re.match(r"\{\s*\n(\s*)", text) else 2
        path.write_text(json.dumps(pkg, indent=indent) + ("\n" if text.endswith("\n") else ""))
    else:
        header = {"pyproject.toml": r"^\[tool\.pytest", "setup.cfg": r"^\[tool:pytest\]", "tox.ini": r"^\[pytest\]"}[name]
        text = path.read_text(errors="ignore")
        current = _ini_section(text, header)
        if current:
            text = text.replace(current, original, 1)
        else:
            text = text.rstrip("\n") + "\n\n" + original
        path.write_text(text)

