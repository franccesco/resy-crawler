"""One end-to-end read: seeded store in, ranked table and windows out. No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from resy_busyness import db
from resy_busyness.api import app
from resy_busyness.scoring import observe, venue_attrs

from .conftest import make_hit

NIGHTS = ["2026-09-08", "2026-09-09"]
VERIFY = "2026-09-28"
PARAMS = {
    "party_sizes": [2],
    "days": 2,
    "verify_offset_days": 21,
    "start_day": NIGHTS[0],
    "source": "manual",
}
DINNER = {"dinner": ("17:00", "22:00")}


def _get(client: TestClient, url: str) -> dict[str, Any]:
    # starlette's TestClient is loosely typed in this release; contain that here.
    text = cast("str", client.get(url).text)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return cast("dict[str, Any]", json.loads(text))


def _seed_run(
    conn: Any, run_id: int, hits_by_day: dict[str, list[dict[str, Any]]]
) -> None:
    assert db.create_run(conn, PARAMS) == run_id
    db.update_run(conn, run_id, started_at="2026-09-07T18:00:00+00:00")
    for day, hits in hits_by_day.items():
        for hit in hits:
            vid = int(hit["id"]["resy"])
            db.upsert_venue(conn, run_id, vid, venue_attrs(hit))
            r = hit["rating"]
            db.upsert_rating(conn, run_id, vid, r["average"], r["count"])
            db.upsert_day_state(conn, run_id, vid, day, 2, observe(hit))
    db.update_run(
        conn, run_id, status="done", finished_at=f"2026-09-07T18:0{run_id}:00+00:00"
    )


def _world(sold_out_slots_night_one: list[str]) -> dict[str, list[dict[str, Any]]]:
    def hits(day: str, extra_a: list[str]) -> list[dict[str, Any]]:
        return [
            make_hit(1, "Sold Out", day, DINNER, extra_a),
            make_hit(
                2,
                "Half Open",
                day,
                DINNER,
                ["17:00", "18:00", "19:00", "20:00", "21:00"],
            ),
            make_hit(3, "Shut", day, DINNER, [], reopen={"date": "2026-12-01"}),
        ]

    return {
        NIGHTS[0]: hits(NIGHTS[0], sold_out_slots_night_one),
        NIGHTS[1]: hits(NIGHTS[1], []),
        VERIFY: [
            make_hit(1, "Sold Out", VERIFY, DINNER, ["19:00", "20:00"]),
            make_hit(2, "Half Open", VERIFY, DINNER, ["19:00"]),
            make_hit(3, "Shut", VERIFY, DINNER, [], reopen={"date": "2026-12-01"}),
        ],
    }


def test_results_rank_by_score_and_windows_flag_new_openings(tmp_db: Path) -> None:
    conn = db.connect(str(tmp_db))
    _seed_run(conn, 1, _world([]))
    _seed_run(conn, 2, _world(["19:00"]))
    conn.close()

    client = TestClient(app)
    run1 = _get(client, "/api/results?run_id=1")
    assert [r["name"] for r in run1["rows"] if not r["excluded"]] == [
        "Sold Out",
        "Half Open",
    ]
    top = run1["rows"][0]
    assert top["score"] == pytest.approx(1.0)
    assert top["verified_far_out_open"] == 2
    assert "inventory exists" in top["evidence"]
    assert run1["rows"][1]["score"] == round(6 / 11, 4)
    shut = next(r for r in run1["rows"] if r["excluded"])
    assert shut["reason"] == "closed"
    assert "2026-12-01" in shut["evidence"]

    latest = _get(client, "/api/results")
    assert latest["run_id"] == 2
    assert latest["rows"][0]["score"] == round((10 / 11 + 1.0) / 2, 4)

    win = _get(client, "/api/venues/1/windows")
    nights: list[dict[str, Any]] = win["nights"]
    by_day = {n["day"]: n for n in nights}
    assert by_day[NIGHTS[0]]["open_times"] == ["19:00"]
    assert by_day[NIGHTS[0]]["new_times"] == ["19:00"]
    assert by_day[NIGHTS[1]]["new_times"] == []
    assert win["phone"] == "+14155550100"
    assert win["new_total"] == 1
