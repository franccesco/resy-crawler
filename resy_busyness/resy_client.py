"""Thin client for the undocumented api.resy.com venue search."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .config import settings

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class ResyClient:
    def __init__(self, api_key: str | None = None, base: str | None = None):
        key = api_key or settings.resy_api_key
        self.http = httpx.Client(
            base_url=base or settings.resy_api_base,
            timeout=30,
            headers={
                "Authorization": f'ResyAPI api_key="{key}"',
                "X-Origin": "https://resy.com",
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": UA,
            },
        )

    def close(self) -> None:
        self.http.close()

    def search_availability(self, day: str, party_size: int, page: int) -> dict[str, Any]:
        """One page of SF venues with their slot list for `day` and `party_size`."""
        body = {
            "geo": {"latitude": settings.center_lat, "longitude": settings.center_lng},
            "venue_filter": {"location_code": settings.location_code},
            "availability": True,
            "slot_filter": {"day": day, "party_size": party_size},
            "order_by": "availability",
            "per_page": settings.per_page,
            "page": page,
            "types": ["venue"],
            "query": "",
        }
        for attempt in range(4):
            r = self.http.post("/3/venuesearch/search", json=body)
            if r.status_code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")
