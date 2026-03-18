#!/usr/bin/env python3
"""
export_megaplan_invoices.py — выгрузка счетов из Megaplan и заполнение Planfix.

Алгоритм:
  1. GET /api/v3/invoice с пагинацией → все 739 счетов
  2. Фильтр по invoiceDate: август 2025 — февраль 2026
  3. Tax rate выводим из taxTotal / sum (без дополнительных запросов)
  4. Ищем задачу в Planfix (шаблон 21) по полю 128167 = invoice.number
  5. Заполняем дату оплаты (если есть) и аналитику TAX

Запуск:
    python export_megaplan_invoices.py           # dry-run
    python export_megaplan_invoices.py --live    # реальная запись
    python export_megaplan_invoices.py --live --overwrite  # перезаписать
"""

import argparse
import csv
import datetime
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
MEGAPLAN_HOST = os.getenv("MEGAPLAN_HOST", "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN", "")

PLANFIX_DELAY  = float(os.getenv("PLANFIX_DELAY", "0.5"))
MEGAPLAN_DELAY = float(os.getenv("MEGAPLAN_DELAY", "0.3"))

# Диапазон дат для фильтрации счетов
DATE_FROM = datetime.date(2025, 8, 1)
DATE_TO   = datetime.date(2026, 2, 28)

INVOICE_TEMPLATE_ID     = 21
PF_INVOICE_NUMBER_FIELD = 128167
PF_PAYMENT_DATE_FIELD   = 128157
TAX_DATATAG_ID          = 9611
TAX_TYPE_FIELD_ID       = 58393
TAX_RATE_TO_KEY         = {0: 3, 12: 2, 16: 1}

RESULT_CSV = "export_megaplan_invoices_result.csv"

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("export_megaplan_invoices.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# HTTP SESSIONS
# =============================================================================

_pf = requests.Session()
_pf.headers.update({"Authorization": f"Bearer {PLANFIX_TOKEN}",
                     "Content-Type": "application/json"})

_mp = requests.Session()
_mp.headers.update({"Authorization": f"Bearer {MEGAPLAN_TOKEN}"})


def pf_req(method, path, **kwargs):
    for attempt in range(4):
        time.sleep(PLANFIX_DELAY if attempt == 0 else 3 * attempt)
        r = _pf.request(method, f"{PLANFIX_HOST}/rest{path}", **kwargs)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 10)))
            continue
        if not r.ok:
            raise RuntimeError(f"Planfix {method} {path} → {r.status_code}: {r.text[:200]}")
        return r.json() if r.text.strip() else {}
    raise RuntimeError(f"Planfix {method} {path} failed")


