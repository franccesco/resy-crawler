"""Pure functions.

Ingestion keeps the raw observation for one (venue, night, party size): every service
window and every open slot time. Scoring is applied at read time from parameters, so
"dinner on a 30-minute grid" is a query, not something baked into the store.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from .models import (
    ExclusionReason,
    NightEval,
    Observation,
    ServiceWindow,
    Slot,
    VenueAttrs,
    Verdict,
)

PRICE = {1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}
SERVICES = {"brunch": 1, "dinner": 2, "lunch": 3, "breakfast": 5}
SERVICE_NAME = {v: k for k, v in SERVICES.items()}
RATING_TIERS = [("S", 4.85), ("A", 4.70), ("B", 4.55), ("C", 4.40), ("D", 4.00)]
MIN_REVIEWS_FOR_TIER = 20


def _sha(payload: str) -> str:
    return hashlib.sha1(payload.encode()).hexdigest()


# ---- observation (ingestion) ----------------------------------------------------


def observe(hit: dict[str, Any]) -> Observation:
    """Build the raw per-night state from one search hit.

    Args:
        hit: One entry of `search.hits` from Resy's venue search response.

    Returns:
        The observation, with `hash` covering everything that can move.

    """
    availability: dict[str, Any] = hit.get("availability") or {}
    windows: list[ServiceWindow] = []
    options: list[dict[str, Any]] = availability.get("notify_options") or []
    for opt in options:
        try:
            t0 = datetime.fromisoformat(opt["min_request_datetime"])
            t1 = datetime.fromisoformat(opt["max_request_datetime"])
        except (KeyError, ValueError):
            continue
        windows.append(
            ServiceWindow(
                svc=opt.get("service_type_id"),
                start=f"{t0:%H:%M}",
                end=f"{t1:%H:%M}",
                step=opt.get("step_minutes") or 30,
            )
        )
    windows.sort(key=lambda w: (w.svc or 0, w.start))
    seen: set[tuple[str, str]] = set()
    raw_slots: list[dict[str, Any]] = availability.get("slots") or []
    for slot in raw_slots:
        when: dict[str, Any] = slot.get("date") or {}
        start: str | None = when.get("start")
        if start:
            config: dict[str, Any] = slot.get("config") or {}
            seen.add((start[11:16], config.get("type") or ""))
    source: dict[str, Any] = hit.get("source") or {}
    reopen: dict[str, Any] = hit.get("reopen") or {}
    obs = Observation(
        windows=windows,
        slots=[Slot(t=t, type=ty) for t, ty in sorted(seen)],
        events=len(availability.get("events") or []),
        is_tock=bool(hit.get("is_tock_inventory")),
        source_name=source.get("name"),
        reopen_date=reopen.get("date"),
    )
    obs.hash = _sha(obs.model_dump_json(exclude={"hash"}))
    return obs


def venue_attrs(hit: dict[str, Any]) -> VenueAttrs:
    """Extract the descriptive attributes of a venue from one search hit.

    Args:
        hit: One entry of `search.hits` from Resy's venue search response.

    Returns:
        The attributes, with `hash` set so SCD2 can detect a change.

    """
    loc: dict[str, Any] = hit.get("location") or {}
    geo: dict[str, Any] = hit.get("_geoloc") or {}
    contact: dict[str, Any] = hit.get("contact") or {}
    slug: str | None = hit.get("url_slug")
    city = loc.get("url_slug", "san-francisco-ca")
    cuisine: list[str] = hit.get("cuisine") or []
    attrs = VenueAttrs(
        name=hit.get("name"),
        url_slug=slug,
        url=f"https://resy.com/cities/{city}/venues/{slug}" if slug else None,
        neighborhood=hit.get("neighborhood"),
        cuisine="; ".join(cuisine) or None,
        price=PRICE.get(hit.get("price_range_id") or 0),
        locality=hit.get("locality"),
        location_code=loc.get("code"),
        lat=geo.get("lat"),
        lng=geo.get("lng"),
        phone=contact.get("phone_number"),
        max_party_size=hit.get("max_party_size"),
    )
    attrs.hash = _sha(attrs.model_dump_json(exclude={"hash"}))
    return attrs


def venue_id(hit: dict[str, Any]) -> int | None:
    """Return Resy's numeric venue id from a search hit, or None if absent.

    Args:
        hit: One entry of `search.hits`.

    Returns:
        The id, or None.

    """
    ident: dict[str, Any] = hit.get("id") or {}
    raw = ident.get("resy")
    return int(raw) if raw else None


# ---- rating tier ------------------------------------------------------------------


def rating_tier(avg: float | None, count: int | None) -> str | None:
    """Grade Resy's 5-point average as a letter.

    Resy averages cluster between 4.4 and 4.9, so the cuts are tight at the top: S is
    roughly the top tenth of SF venues and A the next two fifths.

    Args:
        avg: Resy's average rating.
        count: Number of Resy reviews.

    Returns:
        One of S, A, B, C, D, F, or None under 20 reviews or with no rating.

    """
    if avg is None or (count or 0) < MIN_REVIEWS_FOR_TIER:
        return None
    for letter, floor in RATING_TIERS:
        if avg >= floor:
            return letter
    return "F"


# ---- scoring (read time) ----------------------------------------------------------


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def score_night(
    obs: Observation, day: date, svc_ids: set[int] | None, grid: int
) -> NightEval:
    """Score one night of one venue.

    Boxes are grid points from first to last seating (inclusive) of the selected
    services. A box is open when any slot, snapped down to the grid, lands in it.

    Args:
        obs: The stored observation.
        day: The service day the observation is for.
        svc_ids: Resy service type ids to count, or None for every service.
        grid: Minutes per box, 15 or 30.

    Returns:
        The night's box counts and open times.

    """
    boxes: set[int] = set()
    spans: list[str] = []
    for w in obs.windows:
        if svc_ids is not None and w.svc not in svc_ids:
            continue
        t, end = _minutes(w.start), _minutes(w.end)
        spans.append(f"{SERVICE_NAME.get(w.svc or 0, 'service')} {w.start}-{w.end}")
        while t <= end:
            boxes.add(t)
            t += grid
    opened: set[int] = set()
    for s in obs.slots:
        key = (_minutes(s.t) // grid) * grid
        if key in boxes:
            opened.add(key)
    return NightEval(
        day=day,
        boxes=len(boxes),
        open_boxes=len(opened),
        taken=len(boxes) - len(opened),
        open_times=[_hhmm(m) for m in sorted(opened)],
        window=", ".join(spans) or None,
        any_window=bool(obs.windows),
        events=obs.events,
        is_tock=obs.is_tock,
        source_name=obs.source_name,
        reopen_date=obs.reopen_date,
    )


def _excl(reason: ExclusionReason, evidence: str) -> Verdict:
    return Verdict(excluded=True, reason=reason, evidence=evidence)


def _flag_exclusion(latest: NightEval, today: str) -> Verdict | None:
    reopen = latest.reopen_date
    if reopen and reopen > today:
        return _excl(
            ExclusionReason.closed, f"Resy lists a reopening date of {reopen}."
        )
    if latest.is_tock or latest.source_name:
        who = latest.source_name or "Tock"
        return _excl(
            ExclusionReason.other_platform,
            f"Inventory is flagged as coming from {who}, not Resy.",
        )
    return None


def _no_window_exclusion(nights: list[NightEval], label: str) -> Verdict:
    events = sum(n.events for n in nights)
    if events:
        return _excl(
            ExclusionReason.events_only,
            f"{events} ticketed event(s) in the week and no regular {label} "
            "seating window for this party size.",
        )
    if any(n.any_window for n in nights):
        return _excl(
            ExclusionReason.no_service,
            f"Seats this party size at other services only; no {label} window "
            "in the week.",
        )
    return _excl(
        ExclusionReason.no_service,
        "No seating window for this party size on any day of the week.",
    )


def classify(
    nights: list[NightEval],
    far: NightEval | None,
    today: str,
    min_nights: int,
    label: str,
) -> Verdict:
    """Decide scored versus excluded and compute the score.

    Args:
        nights: The week's evaluations in day order.
        far: Evaluation of the far-out verification day, if observed.
        today: Today's date (ISO) for the reopening check.
        min_nights: Nights with a window needed to be scored.
        label: Service name used in evidence strings.

    Returns:
        The verdict with score and inputs, or the exclusion reason and evidence.

    """
    if not nights:
        return _no_window_exclusion(nights, label)
    flagged = _flag_exclusion(nights[-1], today)
    if flagged:
        return flagged
    with_window = [n for n in nights if n.boxes > 0]
    if not with_window:
        return _no_window_exclusion(nights, label)
    if len(with_window) < min_nights:
        return _excl(
            ExclusionReason.insufficient_data,
            f"Only {len(with_window)} night(s) with a {label} window; "
            f"need {min_nights}.",
        )
    all_full = all(n.open_boxes == 0 for n in with_window)
    far_open = far.open_boxes if far else None
    if all_full and not far_open:
        return _excl(
            ExclusionReason.no_inventory,
            f"Zero open {label} tables on all {len(with_window)} nights and still "
            f"zero {'' if far else '(no data) '}three weeks out: Resy holds no real "
            "inventory for this party size here.",
        )
    evidence = (
        f"Sold out every night this week; {far_open} box(es) open three weeks out "
        "proves inventory exists."
        if all_full
        else None
    )
    ratios = [n.taken / n.boxes for n in with_window]
    return Verdict(
        excluded=False,
        evidence=evidence,
        score=round(sum(ratios) / len(ratios), 4),
        days_scored=len(with_window),
        taken_total=sum(n.taken for n in with_window),
        boxes_total=sum(n.boxes for n in with_window),
    )
