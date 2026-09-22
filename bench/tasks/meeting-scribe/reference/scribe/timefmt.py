"""Timestamp and duration formatting."""


def format_timestamp(seconds: float) -> str:
    """``MM:SS`` under one hour, ``H:MM:SS`` from one hour up."""
    total = int(seconds)
    ss = total % 60
    mm = (total // 60) % 60
    hh = total // 3600
    if hh:
        return f"{hh}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def format_duration(seconds: float) -> str:
    """Compact duration: ``45s``, ``1m12s``, ``1h2m5s``."""
    d = int(seconds)
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m{d % 60}s"
    return f"{d // 3600}h{(d % 3600) // 60}m{d % 60}s"