def mp_get(params: dict) -> dict:
    import json as _json
    for attempt in range(3):
        time.sleep(MEGAPLAN_DELAY if attempt == 0 else 2 * attempt)
        r = _mp.get(f"{MEGAPLAN_HOST}/api/v3/invoice",
                    params=_json.dumps(params), timeout=30)
        if not r.ok:
            raise RuntimeError(f"Megaplan → {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError("Megaplan failed")


# =============================================================================
# MEGAPLAN: выгрузить все счета за период
# =============================================================================

def fetch_megaplan_invoices() -> list[dict]:
    log.info("Fetching Megaplan invoices (pages)...")
    all_invoices = []
    page = 0
    limit = 100

    while True:
        data = mp_get({"limit": limit, "page": page})
        items = data.get("data", [])
        meta  = data.get("meta", {}).get("pagination", {})
        log.info("  page=%d  got=%d  total=%s", page, len(items), meta.get("count", "?"))
        all_invoices.extend(items)
        if not meta.get("hasMoreNext", False):
            break
        page += 1

    log.info("Total from Megaplan: %d invoices", len(all_invoices))
    return all_invoices


def parse_mp_date(d) -> datetime.date | None:
    if not d:
        return None
    if isinstance(d, dict):
        try:
            return datetime.date(d["year"], d["month"], d["day"])
        except Exception:
            return None
    return None


def derive_tax_rate(invoice: dict) -> int | None:
    """Вычисляет ставку налога из taxTotal и sum."""
    tax_total = invoice.get("taxTotal") or 0
    sum_val   = (invoice.get("sum") or {}).get("value") or 0
    if not sum_val:
        return None
    if tax_total == 0:
        return 0
    # rate/(100+rate) = tax_total/sum_val
    ratio = tax_total / sum_val
    for rate in (12, 16):
        if abs(ratio - rate / (100 + rate)) < 0.01:
            return rate
    return None


def filter_invoices(invoices: list[dict]) -> list[dict]:
    result = []
    for inv in invoices:
        d = parse_mp_date(inv.get("invoiceDate"))
        if d and DATE_FROM <= d <= DATE_TO:
            result.append(inv)
    log.info("After date filter (%s – %s): %d invoices",
             DATE_FROM, DATE_TO, len(result))
    return result


# =============================================================================
# PLANFIX: загрузить все Invoice задачи
# =============================================================================

def load_planfix_invoices() -> dict[str, dict]:
    log.info("Loading Planfix Invoice tasks (template=%d)...", INVOICE_TEMPLATE_ID)
    fields = f"id,name,{PF_INVOICE_NUMBER_FIELD},{PF_PAYMENT_DATE_FIELD}"
    result = {}
    offset = 0

    while True:
        resp = pf_req("POST", "/task/list", json={
            "offset": offset, "pageSize": 100, "fields": fields,
            "filters": [{"type": 325, "operator": "equal", "value": INVOICE_TEMPLATE_ID}],
        })
        tasks = resp.get("tasks") or []
        if not tasks:
            break
        for task in tasks:
            cfd = task.get("customFieldData") or []
            inv_num = None
            has_date = False
            for e in cfd:
                fid = (e.get("field") or {}).get("id")
                val = e.get("value")
                if fid == PF_INVOICE_NUMBER_FIELD:
                    inv_num = (val or "").strip()
                elif fid == PF_PAYMENT_DATE_FIELD:
                    has_date = bool(val)
            if inv_num:
                result[inv_num] = {"id": task["id"], "name": task.get("name",""), "has_date": has_date}
        log.info("  fetched %d (offset=%d)", len(tasks), offset)
        if len(tasks) < 100:
            break
        offset += 100

    log.info("Loaded %d Planfix invoices", len(result))
    return result


def load_tasks_with_tax() -> set[int]:
    log.info("Loading existing TAX entries...")
    task_ids: set[int] = set()
    offset = 0
    while True:
        resp = pf_req("POST", f"/datatag/{TAX_DATATAG_ID}/entry/list", json={
            "offset": offset, "pageSize": 100, "fields": "key,task",
        })
        entries = resp.get("dataTagEntries") or []
        for e in entries:
            tid = (e.get("task") or {}).get("id")
            if tid:
                task_ids.add(tid)
        if len(entries) < 100:
            break
        offset += 100
    log.info("  %d tasks already have TAX", len(task_ids))
    return task_ids


# =============================================================================
# PLANFIX: запись
# =============================================================================

def set_payment_date(task_id: int, d: datetime.date, dry_run: bool) -> str:
    ts = int(datetime.datetime(d.year, d.month, d.day,
                               tzinfo=datetime.timezone.utc).timestamp())
    if dry_run:
        log.info("  [DRY] task #%d → date %s", task_id, d)
        return str(d)
    pf_req("POST", f"/task/{task_id}", json={
        "customFieldData": [{"field": {"id": PF_PAYMENT_DATE_FIELD}, "value": ts}]
    })
    return str(d)


def create_tax_entry(task_id: int, rate: int, dry_run: bool) -> str:
    key = TAX_RATE_TO_KEY.get(rate)
    if key is None:
        return f"unknown_rate_{rate}"
    if dry_run:
        log.info("  [DRY] task #%d → TAX %d%% (key=%d)", task_id, rate, key)
        return f"{rate}%"
    pf_req("POST", f"/task/{task_id}/datatags/", json={
        "dataTag": {"id": TAX_DATATAG_ID},
        "items": [{"customFieldData": [{"field": {"id": TAX_TYPE_FIELD_ID}, "value": key}]}]
    })
    return f"{rate}%"


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",      action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    dry_run = not args.live

    log.info("=== %s ===", "LIVE" if args.live else "DRY-RUN")
    log.info("Date range: %s – %s", DATE_FROM, DATE_TO)

    mp_invoices   = fetch_megaplan_invoices()
    mp_filtered   = filter_invoices(mp_invoices)
    pf_invoices   = load_planfix_invoices()
    tasks_with_tax = load_tasks_with_tax()

    stats = defaultdict(int)
    rows  = []

    for inv in mp_filtered:
        inv_number = inv.get("number") or inv.get("name") or ""
        inv_date   = parse_mp_date(inv.get("invoiceDate"))
        pay_date   = parse_mp_date(inv.get("actualPaymentDate"))
        tax_rate   = derive_tax_rate(inv)

        row = {
            "mp_id": inv.get("id",""),
            "number": inv_number,
            "inv_date": str(inv_date) if inv_date else "",
            "pay_date": str(pay_date) if pay_date else "",
            "tax_rate": tax_rate if tax_rate is not None else "",
            "pf_task_id": "",
            "status": "",
            "wrote_date": "",
            "wrote_tax": "",
            "error": "",
        }

        log.info("Invoice %s (id=%s)  pay=%s  tax=%s%%",
                 inv_number, inv.get("id"), pay_date or "—",
                 tax_rate if tax_rate is not None else "—")

        pf_task = pf_invoices.get(inv_number)
        if not pf_task:
            log.warning("  → Planfix task not found for '%s'", inv_number)
            stats["pf_not_found"] += 1
            row["status"] = "pf_not_found"
            rows.append(row)
            continue

        task_id = pf_task["id"]
        row["pf_task_id"] = task_id
        errors = []

        # Дата оплаты
        if pay_date and (not pf_task["has_date"] or args.overwrite):
            try:
                row["wrote_date"] = set_payment_date(task_id, pay_date, dry_run)
            except Exception as e:
                errors.append(f"date: {e}")
        elif pf_task["has_date"] and not args.overwrite:
            log.info("  task #%d: date already set", task_id)

        # TAX
        task_has_tax = task_id in tasks_with_tax
        if tax_rate is not None and (not task_has_tax or args.overwrite):
            try:
                row["wrote_tax"] = create_tax_entry(task_id, tax_rate, dry_run)
                if not dry_run:
                    tasks_with_tax.add(task_id)
            except Exception as e:
                errors.append(f"tax: {e}")
        elif task_has_tax and not args.overwrite:
            log.info("  task #%d: TAX already set", task_id)

        if errors:
            stats["error"] += 1
            row["status"] = "error"
            row["error"]  = "; ".join(str(e) for e in errors)
        elif row["wrote_date"] or row["wrote_tax"]:
            stats["filled"] += 1
            row["status"] = "filled"
            log.info("  task #%d ✓  date=%s  tax=%s",
                     task_id, row["wrote_date"] or "—", row["wrote_tax"] or "—")
        else:
            stats["already_set"] += 1
            row["status"] = "already_set"

        rows.append(row)

    log.info("")
    log.info("=== DONE ===")
    log.info("filled=%d  already_set=%d  pf_not_found=%d  errors=%d",
             stats["filled"], stats["already_set"], stats["pf_not_found"], stats["error"])

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    log.info("Results: %s", RESULT_CSV)

    if dry_run:
        log.info("Re-run with --live to apply changes")


if __name__ == "__main__":
    main()
