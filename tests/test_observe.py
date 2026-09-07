"""The observation hash decides when SCD2 opens a new version."""

from __future__ import annotations

from resy_busyness.scoring import observe, rating_tier

from .conftest import make_hit


def test_hash_ignores_key_order_and_ratings() -> None:
    a = make_hit(windows={"dinner": ("17:00", "22:00")}, slots=["19:00"])
    b = dict(reversed(list(a.items())))
    b["rating"] = {"average": 1.0, "count": 1}
    assert observe(a).hash == observe(b).hash


def test_hash_changes_when_a_slot_or_its_seating_type_changes() -> None:
    base = make_hit(windows={"dinner": ("17:00", "22:00")}, slots=[("19:00", "Bar")])
    moved = make_hit(windows={"dinner": ("17:00", "22:00")}, slots=[("19:30", "Bar")])
    relabelled = make_hit(
        windows={"dinner": ("17:00", "22:00")}, slots=[("19:00", "Patio")]
    )
    hashes = {observe(h).hash for h in (base, moved, relabelled)}
    assert len(hashes) == 3


def test_rating_tier_cutoffs_and_review_floor() -> None:
    assert rating_tier(4.85, 100) == "S"
    assert rating_tier(4.849, 100) == "A"
    assert rating_tier(4.70, 100) == "A"
    assert rating_tier(4.55, 100) == "B"
    assert rating_tier(4.40, 100) == "C"
    assert rating_tier(4.00, 100) == "D"
    assert rating_tier(3.96, 2387) == "F"
    assert rating_tier(4.9, 19) is None
    assert rating_tier(None, 500) is None
