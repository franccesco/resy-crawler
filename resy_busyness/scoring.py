"""Pure functions.

Ingestion keeps the raw observation for one (venue, night, party size): every service
window and every open slot time. Scoring is applied at read time from parameters, so
"dinner on a 30-minute grid" is a query, not something baked into the store.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

PRICE = {1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}
SERVICES = {"brunch": 1, "dinner": 2, "lunch": 3, "breakfast": 5}
SERVICE_NAME = {v: k for k, v in SERVICES.items()}


# ---- observation (ingestion) ----------------------------------------------------

def observe(hit: dict[str, Any]) -> dict[str, Any]:
    """Raw per-night state from one search hit. Hash covers everything that can move."""
    a = hit.get("availability") or {}
    windows = []
    for o in a.get("notify_options") or []:
        try:
            t0 = datetime.fromisoformat(o["min_request_datetime"])
            t1 = datetime.fromisoformat(o["max_request_datetime"])
        except (KeyError, ValueError):
            continue
        windows.append({"svc": o.get("service_type_id"), "start": f"{t0:%H:%M}", "end": f"{t1:%H:%M}", "step": o.get("step_minutes") or 30})
    windows.sort(key=lambda w: (w["svc"] or 0, w["start"]))
    slots = sorted({
        (s["date"]["start"][11:16], (s.get("config") or {}).get("type") or "")
        for s in a.get("slots") or [] if (s.get("date") or {}).get("start")
    })
    state = {
        "windows": windows,
        "slots": [{"t": t, "type": ty} for t, ty in slots],
        "events": len(a.get("events") or []),
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


# ---- scoring (read time) ----------------------------------------------------------

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def score_night(state: dict[str, Any], svc_ids: set[int] | None, grid: int) -> dict[str, Any]:
    """Boxes are grid points from first to last seating (inclusive) of the selected services.
    A box is open when any slot, snapped down to the grid, lands in it."""
    boxes: set[int] = set()
    spans = []
    for w in state.get("windows") or []:
        if svc_ids is not None and w.get("svc") not in svc_ids:
            continue
        t, end = _minutes(w["start"]), _minutes(w["end"])
        spans.append(f"{SERVICE_NAME.get(w.get('svc'), 'service')} {w['start']}-{w['end']}")
        while t <= end:
            boxes.add(t)
            t += grid
    opened: set[int] = set()
    for s in state.get("slots") or []:
        m = _minutes(s["t"])
        key = (m // grid) * grid
        if key in boxes:
            opened.add(key)
    return {
        "boxes": len(boxes),
        "open_boxes": len(opened),
        "taken": len(boxes) - len(opened),
        "open_times": [_hhmm(m) for m in sorted(opened)],
        "window": ", ".join(spans) or None,
        "any_window": bool(state.get("windows")),
        "events": state.get("events", 0),
        "is_tock": state.get("is_tock", False),
        "source_name": state.get("source_name"),
        "reopen_date": state.get("reopen_date"),
    }


def classify(nights: list[dict[str, Any]], far: dict[str, Any] | None, today: str, min_nights: int, service_label: str) -> dict[str, Any]:
    """Scored vs excluded, plus the score. `nights` are score_night() outputs in day order."""
    latest = nights[-1] if nights else {}
    reopen = latest.get("reopen_date")
    if reopen and reopen > today:
        return _excl("closed", f"Resy lists a reopening date of {reopen}.")
    if latest.get("is_tock") or latest.get("source_name"):
        return _excl("other_platform", f"Inventory is flagged as coming from {latest.get('source_name') or 'Tock'}, not Resy.")
    with_window = [n for n in nights if n["boxes"] > 0]
    events = sum(n["events"] for n in nights)
    if not with_window:
        if events:
            return _excl("events_only", f"{events} ticketed event(s) in the week and no regular {service_label} seating window for this party size.")
        if any(n["any_window"] for n in nights):
            return _excl("no_service", f"Seats this party size at other services only; no {service_label} window in the week.")
        return _excl("no_service", "No seating window for this party size on any day of the week.")
    if len(with_window) < min_nights:
        return _excl("insufficient_data", f"Only {len(with_window)} night(s) with a {service_label} window; need {min_nights}.")
    all_full = all(n["open_boxes"] == 0 for n in with_window)
    far_open = far["open_boxes"] if far else None
    if all_full and not far_open:
        return _excl("no_inventory", f"Zero open {service_label} tables on all {len(with_window)} nights and still zero {'' if far else '(no data) '}three weeks out: Resy holds no real inventory for this party size here.")
    ratios = [n["taken"] / n["boxes"] for n in with_window]
    return {
        "excluded": False, "reason": None,
        "evidence": f"Sold out every night this week; {far_open} box(es) open three weeks out proves inventory exists." if all_full else None,
        "score": round(sum(ratios) / len(ratios), 4),
        "days_scored": len(with_window),
        "taken_total": sum(n["taken"] for n in with_window),
        "boxes_total": sum(n["boxes"] for n in with_window),
    }


def _excl(reason: str, evidence: str) -> dict[str, Any]:
    return {"excluded": True, "reason": reason, "evidence": evidence, "score": None, "days_scored": 0, "taken_total": 0, "boxes_total": 0}
