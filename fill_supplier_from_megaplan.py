#!/usr/bin/env python3
"""
fill_supplier_from_megaplan.py — заполнение поля Поставщик для задач, у которых
контрагент не найден в Планфикс по Megaplan ID.

Алгоритм:
  1. Читает not_found_tasks.csv (696 задач, 48 уникальных megaplan_id)
  2. Для каждого megaplan_id ищет в Мегаплане:
     - сначала /contractor/{id}  (компании, ИП, ContractorOrganization / ContractorHuman)
     - если не найден — /contact/{id}  (физлица, привязанные к контрагенту)
  3. Определяет тип сущности (компания vs физлицо)
  4. Ищет в Планфикс по megaplan_id (поле 128997), затем по названию с учётом isCompany
  5. Если найден — заполняет поле Поставщик в задаче Планфикс

Запуск (dry-run по умолчанию):
    python fill_supplier_from_megaplan.py

Реальная запись:
    python fill_supplier_from_megaplan.py --live
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

MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST",  "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN", "")

NOT_FOUND_CSV = "not_found_tasks.csv"
RESULT_CSV    = "fill_megaplan_result.csv"

# Planfix field IDs
CONTACT_MEGAPLAN_ID_FIELD = 128997   # "ID Megaplan" in Planfix contacts
SUPPLIER_FIELDS = {
    136609: "Поставщик (конф.)",   # template 15
    136611: "Поставщик (безн.)",   # template 7691
}

MEGAPLAN_DELAY = 1.2   # seconds between Megaplan requests
PLANFIX_DELAY  = 0.5   # seconds between Planfix requests

_INVALID_SUPPLIER_NAMES = frozenset({"", "no name", "без имени", "undefined", "null"})

# Megaplan contentType -> is_company
# ContractorCompany = юрлицо/компания -> isCompany=True in Planfix
# ContractorHuman   = физлицо/ИП      -> isCompany=False in Planfix
_COMPANY_CONTENT_TYPES = frozenset({
    "ContractorCompany",
})

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fill_megaplan.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# HTTP SESSIONS
# =============================================================================

_pf_session = requests.Session()
_pf_session.headers.update({
    "Authorization": f"Bearer {PLANFIX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
})

_mp_session = requests.Session()
_mp_session.headers.update({
    "Authorization": f"Bearer {MEGAPLAN_TOKEN}",
    "Content-Type": "application/json",
})

# =============================================================================
# MEGAPLAN API
# =============================================================================

def megaplan_get_contractor(megaplan_id: str) -> dict | None:
    """Fetch contractor from Megaplan /contractor/{id}. Returns data dict or None."""
    time.sleep(MEGAPLAN_DELAY)
    url = f"{MEGAPLAN_HOST}/api/v3/contractor/{megaplan_id}"
    try:
        r = _mp_session.get(url, timeout=20)
    except requests.RequestException as e:
        log.error("Megaplan network error contractor/%s: %s", megaplan_id, e)
        return None

    if r.status_code == 404:
        return None
    if not r.ok:
        try:
            errors = r.json().get("meta", {}).get("errors", [])
            msg = errors[0].get("message", r.text[:100]) if errors else r.text[:100]
        except Exception:
            msg = r.text[:100]
        log.warning("Megaplan contractor/%s -> %s: %s", megaplan_id, r.status_code, msg)
        return None

    data = r.json().get("data", {})
    return data if data else None


def entity_is_company(entity: dict) -> bool:
    """True if ContractorCompany, False if ContractorHuman."""
    return entity.get("contentType", "") in _COMPANY_CONTENT_TYPES


def entity_name(entity: dict) -> str:
    """Extract the best display name from a Megaplan entity."""
    # For companies: name field
    # For humans: try fullName, then lastName+firstName, then name
    for field in ("name", "fullName", "shortName"):
        val = entity.get(field)
        if val and str(val).strip():
            return str(val).strip()

    # Assemble from parts
    parts = []
    for part in ("lastName", "firstName", "middleName"):
        val = entity.get(part)
        if val and str(val).strip():
            parts.append(str(val).strip())
    if parts:
        return " ".join(parts)

    return ""

# =============================================================================
# PLANFIX API
# =============================================================================

def _pf_request(method: str, path: str, **kwargs) -> dict:
    time.sleep(PLANFIX_DELAY)
    url = f"{PLANFIX_HOST}/rest{path}"
    r = _pf_session.request(method, url, **kwargs)
    if not r.ok:
        raise RuntimeError(f"Planfix {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def find_pf_contact_by_megaplan_id(megaplan_id: str) -> dict | None:
    """Search Planfix contacts by megaplan_id stored in field 128997."""
    result = _pf_request("POST", "/contact/list", json={
        "offset": 0,
        "pageSize": 5,
        "fields": "id,name,isCompany",
        "filters": [{
            "type": 4101,
            "field": str(CONTACT_MEGAPLAN_ID_FIELD),
            "operator": "equal",
            "value": megaplan_id,
        }],
    })
    contacts = result.get("contacts") or result.get("data") or []
    return contacts[0] if contacts else None


def find_pf_contact_by_name(name: str, is_company: bool) -> dict | None:
    """
    Search Planfix contacts by name (type 4001 = name/company name contains).
    First tries with isCompany filter (type 4006), then without — because
    the company/person classification may differ between Megaplan and Planfix.
    """
    if not name:
        return None

    # Try with isCompany filter first
    body = {
        "offset": 0,
        "pageSize": 10,
        "fields": "id,name,isCompany",
        "filters": [
            {
                "type": 4006,       # isCompany filter
                "operator": "equal",
                "value": 1 if is_company else 0,
            },
            {
                "type": 4001,       # name / company name contains
                "operator": "equal",
                "value": name,
            },
        ],
    }
    result = _pf_request("POST", "/contact/list", json=body)
    contacts = result.get("contacts") or result.get("data") or []
    if contacts:
        return contacts[0]

    # Retry without isCompany filter — classification may differ across systems
    body2 = {
        "offset": 0,
        "pageSize": 5,
        "fields": "id,name,isCompany",
        "filters": [{"type": 4001, "operator": "equal", "value": name}],
    }
    result2 = _pf_request("POST", "/contact/list", json=body2)
    contacts2 = result2.get("contacts") or result2.get("data") or []
    if contacts2:
        log.info("  [PLANFIX] found by name without isCompany filter (Megaplan is_company=%s, Planfix isCompany=%s)",
                 is_company, contacts2[0].get("isCompany"))
    return contacts2[0] if contacts2 else None


def detect_supplier_field(task_id: str) -> int | None:
    """
    Fetch task with both supplier fields at once, return whichever is present.
    One API call instead of two.
    """
    sf_ids = list(SUPPLIER_FIELDS.keys())  # [136609, 136611]
    fields_param = ",".join(["id"] + [str(f) for f in sf_ids])
    try:
        result = _pf_request("GET", f"/task/{task_id}", params={"fields": fields_param})
    except RuntimeError as e:
        log.error("Task #%s fetch error: %s", task_id, e)
        return None

    task = result.get("task") or result.get("data") or {}
    for entry in task.get("customFieldData") or []:
        fid = (entry.get("field") or {}).get("id")
        if fid in SUPPLIER_FIELDS:
            return fid
    return None


def get_contact_field_value(task: dict, field_id: int):
    """Return contact field value, or None if empty/invalid."""
    for entry in task.get("customFieldData") or []:
        if (entry.get("field") or {}).get("id") == field_id:
            val = entry.get("value")
            name = (entry.get("stringValue") or "").strip().lower()
            if val is not None and name not in _INVALID_SUPPLIER_NAMES:
                return val
    return None


def fetch_task_supplier(task_id: str, supplier_field_id: int) -> dict:
    """Fetch task and return the raw task dict (for supplier field check)."""
    result = _pf_request("GET", f"/task/{task_id}", params={"fields": f"id,{supplier_field_id}"})
    return result.get("task") or result.get("data") or {}


def set_supplier(task_id: str, supplier_field_id: int, contact_id: int, dry_run: bool) -> bool:
    """Write supplier contact to task. Returns True on success."""
    if dry_run:
        return True
    try:
        _pf_request("POST", f"/task/{task_id}", json={
            "customFieldData": [
                {"field": {"id": supplier_field_id}, "value": contact_id}
            ]
        })
        return True
    except RuntimeError as e:
        log.error("set_supplier task %s: %s", task_id, e)
        return False

# =============================================================================
# MAIN
# =============================================================================

def load_not_found(csv_path: str) -> list[dict]:
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Write to Planfix (default: dry-run)")
    args = parser.parse_args()
    dry_run = not args.live

    mode = "LIVE WRITE" if not dry_run else "DRY RUN"
    log.info("Start [%s]", mode)

    rows = load_not_found(NOT_FOUND_CSV)
    log.info("Loaded %d not_found tasks from %s", len(rows), NOT_FOUND_CSV)

    # Group tasks by megaplan_id to minimise Megaplan API calls
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["megaplan_id"]].append(row)
    log.info("Unique megaplan_ids to resolve: %d", len(groups))

    # Cache: megaplan_id -> (pf_contact | None)
    contact_cache: dict[str, dict | None] = {}

    stats = {
        "total": 0, "filled": 0,
        "not_in_megaplan": 0, "not_in_planfix": 0,
        "already_set": 0, "no_field": 0, "errors": 0,
    }
    result_rows = []

    for mp_id, task_rows in sorted(groups.items(), key=lambda x: int(x[0])):
        log.info("--- megaplan_id=%s (%d tasks) ---", mp_id, len(task_rows))

        # --- Step 1: resolve in Megaplan ---
        entity = megaplan_get_contractor(mp_id)

        if entity is None:
            log.warning("  [MEGAPLAN] id=%s not found", mp_id)
            for row in task_rows:
                stats["total"] += 1
                stats["not_in_megaplan"] += 1
                result_rows.append({**row, "status": "not_in_megaplan", "is_company": "", "mp_name": "", "pf_contact_id": "", "pf_contact_name": ""})
            continue

        is_company = entity_is_company(entity)
        mp_name = entity_name(entity)
        content_type = entity.get("contentType", "?")
        log.info("  [MEGAPLAN] contentType=%s is_company=%s name=%r", content_type, is_company, mp_name)

        # --- Step 2: find in Planfix ---
        if mp_id not in contact_cache:
            pf_contact = find_pf_contact_by_megaplan_id(mp_id)

            if pf_contact:
                log.info("  [PLANFIX] found by megaplan_id: id=%s name=%r isCompany=%s",
                         pf_contact.get("id"), pf_contact.get("name"), pf_contact.get("isCompany"))
            else:
                log.info("  [PLANFIX] not found by megaplan_id, searching by name (is_company=%s)...", is_company)
                pf_contact = find_pf_contact_by_name(mp_name, is_company)
                if pf_contact:
                    log.info("  [PLANFIX] found by name: id=%s name=%r isCompany=%s",
                             pf_contact.get("id"), pf_contact.get("name"), pf_contact.get("isCompany"))
                else:
                    log.warning("  [PLANFIX] not found: mp_id=%s name=%r is_company=%s", mp_id, mp_name, is_company)

            contact_cache[mp_id] = pf_contact

        pf_contact = contact_cache[mp_id]

        if pf_contact is None:
            for row in task_rows:
                stats["total"] += 1
                stats["not_in_planfix"] += 1
                result_rows.append({**row, "status": "not_in_planfix", "is_company": is_company, "mp_name": mp_name, "pf_contact_id": "", "pf_contact_name": ""})
            continue

        pf_id = pf_contact["id"]
        pf_name = pf_contact.get("name", "")
        pf_is_company = pf_contact.get("isCompany", "?")

        # --- Step 3: fill each task ---
        for row in task_rows:
            stats["total"] += 1
            task_id = row["task_id"]
            task_name = row["task_name"]

            # Detect which supplier field this task uses (one API call, both fields)
            sf = detect_supplier_field(task_id)
            if sf is None:
                log.error("  Task #%s: supplier field not detected (not in template?)", task_id)
                stats["no_field"] += 1
                result_rows.append({**row, "status": "no_supplier_field", "is_company": is_company, "mp_name": mp_name, "pf_contact_id": pf_id, "pf_contact_name": pf_name})
                continue

            # Check current value (reuse the same fetch — detect_supplier_field already gave us the field)
            # Re-fetch to get current value of the detected field
            try:
                task_data = fetch_task_supplier(task_id, sf)
                existing = get_contact_field_value(task_data, sf)
            except RuntimeError as e:
                log.error("  Task #%s fetch supplier: %s", task_id, e)
                stats["errors"] += 1
                result_rows.append({**row, "status": "error_fetch", "is_company": is_company, "mp_name": mp_name, "pf_contact_id": pf_id, "pf_contact_name": pf_name})
                continue

            if existing is not None:
                log.info("  Task #%s: already set (contact:%s), skip", task_id, existing)
                stats["already_set"] += 1
                result_rows.append({**row, "status": "already_set", "is_company": is_company, "mp_name": mp_name, "pf_contact_id": pf_id, "pf_contact_name": pf_name})
                continue

            # Write
            ok = set_supplier(task_id, sf, pf_id, dry_run)
            if ok:
                prefix = "[DRY RUN]" if dry_run else "OK"
                log.info("  %s Task #%s %s -> %s := contact:%s (%s) [isCompany=%s]",
                         prefix, task_id, task_name, SUPPLIER_FIELDS[sf], pf_id, pf_name, pf_is_company)
                stats["filled"] += 1
                result_rows.append({**row, "status": "dry_run" if dry_run else "filled", "is_company": is_company, "mp_name": mp_name, "pf_contact_id": pf_id, "pf_contact_name": pf_name})
            else:
                stats["errors"] += 1
                result_rows.append({**row, "status": "error_write", "is_company": is_company, "mp_name": mp_name, "pf_contact_id": pf_id, "pf_contact_name": pf_name})

    # Save result CSV
    fieldnames = ["task_id", "task_name", "megaplan_id", "status", "is_company", "mp_name", "pf_contact_id", "pf_contact_name"]
    with open(RESULT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(result_rows)

    log.info("=== DONE ===")
    log.info(
        "total=%d  filled=%d  not_in_megaplan=%d  not_in_planfix=%d  "
        "already_set=%d  no_field=%d  errors=%d",
        stats["total"], stats["filled"], stats["not_in_megaplan"],
        stats["not_in_planfix"], stats["already_set"], stats["no_field"], stats["errors"],
    )
    log.info("Results -> %s", RESULT_CSV)


if __name__ == "__main__":
    main()
