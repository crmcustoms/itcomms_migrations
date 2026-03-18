#!/usr/bin/env python3
"""
fill_invoice_payment.py — заполнение фактической даты оплаты в Invoice задачах Планфикс.

Алгоритм:
  1. GET n8n webhook → список счетов, фильтр по property_invoicedate (авг 2025 – фев 2026)
  2. Для каждого счёта → GET Megaplan /api/v3/invoice/{property_invoiseidmp}
  3. Берёт actualPaymentDate → пишет в поле 128157 (Invoice Payment Date)
  4. Находит задачу в Планфикс (шаблон 21) по полю 128167 = property_invoice_name

Запуск:
    python fill_invoice_payment.py           # dry-run
    python fill_invoice_payment.py --live    # реальная запись
    python fill_invoice_payment.py --live --overwrite  # перезаписать уже заполненные
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
from requests.exceptions import ConnectionError, SSLError

load_dotenv()

PLANFIX_HOST   = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN  = os.getenv("PLANFIX_TOKEN", "")
MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST", "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN", "")
PLANFIX_DELAY  = float(os.getenv("PLANFIX_DELAY",  "0.5"))
MEGAPLAN_DELAY = float(os.getenv("MEGAPLAN_DELAY", "0.3"))

N8N_WEBHOOK_URL = "https://n8n.crmcustoms.com/webhook/ebcec118-f1fc-4214-9586-a539fb92a0e4"

DATE_FROM = datetime.date(2025, 8, 1)
DATE_TO   = datetime.date(2026, 2, 28)

INVOICE_TEMPLATE_ID     = 21
PF_INVOICE_NUMBER_FIELD = 128167
PF_PAYMENT_DATE_FIELD   = 128157

RESULT_CSV = "fill_invoice_payment_result.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fill_invoice_payment.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

_pf = requests.Session()
_pf.headers.update({"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"})

_mp = requests.Session()
_mp.headers.update({"Authorization": f"Bearer {MEGAPLAN_TOKEN}", "Content-Type": "application/json"})


def pf_req(method, path, **kwargs):
    for attempt in range(4):
        time.sleep(PLANFIX_DELAY if attempt == 0 else 5 * attempt)
        try:
            r = _pf.request(method, f"{PLANFIX_HOST}/rest{path}", **kwargs)
        except (SSLError, ConnectionError) as e:
            log.warning("Planfix error retry %d: %s", attempt + 1, e)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 10)))
            continue
        if not r.ok:
            raise RuntimeError(f"Planfix {r.status_code}: {r.text[:200]}")
        return r.json() if r.text.strip() else {}
    raise RuntimeError("Planfix failed after 4 retries")


def mp_get(invoice_id):
    for attempt in range(3):
        time.sleep(MEGAPLAN_DELAY if attempt == 0 else 3 * attempt)
        try:
            r = _mp.get(f"{MEGAPLAN_HOST}/api/v3/invoice/{invoice_id}", timeout=15)
        except (SSLError, ConnectionError) as e:
            log.warning("Megaplan error retry %d: %s", attempt + 1, e)
            continue
        if r.status_code == 404:
            return None
        if not r.ok:
            log.warning("Megaplan %s → %d", invoice_id, r.status_code)
            return None
        return r.json().get("data")
    return None


def parse_ddmmyyyy(s):
    if not s or not s.strip():
        return None
    try:
        return datetime.datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_actual_payment_date(data):
    apd = data.get("actualPaymentDate")
    if not apd:
        return None
    value = apd.get("value") if isinstance(apd, dict) else str(apd)
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        d = dt.date()
        return int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return None


def load_planfix_invoices():
    log.info("Loading Planfix Invoice tasks...")
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
            inv_num = None
            has_date = False
            for e in task.get("customFieldData") or []:
                fid = (e.get("field") or {}).get("id")
                if fid == PF_INVOICE_NUMBER_FIELD:
                    inv_num = (e.get("value") or "").strip()
                elif fid == PF_PAYMENT_DATE_FIELD:
                    has_date = bool(e.get("value"))
            if inv_num:
                result[inv_num] = {"id": task["id"], "name": task.get("name", ""), "has_date": has_date}
        log.info("  fetched %d (offset=%d)", len(tasks), offset)
        if len(tasks) < 100:
            break
        offset += 100
    log.info("Loaded %d Planfix invoices", len(result))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",      action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    dry_run = not args.live

    log.info("=== %s ===", "DRY-RUN" if dry_run else "LIVE")
    log.info("Period: %s – %s", DATE_FROM, DATE_TO)

    # 1. n8n список → фильтр по периоду
    log.info("Fetching n8n invoices...")
    r = requests.get(N8N_WEBHOOK_URL, timeout=120)
    r.raise_for_status()
    all_items = r.json()
    log.info("  Got %d from n8n", len(all_items))

    items = [i for i in all_items
             if (d := parse_ddmmyyyy(i.get("property_invoicedate") or ""))
             and DATE_FROM <= d <= DATE_TO]
    log.info("  After period filter: %d invoices", len(items))

    # 2. Планфикс задачи
    pf_invoices = load_planfix_invoices()

    stats = defaultdict(int)
    rows = []

    for item in items:
        inv_name   = (item.get("property_invoice_name") or "").strip()
        mp_id      = item.get("property_invoiseidmp")

        row = {"invoice_name": inv_name, "mp_id": mp_id or "", "pf_task_id": "",
               "status": "", "date": "", "error": ""}

        if not inv_name or not mp_id:
            stats["skip"] += 1
            row["status"] = "skip"
            rows.append(row)
            continue

        # Найти задачу в Planfix
        pf_task = pf_invoices.get(inv_name)
        if not pf_task:
            log.warning("  '%s': Planfix task not found", inv_name)
            stats["pf_not_found"] += 1
            row["status"] = "pf_not_found"
            rows.append(row)
            continue

        task_id = pf_task["id"]
        row["pf_task_id"] = task_id

        # Пропустить если дата уже стоит
        if pf_task["has_date"] and not args.overwrite:
            log.info("  '%s' task #%d: date already set, skipping", inv_name, task_id)
            stats["already_set"] += 1
            row["status"] = "already_set"
            rows.append(row)
            continue

        # Получить actualPaymentDate из Megaplan
        log.info("  '%s' (mp=%s) → Megaplan...", inv_name, mp_id)
        mp_data = mp_get(mp_id)
        if not mp_data:
            stats["mp_error"] += 1
            row["status"] = "mp_error"
            rows.append(row)
            continue

        ts = parse_actual_payment_date(mp_data)
        if not ts:
            log.info("  '%s': no actualPaymentDate in Megaplan", inv_name)
            stats["no_date"] += 1
            row["status"] = "no_date"
            rows.append(row)
            continue

        date_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        row["date"] = date_str

        if dry_run:
            log.info("  [DRY-RUN] task #%d '%s' → payment date = %s", task_id, inv_name, date_str)
            stats["filled"] += 1
            row["status"] = "filled"
        else:
            try:
                pf_req("POST", f"/task/{task_id}", json={
                    "customFieldData": [{"field": {"id": PF_PAYMENT_DATE_FIELD}, "value": ts}]
                })
                log.info("  task #%d '%s' ✓ date=%s", task_id, inv_name, date_str)
                stats["filled"] += 1
                row["status"] = "filled"
            except Exception as e:
                log.error("  task #%d error: %s", task_id, e)
                stats["error"] += 1
                row["status"] = "error"
                row["error"] = str(e)[:150]

        rows.append(row)

    log.info("")
    log.info("=== DONE ===")
    log.info("filled=%d  already_set=%d  no_date=%d  pf_not_found=%d  mp_error=%d  errors=%d",
             stats["filled"], stats["already_set"], stats["no_date"],
             stats["pf_not_found"], stats["mp_error"], stats["error"])

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["invoice_name", "mp_id", "pf_task_id", "status", "date", "error"])
        writer.writeheader()
        writer.writerows(rows)
    log.info("Results: %s", RESULT_CSV)
    if dry_run:
        log.info("Re-run with --live to apply")


if __name__ == "__main__":
    main()
