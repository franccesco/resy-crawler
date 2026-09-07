"""Scored versus excluded: empty must never pass for full."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from resy_busyness.models import ExclusionReason, NightEval
from resy_busyness.scoring import classify

TODAY = "2026-09-07"


def night(
    boxes: int,
    open_boxes: int,
    day: int = 8,
    *,
    any_window: bool | None = None,
    events: int = 0,
    is_tock: bool = False,
    source_name: str | None = None,
    reopen_date: str | None = None,
) -> NightEval:
    """Build a NightEval directly; scoring math is covered elsewhere."""
    d = date(2026, 9, 1) + timedelta(days=day)
    return NightEval(
        day=d,
        boxes=boxes,
        open_boxes=open_boxes,
        taken=boxes - open_boxes,
        open_times=[f"{18 + i}:00" for i in range(open_boxes)],
        window="dinner 17:00-22:00" if boxes else None,
        any_window=bool(boxes) if any_window is None else any_window,
        events=events,
        is_tock=is_tock,
        source_name=source_name,
        reopen_date=reopen_date,
    )


def test_plain_mean_over_nights_with_a_window() -> None:
    nights = [night(11, 2, 9), night(11, 0, 12), night(0, 0, 13)]
    v = classify(nights, None, TODAY, 2, "dinner")
    assert not v.excluded
    assert v.score == round(((9 / 11) + 1.0) / 2, 4)
    assert v.days_scored == 2
    assert (v.taken_total, v.boxes_total) == (20, 22)


def test_future_reopen_date_is_closed() -> None:
    v = classify([night(11, 0, reopen_date="2026-12-01")], None, TODAY, 1, "dinner")
    assert v.reason == ExclusionReason.closed
    assert v.evidence and "2026-12-01" in v.evidence


def test_past_reopen_date_is_not_closed() -> None:
    v = classify([night(11, 5, reopen_date="2021-03-05")], None, TODAY, 1, "dinner")
    assert not v.excluded


def test_other_platform_via_tock_flag_or_source_name() -> None:
    tock = classify([night(11, 5, is_tock=True)], None, TODAY, 1, "dinner")
    src = classify([night(11, 5, source_name="OpenTable")], None, TODAY, 1, "dinner")
    assert tock.reason == ExclusionReason.other_platform
    assert src.reason == ExclusionReason.other_platform
    assert src.evidence and "OpenTable" in src.evidence


def test_events_only_when_events_but_never_a_window() -> None:
    v = classify([night(0, 0, events=2), night(0, 0)], None, TODAY, 1, "dinner")
    assert v.reason == ExclusionReason.events_only


def test_no_service_distinguishes_other_services_from_nothing() -> None:
    lunch_only = classify([night(0, 0, any_window=True)], None, TODAY, 1, "dinner")
    nothing = classify([night(0, 0)], None, TODAY, 1, "dinner")
    assert lunch_only.reason == ExclusionReason.no_service
    assert lunch_only.evidence and "other services" in lunch_only.evidence
    assert nothing.reason == ExclusionReason.no_service
    assert nothing.evidence and "any day" in nothing.evidence


def test_too_few_nights_with_a_window() -> None:
    v = classify([night(11, 3), night(0, 0, 9)], None, TODAY, 2, "dinner")
    assert v.reason == ExclusionReason.insufficient_data


def test_sold_out_all_week_with_nothing_far_out_is_no_inventory() -> None:
    nights = [night(11, 0, 8), night(11, 0, 9)]
    v = classify(nights, night(11, 0, 28), TODAY, 2, "dinner")
    assert v.reason == ExclusionReason.no_inventory
    missing = classify(nights, None, TODAY, 2, "dinner")
    assert missing.reason == ExclusionReason.no_inventory
    assert missing.evidence and "no data" in missing.evidence


def test_sold_out_all_week_with_tables_far_out_scores_one() -> None:
    nights = [night(11, 0, 8), night(11, 0, 9)]
    v = classify(nights, night(11, 5, 28), TODAY, 2, "dinner")
    assert not v.excluded
    assert v.score == pytest.approx(1.0)
    assert v.evidence and "5 box(es) open three weeks out" in v.evidence
