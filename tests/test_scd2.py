"""SCD type 2: store only change, read as of any run."""

from __future__ import annotations

from pathlib import Path

from resy_busyness import db
from resy_busyness.scoring import observe

from .conftest import make_hit

DAY = "2026-09-12"


def test_versions_open_only_on_change_and_reads_are_point_in_time(
    tmp_db: Path,
) -> None:
    conn = db.connect(str(tmp_db))
    first = observe(make_hit(windows={"dinner": ("17:00", "22:00")}, slots=["19:00"]))
    same = observe(make_hit(windows={"dinner": ("17:00", "22:00")}, slots=["19:00"]))
    changed = observe(
        make_hit(windows={"dinner": ("17:00", "22:00")}, slots=["19:00", "20:30"])
    )

    assert db.upsert_day_state(conn, 1, 7, DAY, 2, first) is True
    assert db.upsert_day_state(conn, 2, 7, DAY, 2, same) is False
    assert db.upsert_day_state(conn, 3, 7, DAY, 2, changed) is True
    conn.commit()

    rows = conn.execute(
        "SELECT is_current, valid_to, first_run_id, last_seen_run_id "
        "FROM venue_day_state ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (0, rows[0]["valid_to"], 1, 2),
        (1, None, 3, 3),
    ]
    assert rows[0]["valid_to"] is not None

    as_of_2 = db.states_as_of_run(conn, 2, 2, [DAY])[7][DAY]
    as_of_3 = db.states_as_of_run(conn, 3, 2, [DAY])[7][DAY]
    assert [s.t for s in as_of_2.slots] == ["19:00"]
    assert [s.t for s in as_of_3.slots] == ["19:00", "20:30"]

    prev = db.previous_state(conn, 3, 7, DAY, 2)
    assert prev is not None and prev["last_seen_run_id"] == 2
    assert db.previous_state(conn, 2, 7, DAY, 2) is None
    conn.close()
