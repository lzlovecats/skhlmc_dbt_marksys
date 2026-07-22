"""Suspend/RTC scheduling without guessing activity from GPU utilisation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from workstation.config import PowerConfig


POWER_OVERRIDE_FILENAME = "power-override.json"
POWER_OVERRIDE_MAX_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class PowerDecision:
    action: str
    wake_epoch: int = 0
    next_check_epoch: int = 0
    reason: str = ""


def read_power_override(state_root: Path, *, now_epoch: int | None = None) -> int:
    path = state_root / POWER_OVERRIDE_FILENAME
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < path.stat().st_size <= 4_096
        ):
            raise ValueError("invalid power override receipt")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "until_epoch",
        } or value.get("schema_version") != 1:
            raise ValueError("invalid power override receipt")
        until_epoch = int(value.get("until_epoch") or 0)
        current = int(time.time() if now_epoch is None else now_epoch)
        if until_epoch <= current or until_epoch > current + POWER_OVERRIDE_MAX_SECONDS:
            return 0
        return until_epoch
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def _at_time(current: datetime, value: str) -> datetime:
    hour, minute = (int(part) for part in value.split(":"))
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_wake_epoch(config: PowerConfig, now: datetime | None = None) -> int:
    zone = ZoneInfo(config.timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    wake = _at_time(current, config.wake_at)
    if wake <= current:
        wake += timedelta(days=1)
    return int(wake.timestamp())


def decide_power_action(
    config: PowerConfig,
    manager_snapshot: dict,
    *,
    now: datetime | None = None,
    override_until_epoch: int = 0,
) -> PowerDecision:
    zone = ZoneInfo(config.timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    if not config.enabled:
        return PowerDecision("none", reason="schedule_disabled")
    if int(override_until_epoch or 0) > int(current.timestamp()):
        return PowerDecision("none", next_check_epoch=int(override_until_epoch), reason="temporary_override")
    suspend_today = _at_time(current, config.suspend_at)
    wake_today = _at_time(current, config.wake_at)
    if suspend_today < wake_today:
        in_suspend_window = suspend_today <= current < wake_today
        wake = wake_today
        next_suspend = (
            suspend_today if current < suspend_today
            else suspend_today + timedelta(days=1)
        )
    else:
        # An overnight schedule has two calendar-day pieces: from suspend
        # until midnight, and from midnight until wake.  The latter belongs to
        # the suspend occurrence on the previous date.
        in_suspend_window = current >= suspend_today or current < wake_today
        wake = (
            wake_today + timedelta(days=1)
            if current >= suspend_today else wake_today
        )
        next_suspend = suspend_today
    if not in_suspend_window:
        return PowerDecision(
            "none",
            next_check_epoch=int(next_suspend.timestamp()),
            reason=(
                "before_suspend_window"
                if current < suspend_today else "outside_suspend_window"
            ),
        )
    active = bool(
        manager_snapshot.get("active_operation")
        or manager_snapshot.get("voice_session_active")
        or manager_snapshot.get("voice_session_pending")
        or manager_snapshot.get("sleep_inhibited")
    )
    if active:
        return PowerDecision("delay", next_check_epoch=int(current.timestamp()) + 60, reason="managed_work_active")
    return PowerDecision("suspend", wake_epoch=int(wake.timestamp()), reason="scheduled_idle")
