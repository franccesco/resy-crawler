"""FastAPI surface. Start with: uv run uvicorn resy_busyness.api:app --reload"""
from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, pipeline, scheduler
from .config import settings
from .models import (
    ExclusionReason, LogLine, NightScore, ResultsResponse, RunDetail, RunParams, RunStatus, RunSummary, SchedulerStatus,
    ScoringParams, VenueHistoryRow, VenueResult, VenueWindows, WindowNight, WindowsResponse,
)
from .scoring import SERVICES, classify, rating_tier, score_night


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.start()
    yield


app = FastAPI(title="Resy SF Busyness", version="0.2.0", lifespan=lifespan,
              description="How booked each San Francisco restaurant on Resy is, and where the windows are.")


def _summary(r) -> RunSummary:
    return RunSummary(
        id=r["id"], status=RunStatus(r["status"]), started_at=r["started_at"], finished_at=r["finished_at"],
        params=RunParams(**json.loads(r["params_json"])), requests_total=r["requests_total"], requests_done=r["requests_done"],
        venues_seen=r["venues_seen"], states_new=r["states_new"], states_unchanged=r["states_unchanged"], error=r["error"],
    )


# ---- runs ------------------------------------------------------------------------

@app.post("/api/runs", response_model=RunSummary, status_code=202)
def start_run(params: RunParams = RunParams()) -> RunSummary:
    run_id = pipeline.start_run(params)
    if run_id is None:
        raise HTTPException(409, "A run is already in progress.")
    conn = db.connect()
    try:
        return _summary(db.get_run(conn, run_id))
    finally:
        conn.close()


