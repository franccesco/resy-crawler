from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class RunParams(BaseModel):
    """What a run queries. Defaults mirror the decisions in METHODOLOGY.md."""

    party_size: int = Field(2, ge=1, le=20)
    days: int = Field(7, ge=1, le=30, description="Consecutive days starting tomorrow")
    service: Literal["dinner"] = "dinner"
    verify_offset_days: int = Field(21, ge=8, le=60, description="Far-out day used to tell 'sold out' from 'no inventory'")
    start_day: date | None = Field(None, description="First service day; defaults to tomorrow in Pacific time")


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
    scored: int
    excluded: int
    error: str | None = None


class RunDetail(RunSummary):
    log: list[LogLine]


class NightScore(BaseModel):
    day: date
    boxes: int = Field(description="Half-hour slots between first and last dinner seating for the party")
    open_boxes: int
    taken: int
    ratio: float | None = Field(description="taken / boxes; None when no dinner window that day")
    open_times: list[str]
    window: str | None


class ExclusionReason(str, Enum):
    closed = "closed"
    other_platform = "other_platform"
    events_only = "events_only"
    no_dinner_service = "no_dinner_service"
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
    rating_avg: float | None
    rating_count: int | None
    score: float | None = Field(description="Mean of nightly taken/boxes over scored nights, 0 = wide open, 1 = sold out")
    days_scored: int
    taken_total: int
    boxes_total: int
    nights: list[NightScore]
    verified_far_out_open: int | None = Field(description="Open dinner boxes on the far-out verification day")
    excluded: bool
    reason: ExclusionReason | None
    evidence: str | None


class ResultsResponse(BaseModel):
    run_id: int
    computed_at: datetime
    listed: int
    scored: int
    excluded: int
    rows: list[VenueResult]


class VenueHistoryRow(BaseModel):
    service_day: date
    boxes: int
    open_boxes: int
    open_times: list[str]
    valid_from: datetime
    valid_to: datetime | None
    is_current: bool
    first_run_id: int
