"""The run: read the week from Resy, store change (SCD2), score, materialize results."""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import settings
from .models import RunParams
from .resy_client import ResyClient
from .scoring import classify, night_state, venue_attrs

PACIFIC = ZoneInfo("America/Los_Angeles")


def today_pacific() -> date:
    return datetime.now(PACIFIC).date()


def plan_days(params: RunParams) -> tuple[list[str], str]:
    start = params.start_day or (today_pacific() + timedelta(days=1))
    days = [(start + timedelta(days=i)).isoformat() for i in range(params.days)]
    verify = (today_pacific() + timedelta(days=params.verify_offset_days)).isoformat()
    return days, verify


def execute(run_id: int, params: RunParams) -> None:
    conn = db.connect()
    client = ResyClient()
    try:
        days, verify_day = plan_days(params)
        all_days = days + [verify_day]
        db.update_run(conn, run_id, status="running", requests_total=len(all_days) * 2)
        db.log(conn, run_id, f"Run {run_id}: {len(days)} dinner nights {days[0]} → {days[-1]}, party of {params.party_size}; verification day {verify_day}", "start")
        seen: set[int] = set()
        new = unchanged = done = 0
        pages_total = 0
        for day in all_days:
            page, nb_pages = 1, 1
            kind = "verify" if day == verify_day else "night"
            while page <= nb_pages:
                if done:
                    time.sleep(settings.request_delay_s)
                t0 = time.time()
                data = client.search_availability(day, params.party_size, page)
                s = data.get("search") or {}
                nb_pages = int(s.get("nbPages") or 1)
                hits = s.get("hits") or []
                done += 1
                pages_total += 1
                changed = 0
                for h in hits:
                    vid = (h.get("id") or {}).get("resy")
                    if not vid:
                        continue
                    seen.add(vid)
                    db.upsert_venue(conn, run_id, vid, venue_attrs(h))
                    r = h.get("rating") or {}
                    db.upsert_rating(conn, run_id, vid, r.get("average"), r.get("count"))
                    if db.upsert_day_state(conn, run_id, vid, day, params.party_size, night_state(h, settings.dinner_service_type_id)):
                        new += 1
                        changed += 1
                    else:
                        unchanged += 1
                conn.commit()
                remaining = sum(2 for d in all_days if d > day) + (nb_pages - page)
                db.update_run(conn, run_id, requests_done=done, requests_total=done + remaining, venues_seen=len(seen), states_new=new, states_unchanged=unchanged)
                db.log(
                    conn, run_id,
                    f"POST /3/venuesearch/search day={day} party={params.party_size} page={page}/{nb_pages} ({(time.time() - t0) * 1000:.0f} ms)",
                    f"{kind} · {len(hits)} venues · {changed} changed",
                )
                page += 1
        scored, excluded = materialize(conn, run_id, days, verify_day, params)
        db.update_run(conn, run_id, status="done", finished_at=db.now(), scored=scored, excluded=excluded)
        db.log(conn, run_id, f"Done: {len(seen)} venues, {new} state rows opened, {unchanged} unchanged, {scored} scored, {excluded} excluded", "done")
    except Exception as e:  # noqa: BLE001
        db.update_run(conn, run_id, status="failed", finished_at=db.now(), error=f"{type(e).__name__}: {e}")
        db.log(conn, run_id, f"{type(e).__name__}: {e}", "failed", level="error")
        traceback.print_exc()
    finally:
        client.close()
        conn.close()


def materialize(conn, run_id: int, days: list[str], verify_day: str, params: RunParams) -> tuple[int, int]:
    today = today_pacific().isoformat()
    rows = []
    for vid in db.venues_seen_in_run(conn, run_id):
        states = db.current_states(conn, vid, days + [verify_day], params.party_size)
        nights = [dict(day=d, **states[d]) for d in days if d in states]
        far = states.get(verify_day)
        c = classify(nights, far, today, settings.min_days_scored)
        rows.append((vid, c, nights, far))
    scored = [r for r in rows if not r[1]["excluded"]]
    scored.sort(key=lambda r: (-r[1]["score"], -r[1]["taken_total"]))
    rank = {r[0]: i + 1 for i, r in enumerate(scored)}
    conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
    for vid, c, nights, far in rows:
        nights_out = [
            {"day": n["day"], "boxes": n["boxes"], "open_boxes": n["open_boxes"], "taken": n["boxes"] - n["open_boxes"],
             "ratio": (round((n["boxes"] - n["open_boxes"]) / n["boxes"], 4) if n["boxes"] else None),
             "open_times": n["open_times"], "window": n["window"]}
            for n in nights
        ]
        conn.execute(
            "INSERT INTO results(run_id, venue_id, rank, score, excluded, reason, evidence, days_scored, taken_total, boxes_total, far_open, nights_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, vid, rank.get(vid), c["score"], int(c["excluded"]), c["reason"], c["evidence"], c["days_scored"],
             c["taken_total"], c["boxes_total"], far["open_boxes"] if far else None, json.dumps(nights_out)),
        )
    conn.commit()
    return len(scored), len(rows) - len(scored)


def main() -> None:
    """CLI: run once without the server. `uv run resy-run [--days 7] [--party 2]`."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=settings.days)
    ap.add_argument("--party", type=int, default=settings.party_size)
    a = ap.parse_args()
    params = RunParams(party_size=a.party, days=a.days, verify_offset_days=settings.verify_offset_days)
    conn = db.connect()
    run_id = db.create_run(conn, params.model_dump(mode="json"))
    conn.close()
    execute(run_id, params)
    conn = db.connect()
    for r in conn.execute("SELECT ts, msg, tag FROM run_log WHERE run_id = ? ORDER BY id", (run_id,)):
        print(r["ts"][11:19], r["msg"], "·", r["tag"], file=sys.stderr)
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    print(f"run {run_id}: {run['status']} scored={run['scored']} excluded={run['excluded']}")


if __name__ == "__main__":
    main()
