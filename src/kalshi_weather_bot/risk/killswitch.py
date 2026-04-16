"""File-based kill switch.

Presence of the lock file halts order placement for all modes. Keeps the
answer simple: an operator (or another process, via ``activate``) can halt
trading without needing IPC or a running daemon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def is_killed(lock_path: Path) -> bool:
    return lock_path.exists()


def activate(lock_path: Path, reason: str) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).isoformat()
    lock_path.write_text(f"{ts} {reason}\n")


def deactivate(lock_path: Path) -> bool:
    if lock_path.exists():
        lock_path.unlink()
        return True
    return False


def read_reason(lock_path: Path) -> str | None:
    if not lock_path.exists():
        return None
    return lock_path.read_text().strip()


__all__ = ["activate", "deactivate", "is_killed", "read_reason"]
