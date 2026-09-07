"""FastAPI surface. Start with: uv run uvicorn resy_busyness.api:app --reload."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date
from types import TracebackType
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, pipeline, scheduler
from .config import settings
from .models import (
    LogLine,
    NightEval,
    NightScore,
    Observation,
    ResultsResponse,
    RunDetail,
    RunParams,
    RunStatus,
    RunSummary,
    SchedulerStatus,
    ScoringParams,
    VenueHistoryRow,
    VenueResult,
    VenueWindows,
    Verdict,
    WindowNight,
    WindowsResponse,
)
from .scoring import SERVICES, classify, rating_tier, score_night


class _Lifespan:
    """Start the scheduler when the app starts; nothing to stop on shutdown."""

    def __init__(self, _: FastAPI) -> None:
        pass

    async def __aenter__(self) -> None:
        scheduler.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


app = FastAPI(
    title="Resy SF Busyness",
    version="0.2.0",
    lifespan=_Lifespan,
    description=(
        "How booked each San Francisco restaurant on Resy is, "
        "and where the windows are."
    ),
)


def _summary(r: sqlite3.Row) -> RunSummary:
    return RunSummary(
        id=r["id"],
        status=RunStatus(r["status"]),
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        params=RunParams.model_validate_json(r["params_json"]),
        requests_total=r["requests_total"],
        requests_done=r["requests_done"],
        venues_seen=r["venues_seen"],
        states_new=r["states_new"],
        states_unchanged=r["states_unchanged"],
        error=r["error"],
    )


# ---- runs ------------------------------------------------------------------------


@app.post("/api/runs", response_model=RunSummary, status_code=202)
def start_run(params: RunParams | None = None) -> RunSummary:
    """Start a run in the background.

    Args:
        params: Run parameters; defaults to a party of two over seven nights.

    Returns:
        The queued run.

    Raises:
        HTTPException: 409 when a run is already in progress.

    """
    run_id = pipeline.start_run(params or RunParams())
    if run_id is None:
        raise HTTPException(409, "A run is already in progress.")
    conn = db.connect()
    try:
        row = db.get_run(conn, run_id)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(500, "Run vanished after creation.")
    return _summary(row)


@app.get("/api/runs", response_model=list[RunSummary])
def list_runs(limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[RunSummary]:
    """List runs, newest first.

    Args:
        limit: Maximum number of runs.

    Returns:
        Run summaries.

    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [_summary(r) for r in rows]


@app.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: int, log_limit: Annotated[int, Query(ge=1, le=2000)] = 60
) -> RunDetail:
    """Fetch one run with its most recent log lines.

    Args:
        run_id: The run.
        log_limit: Maximum log lines, newest first.

    Returns:
        The run and its log.

    Raises:
        HTTPException: 404 when the run does not exist.

    """
    conn = db.connect()
    try:
        r = db.get_run(conn, run_id)
        if not r:
            raise HTTPException(404, "No such run")
        logs = conn.execute(
            "SELECT ts, level, msg, tag FROM run_log WHERE run_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (run_id, log_limit),
        ).fetchall()
    finally:
        conn.close()
    lines = [
        LogLine(ts=row["ts"], level=row["level"], msg=row["msg"], tag=row["tag"])
        for row in logs
    ]
    return RunDetail(**_summary(r).model_dump(), log=lines)


@app.get("/api/scheduler", response_model=SchedulerStatus)
def scheduler_status() -> SchedulerStatus:
    """Report the hourly scheduler's state.

    Returns:
        Interval, next run, last and active run ids.

    """
    return scheduler.status()


# ---- scoring at read time -------------------------------------------------------


def scoring_params(
    party_size: Annotated[int, Query(ge=1)] = settings.party_size,
    service: Literal["dinner", "lunch", "brunch", "breakfast", "all"] = "dinner",
    grid: Annotated[int, Query(description="15 or 30")] = 30,
    min_nights: Annotated[int, Query(ge=1)] = 2,
) -> ScoringParams:
    """Read scoring parameters from the query string.

    Args:
        party_size: Party size the run must have ingested.
        service: Which seating windows count.
        grid: Minutes per box.
        min_nights: Nights with a window needed to be scored.

    Returns:
        Validated scoring parameters.

    Raises:
        HTTPException: 422 when the grid is not 15 or 30.

    """
    if grid not in (15, 30):
        raise HTTPException(422, "grid must be 15 or 30")
    return ScoringParams(
        party_size=party_size, service=service, grid=grid, min_nights=min_nights
    )