@app.get("/api/runs", response_model=list[RunSummary])
def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[RunSummary]:
    conn = db.connect()
    try:
        return [_summary(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


@app.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, log_limit: int = Query(60, ge=1, le=2000)) -> RunDetail:
    conn = db.connect()
    try:
        r = db.get_run(conn, run_id)
        if not r:
            raise HTTPException(404, "No such run")
        logs = conn.execute("SELECT ts, level, msg, tag FROM run_log WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, log_limit)).fetchall()
        return RunDetail(**_summary(r).model_dump(), log=[LogLine(ts=l["ts"], level=l["level"], msg=l["msg"], tag=l["tag"]) for l in logs])
    finally:
        conn.close()


@app.get("/api/scheduler", response_model=SchedulerStatus)
def scheduler_status() -> SchedulerStatus:
    return SchedulerStatus(**scheduler.status())


# ---- scoring at read time -------------------------------------------------------

def scoring_params(party_size: int = Query(settings.party_size, ge=1), service: Literal["dinner", "lunch", "brunch", "breakfast", "all"] = "dinner",
                   grid: int = Query(30, description="15 or 30"), min_nights: int = Query(2, ge=1)) -> ScoringParams:
    if grid not in (15, 30):
        raise HTTPException(422, "grid must be 15 or 30")
    return ScoringParams(party_size=party_size, service=service, grid=grid, min_nights=min_nights)


def _resolve_run(conn, run_id: int | None):
    r = db.get_run(conn, run_id) if run_id else db.latest_done_run(conn)
    if not r:
        raise HTTPException(404, "No completed run yet. POST /api/runs to start one." if run_id is None else "No such run")
    return r


def _compute(conn, run, sp: ScoringParams) -> tuple[list[dict], list[str], str]:
    days, verify_day = pipeline.run_days(run)
    run_params = RunParams(**json.loads(run["params_json"]))
    if sp.party_size not in run_params.party_sizes:
        raise HTTPException(404, f"Run {run['id']} ingested party sizes {run_params.party_sizes}, not {sp.party_size}.")
    svc_ids = None if sp.service == "all" else {SERVICES[sp.service]}
    today = pipeline.today_pacific().isoformat()
    by_venue = db.states_as_of_run(conn, run["id"], sp.party_size, days + [verify_day])
    rows = []
    for vid, states in by_venue.items():
        nights = [dict(day=d, **score_night(states[d], svc_ids, sp.grid)) for d in days if d in states]
        far = score_night(states[verify_day], svc_ids, sp.grid) if verify_day in states else None
        c = classify(nights, far, today, sp.min_nights, sp.service if sp.service != "all" else "any-service")
        rows.append({"venue_id": vid, "nights": nights, "far": far, **c})
    scored = sorted((r for r in rows if not r["excluded"]), key=lambda r: (-r["score"], -r["taken_total"]))
    for i, r in enumerate(scored):
        r["rank"] = i + 1
    for r in rows:
        r.setdefault("rank", None)
    rows.sort(key=lambda r: (r["excluded"], r["rank"] or 0, r["venue_id"]))
    return rows, days, verify_day


def _venue_result(conn, run_id: int, r: dict) -> VenueResult:
    v = db.venue_as_of_run(conn, run_id, r["venue_id"]) or {}
    avg, count = db.rating(conn, run_id, r["venue_id"])
    return VenueResult(
        rank=r["rank"], venue_id=r["venue_id"], name=v.get("name") or str(r["venue_id"]), url=v.get("url"),
        neighborhood=v.get("neighborhood"), cuisine=v.get("cuisine"), price=v.get("price"), phone=v.get("phone"), rating_avg=avg, rating_count=count,
        rating_tier=rating_tier(avg, count), score=r["score"], days_scored=r["days_scored"], taken_total=r["taken_total"], boxes_total=r["boxes_total"],
        nights=[NightScore(day=n["day"], boxes=n["boxes"], open_boxes=n["open_boxes"], taken=n["taken"],
                           ratio=(round(n["taken"] / n["boxes"], 4) if n["boxes"] else None), open_times=n["open_times"], window=n["window"]) for n in r["nights"]],
        verified_far_out_open=r["far"]["open_boxes"] if r["far"] else None,
        excluded=r["excluded"], reason=ExclusionReason(r["reason"]) if r["reason"] else None, evidence=r["evidence"],
    )


def _results(conn, run_id: int | None, sp: ScoringParams) -> ResultsResponse:
    run = _resolve_run(conn, run_id)
    rows, _, _ = _compute(conn, run, sp)
    out = [_venue_result(conn, run["id"], r) for r in rows]
    return ResultsResponse(run_id=run["id"], computed_at=run["finished_at"] or run["started_at"], scoring=sp,
                           party_sizes_available=RunParams(**json.loads(run["params_json"])).party_sizes,
                           listed=len(out), scored=sum(not x.excluded for x in out), excluded=sum(x.excluded for x in out), rows=out)


@app.get("/api/results", response_model=ResultsResponse)
def results(run_id: int | None = None, view: Literal["all", "scored", "excluded"] = "all", q: str | None = None,
            sp: ScoringParams = Depends(scoring_params)) -> ResultsResponse:
    conn = db.connect()
    try:
        res = _results(conn, run_id, sp)
    finally:
        conn.close()
    rows = res.rows
    if view == "scored":
        rows = [r for r in rows if not r.excluded]
    elif view == "excluded":
        rows = [r for r in rows if r.excluded]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r.name or "").lower() or ql in (r.neighborhood or "").lower() or ql in (r.cuisine or "").lower()]
    return res.model_copy(update={"rows": rows})


