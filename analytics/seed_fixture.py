"""Build a small synthetic store so dbt can be built and tested without Resy.

Used by CI. Writes to `DB_PATH` (default `data/resy.sqlite`); refuses to overwrite an
existing file unless `--force` is given.

    DB_PATH=data/ci.sqlite uv run python analytics/seed_fixture.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from resy_busyness import db
from resy_busyness.config import settings
from resy_busyness.scoring import observe, venue_attrs
from tests.conftest import make_hit

START = date(2026, 9, 8)
NIGHTS = [(START + timedelta(days=i)).isoformat() for i in range(7)]
VERIFY = (START + timedelta(days=20)).isoformat()
DINNER = {"dinner": ("17:00", "22:00")}
LUNCH_AND_DINNER = {"lunch": ("11:30", "14:30"), "dinner": ("17:00", "22:00")}
PARAMS: dict[str, Any] = {
    "party_sizes": [2],
    "days": 7,
    "verify_offset_days": 20,
    "start_day": START.isoformat(),
    "source": "manual",
}


def _hits(day: str, run: int) -> list[dict[str, Any]]:
    """Build four venues with distinct stories for one day.

    Venue 2 gains a 19:00 table in run 2 on Sep 12 so fill speed has a row.

    Args:
        day: Service day, ISO format.
        run: 1 or 2.

    Returns:
        Resy-style search hits.

    """
    sold_out: list[str] = ["19:00", "20:30"] if day == VERIFY else []
    half: list[str] = ["17:00", "17:15", "18:00", "20:00", "21:30"]
    if run == 2 and day == "2026-09-12":
        half = [*half, "19:00"]
    return [
        make_hit(1, "Sold Out Counter", day, DINNER, sold_out, rating=(4.9, 800)),
        make_hit(2, "Half Open Bistro", day, LUNCH_AND_DINNER, half, rating=(4.6, 120)),
        make_hit(3, "Shut For Season", day, DINNER, [], reopen={"date": "2026-12-01"}),
        make_hit(4, "Lunch Only Cafe", day, {"lunch": ("11:00", "14:00")}, ["12:00"]),
    ]


def seed(path: str) -> None:
    """Create two finished runs in a fresh store at `path`.

    Args:
        path: SQLite file to create.

    """
    conn = db.connect(path)
    try:
        for run in (1, 2):
            run_id = db.create_run(conn, PARAMS)
            db.update_run(conn, run_id, started_at=f"2026-09-07T1{run}:00:00+00:00")
            for day in [*NIGHTS, VERIFY]:
                for hit in _hits(day, run):
                    vid = int(hit["id"]["resy"])
                    db.upsert_venue(conn, run_id, vid, venue_attrs(hit))
                    rating: dict[str, Any] = hit["rating"]
                    db.upsert_rating(
                        conn, run_id, vid, rating.get("average"), rating.get("count")
                    )
                    db.upsert_day_state(conn, run_id, vid, day, 2, observe(hit))
            db.log(conn, run_id, f"seeded run {run}", "done")
            db.update_run(
                conn,
                run_id,
                status="done",
                finished_at=f"2026-09-07T1{run}:05:00+00:00",
                requests_total=16,
                requests_done=16,
                venues_seen=4,
            )
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    """Seed the store named by `DB_PATH`.

    Args:
        argv: Command-line arguments; `--force` allows overwriting.

    Returns:
        Process exit code.

    """
    target = Path(settings.db_path)
    if target.exists() and "--force" not in argv:
        print(f"{target} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    if target.exists():
        target.unlink()
    seed(str(target))
    print(f"seeded {target}: 2 runs, 4 venues, {len(NIGHTS)} nights + verify day")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
