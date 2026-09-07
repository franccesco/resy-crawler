"""Shared fixtures: a compact factory for Resy-style search hits and a temp store."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from resy_busyness import db
from resy_busyness.config import settings
from resy_busyness.scoring import SERVICES

Window = tuple[str, str]
SlotSpec = str | tuple[str, str]


def make_hit(
    venue_id: int = 1,
    name: str = "Venue",
    day: str = "2026-09-12",
    windows: dict[str, Window] | None = None,
    slots: Sequence[SlotSpec] | None = None,
    events: int = 0,
    rating: tuple[float, int] | None = (4.7, 500),
    **extra: Any,
) -> dict[str, Any]:
    """Build a Resy venue-search hit from a compact spec.

    `windows` maps a service name (dinner, lunch, brunch, breakfast) to first and last
    seating; `slots` lists start times, optionally with a seating type.
    """
    notify = [
        {
            "service_type_id": SERVICES[svc],
            "min_request_datetime": f"{day} {start}:00",
            "max_request_datetime": f"{day} {end}:00",
            "step_minutes": 30,
        }
        for svc, (start, end) in (windows or {}).items()
    ]
    slot_rows: list[dict[str, Any]] = []
    for spec in slots or []:
        t, ty = (spec, "Dining Room") if isinstance(spec, str) else spec
        slot_rows.append({"date": {"start": f"{day} {t}:00"}, "config": {"type": ty}})
    hit: dict[str, Any] = {
        "id": {"resy": venue_id},
        "name": name,
        "url_slug": name.lower().replace(" ", "-"),
        "neighborhood": "Mission",
        "cuisine": ["Test"],
        "price_range_id": 2,
        "locality": "San Francisco",
        "location": {"code": "sf", "url_slug": "san-francisco-ca"},
        "contact": {"phone_number": "+14155550100"},
        "rating": {"average": rating[0], "count": rating[1]} if rating else {},
        "availability": {
            "notify_options": notify,
            "slots": slot_rows,
            "events": [{"event_type": 2}] * events,
        },
        "is_tock_inventory": False,
        "source": {"name": None},
        "reopen": {"date": None},
    }
    hit.update(extra)
    return hit


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the store at a temp file and keep the scheduler off."""
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(settings, "db_path", str(path))
    monkeypatch.setattr(settings, "run_interval_minutes", 0)
    yield path
    db.connect(str(path)).close()
