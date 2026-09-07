from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Service = Literal["dinner", "lunch", "brunch", "breakfast", "all"]


class RunParams(BaseModel):
    """What a run ingests. Party size has to be an ingestion parameter: Resy answers per party size."""

    party_sizes: list[int] = Field([2], min_length=1, max_length=6, description="Party sizes to ingest; two requests per size per day")
    days: int = Field(7, ge=1, le=30, description="Consecutive days starting tomorrow (Pacific)")
    verify_offset_days: int = Field(21, ge=8, le=60, description="Far-out day used to tell 'sold out' from 'no inventory'")
    start_day: date | None = Field(None, description="First service day; defaults to tomorrow in Pacific time")
    source: Literal["manual", "schedule"] = "manual"


class ScoringParams(BaseModel):
    """How the stored observations are scored. Applied at read time; nothing here is baked in at ingestion."""

    party_size: int = Field(2, ge=1)
    service: Service = Field("dinner", description="Which seating windows count")
    grid: Literal[15, 30] = Field(30, description="Minutes per box")
    min_nights: int = Field(2, ge=1, description="Nights with a window needed to be scored")


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class LogLine(BaseModel):
    ts: datetime
    msg: str
    tag: str = ""
    level: Literal["info", "warn", "error"] = "info"


class RunSummary(BaseModel):
    id: int
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    params: RunParams
    requests_total: int
    requests_done: int
    venues_seen: int
    states_new: int = Field(description="SCD2 rows opened this run (changed or first seen)")
    states_unchanged: int
    error: str | None = None


class RunDetail(RunSummary):
    log: list[LogLine]


class SchedulerStatus(BaseModel):
    enabled: bool
    interval_minutes: int
    next_run_at: datetime | None
    last_run_id: int | None
    active_run_id: int | None


class NightScore(BaseModel):
    day: date
    boxes: int
    open_boxes: int
    taken: int
    ratio: float | None
    open_times: list[str]
    window: str | None


class ExclusionReason(str, Enum):
    closed = "closed"
    other_platform = "other_platform"
    events_only = "events_only"
    no_service = "no_service"
    no_inventory = "no_inventory"
    insufficient_data = "insufficient_data"


class VenueResult(BaseModel):
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
    rating_tier: str | None = Field(description="S/A/B/C/D/F from Resy's average; None under 20 reviews")
    score: float | None = Field(description="Mean of nightly taken/boxes over scored nights, 0 = wide open, 1 = sold out")
    days_scored: int
    taken_total: int
    boxes_total: int
    nights: list[NightScore]
    verified_far_out_open: int | None
    excluded: bool
    reason: ExclusionReason | None
    evidence: str | None


class ResultsResponse(BaseModel):
    run_id: int
    computed_at: datetime
    scoring: ScoringParams
    party_sizes_available: list[int]
    listed: int
    scored: int
    excluded: int
    rows: list[VenueResult]


class WindowNight(BaseModel):
    day: date
    window: str | None
    open_times: list[str]
    new_times: list[str] = Field(description="Open now but not in the previous version of this night's state")
    previous_seen_at: datetime | None


class VenueWindows(BaseModel):
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
    run_id: int
    computed_at: datetime
    scoring: ScoringParams
    min_score: float
    rows: list[VenueWindows]


class VenueHistoryRow(BaseModel):
    service_day: date
    party_size: int
    open_times: list[str]
    windows: list[dict]
    valid_from: datetime
    valid_to: datetime | None
    is_current: bool
    first_run_id: int
    last_seen_run_id: int
