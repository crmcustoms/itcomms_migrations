"""Megaplan API v3 client — read-only, used as migration source."""

import logging
import time
from typing import Iterator

import requests

logger = logging.getLogger(__name__)


class MegaplanError(Exception):
    pass


class MegaplanClient:
    """Thin wrapper around Megaplan REST API v3.

    Handles:
    - Bearer auth
    - Automatic pagination (yields individual items)
    - Rate-limit delay between requests
    - 429 retry with backoff
    """

    _API_PREFIX = "/api/v3"

    def __init__(self, host: str, token: str, delay: float = 0.5):
        self.host = host.rstrip("/")
        self.delay = delay
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Low-level

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.host}{self._API_PREFIX}{path}"
        for attempt in range(4):
            time.sleep(self.delay)
            resp = self._session.get(url, params=params)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning("Rate limit hit, retrying in %ss…", wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise MegaplanError(
                    f"GET {url} → {resp.status_code}: {resp.text[:200]}"
                )
            return resp.json()
        raise MegaplanError(f"GET {url} failed after retries")

    def _paginate(self, path: str, extra_params: dict | None = None) -> Iterator[dict]:
        """Yield all items from a paginated list endpoint."""
        limit = 100
        offset = 0
        params = {**(extra_params or {}), "limit": limit, "offset": offset}
        while True:
            params["offset"] = offset
            data = self._get(path, params)
            items: list = data.get("data", [])
            yield from items
            logger.debug("Fetched %d items from %s (offset=%d)", len(items), path, offset)
            if len(items) < limit:
                break
            offset += limit

    # ------------------------------------------------------------------
    # Public methods

    def get_contractors(self, only_active: bool = True) -> Iterator[dict]:
        """Yield all contractors (companies + IPs) with basic fields."""
        extra: dict = {}
        if only_active:
            extra["isActive"] = "true"
        yield from self._paginate("/contractor", extra)

    def get_contractor_detail(self, contractor_id: str) -> dict:
        """Full contractor card including payer link and contacts."""
        return self._get(f"/contractor/{contractor_id}")["data"]

    def get_contacts(self) -> Iterator[dict]:
        """Yield all contacts (physical persons)."""
        yield from self._paginate("/contact")

    def get_payer(self, payer_id: str) -> dict:
        """Legal/banking details attached to a contractor."""
        return self._get(f"/payer/{payer_id}")["data"]

    def total_contractors(self, only_active: bool = True) -> int:
        """Return totalCount without fetching all records."""
        extra: dict = {"limit": 1, "offset": 0}
        if only_active:
            extra["isActive"] = "true"
        data = self._get("/contractor", extra)
        return data.get("meta", {}).get("totalCount", 0)

    def total_contacts(self) -> int:
        data = self._get("/contact", {"limit": 1, "offset": 0})
        return data.get("meta", {}).get("totalCount", 0)
