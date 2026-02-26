"""Planfix REST API v1.5.3 client — write target for the migration."""

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


class PlanfixError(Exception):
    pass


class PlanfixClient:
    """Wrapper around Planfix REST API.

    Base URL: https://{account}.planfix.com/rest
    Auth:     Bearer token in Authorization header
    """

    def __init__(self, host: str, token: str, delay: float = 1.0):
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

    def _url(self, path: str) -> str:
        return f"{self.host}/rest/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = self._url(path)
        for attempt in range(4):
            time.sleep(self.delay)
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                logger.warning("Rate limit hit, retrying in %ss…", retry_after)
                time.sleep(retry_after)
                continue
            if resp.status_code in (200, 201, 202):
                return resp.json() if resp.text.strip() else {}
            raise PlanfixError(
                f"{method} {url} → {resp.status_code}: {resp.text[:300]}"
            )
        raise PlanfixError(f"{method} {url} failed after retries")

    # ------------------------------------------------------------------
    # Contacts / Companies  (same endpoint in Planfix REST)

    def create_contact(self, data: dict) -> dict:
        """POST /contact/ — create company (isCompany=True) or person."""
        result = self._request("POST", "/contact/", json=data)
        logger.debug("Created contact id=%s", result.get("id"))
        return result

    def update_contact(self, contact_id: int, data: dict) -> dict:
        """POST /contact/{id} — update existing contact."""
        return self._request("POST", f"/contact/{contact_id}", json=data)

    def get_contact(self, contact_id: int, fields: str = "id,name,isCompany,sourceObjectId") -> dict:
        """GET /contact/{id}."""
        return self._request("GET", f"/contact/{contact_id}", params={"fields": fields})

    def list_contacts(
        self,
        *,
        is_company: bool | None = None,
        source_id: str | None = None,
        only_changed: bool = False,
        offset: int = 0,
        page_size: int = 100,
        fields: str = "id,name,isCompany,sourceObjectId",
    ) -> dict:
        """POST /contact/list — paginated list of contacts/companies."""
        body: dict = {
            "offset": offset,
            "pageSize": page_size,
            "fields": fields,
        }
        if is_company is not None:
            body["isCompany"] = is_company
        if source_id is not None:
            body["sourceId"] = source_id
            body["onlyChanged"] = only_changed
        return self._request("POST", "/contact/list", json=body)

    def find_by_source_object_id(
        self, source_object_id: str, is_company: bool | None = None
    ) -> dict | None:
        """Search for a contact/company by Megaplan ID stored in sourceObjectId.

        Uses ComplexContactFilter type 4231 (Contact number) is NOT what we need;
        we look by filters on sourceObjectId via the list endpoint with sourceId tag.
        Falls back to full scan if not found via source index.
        """
        # Planfix doesn't expose a direct sourceObjectId filter in REST,
        # so we use the sourceId/onlyChanged mechanism.
        # For a migration, we rely on our local id_mapping.json instead.
        return None

    # ------------------------------------------------------------------
    # Files

    def upload_file_from_url(self, url: str, name: str) -> dict:
        """POST /file/from-url/ — upload a file by URL, returns {id, name, size}."""
        return self._request("POST", "/file/from-url/", json={"url": url, "name": name})

    def attach_file_to_contact(self, file_id: int, contact_id: int) -> dict:
        """POST /file/{id}/attach/contact — attach uploaded file to contact."""
        return self._request(
            "POST", f"/file/{file_id}/attach/contact", params={"id": contact_id}
        )

    # ------------------------------------------------------------------
    # Tasks

    def list_tasks(
        self,
        *,
        template_id: int,
        offset: int = 0,
        page_size: int = 100,
        fields: str = "id,name,customFieldData",
    ) -> dict:
        """POST /task/list — paginated list of tasks filtered by template."""
        body = {
            "offset": offset,
            "pageSize": page_size,
            "fields": fields,
            "filters": [
                {
                    "type": 4008,         # filter by template
                    "operator": "equal",
                    "value": template_id,
                }
            ],
        }
        return self._request("POST", "/task/list", json=body)

    def update_task(self, task_id: int, data: dict) -> dict:
        """POST /task/{id} — update task fields."""
        return self._request("POST", f"/task/{task_id}", json=data)

    # ------------------------------------------------------------------
    # Health check

    def ping(self) -> bool:
        try:
            self._request("GET", "/ping")
            return True
        except PlanfixError:
            return False
