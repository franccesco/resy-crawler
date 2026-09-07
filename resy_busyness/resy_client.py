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
RETRY_STATUSES = {429, 500, 502, 503}


class ResyClient:
    """Synchronous HTTP client carrying the headers resy.com's web client sends."""

    def __init__(self, api_key: str | None = None, base: str | None = None) -> None:
        """Create the client.

        Args:
            api_key: Resy public API key; defaults to `RESY_API_KEY`.
            base: API base URL; defaults to `RESY_API_BASE`.

        Raises:
            RuntimeError: If no API key is configured.

        """
        key = api_key or settings.resy_api_key
        if not key:
            raise RuntimeError(
                "RESY_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
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
        """Close the underlying HTTP connection pool."""
        self.http.close()

    def search_availability(
        self, day: str, party_size: int, page: int
    ) -> dict[str, Any]:
        """Fetch one page of SF venues with their slot list for a day and party size.

        Args:
            day: Service day, ISO format.
            party_size: Number of guests.
            page: 1-based page; the server returns at most 75 venues per page.

        Retries 429 and 5xx up to three times with a growing pause; any other error
        status propagates from httpx as `HTTPStatusError`.

        Returns:
            The decoded JSON response.

        Raises:
            RuntimeError: If every retry was exhausted without a decision.

        """
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
        attempts = 4
        for attempt in range(attempts):
            response = self.http.post("/3/venuesearch/search", json=body)
            if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
                time.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
        raise RuntimeError("retry loop exhausted")
