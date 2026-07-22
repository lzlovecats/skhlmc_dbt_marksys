"""Fail-closed managed-directory size and quota helpers for Ubuntu."""

from __future__ import annotations

from pathlib import Path
import subprocess


def directory_bytes(path: Path) -> int | None:
    """Return GNU ``du`` apparent bytes without crossing filesystems.

    Workstation v1 targets Ubuntu 24.04, where GNU coreutils provides these
    fixed flags.  An unreadable directory is reported as unknown; callers that
    gate writes must treat unknown as failure.
    """
    if not path.exists():
        return 0
    try:
        result = subprocess.run(
            ["du", "--bytes", "--summarize", "--one-file-system", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            return None
        return max(0, int(result.stdout.split()[0]))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def quota_entry(usage: int | None, quota: int) -> dict:
    return {
        "ok": usage is not None and usage <= int(quota),
        "usage_bytes": usage,
        "quota_bytes": int(quota),
        "remaining_bytes": None if usage is None else max(0, int(quota) - usage),
    }
