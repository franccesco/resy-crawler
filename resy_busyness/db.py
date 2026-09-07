"""SQLite store with slowly-changing-dimension type 2 tables.

Resy has no "changed since" endpoint, so every run re-reads the full state of every
venue for every day in the window (about 16 requests). What we *store* is only change:
a venue's descriptive attributes and its per-night availability state each get a new
row only when their hash differs from the current row. Unchanged observations bump
`last_seen_run_id` on the existing row.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import settings

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
  scored INTEGER DEFAULT 0,
  excluded INTEGER DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts TEXT NOT NULL, level TEXT NOT NULL, msg TEXT NOT NULL, tag TEXT NOT NULL DEFAULT ''
);
-- SCD2: one row per version of a venue's descriptive attributes.
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
-- Ratings move every run; kept per run and out of the SCD2 hash so they do not churn versions.
CREATE TABLE IF NOT EXISTS venue_ratings (
  run_id INTEGER NOT NULL, venue_id INTEGER NOT NULL, avg REAL, count INTEGER,
  PRIMARY KEY (run_id, venue_id)
);
-- SCD2: one row per version of (venue, service day) availability for the party size.
CREATE TABLE IF NOT EXISTS venue_day_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue_id INTEGER NOT NULL,
  service_day TEXT NOT NULL,
  party_size INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  hash TEXT NOT NULL,
  boxes INTEGER NOT NULL,
  open_boxes INTEGER NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  first_run_id INTEGER NOT NULL,
  last_seen_run_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS vds_current ON venue_day_state(venue_id, service_day, party_size, is_current);
-- Materialized scoring per run so results are a plain read.
CREATE TABLE IF NOT EXISTS results (
  run_id INTEGER NOT NULL, venue_id INTEGER NOT NULL,
  rank INTEGER, score REAL, excluded INTEGER NOT NULL, reason TEXT, evidence TEXT,
  days_scored INTEGER, taken_total INTEGER, boxes_total INTEGER, far_open INTEGER,
  nights_json TEXT NOT NULL,
  PRIMARY KEY (run_id, venue_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    import os

    p = path or settings.db_path
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# ---- runs -------------------------------------------------------------------

def create_run(conn: sqlite3.Connection, params: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO runs(status, started_at, params_json) VALUES ('queued', ?, ?)",
        (now(), json.dumps(params, default=str)),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()


def active_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1").fetchone()


def log(conn: sqlite3.Connection, run_id: int, msg: str, tag: str = "", level: str = "info") -> None:
    conn.execute("INSERT INTO run_log(run_id, ts, level, msg, tag) VALUES (?,?,?,?,?)", (run_id, now(), level, msg, tag))
    conn.commit()


# ---- SCD2 upserts -------------------------------------------------------------

def upsert_venue(conn: sqlite3.Connection, run_id: int, venue_id: int, attrs: dict[str, Any]) -> bool:
    """Returns True when a new version row was opened."""
    cur = conn.execute("SELECT hash, valid_from FROM venues WHERE venue_id = ? AND is_current = 1", (venue_id,)).fetchone()
    ts = now()
    if cur and cur["hash"] == attrs["hash"]:
        conn.execute("UPDATE venues SET last_seen_run_id = ? WHERE venue_id = ? AND is_current = 1", (run_id, venue_id))
        return False
    if cur:
        conn.execute("UPDATE venues SET valid_to = ?, is_current = 0 WHERE venue_id = ? AND is_current = 1", (ts, venue_id))
    conn.execute(
        "INSERT INTO venues(venue_id, attrs_json, hash, valid_from, is_current, first_run_id, last_seen_run_id) VALUES (?,?,?,?,1,?,?)",
        (venue_id, json.dumps(attrs), attrs["hash"], ts, run_id, run_id),
    )
    return True


def upsert_rating(conn: sqlite3.Connection, run_id: int, venue_id: int, avg: float | None, count: int | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO venue_ratings(run_id, venue_id, avg, count) VALUES (?,?,?,?)", (run_id, venue_id, avg, count)
    )


def upsert_day_state(conn: sqlite3.Connection, run_id: int, venue_id: int, day: str, party: int, state: dict[str, Any]) -> bool:
    cur = conn.execute(
        "SELECT id, hash FROM venue_day_state WHERE venue_id = ? AND service_day = ? AND party_size = ? AND is_current = 1",
        (venue_id, day, party),
    ).fetchone()
    ts = now()
    if cur and cur["hash"] == state["hash"]:
        conn.execute("UPDATE venue_day_state SET last_seen_run_id = ? WHERE id = ?", (run_id, cur["id"]))
        return False
    if cur:
        conn.execute("UPDATE venue_day_state SET valid_to = ?, is_current = 0 WHERE id = ?", (ts, cur["id"]))
    conn.execute(
        "INSERT INTO venue_day_state(venue_id, service_day, party_size, state_json, hash, boxes, open_boxes, valid_from, is_current, first_run_id, last_seen_run_id)"
        " VALUES (?,?,?,?,?,?,?,?,1,?,?)",
        (venue_id, day, party, json.dumps(state), state["hash"], state["boxes"], state["open_boxes"], ts, run_id, run_id),
    )
    return True


# ---- reads --------------------------------------------------------------------

def current_states(conn: sqlite3.Connection, venue_id: int, days: list[str], party: int) -> dict[str, dict[str, Any]]:
    q = f"SELECT service_day, state_json FROM venue_day_state WHERE venue_id = ? AND party_size = ? AND is_current = 1 AND service_day IN ({','.join('?' * len(days))})"
    rows = conn.execute(q, (venue_id, party, *days)).fetchall()
    return {r["service_day"]: json.loads(r["state_json"]) for r in rows}


def venues_seen_in_run(conn: sqlite3.Connection, run_id: int) -> list[int]:
    rows = conn.execute("SELECT DISTINCT venue_id FROM venue_day_state WHERE last_seen_run_id = ? OR first_run_id = ?", (run_id, run_id)).fetchall()
    return [r["venue_id"] for r in rows]


def current_venue(conn: sqlite3.Connection, venue_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT attrs_json FROM venues WHERE venue_id = ? AND is_current = 1", (venue_id,)).fetchone()
    return json.loads(r["attrs_json"]) if r else None


def rating(conn: sqlite3.Connection, run_id: int, venue_id: int) -> tuple[float | None, int | None]:
    r = conn.execute("SELECT avg, count FROM venue_ratings WHERE run_id = ? AND venue_id = ?", (run_id, venue_id)).fetchone()
    return (r["avg"], r["count"]) if r else (None, None)
