"""Hourly runs inside the API process. A crontab line calling `uv run resy-run` is the alternative."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, pipeline
from .config import settings
from .models import RunParams

_state = {"next_run_at": None, "last_run_id": None, "thread": None}


def status() -> dict:
    conn = db.connect()
    try:
        active = db.active_run(conn)
    finally:
        conn.close()
    return {
        "enabled": settings.run_interval_minutes > 0,
        "interval_minutes": settings.run_interval_minutes,
        "next_run_at": _state["next_run_at"],
        "last_run_id": _state["last_run_id"],
        "active_run_id": active["id"] if active else None,
    }


def _loop() -> None:
    interval = timedelta(minutes=settings.run_interval_minutes)
    if not settings.run_on_start:
        _state["next_run_at"] = datetime.now(timezone.utc) + interval
    while True:
        if _state["next_run_at"] is None or datetime.now(timezone.utc) >= _state["next_run_at"]:
            rid = pipeline.start_run(RunParams(party_sizes=settings.schedule_party_sizes, days=settings.days,
                                               verify_offset_days=settings.verify_offset_days, source="schedule"))
            if rid is not None:
                _state["last_run_id"] = rid
            _state["next_run_at"] = datetime.now(timezone.utc) + interval
        time.sleep(15)


def start() -> None:
    if settings.run_interval_minutes <= 0 or _state["thread"]:
        return
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    _state["thread"] = t
    t.start()
