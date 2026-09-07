"""The run: read the week from Resy for each party size and store change (SCD2)."""
from __future__ import annotations

import sys
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import settings
from .models import RunParams
from .resy_client import ResyClient
from .scoring import observe, venue_attrs

PACIFIC = ZoneInfo("America/Los_Angeles")


def today_pacific() -> date:
    return datetime.now(PACIFIC).date()


def plan_days(params: RunParams, run_started: date | None = None) -> tuple[list[str], str]:
    base = run_started or today_pacific()
    start = params.start_day or (base + timedelta(days=1))
    days = [(start + timedelta(days=i)).isoformat() for i in range(params.days)]
    verify = (base + timedelta(days=params.verify_offset_days)).isoformat()
    return days, verify


def run_days(run_row) -> tuple[list[str], str]:
    """Days a stored run covered, derived from its params and start date (Pacific)."""
    import json

    params = RunParams(**json.loads(run_row["params_json"]))
    started = datetime.fromisoformat(run_row["started_at"]).astimezone(PACIFIC).date()
    return plan_days(params, started)


def execute(run_id: int, params: RunParams) -> None:
    conn = db.connect()
    client = ResyClient()
    try:
        days, verify_day = plan_days(params)
        all_days = days + [verify_day]
        jobs = [(ps, d) for ps in params.party_sizes for d in all_days]
        db.update_run(conn, run_id, status="running", requests_total=len(jobs) * 2)
        db.log(conn, run_id, f"Run {run_id} ({params.source}): {len(days)} nights {days[0]} → {days[-1]}, party sizes {params.party_sizes}, verification day {verify_day}", "start")
        seen: set[int] = set()
        new = unchanged = done = 0
        for idx, (party, day) in enumerate(jobs):
            page, nb_pages = 1, 1
            kind = "verify" if day == verify_day else "night"
            while page <= nb_pages:
                if done:
                    time.sleep(settings.request_delay_s)
                t0 = time.time()
                data = client.search_availability(day, party, page)
                s = data.get("search") or {}
                nb_pages = int(s.get("nbPages") or 1)
                hits = s.get("hits") or []
                done += 1
                changed = 0
                for h in hits:
                    vid = (h.get("id") or {}).get("resy")
                    if not vid:
                        continue
                    seen.add(vid)
                    db.upsert_venue(conn, run_id, vid, venue_attrs(h))
                    r = h.get("rating") or {}
                    db.upsert_rating(conn, run_id, vid, r.get("average"), r.get("count"))
                    if db.upsert_day_state(conn, run_id, vid, day, party, observe(h)):
                        new += 1
                        changed += 1
                    else:
                        unchanged += 1
                conn.commit()
                remaining = (len(jobs) - idx - 1) * 2 + (nb_pages - page)
                db.update_run(conn, run_id, requests_done=done, requests_total=done + remaining, venues_seen=len(seen), states_new=new, states_unchanged=unchanged)
                db.log(conn, run_id, f"POST /3/venuesearch/search day={day} party={party} page={page}/{nb_pages} ({(time.time() - t0) * 1000:.0f} ms)",
                       f"{kind} · {len(hits)} venues · {changed} changed")
                page += 1
        db.update_run(conn, run_id, status="done", finished_at=db.now())
        db.log(conn, run_id, f"Done: {len(seen)} venues, {new} state rows opened, {unchanged} unchanged", "done")
    except Exception as e:  # noqa: BLE001
        db.update_run(conn, run_id, status="failed", finished_at=db.now(), error=f"{type(e).__name__}: {e}")
        db.log(conn, run_id, f"{type(e).__name__}: {e}", "failed", level="error")
        traceback.print_exc()
    finally:
        client.close()
        conn.close()


def start_run(params: RunParams) -> int | None:
    """Create and start a run in a background thread. Returns None if one is already active."""
    import threading

    conn = db.connect()
    try:
        if db.active_run(conn):
            return None
        run_id = db.create_run(conn, params.model_dump(mode="json"))
    finally:
        conn.close()
    threading.Thread(target=execute, args=(run_id, params), daemon=True, name=f"run-{run_id}").start()
    return run_id


def main() -> None:
    """CLI: run once, in the foreground. `uv run resy-run [--days 7] [--party 2 4]`."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=settings.days)
    ap.add_argument("--party", type=int, nargs="+", default=[settings.party_size])
    a = ap.parse_args()
    params = RunParams(party_sizes=a.party, days=a.days, verify_offset_days=settings.verify_offset_days)
    conn = db.connect()
    run_id = db.create_run(conn, params.model_dump(mode="json"))
    conn.close()
    execute(run_id, params)
    conn = db.connect()
    for r in conn.execute("SELECT ts, msg, tag FROM run_log WHERE run_id = ? ORDER BY id", (run_id,)):
        print(r["ts"][11:19], r["msg"], "·", r["tag"], file=sys.stderr)
    run = db.get_run(conn, run_id)
    print(f"run {run_id}: {run['status']} venues={run['venues_seen']} new={run['states_new']} unchanged={run['states_unchanged']}")


if __name__ == "__main__":
    main()