Scoring = Annotated[ScoringParams, Depends(scoring_params)]


def _resolve_run(conn: sqlite3.Connection, run_id: int | None) -> sqlite3.Row:
    r = db.get_run(conn, run_id) if run_id else db.latest_done_run(conn)
    if r is None:
        detail = (
            "No completed run yet. POST /api/runs to start one."
            if run_id is None
            else "No such run"
        )
        raise HTTPException(404, detail)
    return r


class _Scored:
    """One venue's evaluation under a set of scoring parameters."""

    def __init__(
        self,
        venue_id: int,
        nights: list[NightEval],
        far: NightEval | None,
        verdict: Verdict,
    ) -> None:
        self.venue_id = venue_id
        self.nights = nights
        self.far = far
        self.verdict = verdict
        self.rank: int | None = None


def _svc_ids(sp: ScoringParams) -> set[int] | None:
    return None if sp.service == "all" else {SERVICES[sp.service]}


def _compute(
    conn: sqlite3.Connection, run: sqlite3.Row, sp: ScoringParams
) -> list[_Scored]:
    days, verify_day = pipeline.run_days(run)
    run_params = RunParams.model_validate_json(run["params_json"])
    if sp.party_size not in run_params.party_sizes:
        raise HTTPException(
            404,
            f"Run {run['id']} ingested party sizes {run_params.party_sizes}, "
            f"not {sp.party_size}.",
        )
    svc_ids = _svc_ids(sp)
    today = pipeline.today_pacific().isoformat()
    label = sp.service if sp.service != "all" else "any-service"
    by_venue = db.states_as_of_run(conn, run["id"], sp.party_size, [*days, verify_day])
    rows: list[_Scored] = []
    for vid, states in by_venue.items():
        nights = [
            score_night(states[d], date.fromisoformat(d), svc_ids, sp.grid)
            for d in days
            if d in states
        ]
        far = None
        if verify_day in states:
            far_day = date.fromisoformat(verify_day)
            far = score_night(states[verify_day], far_day, svc_ids, sp.grid)
        verdict = classify(nights, far, today, sp.min_nights, label)
        rows.append(_Scored(vid, nights, far, verdict))
    scored = sorted(
        (r for r in rows if not r.verdict.excluded),
        key=lambda r: (-(r.verdict.score or 0), -r.verdict.taken_total),
    )
    for i, r in enumerate(scored):
        r.rank = i + 1
    rows.sort(key=lambda r: (r.verdict.excluded, r.rank or 0, r.venue_id))
    return rows


def _night_score(n: NightEval) -> NightScore:
    return NightScore(
        day=n.day,
        boxes=n.boxes,
        open_boxes=n.open_boxes,
        taken=n.taken,
        ratio=round(n.taken / n.boxes, 4) if n.boxes else None,
        open_times=n.open_times,
        window=n.window,
    )


def _venue_result(conn: sqlite3.Connection, run_id: int, r: _Scored) -> VenueResult:
    v = db.venue_as_of_run(conn, run_id, r.venue_id)
    avg, count = db.rating(conn, run_id, r.venue_id)
    verdict = r.verdict
    return VenueResult(
        rank=r.rank,
        venue_id=r.venue_id,
        name=(v.name if v else None) or str(r.venue_id),
        url=v.url if v else None,
        neighborhood=v.neighborhood if v else None,
        cuisine=v.cuisine if v else None,
        price=v.price if v else None,
        phone=v.phone if v else None,
        rating_avg=avg,
        rating_count=count,
        rating_tier=rating_tier(avg, count),
        score=verdict.score,
        days_scored=verdict.days_scored,
        taken_total=verdict.taken_total,
        boxes_total=verdict.boxes_total,
        nights=[_night_score(n) for n in r.nights],
        verified_far_out_open=r.far.open_boxes if r.far else None,
        excluded=verdict.excluded,
        reason=verdict.reason,
        evidence=verdict.evidence,
    )


