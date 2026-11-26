"""RF5: VerificationResult.as_dict reports sandboxed."""

from __future__ import annotations

from subagent.verify import VerificationResult


def test_rf5_as_dict_includes_sandboxed_true() -> None:
    """RF5: as_dict() carries "sandboxed" equal to the attribute (True)."""
    d = VerificationResult("c", True, "exit 0", 0, sandboxed=True).as_dict()
    assert "sandboxed" in d and d["sandboxed"] is True


def test_rf5_as_dict_includes_sandboxed_false() -> None:
    """RF5: as_dict() carries "sandboxed" equal to the attribute (False)."""
    d = VerificationResult("c", True, "exit 0", 0, sandboxed=False).as_dict()
    assert "sandboxed" in d and d["sandboxed"] is False
