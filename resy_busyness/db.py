"""SQLite store with slowly-changing-dimension type 2 tables.

Resy has no "changed since" endpoint, so every run re-reads the full state of every
venue for every day in the window. What we *store* is only change: a venue's
descriptive attributes and its per-night raw observation each get a new row only when
their hash differs from the current row. Unchanged observations bump `last_seen_run_id`.

A row is valid for run R when first_run_id <= R <= last_seen_run_id, which gives
point-in-time reads without timestamp arithmetic.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .config import settings
from .models import Observation, VenueAttrs

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  params_json TEXT NOT NULL,
  requests_total INTEGER DEFAULT 0,
  requests_done INTEGER DEFAULT 0,
  venues_seen INTEGER DEFAULT 0,
  states_new INTEGER DEFAULT 0,
  states_unchanged INTEGER DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts TEXT NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL, tag TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS venues (
  venue_id INTEGER NOT NULL,
  attrs_json TEXT NOT NULL,
  hash TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  first_run_id INTEGER NOT NULL,
  last_seen_run_id INTEGER NOT NULL,
  PRIMARY KEY (venue_id, valid_from)
);
CREATE INDEX IF NOT EXISTS venues_current ON venues(venue_id, is_current);
CREATE TABLE IF NOT EXISTS venue_ratings (
  run_id INTEGER NOT NULL, venue_id INTEGER NOT NULL, avg REAL, count INTEGER,
  PRIMARY KEY (run_id, venue_id)
);
-- SCD2: one row per version of the raw observation for (venue, day, party size).
CREATE TABLE IF NOT EXISTS venue_day_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue_id INTEGER NOT NULL,
  service_day TEXT NOT NULL,
  party_size INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  hash TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  first_run_id INTEGER NOT NULL,
  last_seen_run_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS vds_current
  ON venue_day_state(venue_id, service_day, party_size, is_current);
CREATE INDEX IF NOT EXISTS vds_runs
  ON venue_day_state(party_size, first_run_id, last_seen_run_id);
"""


def now() -> str:
    """Return the current UTC time as an ISO string with second precision.

    Returns:
        ISO 8601 timestamp.

    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the store, creating the schema if needed.

    Args:
        path: SQLite file; defaults to `DB_PATH`.

    Returns:
        A connection with `sqlite3.Row` rows and WAL journaling.

    """
    p = path or settings.db_path
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---- runs -------------------------------------------------------------------


