from __future__ import annotations

from pathlib import Path

from kalshi_weather_bot.risk.killswitch import (
    activate,
    deactivate,
    is_killed,
    read_reason,
)


def test_lifecycle(tmp_path: Path) -> None:
    lock = tmp_path / "kill.lock"
    assert not is_killed(lock)
    activate(lock, "manual")
    assert is_killed(lock)
    reason = read_reason(lock)
    assert reason is not None and "manual" in reason
    assert deactivate(lock) is True
    assert not is_killed(lock)
    assert read_reason(lock) is None


def test_deactivate_when_absent_returns_false(tmp_path: Path) -> None:
    lock = tmp_path / "absent.lock"
    assert deactivate(lock) is False


def test_activate_creates_parent_dir(tmp_path: Path) -> None:
    lock = tmp_path / "deeper" / "kill.lock"
    activate(lock, "auto: data_stale")
    assert lock.exists()