@app.get("/api/results.csv")
def results_csv(run_id: int | None = None, sp: ScoringParams = Depends(scoring_params)):
    conn = db.connect()
    try:
        res = _results(conn, run_id, sp)
    finally:
        conn.close()
    buf = io.StringIO()
    days = [n.day.isoformat() for n in res.rows[0].nights] if res.rows and res.rows[0].nights else []
    w = csv.writer(buf)
    w.writerow([f"# run {res.run_id} computed {res.computed_at} · scoring: party_size={sp.party_size} service={sp.service} grid={sp.grid}min min_nights={sp.min_nights}"])
    w.writerow(["rank", "venue_id", "name", "neighborhood", "cuisine", "price", "score", "days_scored", "taken_total", "boxes_total",
                "far_out_open_boxes", "excluded", "reason", "evidence", "rating_tier", "rating_avg", "rating_count", "url", *[f"taken_over_boxes_{d}" for d in days], *[f"open_times_{d}" for d in days]])
    for r in res.rows:
        by_day = {n.day.isoformat(): n for n in r.nights}
        w.writerow([r.rank, r.venue_id, r.name, r.neighborhood, r.cuisine, r.price, r.score, r.days_scored, r.taken_total, r.boxes_total,
                    r.verified_far_out_open, int(r.excluded), r.reason.value if r.reason else "", r.evidence, r.rating_tier, r.rating_avg, r.rating_count, r.url,
                    *[(f"{by_day[d].taken}/{by_day[d].boxes}" if d in by_day and by_day[d].boxes else "") for d in days],
                    *[(" ".join(by_day[d].open_times) if d in by_day else "") for d in days]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="sf_busyness_run{res.run_id}_{sp.service}_p{sp.party_size}.csv"'})


# ---- windows: where the exclusive places have tables ---------------------------------

def _venue_windows(conn, run, r: dict, sp: ScoringParams) -> VenueWindows:
    svc_ids = None if sp.service == "all" else {SERVICES[sp.service]}
    nights = []
    for n in r["nights"]:
        if not n["boxes"]:
            continue
        prev = db.previous_state(conn, run["id"], r["venue_id"], n["day"], sp.party_size)
        prev_open = set(score_night(json.loads(prev["state_json"]), svc_ids, sp.grid)["open_times"]) if prev else set()
        new_times = [t for t in n["open_times"] if t not in prev_open] if prev else []
        nights.append(WindowNight(day=n["day"], window=n["window"], open_times=n["open_times"], new_times=new_times,
                                  previous_seen_at=prev["valid_to"] if prev else None))
    v = db.venue_as_of_run(conn, run["id"], r["venue_id"]) or {}
    return VenueWindows(rank=r["rank"], venue_id=r["venue_id"], name=v.get("name") or str(r["venue_id"]), url=v.get("url"), phone=v.get("phone"),
                        neighborhood=v.get("neighborhood"), cuisine=v.get("cuisine"), price=v.get("price"), score=r["score"],
                        nights=nights, open_total=sum(len(x.open_times) for x in nights), new_total=sum(len(x.new_times) for x in nights))


@app.get("/api/windows", response_model=WindowsResponse)
def windows(run_id: int | None = None, min_score: float = Query(0.7, ge=0, le=1), sp: ScoringParams = Depends(scoring_params)) -> WindowsResponse:
    conn = db.connect()
    try:
        run = _resolve_run(conn, run_id)
        rows, _, _ = _compute(conn, run, sp)
        out = [_venue_windows(conn, run, r, sp) for r in rows if not r["excluded"] and r["score"] >= min_score]
        return WindowsResponse(run_id=run["id"], computed_at=run["finished_at"] or run["started_at"], scoring=sp, min_score=min_score, rows=out)
    finally:
        conn.close()


@app.get("/api/venues/{venue_id}/windows", response_model=VenueWindows)
def venue_windows(venue_id: int, run_id: int | None = None, sp: ScoringParams = Depends(scoring_params)) -> VenueWindows:
    """Open boxes per night for one venue, with the ones that appeared since the previous snapshot."""
    conn = db.connect()
    try:
        run = _resolve_run(conn, run_id)
        rows, _, _ = _compute(conn, run, sp)
        match = [r for r in rows if r["venue_id"] == venue_id]
        if not match:
            raise HTTPException(404, "Venue not in this run")
        return _venue_windows(conn, run, match[0], sp)
    finally:
        conn.close()


@app.get("/api/venues/{venue_id}/history", response_model=list[VenueHistoryRow])
def venue_history(venue_id: int, party_size: int | None = None) -> list[VenueHistoryRow]:
    conn = db.connect()
    try:
        q = "SELECT * FROM venue_day_state WHERE venue_id = ?" + (" AND party_size = ?" if party_size else "") + " ORDER BY service_day, party_size, valid_from"
        rows = conn.execute(q, (venue_id, party_size) if party_size else (venue_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        raise HTTPException(404, "No observations for this venue")
    out = []
    for r in rows:
        st = json.loads(r["state_json"])
        out.append(VenueHistoryRow(service_day=r["service_day"], party_size=r["party_size"], open_times=[s["t"] for s in st.get("slots", [])],
                                   windows=st.get("windows", []), valid_from=r["valid_from"], valid_to=r["valid_to"], is_current=bool(r["is_current"]),
                                   first_run_id=r["first_run_id"], last_seen_run_id=r["last_seen_run_id"]))
    return out


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
