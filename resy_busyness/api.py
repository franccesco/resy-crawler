"""FastAPI surface. Start with: uv run uvicorn resy_busyness.api:app --reload"""
from __future__ import annotations

import csv
import io
import json
import threading
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, pipeline
from .models import (
    ExclusionReason, LogLine, NightScore, ResultsResponse, RunDetail, RunParams, RunStatus, RunSummary,
    VenueHistoryRow, VenueResult,
)

app = FastAPI(title="Resy SF Busyness", version="0.1.0", description="How booked each San Francisco restaurant on Resy is for a party of two.")


def _summary(r) -> RunSummary:
    return RunSummary(
        id=r["id"], status=RunStatus(r["status"]), started_at=r["started_at"], finished_at=r["finished_at"],
        params=RunParams(**json.loads(r["params_json"])), requests_total=r["requests_total"], requests_done=r["requests_done"],
        venues_seen=r["venues_seen"], states_new=r["states_new"], states_unchanged=r["states_unchanged"],
        scored=r["scored"], excluded=r["excluded"], error=r["error"],
    )


@app.post("/api/runs", response_model=RunSummary, status_code=202)
def start_run(params: RunParams = RunParams()) -> RunSummary:
    conn = db.connect()
    try:
        if db.active_run(conn):
            raise HTTPException(409, "A run is already in progress.")
        run_id = db.create_run(conn, params.model_dump(mode="json"))
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    threading.Thread(target=pipeline.execute, args=(run_id, params), daemon=True).start()
    return _summary(row)


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
        r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise HTTPException(404, "No such run")
        logs = conn.execute("SELECT ts, level, msg, tag FROM run_log WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, log_limit)).fetchall()
        return RunDetail(**_summary(r).model_dump(), log=[LogLine(ts=l["ts"], level=l["level"], msg=l["msg"], tag=l["tag"]) for l in logs])
    finally:
        conn.close()


def _latest_done(conn) -> int:
    r = conn.execute("SELECT id FROM runs WHERE status = 'done' ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        raise HTTPException(404, "No completed run yet. POST /api/runs to start one.")
    return r["id"]


def _results(conn, run_id: int) -> ResultsResponse:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(404, "No such run")
    rows = []
    for r in conn.execute("SELECT * FROM results WHERE run_id = ? ORDER BY excluded, rank, venue_id", (run_id,)):
        v = db.current_venue(conn, r["venue_id"]) or {}
        avg, count = db.rating(conn, run_id, r["venue_id"])
        rows.append(VenueResult(
            rank=r["rank"], venue_id=r["venue_id"], name=v.get("name") or str(r["venue_id"]), url=v.get("url"),
            neighborhood=v.get("neighborhood"), cuisine=v.get("cuisine"), price=v.get("price"), rating_avg=avg, rating_count=count,
            score=r["score"], days_scored=r["days_scored"], taken_total=r["taken_total"], boxes_total=r["boxes_total"],
            nights=[NightScore(**n) for n in json.loads(r["nights_json"])], verified_far_out_open=r["far_open"],
            excluded=bool(r["excluded"]), reason=ExclusionReason(r["reason"]) if r["reason"] else None, evidence=r["evidence"],
        ))
    return ResultsResponse(run_id=run_id, computed_at=run["finished_at"] or run["started_at"], listed=len(rows),
                           scored=sum(not x.excluded for x in rows), excluded=sum(x.excluded for x in rows), rows=rows)


@app.get("/api/results", response_model=ResultsResponse)
def results(run_id: int | None = None, view: Literal["all", "scored", "excluded"] = "all", q: str | None = None) -> ResultsResponse:
    conn = db.connect()
    try:
        res = _results(conn, run_id or _latest_done(conn))
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
def results_csv(run_id: int | None = None):
    conn = db.connect()
    try:
        res = _results(conn, run_id or _latest_done(conn))
    finally:
        conn.close()
    buf = io.StringIO()
    days = [n.day.isoformat() for n in res.rows[0].nights] if res.rows and res.rows[0].nights else []
    w = csv.writer(buf)
    w.writerow(["rank", "venue_id", "name", "neighborhood", "cuisine", "price", "score", "days_scored", "taken_total", "boxes_total",
                "far_out_open_boxes", "excluded", "reason", "evidence", "rating_avg", "rating_count", "url", *[f"taken_over_boxes_{d}" for d in days]])
    for r in res.rows:
        by_day = {n.day.isoformat(): n for n in r.nights}
        w.writerow([r.rank, r.venue_id, r.name, r.neighborhood, r.cuisine, r.price, r.score, r.days_scored, r.taken_total, r.boxes_total,
                    r.verified_far_out_open, int(r.excluded), r.reason.value if r.reason else "", r.evidence, r.rating_avg, r.rating_count, r.url,
                    *[(f"{by_day[d].taken}/{by_day[d].boxes}" if d in by_day and by_day[d].boxes else "") for d in days]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="sf_busyness_run{res.run_id}.csv"'})


@app.get("/api/venues/{venue_id}/history", response_model=list[VenueHistoryRow])
def venue_history(venue_id: int) -> list[VenueHistoryRow]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM venue_day_state WHERE venue_id = ? ORDER BY service_day, valid_from", (venue_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        raise HTTPException(404, "No observations for this venue")
    return [VenueHistoryRow(service_day=r["service_day"], boxes=r["boxes"], open_boxes=r["open_boxes"],
                            open_times=json.loads(r["state_json"])["open_times"], valid_from=r["valid_from"], valid_to=r["valid_to"],
                            is_current=bool(r["is_current"]), first_run_id=r["first_run_id"]) for r in rows]


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