def _results(
    conn: sqlite3.Connection, run_id: int | None, sp: ScoringParams
) -> ResultsResponse:
    run = _resolve_run(conn, run_id)
    rows = _compute(conn, run, sp)
    out = [_venue_result(conn, run["id"], r) for r in rows]
    return ResultsResponse(
        run_id=run["id"],
        computed_at=run["finished_at"] or run["started_at"],
        scoring=sp,
        party_sizes_available=RunParams.model_validate_json(
            run["params_json"]
        ).party_sizes,
        listed=len(out),
        scored=sum(not x.excluded for x in out),
        excluded=sum(x.excluded for x in out),
        rows=out,
    )


@app.get("/api/results", response_model=ResultsResponse)
def results(
    sp: Scoring,
    run_id: int | None = None,
    view: Literal["all", "scored", "excluded"] = "all",
    q: str | None = None,
) -> ResultsResponse:
    """Return the ranked table, scored at read time from stored observations.

    Args:
        sp: Scoring parameters from the query string.
        run_id: The run to score; defaults to the latest finished run.
        view: Restrict to scored or excluded venues.
        q: Case-insensitive filter on name, neighborhood or cuisine.

    Returns:
        The ranked table with per-night inputs.

    """
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
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in (r.name or "").lower()
            or needle in (r.neighborhood or "").lower()
            or needle in (r.cuisine or "").lower()
        ]
    return res.model_copy(update={"rows": rows})


def _csv_rows(res: ResultsResponse, sp: ScoringParams) -> str:
    buf = io.StringIO()
    days = [n.day.isoformat() for n in res.rows[0].nights] if res.rows else []
    w = csv.writer(buf)
    w.writerow(
        [
            f"# run {res.run_id} computed {res.computed_at} · scoring: "
            f"party_size={sp.party_size} service={sp.service} grid={sp.grid}min "
            f"min_nights={sp.min_nights}"
        ]
    )
    head = [
        "rank",
        "venue_id",
        "name",
        "neighborhood",
        "cuisine",
        "price",
        "score",
        "days_scored",
        "taken_total",
        "boxes_total",
        "far_out_open_boxes",
        "excluded",
        "reason",
        "evidence",
        "rating_tier",
        "rating_avg",
        "rating_count",
        "url",
    ]
    w.writerow(
        [
            *head,
            *[f"taken_over_boxes_{d}" for d in days],
            *[f"open_times_{d}" for d in days],
        ]
    )
    for r in res.rows:
        by_day = {n.day.isoformat(): n for n in r.nights}
        taken = [
            f"{by_day[d].taken}/{by_day[d].boxes}"
            if d in by_day and by_day[d].boxes
            else ""
            for d in days
        ]
        opened = [" ".join(by_day[d].open_times) if d in by_day else "" for d in days]
        w.writerow(
            [
                r.rank,
                r.venue_id,
                r.name,
                r.neighborhood,
                r.cuisine,
                r.price,
                r.score,
                r.days_scored,
                r.taken_total,
                r.boxes_total,
                r.verified_far_out_open,
                int(r.excluded),
                r.reason.value if r.reason else "",
                r.evidence,
                r.rating_tier,
                r.rating_avg,
                r.rating_count,
                r.url,
                *taken,
                *opened,
            ]
        )
    return buf.getvalue()


