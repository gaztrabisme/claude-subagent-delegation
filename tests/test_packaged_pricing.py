"""Default pricing is present and loadable from the installed package."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from subagent.telemetry import cost

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_pricing_has_known_provider_costs():
    pricing = cost.load_pricing(cost.DEFAULT_PRICING, None)
    monthly_usd = pricing.providers["glm"]["monthly_usd"]
    assert isinstance(monthly_usd, (int, float)) and not isinstance(monthly_usd, bool)
    deepseek = pricing.providers["deepseek"]
    for key in ("input", "output", "cache_read"):
        assert isinstance(deepseek[key], (int, float))


def test_packaged_pricing_loads_without_repository_scripts_directory(tmp_path):
    site_packages = tmp_path / "site-packages"
    shutil.copytree(
        ROOT / "src" / "subagent",
        site_packages / "subagent",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    assert not (tmp_path / "scripts").exists()

    script = (
        "from subagent.telemetry.cost import DEFAULT_PRICING, load_pricing; "
        "assert DEFAULT_PRICING.is_file(); "
        "assert load_pricing(DEFAULT_PRICING, None).providers['glm']['monthly_usd'] == 80"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(site_packages)
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True)
