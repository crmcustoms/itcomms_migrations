#!/usr/bin/env python3
"""
enrich_contacts.py — обогащение Планфикс-контактов данными из Мегаплана.

Для 42 контактов, созданных в Планфикс:
  1. Читает create_contacts_result.csv — пары (megaplan_id, planfix_id)
  2. Загружает полную карточку контрагента из Мегаплана
  3. Обновляет контакт в Планфикс: телефоны, email, адрес, комментарий, сайт
  4. Прикрепляет файлы (если есть)

Запуск:
    python enrich_contacts.py           # dry-run (только показывает что найдено)
    python enrich_contacts.py --live    # реальная запись
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.exceptions import ConnectionError, SSLError

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================

PLANFIX_HOST  = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN = os.getenv("PLANFIX_TOKEN", "")

MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST",  "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN", "")

INPUT_CSV  = "create_contacts_result.csv"
RESULT_CSV = "enrich_contacts_result.csv"

PLANFIX_DELAY  = float(os.getenv("PLANFIX_DELAY",  "1.0"))
MEGAPLAN_DELAY = float(os.getenv("MEGAPLAN_DELAY", "0.5"))

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("enrich_contacts.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# HTTP SESSIONS
# =============================================================================

_pf = requests.Session()
_pf.headers.update({"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"})

_mp = requests.Session()
_mp.headers.update({"Authorization": f"Bearer {MEGAPLAN_TOKEN}", "Content-Type": "application/json"})


def pf_req(method: str, path: str, **kwargs) -> dict:
    for attempt in range(4):
        if attempt:
            time.sleep(5 * attempt)
        else:
            time.sleep(PLANFIX_DELAY)
        try:
            r = _pf.request(method, f"{PLANFIX_HOST}/rest{path}", **kwargs)
        except (SSLError, ConnectionError) as e:
            log.warning("Planfix connection error, retry %d/4: %s", attempt + 1, e)
            continue
        if not r.ok:
            raise RuntimeError(f"Planfix {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}
    raise RuntimeError(f"Planfix {method} {path} failed after 4 retries")


def mp_req(path: str) -> dict:
    for attempt in range(4):
        if attempt:
            time.sleep(5 * attempt)
        else:
            time.sleep(MEGAPLAN_DELAY)
        try:
            r = _mp.get(f"{MEGAPLAN_HOST}/api/v3{path}")
        except (SSLError, ConnectionError) as e:
            log.warning("Megaplan connection error, retry %d/4: %s", attempt + 1, e)
            continue
        if not r.ok:
            raise RuntimeError(f"Megaplan GET {path} → {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"Megaplan GET {path} failed after 4 retries")


# =============================================================================
# MEGAPLAN → PLANFIX MAPPING
# =============================================================================

PHONE_TYPE_MAP = {
    "work":   "work",
    "mobile": "mobile",
    "home":   "home",
    "fax":    "fax",
    "other":  "other",
}

EMAIL_TYPE_MAP = {
    "work":   "work",
    "home":   "home",
    "other":  "other",
}


def extract_phones(mp_data: dict) -> list[dict]:
    """Извлекает телефоны из карточки Мегаплана.
    Поля: phones[].number/value, или extraFields с типом phone.
    """
    phones = []
    # Стандартное поле phones
    for ph in mp_data.get("phones", []) or []:
        number = ph.get("number") or ph.get("value") or ""
        if not number:
            continue
        raw_type = (ph.get("type") or "work").lower()
        pf_type = PHONE_TYPE_MAP.get(raw_type, "work")
        phones.append({"type": pf_type, "number": number})
    # extraFields — кастомные поля
    for field in mp_data.get("extraFields", []) or []:
        if field.get("type") == "phone":
            number = field.get("value") or ""
            if number:
                phones.append({"type": "work", "number": number})
    return phones


def extract_emails(mp_data: dict) -> list[dict]:
    """Извлекает email из карточки Мегаплана.
    Поля: emails[].value/address, loginEmail, extraFields с типом email.
    """
    emails = []
    seen = set()

    def add(address: str, etype: str = "work") -> None:
        if address and "@" in address and address not in seen:
            seen.add(address)
            emails.append({"type": etype, "address": address})

    # Стандартное поле emails
    for em in mp_data.get("emails", []) or []:
        address = em.get("value") or em.get("address") or em.get("email") or ""
        raw_type = (em.get("type") or "work").lower()
        add(address, EMAIL_TYPE_MAP.get(raw_type, "work"))

    # loginEmail — системный email
    add(mp_data.get("loginEmail") or "")

    # extraFields
    for field in mp_data.get("extraFields", []) or []:
        if field.get("type") == "email":
            add(field.get("value") or "")

    return emails


def extract_description(mp_data: dict) -> str:
    """Собирает адрес, комментарий и теги в текстовое описание."""
    parts = []

    # Адрес
    addr = mp_data.get("address") or {}
    if isinstance(addr, dict):
        addr_parts = [
            addr.get("country", ""),
            addr.get("region", ""),
            addr.get("city", ""),
            addr.get("street", ""),
            addr.get("house", ""),
        ]
        addr_str = ", ".join(p for p in addr_parts if p)
        if addr_str:
            parts.append(f"Адрес: {addr_str}")
    elif isinstance(addr, str) and addr:
        parts.append(f"Адрес: {addr}")

    # Комментарий
    comment = mp_data.get("comment") or mp_data.get("description") or ""
    if comment:
        parts.append(comment)

    # Теги
    tags = [t.get("name") for t in (mp_data.get("tags") or []) if t.get("name")]
    if tags:
        parts.append("Теги: " + ", ".join(tags))

    return "\n".join(parts)


def extract_site(mp_data: dict) -> str:
    return mp_data.get("site") or mp_data.get("website") or mp_data.get("url") or ""


def extract_files(mp_data: dict) -> list[dict]:
    """Возвращает список файлов {name, url}."""
    files = []
    for f in mp_data.get("files", []) or []:
        url  = f.get("url") or f.get("downloadUrl") or ""
        name = f.get("name") or f.get("fileName") or "file"
        if url:
            files.append({"name": name, "url": url})
    return files


# =============================================================================
# PLANFIX OPERATIONS
# =============================================================================

def pf_update_contact(planfix_id: str, phones: list, emails: list,
                      description: str, site: str, dry_run: bool) -> bool:
    body = {}
    if phones:
        body["phones"] = phones
    if emails:
        body["emails"] = emails
    if description:
        body["description"] = description
    if site:
        body["site"] = site

    if not body:
        log.info("  → no data to update, skip")
        return True

    if dry_run:
        log.info("  [DRY-RUN] would update contact %s: %s", planfix_id, json.dumps(body, ensure_ascii=False)[:200])
        return True

    pf_req("POST", f"/contact/{planfix_id}", json=body)
    log.info("  ✓ updated contact %s (phones=%d emails=%d desc=%d site=%s)",
             planfix_id, len(phones), len(emails), len(description), bool(site))
    return True


def pf_attach_file(planfix_id: str, file_url: str, file_name: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("  [DRY-RUN] would attach file '%s' from %s", file_name, file_url[:80])
        return True
    try:
        # Step 1: upload by URL
        result = pf_req("POST", "/file/from-url/", json={"url": file_url, "name": file_name})
        file_id = result.get("id")
        if not file_id:
            log.warning("  file upload returned no id for '%s'", file_name)
            return False
        # Step 2: attach to contact
        pf_req("POST", f"/file/{file_id}/attach/contact", params={"id": planfix_id})
        log.info("  ✓ attached file '%s' (id=%s)", file_name, file_id)
        return True
    except Exception as e:
        log.warning("  ✗ file '%s' failed: %s", file_name, e)
        return False


# =============================================================================
# MAIN
# =============================================================================

def load_contacts_from_csv(path: str) -> dict[str, str]:
    """Returns {megaplan_id: planfix_id} for the 42 created contacts."""
    mapping: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mid = row.get("megaplan_id", "").strip()
            cid = row.get("new_contact_id", "").strip()
            if mid and cid and mid not in mapping:
                mapping[mid] = cid
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich Planfix contacts from Megaplan")
    parser.add_argument("--live", action="store_true", help="Actually write to Planfix")
    args = parser.parse_args()
    dry_run = not args.live

    if dry_run:
        log.info("=== DRY-RUN mode (use --live to write) ===")
    else:
        log.info("=== LIVE mode — writing to Planfix ===")

    # Load mapping
    contacts = load_contacts_from_csv(INPUT_CSV)
    log.info("Loaded %d contacts to enrich", len(contacts))

    stats = defaultdict(int)
    results = []

    for megaplan_id, planfix_id in contacts.items():
        log.info("── mp=%s pf=%s ──", megaplan_id, planfix_id)
        row = {"megaplan_id": megaplan_id, "planfix_id": planfix_id,
               "status": "", "phones": 0, "emails": 0, "files": 0, "error": ""}
        try:
            # Fetch from Megaplan
            mp_resp = mp_req(f"/contractor/{megaplan_id}")
            mp_data = mp_resp.get("data", {})

            if not mp_data:
                log.warning("  Megaplan returned empty data for %s", megaplan_id)
                row["status"] = "no_data"
                stats["no_data"] += 1
                results.append(row)
                continue

            # Log what we found
            phones      = extract_phones(mp_data)
            emails      = extract_emails(mp_data)
            description = extract_description(mp_data)
            site        = extract_site(mp_data)
            files       = extract_files(mp_data)

            log.info("  found: phones=%d emails=%d description=%d site=%s files=%d",
                     len(phones), len(emails), len(description), bool(site), len(files))

            # Update contact fields
            pf_update_contact(planfix_id, phones, emails, description, site, dry_run)

            # Attach files
            files_ok = 0
            for file in files:
                ok = pf_attach_file(planfix_id, file["url"], file["name"], dry_run)
                if ok:
                    files_ok += 1

            row.update({"status": "enriched", "phones": len(phones),
                        "emails": len(emails), "files": files_ok})
            stats["enriched"] += 1

        except Exception as e:
            log.error("  ERROR mp=%s pf=%s: %s", megaplan_id, planfix_id, e)
            row["status"] = "error"
            row["error"] = str(e)[:200]
            stats["errors"] += 1

        results.append(row)

    # Write result CSV
    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["megaplan_id", "planfix_id", "status",
                                               "phones", "emails", "files", "error"])
        writer.writeheader()
        writer.writerows(results)

    log.info("")
    log.info("=== DONE ===")
    log.info("enriched=%d  no_data=%d  errors=%d",
             stats["enriched"], stats["no_data"], stats["errors"])
    log.info("Results: %s", RESULT_CSV)

    if dry_run:
        log.info("Re-run with --live to apply changes")


if __name__ == "__main__":
    main()
