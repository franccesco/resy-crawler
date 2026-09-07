"""Pydantic models: run parameters, stored observations, scoring output, API shapes."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

Service = Literal["dinner", "lunch", "brunch", "breakfast", "all"]


# ---- run parameters --------------------------------------------------------------


class RunParams(BaseModel):
    """What a run ingests.

    Party size is an ingestion parameter because Resy answers per party size and
    applies its own table assignment; there is no inventory to derive other sizes from.
    """

    party_sizes: list[int] = Field(
        default=[2],
        min_length=1,
        max_length=6,
        description="Party sizes to ingest; two requests per size per day",
    )
    days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Consecutive days starting tomorrow (Pacific)",
    )
    verify_offset_days: int = Field(
        default=21,
        ge=8,
        le=60,
        description="Far-out day used to tell 'sold out' from 'no inventory'",
    )
    start_day: date | None = Field(
        default=None,
        description="First service day; defaults to tomorrow, Pacific time",
    )
    source: Literal["manual", "schedule"] = "manual"


class ScoringParams(BaseModel):
    """How stored observations are scored. Applied at read time, never at ingestion."""

    party_size: int = Field(default=2, ge=1)
    service: Service = Field(
        default="dinner", description="Which seating windows count"
    )
    grid: Literal[15, 30] = Field(default=30, description="Minutes per box")
    min_nights: int = Field(
        default=2, ge=1, description="Nights with a window needed to be scored"
    )


# ---- stored observation (one venue, one day, one party size) ----------------------


class ServiceWindow(BaseModel):
    """First and last seating for one service type, from Resy's notify range."""

    svc: int | None
    start: str
    end: str
    step: int = 30


class Slot(BaseModel):
    """One bookable start time and the seating area Resy assigned to it."""

    t: str
    type: str = ""


class Observation(BaseModel):
    """Raw availability state as Resy reported it; the hash covers all that moves."""

    windows: list[ServiceWindow] = []
    slots: list[Slot] = []
    events: int = 0
    is_tock: bool = False
    source_name: str | None = None
    reopen_date: str | None = None
    hash: str = ""


class VenueAttrs(BaseModel):
    """Descriptive venue attributes; a new SCD2 version opens when the hash changes."""

    name: str | None
    url_slug: str | None
    url: str | None
    neighborhood: str | None
    cuisine: str | None
    price: str | None
    locality: str | None
    location_code: str | None
    lat: float | None
    lng: float | None
    phone: str | None
    max_party_size: int | None
    hash: str = ""


# ---- scoring output ----------------------------------------------------------------


class NightEval(BaseModel):
    """One night of one venue, scored under a set of `ScoringParams`."""

    day: date
    boxes: int
    open_boxes: int
    taken: int
    open_times: list[str]
    window: str | None
    any_window: bool
    events: int
    is_tock: bool
    source_name: str | None
    reopen_date: str | None


class ExclusionReason(StrEnum):
    """Why a venue is held out of the ranked set."""

    closed = "closed"
    other_platform = "other_platform"
    events_only = "events_only"
    no_service = "no_service"
    no_inventory = "no_inventory"
    insufficient_data = "insufficient_data"


class Verdict(BaseModel):
    """Scored or excluded, with the score and its inputs."""

    excluded: bool
    reason: ExclusionReason | None = None
    evidence: str | None = None
    score: float | None = None
    days_scored: int = 0
    taken_total: int = 0
    boxes_total: int = 0


# ---- API shapes -------------------------------------------------------------------


class RunStatus(StrEnum):
    """Lifecycle of a run."""

    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class LogLine(BaseModel):
    """One request or lifecycle event from a run."""

    ts: datetime
    msg: str
    tag: str = ""
    level: Literal["info", "warn", "error"] = "info"


class RunSummary(BaseModel):
    """Counters for one run."""

    id: int
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    params: RunParams
    requests_total: int
    requests_done: int
    venues_seen: int
    states_new: int = Field(
        description="SCD2 rows opened this run (changed or first seen)"
    )
    states_unchanged: int
    error: str | None = None


class RunDetail(RunSummary):
    """A run with its most recent log lines."""

    log: list[LogLine]


class SchedulerStatus(BaseModel):
    """State of the in-process hourly scheduler."""

    enabled: bool
    interval_minutes: int
    next_run_at: datetime | None
    last_run_id: int | None
    active_run_id: int | None


class NightScore(BaseModel):
    """Per-night inputs shown next to a score."""

    day: date
    boxes: int
    open_boxes: int
    taken: int
    ratio: float | None
    open_times: list[str]
    window: str | None


class VenueResult(BaseModel):
    """One row of the ranked table."""

    rank: int | None
    venue_id: int
    name: str
    url: str | None
    neighborhood: str | None
    cuisine: str | None
    price: str | None
    phone: str | None
    rating_avg: float | None
    rating_count: int | None
    rating_tier: str | None = Field(
        description="S/A/B/C/D/F from Resy's average; None under 20 reviews"
    )
    score: float | None = Field(
        description="Mean of nightly taken/boxes; 0 = wide open, 1 = sold out"
    )
    days_scored: int
    taken_total: int
    boxes_total: int
    nights: list[NightScore]
    verified_far_out_open: int | None
    excluded: bool
    reason: ExclusionReason | None
    evidence: str | None


class ResultsResponse(BaseModel):
    """The ranked table for one run under one set of scoring parameters."""

    run_id: int
    computed_at: datetime
    scoring: ScoringParams
    party_sizes_available: list[int]
    listed: int
    scored: int
    excluded: int
    rows: list[VenueResult]


class WindowNight(BaseModel):
    """Open boxes on one night, and which of them are new since the last snapshot."""

    day: date
    window: str | None
    open_times: list[str]
    new_times: list[str] = Field(
        description="Open now but not in the previous version of this night's state"
    )
    previous_seen_at: datetime | None


class VenueWindows(BaseModel):
    """Everything bookable at one venue this week, night by night."""

    rank: int | None
    venue_id: int
    name: str
    url: str | None
    phone: str | None
    neighborhood: str | None
    cuisine: str | None
    price: str | None
    score: float | None
    nights: list[WindowNight]
    open_total: int
    new_total: int


class WindowsResponse(BaseModel):
    """Windows across every venue above a score threshold."""

    run_id: int
    computed_at: datetime
    scoring: ScoringParams
    min_score: float
    rows: list[VenueWindows]


class VenueHistoryRow(BaseModel):
    """One SCD2 version of a venue's nightly observation."""

    service_day: date
    party_size: int
    open_times: list[str]
    windows: list[ServiceWindow]
    valid_from: datetime
    valid_to: datetime | None
    is_current: bool
    first_run_id: int
    last_seen_run_id: int
