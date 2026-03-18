#!/usr/bin/env python3
"""
fill_invoice_payment.py — заполнение даты оплаты и суммы в Invoice задачах Планфикс.

Алгоритм:
  1. GET n8n webhook → список счетов
  2. Фильтр по property_invoicedate: август 2025 — февраль 2026
  3. Для каждого счёта:
       - property_dedlinepay  → поле 128157 (Invoice Payment Date)
       - property_sum_fact    → поле 128161 (Payment Amount)
  4. Находит задачу в Планфикс (шаблон 21) по полю 128167 (Invoice Number) = property_invoice_name

Запуск:
    python fill_invoice_payment.py           # dry-run, показывает что будет
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

# =============================================================================
# CONFIG
# =============================================================================

PLANFIX_HOST  = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN = os.getenv("PLANFIX_TOKEN", "")
PLANFIX_DELAY = float(os.getenv("PLANFIX_DELAY", "0.5"))

N8N_WEBHOOK_URL = "https://n8n.crmcustoms.com/webhook/ebcec118-f1fc-4214-9586-a539fb92a0e4"

# Фильтр по дате счёта
DATE_FROM = datetime.date(2025, 8, 1)
DATE_TO   = datetime.date(2026, 2, 28)

# Планфикс: шаблон Invoice (template id=21)
INVOICE_TEMPLATE_ID     = 21
PF_INVOICE_NUMBER_FIELD = 128167   # Invoice Number (text)
PF_PAYMENT_DATE_FIELD   = 128157   # Invoice Payment Date (date)
PF_PAYMENT_AMOUNT_FIELD = 128161   # Payment Amount (number)

RESULT_CSV = "fill_invoice_payment_result.csv"

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fill_invoice_payment.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# HTTP SESSION
# =============================================================================

_pf = requests.Session()
_pf.headers.update({"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"})


def pf_req(method: str, path: str, **kwargs) -> dict:
    for attempt in range(4):
        time.sleep(PLANFIX_DELAY if attempt == 0 else 5 * attempt)
        try:
            r = _pf.request(method, f"{PLANFIX_HOST}/rest{path}", **kwargs)
        except (SSLError, ConnectionError) as e:
            log.warning("Planfix connection error, retry %d/4: %s", attempt + 1, e)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 10)))
            continue
        if not r.ok:
            raise RuntimeError(f"Planfix {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}
    raise RuntimeError(f"Planfix {method} {path} failed after 4 retries")


# =============================================================================
# HELPERS
# =============================================================================

def parse_date_ddmmyyyy(s: str) -> datetime.date | None:
    """Парсит "DD.MM.YYYY" → date. Возвращает None если пусто или ошибка."""
    if not s or not s.strip():
        return None
    try:
        return datetime.datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def date_to_ts(d: datetime.date) -> int:
    """date → Unix timestamp UTC полночь."""
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp())


# =============================================================================
# N8N: список счетов
# =============================================================================

def fetch_n8n_invoices() -> list[dict]:
    log.info("Fetching invoice list from n8n webhook...")
    r = requests.get(N8N_WEBHOOK_URL, timeout=120)
    r.raise_for_status()
    data = r.json()
    log.info("  Got %d invoices from n8n", len(data))
    return data


def filter_by_period(invoices: list[dict]) -> list[dict]:
    """Оставляет только счета с property_invoicedate в диапазоне DATE_FROM – DATE_TO."""
    result = []
    for inv in invoices:
        d = parse_date_ddmmyyyy(inv.get("property_invoicedate") or "")
        if d and DATE_FROM <= d <= DATE_TO:
            result.append(inv)
    log.info("  After date filter (%s – %s): %d invoices",
             DATE_FROM, DATE_TO, len(result))
    return result


# =============================================================================
# PLANFIX: загрузить все Invoice задачи (шаблон 21)
# =============================================================================

def load_planfix_invoices() -> dict[str, dict]:
    """Возвращает {invoice_number: {"id": task_id, "has_date": bool, "has_amount": bool}}."""
    log.info("Loading all Planfix Invoice tasks (template=%d)...", INVOICE_TEMPLATE_ID)
    fields = f"id,name,{PF_INVOICE_NUMBER_FIELD},{PF_PAYMENT_DATE_FIELD},{PF_PAYMENT_AMOUNT_FIELD}"
    result = {}
    offset = 0
    page_size = 100

    while True:
        resp = pf_req("POST", "/task/list", json={
            "offset": offset,
            "pageSize": page_size,
            "fields": fields,
            "filters": [{"type": 325, "operator": "equal", "value": INVOICE_TEMPLATE_ID}],
        })
        tasks = resp.get("tasks") or resp.get("data") or []
        if not tasks:
            break

        for task in tasks:
            task_id = task["id"]
            cfd = task.get("customFieldData") or []
            invoice_number = None
            has_date = False
            has_amount = False

            for entry in cfd:
                fid = (entry.get("field") or {}).get("id")
                val = entry.get("value")
                if fid == PF_INVOICE_NUMBER_FIELD:
                    invoice_number = (val or "").strip()
                elif fid == PF_PAYMENT_DATE_FIELD:
                    has_date = bool(val)
                elif fid == PF_PAYMENT_AMOUNT_FIELD:
                    has_amount = bool(val)

            if invoice_number:
                result[invoice_number] = {
                    "id": task_id,
                    "name": task.get("name", ""),
                    "has_date": has_date,
                    "has_amount": has_amount,
                }

        log.info("  fetched %d tasks (offset=%d)", len(tasks), offset)
        if len(tasks) < page_size:
            break
        offset += page_size

    log.info("Loaded %d Planfix invoices with invoice number", len(result))
    return result


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",      action="store_true", help="Write to Planfix")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite already filled fields")
    args = parser.parse_args()
    dry_run = not args.live

    if dry_run:
        log.info("=== DRY-RUN (use --live to write) ===")
    else:
        log.info("=== LIVE mode ===")

    log.info("Period: %s – %s", DATE_FROM, DATE_TO)

    # 1. Получить список счетов из n8n и отфильтровать по периоду
    all_invoices = fetch_n8n_invoices()
    n8n_invoices = filter_by_period(all_invoices)

    # 2. Загрузить все Invoice задачи из Планфикс
    pf_invoices = load_planfix_invoices()

    stats = defaultdict(int)
    rows = []

    for item in n8n_invoices:
        invoice_name = (item.get("property_invoice_name") or "").strip()

        row = {
            "invoice_name":  invoice_name,
            "pf_task_id":    "",
            "status":        "",
            "date":          "",
            "amount":        "",
            "error":         "",
        }

        if not invoice_name:
            stats["no_name"] += 1
            row["status"] = "no_name"
            rows.append(row)
            continue

        # Данные из n8n
        pay_date  = parse_date_ddmmyyyy(item.get("property_dedlinepay") or "")
        sum_fact  = item.get("property_sum_fact")

        log.info("Invoice '%s'  dedlinepay=%s  sum_fact=%s",
                 invoice_name,
                 pay_date.strftime("%Y-%m-%d") if pay_date else "—",
                 sum_fact if sum_fact is not None else "—")

        # 3. Найти задачу в Планфикс по номеру счёта
        pf_task = pf_invoices.get(invoice_name)
        if not pf_task:
            log.warning("  → Planfix task not found for '%s'", invoice_name)
            stats["pf_not_found"] += 1
            row["status"] = "pf_not_found"
            rows.append(row)
            continue

        task_id = pf_task["id"]
        row["pf_task_id"] = task_id
        errors = []
        fields_to_write = []

        # 4a. Дата оплаты
        if pay_date and (not pf_task["has_date"] or args.overwrite):
            fields_to_write.append({"field": {"id": PF_PAYMENT_DATE_FIELD}, "value": date_to_ts(pay_date)})
            row["date"] = pay_date.strftime("%Y-%m-%d")
        elif pf_task["has_date"] and not args.overwrite:
            log.info("  task #%d: date already set, skipping", task_id)

        # 4b. Сумма оплаты
        if sum_fact is not None and (not pf_task["has_amount"] or args.overwrite):
            fields_to_write.append({"field": {"id": PF_PAYMENT_AMOUNT_FIELD}, "value": sum_fact})
            row["amount"] = str(sum_fact)
        elif pf_task["has_amount"] and not args.overwrite:
            log.info("  task #%d: amount already set, skipping", task_id)

        # 5. Записать в Планфикс одним запросом
        if fields_to_write:
            if dry_run:
                log.info("  [DRY-RUN] task #%d '%s' → date=%s  amount=%s",
                         task_id, pf_task["name"][:40],
                         row["date"] or "—", row["amount"] or "—")
            else:
                try:
                    pf_req("POST", f"/task/{task_id}", json={"customFieldData": fields_to_write})
                    log.info("  task #%d '%s' ✓  date=%s  amount=%s",
                             task_id, pf_task["name"][:40],
                             row["date"] or "—", row["amount"] or "—")
                except Exception as e:
                    log.error("  task #%d write error: %s", task_id, e)
                    errors.append(str(e)[:150])
                    row["date"] = ""
                    row["amount"] = ""

        if errors:
            stats["error"] += 1
            row["status"] = "error"
            row["error"] = "; ".join(errors)
        elif row["date"] or row["amount"]:
            stats["filled"] += 1
            row["status"] = "filled"
        else:
            stats["already_set"] += 1
            row["status"] = "already_set"

        rows.append(row)

    # Итог
    log.info("")
    log.info("=== DONE ===")
    log.info("filled=%d  already_set=%d  pf_not_found=%d  no_name=%d  errors=%d",
             stats["filled"], stats["already_set"], stats["pf_not_found"],
             stats["no_name"], stats["error"])

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f,
            fieldnames=["invoice_name", "pf_task_id", "status", "date", "amount", "error"])
        writer.writeheader()
        writer.writerows(rows)
    log.info("Results: %s", RESULT_CSV)

    if dry_run:
        log.info("Re-run with --live to apply")


if __name__ == "__main__":
    main()
