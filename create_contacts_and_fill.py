#!/usr/bin/env python3
"""
create_contacts_and_fill.py

Для 610 задач у которых контрагент не найден в Планфикс:
  1. Берёт данные из fill_megaplan_result.csv (статус not_in_planfix)
  2. Для каждого уникального megaplan_id создаёт контакт в Планфикс
     (name из Мегаплана, isCompany по contentType, field 128997 = megaplan_id)
  3. Заполняет поле Поставщик во всех связанных задачах

Запуск:
    python create_contacts_and_fill.py           # dry-run
    python create_contacts_and_fill.py --live    # реальная запись
"""

import argparse
import csv
import logging
import os
import sys
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================

PLANFIX_HOST  = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN = os.getenv("PLANFIX_TOKEN", "")

INPUT_CSV  = "fill_megaplan_result.csv"
RESULT_CSV = "create_contacts_result.csv"

CONTACT_MEGAPLAN_ID_FIELD = 128997
SUPPLIER_FIELDS = {136609, 136611}

PLANFIX_DELAY  = 0.5

_INVALID_NAMES = frozenset({"", "no name", "без имени", "undefined", "null"})
_COMPANY_CONTENT_TYPES = frozenset({"ContractorCompany"})

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("create_contacts.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# HTTP SESSIONS
# =============================================================================

_pf = requests.Session()
_pf.headers.update({
    "Authorization": f"Bearer {PLANFIX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
})

# =============================================================================
# PLANFIX
# =============================================================================

def pf_req(method: str, path: str, **kwargs) -> dict:
    for attempt in range(4):
        time.sleep(PLANFIX_DELAY if attempt == 0 else 5 * attempt)
        try:
            r = _pf.request(method, f"{PLANFIX_HOST}/rest{path}", **kwargs)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            log.warning("Planfix connection error (attempt %d/4): %s", attempt + 1, e)
            continue
        if not r.ok:
            raise RuntimeError(f"Planfix {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError(f"Planfix {method} {path} failed after 4 retries")


def pf_find_contact_by_megaplan_id(mp_id: str) -> dict | None:
    """Double-check if contact was already created (race condition guard)."""
    result = pf_req("POST", "/contact/list", json={
        "offset": 0, "pageSize": 3, "fields": "id,name,isCompany",
        "filters": [{"type": 4101, "field": str(CONTACT_MEGAPLAN_ID_FIELD),
                     "operator": "equal", "value": mp_id}],
    })
    contacts = result.get("contacts") or result.get("data") or []
    return contacts[0] if contacts else None


def pf_create_contact(name: str, is_company: bool, mp_id: str, dry_run: bool) -> dict | None:
    """Create contact in Planfix. Returns created contact dict or None."""
    if dry_run:
        return {"id": f"DRY:{mp_id}", "name": name}

    # Flat body (no "contact" wrapper) + template: 1=person, 2=company
    body = {
        "template": {"id": 2 if is_company else 1},
        "name": name,
        "isCompany": is_company,
        "customFieldData": [
            {"field": {"id": CONTACT_MEGAPLAN_ID_FIELD}, "value": mp_id}
        ],
    }
    try:
        result = pf_req("POST", "/contact", json=body)
        # Response may return the contact under different keys
        # Planfix returns {"result":"success","id": N}
        contact = result.get("contact") or result.get("data")
        if not contact:
            cid = result.get("id")
            if cid:
                contact = {"id": cid, "name": name, "isCompany": is_company}
        return contact
    except RuntimeError as e:
        log.error("Create contact %r: %s", name, e)
        return None


def detect_supplier_field(task_id: str) -> int | None:
    """Fetch task with both supplier fields, return which one is present."""
    try:
        result = pf_req("GET", f"/task/{task_id}",
                        params={"fields": "id,136609,136611"})
    except RuntimeError as e:
        log.error("detect_supplier_field task %s: %s", task_id, e)
        return None
    task = result.get("task") or result.get("data") or {}
    for entry in task.get("customFieldData") or []:
        fid = (entry.get("field") or {}).get("id")
        if fid in SUPPLIER_FIELDS:
            return fid
    return None


def pf_get_supplier(task_id: str, sf: int):
    """Return current supplier value or None."""
    try:
        result = pf_req("GET", f"/task/{task_id}", params={"fields": f"id,{sf}"})
    except RuntimeError:
        return None
    task = result.get("task") or result.get("data") or {}
    for entry in task.get("customFieldData") or []:
        if (entry.get("field") or {}).get("id") == sf:
            val = entry.get("value")
            name = (entry.get("stringValue") or "").strip().lower()
            if val is not None and name not in _INVALID_NAMES:
                return val
    return None


def pf_set_supplier(task_id: str, sf: int, contact_id, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        pf_req("POST", f"/task/{task_id}", json={
            "customFieldData": [
                {"field": {"id": sf}, "value": contact_id}
            ]
        })
        return True
    except RuntimeError as e:
        log.error("set_supplier task %s: %s", task_id, e)
        return False

# =============================================================================
# MAIN
# =============================================================================

def load_not_in_planfix() -> list[dict]:
    rows = []
    with open(INPUT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["status"] == "not_in_planfix":
                rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    dry_run = not args.live
    mode = "LIVE" if not dry_run else "DRY RUN"
    log.info("=== Start [%s] ===", mode)

    rows = load_not_in_planfix()
    log.info("Tasks to process: %d", len(rows))

    # Group by megaplan_id
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["megaplan_id"]].append(r)
    log.info("Unique megaplan_ids to create: %d", len(groups))

    stats = {"total": 0, "contact_created": 0, "contact_existed": 0,
             "contact_failed": 0, "filled": 0, "already_set": 0, "errors": 0}
    result_rows = []

    for mp_id, task_rows in sorted(groups.items(), key=lambda x: int(x[0])):
        log.info("--- mp_id=%s (%d tasks) ---", mp_id, len(task_rows))

        # Use cached data from CSV (already fetched from Megaplan in dry-run)
        sample = task_rows[0]
        name = sample.get("mp_name", "").strip()
        is_company = sample.get("is_company", "False").strip().lower() == "true"
        log.info("  CSV: is_company=%s name=%r", is_company, name)

        if not name:
            log.warning("  Empty name, skipping")
            for row in task_rows:
                stats["total"] += 1
                stats["errors"] += 1
                result_rows.append({**row, "action": "empty_name", "new_contact_id": ""})
            continue

        # Check if contact already exists (was created in a previous run)
        existing = pf_find_contact_by_megaplan_id(mp_id)
        if existing:
            contact_id = existing["id"]
            log.info("  Planfix: contact already exists id=%s", contact_id)
            stats["contact_existed"] += 1
        else:
            # Create contact
            created = pf_create_contact(name, is_company, mp_id, dry_run)
            if created is None:
                log.error("  Failed to create contact for %r", name)
                for row in task_rows:
                    stats["total"] += 1
                    stats["contact_failed"] += 1
                    result_rows.append({**row, "action": "create_failed", "new_contact_id": ""})
                continue
            contact_id = created["id"]
            log.info("  [%s] Created contact: id=%s name=%r isCompany=%s",
                     mode, contact_id, name, is_company)
            stats["contact_created"] += 1

        # Fill tasks
        for row in task_rows:
            stats["total"] += 1
            task_id = row["task_id"]

            sf = detect_supplier_field(task_id)
            if sf is None:
                log.error("  Task #%s: supplier field not detected", task_id)
                stats["errors"] += 1
                result_rows.append({**row, "action": "no_field", "new_contact_id": contact_id})
                continue

            existing_val = pf_get_supplier(task_id, sf)
            if existing_val is not None:
                log.info("  Task #%s: already set, skip", task_id)
                stats["already_set"] += 1
                result_rows.append({**row, "action": "already_set", "new_contact_id": contact_id})
                continue

            ok = pf_set_supplier(task_id, sf, contact_id, dry_run)
            if ok:
                prefix = "[DRY RUN]" if dry_run else "OK"
                log.info("  %s Task #%s -> Поставщик := contact:%s (%s)",
                         prefix, task_id, contact_id, name)
                stats["filled"] += 1
                result_rows.append({**row, "action": "dry_run" if dry_run else "filled",
                                    "new_contact_id": contact_id})
            else:
                stats["errors"] += 1
                result_rows.append({**row, "action": "fill_error", "new_contact_id": contact_id})

    # Save result
    fieldnames = list(rows[0].keys()) + ["action", "new_contact_id"]
    with open(RESULT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(result_rows)

    log.info("=== DONE ===")
    log.info("total=%d  contact_created=%d  contact_existed=%d  contact_failed=%d  "
             "filled=%d  already_set=%d  errors=%d",
             stats["total"], stats["contact_created"], stats["contact_existed"],
             stats["contact_failed"], stats["filled"], stats["already_set"], stats["errors"])
    log.info("Results -> %s", RESULT_CSV)


if __name__ == "__main__":
    main()
