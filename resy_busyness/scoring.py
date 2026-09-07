"""Pure functions: turn one Resy hit into a nightly state, and nightly states into a score."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

PRICE = {1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}
STEP = timedelta(minutes=30)


def dinner_boxes(hit: dict[str, Any], service_type_id: int) -> tuple[list[str], str | None]:
    """Half-hour grid from first to last dinner seating, taken from notify_options."""
    boxes: set[str] = set()
    spans = []
    for o in hit.get("availability", {}).get("notify_options") or []:
        if o.get("service_type_id") != service_type_id:
            continue
        t = datetime.fromisoformat(o["min_request_datetime"])
        end = datetime.fromisoformat(o["max_request_datetime"])
        spans.append(f"{t:%H:%M}-{end:%H:%M}")
        while t <= end:
            boxes.add(f"{t:%H:%M}")
            t += STEP
    return sorted(boxes), (", ".join(spans) or None)


def open_boxes(hit: dict[str, Any], boxes: list[str]) -> list[str]:
    """Slots arrive at 15-minute granularity; snap each down to its half-hour box."""
    grid = set(boxes)
    found: set[str] = set()
    for s in hit.get("availability", {}).get("slots") or []:
        start = s.get("date", {}).get("start")
        if not start:
            continue
        t = datetime.fromisoformat(start)
        key = f"{t.hour:02d}:{(t.minute // 30) * 30:02d}"
        if key in grid:
            found.add(key)
    return sorted(found)


def night_state(hit: dict[str, Any], service_type_id: int) -> dict[str, Any]:
    boxes, window = dinner_boxes(hit, service_type_id)
    opened = open_boxes(hit, boxes)
    a = hit.get("availability") or {}
    state = {
        "boxes": len(boxes),
        "open_boxes": len(opened),
        "open_times": opened,
        "window": window,
        "events": len(a.get("events") or []),
        "any_window": bool(a.get("notify_options")),
        "is_tock": bool(hit.get("is_tock_inventory")),
        "source_name": (hit.get("source") or {}).get("name"),
        "reopen_date": (hit.get("reopen") or {}).get("date"),
    }
    state["hash"] = hashlib.sha1(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return state


def venue_attrs(hit: dict[str, Any]) -> dict[str, Any]:
    loc = hit.get("location") or {}
    geo = hit.get("_geoloc") or {}
    slug = hit.get("url_slug")
    attrs = {
        "name": hit.get("name"),
        "url_slug": slug,
        "url": f"https://resy.com/cities/{loc.get('url_slug', 'san-francisco-ca')}/venues/{slug}" if slug else None,
        "neighborhood": hit.get("neighborhood"),
        "cuisine": "; ".join(hit.get("cuisine") or []) or None,
        "price": PRICE.get(hit.get("price_range_id")),
        "locality": hit.get("locality"),
        "location_code": loc.get("code"),
        "lat": geo.get("lat"),
        "lng": geo.get("lng"),
        "phone": (hit.get("contact") or {}).get("phone_number"),
        "max_party_size": hit.get("max_party_size"),
    }
    attrs["hash"] = hashlib.sha1(json.dumps(attrs, sort_keys=True).encode()).hexdigest()
    return attrs


def classify(nights: list[dict[str, Any]], far_out: dict[str, Any] | None, today: str, min_days: int) -> dict[str, Any]:
    """Decide scored vs excluded and compute the score. `nights` are the week's current states."""
    latest = nights[-1] if nights else {}
    reopen = latest.get("reopen_date")
    if reopen and reopen > today:
        return _excl("closed", f"Resy lists a reopening date of {reopen}.")
    if latest.get("is_tock") or latest.get("source_name"):
        who = latest.get("source_name") or "Tock"
        return _excl("other_platform", f"Inventory is flagged as coming from {who}, not Resy.")
    with_window = [n for n in nights if n["boxes"] > 0]
    events = sum(n["events"] for n in nights)
    if not with_window:
        if events:
            return _excl("events_only", f"{events} ticketed event(s) in the week and no regular dinner seating window for two.")
        if any(n["any_window"] for n in nights):
            return _excl("no_dinner_service", "Seats a party of two at lunch or brunch only; no dinner window in the week.")
        return _excl("no_dinner_service", "No seating window for a party of two on any day of the week.")
    if len(with_window) < min_days:
        return _excl("insufficient_data", f"Only {len(with_window)} night(s) with a dinner window; need {min_days}.")
    taken = sum(n["boxes"] - n["open_boxes"] for n in with_window)
    boxes = sum(n["boxes"] for n in with_window)
    all_full = all(n["open_boxes"] == 0 for n in with_window)
    far_open = far_out.get("open_boxes") if far_out else None
    if all_full and not far_open:
        return _excl(
            "no_inventory",
            f"Zero open dinner tables on all {len(with_window)} nights and still zero {'' if far_out else '(no data) '}three weeks out: Resy holds no real two-top inventory here.",
        )
    ratios = [(n["boxes"] - n["open_boxes"]) / n["boxes"] for n in with_window]
    return {
        "excluded": False,
        "reason": None,
        "evidence": (
            f"Sold out every night this week; {far_open} box(es) open three weeks out proves inventory exists."
            if all_full else None
        ),
        "score": round(sum(ratios) / len(ratios), 4),
        "days_scored": len(with_window),
        "taken_total": taken,
        "boxes_total": boxes,
    }


def _excl(reason: str, evidence: str) -> dict[str, Any]:
    return {"excluded": True, "reason": reason, "evidence": evidence, "score": None, "days_scored": 0, "taken_total": 0, "boxes_total": 0}
