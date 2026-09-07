"""Hourly runs inside the API process.

A crontab line calling `uv run resy-run` is the alternative.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import db, pipeline
from .config import settings
from .models import RunParams, SchedulerStatus

POLL_SECONDS = 15


@dataclass
class _State:
    next_run_at: datetime | None = None
    last_run_id: int | None = None
    thread: threading.Thread | None = None


_state = _State()


def status() -> SchedulerStatus:
    """Describe the scheduler and whether a run is active.

    Returns:
        The scheduler status.

    """
    conn = db.connect()
    try:
        active = db.active_run(conn)
    finally:
        conn.close()
    return SchedulerStatus(
        enabled=settings.run_interval_minutes > 0,
        interval_minutes=settings.run_interval_minutes,
        next_run_at=_state.next_run_at,
        last_run_id=_state.last_run_id,
        active_run_id=int(active["id"]) if active else None,
    )


def _loop() -> None:
    interval = timedelta(minutes=settings.run_interval_minutes)
    if not settings.run_on_start:
        _state.next_run_at = datetime.now(UTC) + interval
    while True:
        due = _state.next_run_at is None or datetime.now(UTC) >= _state.next_run_at
        if due:
            params = RunParams(
                party_sizes=settings.schedule_party_sizes,
                days=settings.days,
                verify_offset_days=settings.verify_offset_days,
                source="schedule",
            )
            rid = pipeline.start_run(params)
            if rid is not None:
                _state.last_run_id = rid
            _state.next_run_at = datetime.now(UTC) + interval
        time.sleep(POLL_SECONDS)


def start() -> None:
    """Start the scheduler thread once, if an interval is configured."""
    if settings.run_interval_minutes <= 0 or _state.thread is not None:
        return
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    _state.thread = t
    t.start()
