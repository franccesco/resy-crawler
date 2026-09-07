"""Compare dbt's fct_night_boxes with the app's scoring for the latest finished run.

Run from the repository root after `dbt run`:

    uv run python analytics/crosscheck.py

Exits non-zero when any (venue, night) disagrees on boxes or open boxes.
"""

from __future__ import annotations

import os
import sys
from datetime import date

import duckdb

from resy_busyness import db
from resy_busyness.models import Observation
from resy_busyness.scoring import SERVICES, score_night

ANALYTICS_DB = os.environ.get("RESY_ANALYTICS_DB", "data/analytics.duckdb")


def main() -> int:
    """Cross-check every version dbt scored against `score_night`.

    Returns:
        Process exit code: 0 when all rows agree, 1 otherwise.

    """
    con = duckdb.connect(ANALYTICS_DB, read_only=True)
    rows: list[tuple[int, int, int]] = con.execute(
        "SELECT state_id, boxes, open_boxes FROM fct_night_boxes"
    ).fetchall()
    con.close()
    dbt_by_id = {int(sid): (int(b), int(o)) for sid, b, o in rows}

    conn = db.connect()
    try:
        stored = conn.execute(
            "SELECT id, service_day, state_json FROM venue_day_state"
        ).fetchall()
    finally:
        conn.close()

    mismatches: list[str] = []
    checked = 0
    for r in stored:
        sid = int(r["id"])
        if sid not in dbt_by_id:
            mismatches.append(f"state {sid}: missing from fct_night_boxes")
            continue
        obs = Observation.model_validate_json(r["state_json"])
        night = score_night(
            obs, date.fromisoformat(r["service_day"]), {SERVICES["dinner"]}, 30
        )
        expected = (night.boxes, night.open_boxes)
        checked += 1
        if dbt_by_id[sid] != expected:
            mismatches.append(
                f"state {sid} {r['service_day']}: dbt {dbt_by_id[sid]} "
                f"python {expected}"
            )
    print(f"checked {checked} versions, {len(mismatches)} mismatch(es)")
    for line in mismatches[:20]:
        print("  " + line)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
