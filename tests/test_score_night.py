"""The box grid: what counts as a box and what counts as open."""

from __future__ import annotations

from datetime import date

from resy_busyness.scoring import SERVICES, observe, score_night

from .conftest import make_hit

DAY = date(2026, 9, 12)
DINNER = {SERVICES["dinner"]}


def test_boxes_run_from_first_to_last_seating_inclusive() -> None:
    obs = observe(make_hit(windows={"dinner": ("17:00", "22:00")}))
    night = score_night(obs, DAY, DINNER, 30)
    assert night.boxes == 11
    assert night.open_boxes == 0
    assert night.taken == 11


def test_quarter_hour_slots_snap_down_into_half_hour_boxes() -> None:
    hit = make_hit(
        windows={"dinner": ("17:00", "22:00")},
        slots=["19:00", "19:15", "19:30", "19:45"],
    )
    night = score_night(observe(hit), DAY, DINNER, 30)
    assert night.open_boxes == 2
    assert night.open_times == ["19:00", "19:30"]


def test_fifteen_minute_grid_counts_each_quarter_hour() -> None:
    hit = make_hit(
        windows={"dinner": ("17:00", "22:00")},
        slots=["19:00", "19:15", "19:30", "19:45"],
    )
    night = score_night(observe(hit), DAY, DINNER, 15)
    assert night.boxes == 21
    assert night.open_boxes == 4


def test_slots_outside_the_selected_window_are_ignored() -> None:
    hit = make_hit(
        windows={"dinner": ("17:00", "22:00"), "lunch": ("11:00", "14:00")},
        slots=["12:00", "12:30", "20:00"],
    )
    dinner = score_night(observe(hit), DAY, DINNER, 30)
    assert dinner.boxes == 11
    assert dinner.open_times == ["20:00"]
    everything = score_night(observe(hit), DAY, None, 30)
    assert everything.boxes == 11 + 7
    assert everything.open_times == ["12:00", "12:30", "20:00"]


def test_no_window_for_the_service_means_zero_boxes_not_full() -> None:
    hit = make_hit(windows={"lunch": ("11:00", "14:00")}, slots=["12:00"])
    night = score_night(observe(hit), DAY, DINNER, 30)
    assert night.boxes == 0
    assert night.any_window is True