def create_run(conn: sqlite3.Connection, params: dict[str, Any]) -> int:
    """Insert a queued run.

    Args:
        conn: Open connection.
        params: Serialized `RunParams`.

    Returns:
        The new run id.

    """
    cur = conn.execute(
        "INSERT INTO runs(status, started_at, params_json) VALUES ('queued', ?, ?)",
        (now(), json.dumps(params, default=str)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    """Update columns of a run row and commit.

    Args:
        conn: Open connection.
        run_id: The run to update.
        **fields: Column name to value.

    """
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    """Fetch one run row.

    Args:
        conn: Open connection.
        run_id: The run id.

    Returns:
        The row, or None.

    """
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def active_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the queued or running run, if any.

    Args:
        conn: Open connection.

    Returns:
        The row, or None.

    """
    return conn.execute(
        "SELECT * FROM runs WHERE status IN ('queued','running') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def latest_done_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the most recent finished run, if any.

    Args:
        conn: Open connection.

    Returns:
        The row, or None.

    """
    return conn.execute(
        "SELECT * FROM runs WHERE status = 'done' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def log(
    conn: sqlite3.Connection, run_id: int, msg: str, tag: str = "", level: str = "info"
) -> None:
    """Append a log line to a run and commit.

    Args:
        conn: Open connection.
        run_id: The run.
        msg: Message text.
        tag: Short right-aligned tag shown in the UI.
        level: info, warn or error.

    """
    conn.execute(
        "INSERT INTO run_log(run_id, ts, level, msg, tag) VALUES (?,?,?,?,?)",
        (run_id, now(), level, msg, tag),
    )
    conn.commit()


# ---- SCD2 upserts -------------------------------------------------------------


def upsert_venue(
    conn: sqlite3.Connection, run_id: int, venue_id: int, attrs: VenueAttrs
) -> bool:
    """Record a venue's attributes, opening a new version only if they changed.

    Args:
        conn: Open connection.
        run_id: The observing run.
        venue_id: Resy venue id.
        attrs: Attributes with their hash.

    Returns:
        True when a new version row was opened.

    """
    cur = conn.execute(
        "SELECT hash FROM venues WHERE venue_id = ? AND is_current = 1", (venue_id,)
    ).fetchone()
    ts = now()
    if cur and cur["hash"] == attrs.hash:
        conn.execute(
            "UPDATE venues SET last_seen_run_id = ? "
            "WHERE venue_id = ? AND is_current = 1",
            (run_id, venue_id),
        )
        return False
    if cur:
        conn.execute(
            "UPDATE venues SET valid_to = ?, is_current = 0 "
            "WHERE venue_id = ? AND is_current = 1",
            (ts, venue_id),
        )
    conn.execute(
        "INSERT INTO venues(venue_id, attrs_json, hash, valid_from, is_current, "
        "first_run_id, last_seen_run_id) VALUES (?,?,?,?,1,?,?)",
        (venue_id, attrs.model_dump_json(), attrs.hash, ts, run_id, run_id),
    )
    return True


def upsert_rating(
    conn: sqlite3.Connection,
    run_id: int,
    venue_id: int,
    avg: float | None,
    count: int | None,
) -> None:
    """Store the rating a run saw for a venue (kept outside the SCD2 hash).

    Args:
        conn: Open connection.
        run_id: The observing run.
        venue_id: Resy venue id.
        avg: Average rating.
        count: Review count.

    """
    conn.execute(
        "INSERT OR REPLACE INTO venue_ratings(run_id, venue_id, avg, count) "
        "VALUES (?,?,?,?)",
        (run_id, venue_id, avg, count),
    )


def upsert_day_state(
    conn: sqlite3.Connection,
    run_id: int,
    venue_id: int,
    day: str,
    party: int,
    obs: Observation,
) -> bool:
    """Record one night's observation, opening a new version only if it changed.

    Args:
        conn: Open connection.
        run_id: The observing run.
        venue_id: Resy venue id.
        day: Service day, ISO format.
        party: Party size the observation is for.
        obs: The observation with its hash.

    Returns:
        True when a new version row was opened.

    """
    cur = conn.execute(
        "SELECT id, hash FROM venue_day_state WHERE venue_id = ? AND service_day = ? "
        "AND party_size = ? AND is_current = 1",
        (venue_id, day, party),
    ).fetchone()
    ts = now()
    if cur and cur["hash"] == obs.hash:
        conn.execute(
            "UPDATE venue_day_state SET last_seen_run_id = ? WHERE id = ?",
            (run_id, cur["id"]),
        )
        return False
    if cur:
        conn.execute(
            "UPDATE venue_day_state SET valid_to = ?, is_current = 0 WHERE id = ?",
            (ts, cur["id"]),
        )
    conn.execute(
        "INSERT INTO venue_day_state(venue_id, service_day, party_size, state_json, "
        "hash, valid_from, is_current, first_run_id, last_seen_run_id) "
        "VALUES (?,?,?,?,?,?,1,?,?)",
        (venue_id, day, party, obs.model_dump_json(), obs.hash, ts, run_id, run_id),
    )
    return True


# ---- point-in-time reads -------------------------------------------------------


def states_as_of_run(
    conn: sqlite3.Connection, run_id: int, party: int, days: list[str]
) -> dict[int, dict[str, Observation]]:
    """Read every observation valid at a run for a party size and set of days.

    Args:
        conn: Open connection.
        run_id: The run whose view of the world to reproduce.
        party: Party size.
        days: Service days, ISO format.

    Returns:
        Mapping of venue id to day to observation.

    """
    marks = ",".join("?" * len(days))
    query = (
        "SELECT venue_id, service_day, state_json FROM venue_day_state "
        "WHERE party_size = ? AND first_run_id <= ? AND last_seen_run_id >= ? "
        f"AND service_day IN ({marks})"
    )
    out: dict[int, dict[str, Observation]] = {}
    for r in conn.execute(query, (party, run_id, run_id, *days)):
        obs = Observation.model_validate_json(r["state_json"])
        out.setdefault(int(r["venue_id"]), {})[str(r["service_day"])] = obs
    return out


def previous_state(
    conn: sqlite3.Connection, run_id: int, venue_id: int, day: str, party: int
) -> sqlite3.Row | None:
    """Return the version that preceded the one valid at a run for a venue-night.

    Args:
        conn: Open connection.
        run_id: The run the current version is valid for.
        venue_id: Resy venue id.
        day: Service day, ISO format.
        party: Party size.

    Returns:
        The previous row, or None when this is the first version.

    """
    return conn.execute(
        "SELECT state_json, valid_from, valid_to, last_seen_run_id "
        "FROM venue_day_state WHERE venue_id = ? AND service_day = ? "
        "AND party_size = ? "
        "AND last_seen_run_id < ? ORDER BY valid_from DESC LIMIT 1",
        (venue_id, day, party, run_id),
    ).fetchone()


def venue_as_of_run(
    conn: sqlite3.Connection, run_id: int, venue_id: int
) -> VenueAttrs | None:
    """Return the venue attributes valid at a run, falling back to the latest.

    Args:
        conn: Open connection.
        run_id: The run.
        venue_id: Resy venue id.

    Returns:
        The attributes, or None if the venue was never seen.

    """
    r = conn.execute(
        "SELECT attrs_json FROM venues WHERE venue_id = ? AND first_run_id <= ? "
        "ORDER BY (last_seen_run_id >= ?) DESC, valid_from DESC LIMIT 1",
        (venue_id, run_id, run_id),
    ).fetchone()
    return VenueAttrs.model_validate_json(r["attrs_json"]) if r else None


def rating(
    conn: sqlite3.Connection, run_id: int, venue_id: int
) -> tuple[float | None, int | None]:
    """Return the rating a run saw for a venue.

    Args:
        conn: Open connection.
        run_id: The run.
        venue_id: Resy venue id.

    Returns:
        Average and review count, both None if the run did not see the venue.

    """
    r = conn.execute(
        "SELECT avg, count FROM venue_ratings WHERE run_id = ? AND venue_id = ?",
        (run_id, venue_id),
    ).fetchone()
    return (r["avg"], r["count"]) if r else (None, None)
