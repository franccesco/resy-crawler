"""The run: read the week from Resy for each party size and store change (SCD2)."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import settings
from .models import RunParams
from .resy_client import ResyClient
from .scoring import observe, venue_attrs, venue_id

PACIFIC = ZoneInfo("America/Los_Angeles")


def today_pacific() -> date:
    """Return today's date in Pacific time, Resy's local time for San Francisco.

    Returns:
        Today's date.

    """
    return datetime.now(PACIFIC).date()


def plan_days(params: RunParams, base: date | None = None) -> tuple[list[str], str]:
    """Compute the service days a run covers and its verification day.

    Args:
        params: The run parameters.
        base: The day the run started; defaults to today.

    Returns:
        The consecutive service days and the far-out verification day, ISO format.

    """
    anchor = base or today_pacific()
    start = params.start_day or (anchor + timedelta(days=1))
    days = [(start + timedelta(days=i)).isoformat() for i in range(params.days)]
    verify = (anchor + timedelta(days=params.verify_offset_days)).isoformat()
    return days, verify


def run_days(run_row: sqlite3.Row) -> tuple[list[str], str]:
    """Recover the days a stored run covered from its parameters and start time.

    Args:
        run_row: A row of the `runs` table.

    Returns:
        The service days and the verification day, ISO format.

    """
    params = RunParams.model_validate_json(run_row["params_json"])
    started = datetime.fromisoformat(run_row["started_at"]).astimezone(PACIFIC).date()
    return plan_days(params, started)


@dataclass
class _Progress:
    seen: set[int] = field(default_factory=set)
    new: int = 0
    unchanged: int = 0
    done: int = 0


def _ingest_hits(
    conn: sqlite3.Connection,
    run_id: int,
    day: str,
    party: int,
    hits: list[dict[str, Any]],
    progress: _Progress,
) -> int:
    changed = 0
    for hit in hits:
        vid = venue_id(hit)
        if vid is None:
            continue
        progress.seen.add(vid)
        db.upsert_venue(conn, run_id, vid, venue_attrs(hit))
        rating: dict[str, Any] = hit.get("rating") or {}
        db.upsert_rating(conn, run_id, vid, rating.get("average"), rating.get("count"))
        if db.upsert_day_state(conn, run_id, vid, day, party, observe(hit)):
            progress.new += 1
            changed += 1
        else:
            progress.unchanged += 1
    conn.commit()
    return changed


def _ingest_day(
    conn: sqlite3.Connection,
    client: ResyClient,
    run_id: int,
    party: int,
    day: str,
    kind: str,
    jobs_left: int,
    progress: _Progress,
) -> None:
    page, nb_pages = 1, 1
    while page <= nb_pages:
        if progress.done:
            time.sleep(settings.request_delay_s)
        t0 = time.time()
        data = client.search_availability(day, party, page)
        search: dict[str, Any] = data.get("search") or {}
        nb_pages = int(search.get("nbPages") or 1)
        hits: list[dict[str, Any]] = search.get("hits") or []
        progress.done += 1
        changed = _ingest_hits(conn, run_id, day, party, hits, progress)
        remaining = jobs_left * 2 + (nb_pages - page)
        db.update_run(
            conn,
            run_id,
            requests_done=progress.done,
            requests_total=progress.done + remaining,
            venues_seen=len(progress.seen),
            states_new=progress.new,
            states_unchanged=progress.unchanged,
        )
        elapsed_ms = (time.time() - t0) * 1000
        db.log(
            conn,
            run_id,
            f"POST /3/venuesearch/search day={day} party={party} "
            f"page={page}/{nb_pages} ({elapsed_ms:.0f} ms)",
            f"{kind} · {len(hits)} venues · {changed} changed",
        )
        page += 1


def execute(run_id: int, params: RunParams) -> None:
    """Perform a run to completion, recording progress and failures in the store.

    Args:
        run_id: The queued run to execute.
        params: Its parameters.

    """
    conn = db.connect()
    client = ResyClient()
    try:
        days, verify_day = plan_days(params)
        all_days = [*days, verify_day]
        jobs = [(ps, d) for ps in params.party_sizes for d in all_days]
        db.update_run(conn, run_id, status="running", requests_total=len(jobs) * 2)
        db.log(
            conn,
            run_id,
            f"Run {run_id} ({params.source}): {len(days)} nights {days[0]} → "
            f"{days[-1]}, party sizes {params.party_sizes}, verification day "
            f"{verify_day}",
            "start",
        )
        progress = _Progress()
        for idx, (party, day) in enumerate(jobs):
            kind = "verify" if day == verify_day else "night"
            jobs_left = len(jobs) - idx - 1
            _ingest_day(conn, client, run_id, party, day, kind, jobs_left, progress)
        db.update_run(conn, run_id, status="done", finished_at=db.now())
        db.log(
            conn,
            run_id,
            f"Done: {len(progress.seen)} venues, {progress.new} state rows opened, "
            f"{progress.unchanged} unchanged",
            "done",
        )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        db.update_run(conn, run_id, status="failed", finished_at=db.now(), error=error)
        db.log(conn, run_id, error, "failed", level="error")
        traceback.print_exc()
    finally:
        client.close()
        conn.close()


def start_run(params: RunParams) -> int | None:
    """Create a run and execute it in a background thread.

    Args:
        params: The run parameters.

    Returns:
        The new run id, or None if a run is already active.

    """
    conn = db.connect()
    try:
        if db.active_run(conn):
            return None
        run_id = db.create_run(conn, params.model_dump(mode="json"))
    finally:
        conn.close()
    threading.Thread(
        target=execute, args=(run_id, params), daemon=True, name=f"run-{run_id}"
    ).start()
    return run_id


def main() -> None:
    """Run once in the foreground: `uv run resy-run [--days 7] [--party 2 4]`."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=settings.days)
    ap.add_argument("--party", type=int, nargs="+", default=[settings.party_size])
    a = ap.parse_args()
    params = RunParams(
        party_sizes=a.party, days=a.days, verify_offset_days=settings.verify_offset_days
    )
    conn = db.connect()
    run_id = db.create_run(conn, params.model_dump(mode="json"))
    conn.close()
    execute(run_id, params)
    conn = db.connect()
    for r in conn.execute(
        "SELECT ts, msg, tag FROM run_log WHERE run_id = ? ORDER BY id", (run_id,)
    ):
        print(r["ts"][11:19], r["msg"], "·", r["tag"], file=sys.stderr)
    run = db.get_run(conn, run_id)
    if run is not None:
        print(
            f"run {run_id}: {run['status']} venues={run['venues_seen']} "
            f"new={run['states_new']} unchanged={run['states_unchanged']}"
        )


if __name__ == "__main__":
    main()