@app.get("/api/results.csv")
def results_csv(sp: Scoring, run_id: int | None = None) -> StreamingResponse:
    """Return the ranked table as CSV; the first line records the scoring parameters.

    Args:
        sp: Scoring parameters from the query string.
        run_id: The run to score; defaults to the latest finished run.

    Returns:
        A CSV download.

    """
    conn = db.connect()
    try:
        res = _results(conn, run_id, sp)
    finally:
        conn.close()
    name = f"sf_busyness_run{res.run_id}_{sp.service}_p{sp.party_size}.csv"
    return StreamingResponse(
        iter([_csv_rows(res, sp)]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---- windows: where the exclusive places have tables ---------------------------------


def _venue_windows(
    conn: sqlite3.Connection, run: sqlite3.Row, r: _Scored, sp: ScoringParams
) -> VenueWindows:
    svc_ids = _svc_ids(sp)
    nights: list[WindowNight] = []
    for n in r.nights:
        if not n.boxes:
            continue
        prev = db.previous_state(
            conn, run["id"], r.venue_id, n.day.isoformat(), sp.party_size
        )
        prev_open: set[str] = set()
        if prev:
            prev_obs = Observation.model_validate_json(prev["state_json"])
            prev_open = set(score_night(prev_obs, n.day, svc_ids, sp.grid).open_times)
        new_times = [t for t in n.open_times if t not in prev_open] if prev else []
        nights.append(
            WindowNight(
                day=n.day,
                window=n.window,
                open_times=n.open_times,
                new_times=new_times,
                previous_seen_at=prev["valid_to"] if prev else None,
            )
        )
    v = db.venue_as_of_run(conn, run["id"], r.venue_id)
    return VenueWindows(
        rank=r.rank,
        venue_id=r.venue_id,
        name=(v.name if v else None) or str(r.venue_id),
        url=v.url if v else None,
        phone=v.phone if v else None,
        neighborhood=v.neighborhood if v else None,
        cuisine=v.cuisine if v else None,
        price=v.price if v else None,
        score=r.verdict.score,
        nights=nights,
        open_total=sum(len(x.open_times) for x in nights),
        new_total=sum(len(x.new_times) for x in nights),
    )


@app.get("/api/windows", response_model=WindowsResponse)
def windows(
    sp: Scoring,
    run_id: int | None = None,
    min_score: Annotated[float, Query(ge=0, le=1)] = 0.7,
) -> WindowsResponse:
    """List exclusive venues' open boxes and what appeared since the last snapshot.

    Args:
        sp: Scoring parameters from the query string.
        run_id: The run to score; defaults to the latest finished run.
        min_score: Minimum busyness score to include.

    Returns:
        Windows per venue, highest score first.

    """
    conn = db.connect()
    try:
        run = _resolve_run(conn, run_id)
        rows = _compute(conn, run, sp)
        out = [
            _venue_windows(conn, run, r, sp)
            for r in rows
            if not r.verdict.excluded and (r.verdict.score or 0) >= min_score
        ]
    finally:
        conn.close()
    return WindowsResponse(
        run_id=run["id"],
        computed_at=run["finished_at"] or run["started_at"],
        scoring=sp,
        min_score=min_score,
        rows=out,
    )


@app.get("/api/venues/{venue_id}/windows", response_model=VenueWindows)
def venue_windows(
    venue_id: int, sp: Scoring, run_id: int | None = None
) -> VenueWindows:
    """Return one venue's open boxes per night; this is what an expanded row shows.

    Args:
        venue_id: Resy venue id.
        sp: Scoring parameters from the query string.
        run_id: The run to score; defaults to the latest finished run.

    Returns:
        The venue's windows and phone number.

    Raises:
        HTTPException: 404 when the venue was not observed by the run.

    """
    conn = db.connect()
    try:
        run = _resolve_run(conn, run_id)
        rows = _compute(conn, run, sp)
        match = [r for r in rows if r.venue_id == venue_id]
        if not match:
            raise HTTPException(404, "Venue not in this run")
        return _venue_windows(conn, run, match[0], sp)
    finally:
        conn.close()


@app.get("/api/venues/{venue_id}/history", response_model=list[VenueHistoryRow])
def venue_history(
    venue_id: int, party_size: int | None = None
) -> list[VenueHistoryRow]:
    """Return every stored version of a venue's nightly observations.

    Args:
        venue_id: Resy venue id.
        party_size: Restrict to one party size.

    Returns:
        Versions ordered by day, party size and validity start.

    Raises:
        HTTPException: 404 when the venue has no observations.

    """
    conn = db.connect()
    try:
        where = " AND party_size = ?" if party_size else ""
        args: tuple[int, ...] = (venue_id, party_size) if party_size else (venue_id,)
        rows = conn.execute(
            f"SELECT * FROM venue_day_state WHERE venue_id = ?{where} "
            "ORDER BY service_day, party_size, valid_from",
            args,
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise HTTPException(404, "No observations for this venue")
    out: list[VenueHistoryRow] = []
    for r in rows:
        obs = Observation.model_validate_json(r["state_json"])
        out.append(
            VenueHistoryRow(
                service_day=r["service_day"],
                party_size=r["party_size"],
                open_times=[s.t for s in obs.slots],
                windows=obs.windows,
                valid_from=r["valid_from"],
                valid_to=r["valid_to"],
                is_current=bool(r["is_current"]),
                first_run_id=r["first_run_id"],
                last_seen_run_id=r["last_seen_run_id"],
            )
        )
    return out


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the single-page UI.

    Returns:
        The static index page.

    """
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
