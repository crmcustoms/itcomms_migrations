#!/usr/bin/env python3
"""
fill_invoice_payment.py — заполнение даты оплаты и аналитики TAX в Invoice задачах Планфикс.

Алгоритм:
  1. GET n8n webhook → список счетов с Megaplan invoice ID (property_invoiseidmp)
  2. Для каждого счёта → GET https://likhtman.megaplan.ru/api/v3/invoice/{id}
  3. Берёт:
       - actualPaymentDate  → поле 128157 (Invoice Payment Date)
       - rows[0].tax.rate   → создаёт запись аналитики TAX (datatag 9611, поле 58393)
  4. Находит задачу в Планфикс (шаблон 21) по полю 128167 (Invoice Number) = invoice.number
  5. Записывает дату; создаёт строку аналитики TAX если её ещё нет

Аналитика TAX (datatag id=9611):
  - Поле 58393 «Tax Type» — ссылка на справочник 17495 (Taxes) по ключу:
      0%  → key=3
      12% → key=2
      16% → key=1
  - Поле 58395 «TAX amount» — формула, считается автоматически, не передаём

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

MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST",  "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN", "")

PLANFIX_DELAY  = float(os.getenv("PLANFIX_DELAY",  "1.0"))
MEGAPLAN_DELAY = float(os.getenv("MEGAPLAN_DELAY", "0.5"))

N8N_WEBHOOK_URL = "https://n8n.crmcustoms.com/webhook/ebcec118-f1fc-4214-9586-a539fb92a0e4"

# Планфикс: шаблон Invoice (template id=21)
INVOICE_TEMPLATE_ID     = 21
PF_INVOICE_NUMBER_FIELD = 128167   # Invoice Number (text)
PF_PAYMENT_DATE_FIELD   = 128157   # Invoice Payment Date (type=3, date)

# TAX аналитика
TAX_DATATAG_ID          = 9611     # аналитика «TAX»
TAX_TYPE_FIELD_ID       = 58393    # поле «Tax Type» в аналитике (Directory 17495)

# Маппинг: ставка налога из Мегаплана → ключ записи в справочнике Planfix
TAX_RATE_TO_KEY = {
    0:  3,   # Sales Tax 0%
    12: 2,   # Sales Tax 12%
    16: 1,   # Sales Tax 16%
}

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
# HTTP SESSIONS
# =============================================================================

_pf = requests.Session()
_pf.headers.update({"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"})

_mp = requests.Session()
_mp.headers.update({"Authorization": f"Bearer {MEGAPLAN_TOKEN}", "Content-Type": "application/json"})


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


def mp_req(path: str) -> dict:
    for attempt in range(4):
        time.sleep(MEGAPLAN_DELAY if attempt == 0 else 5 * attempt)
        try:
            r = _mp.get(f"{MEGAPLAN_HOST}/api/v3{path}")
        except (SSLError, ConnectionError) as e:
            log.warning("Megaplan connection error, retry %d/4: %s", attempt + 1, e)
            continue
        if r.status_code == 429:
            time.sleep(10)
            continue
        if not r.ok:
            raise RuntimeError(f"Megaplan GET {path} → {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"Megaplan GET {path} failed after 4 retries")


# =============================================================================
# N8N: список счетов
# =============================================================================

def fetch_n8n_invoices() -> list[dict]:
    log.info("Fetching invoice list from n8n webhook...")
    r = requests.get(N8N_WEBHOOK_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    log.info("  Got %d invoices from n8n", len(data))
    return data


# =============================================================================
# MEGAPLAN: данные счёта
# =============================================================================

def get_megaplan_invoice(invoice_id: int | str) -> dict | None:
    try:
        resp = mp_req(f"/invoice/{invoice_id}")
        return resp.get("data")
    except Exception as e:
        log.warning("  Megaplan invoice %s error: %s", invoice_id, e)
        return None


def parse_payment_date(invoice_data: dict) -> int | None:
    """actualPaymentDate → Unix timestamp (UTC полночь)."""
    apd = invoice_data.get("actualPaymentDate")
    if not apd:
        return None
    value = apd.get("value") if isinstance(apd, dict) else str(apd)
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        d = dt.date()
        return int(datetime.datetime(d.year, d.month, d.day,
                                     tzinfo=datetime.timezone.utc).timestamp())
    except Exception as e:
        log.warning("  Can't parse actualPaymentDate '%s': %s", value, e)
        return None


def get_tax_rate(invoice_data: dict) -> int | None:
    """Берёт tax rate из первой строки invoice."""
    rows = invoice_data.get("rows") or []
    for row in rows:
        tax = row.get("tax") or {}
        rate = tax.get("rate")
        if rate is not None:
            return int(rate)
    return None


# =============================================================================
# PLANFIX: загрузить все Invoice задачи (шаблон 21)
# =============================================================================

def load_planfix_invoices() -> dict[str, dict]:
    """Возвращает {invoice_number: {"id": task_id, "has_date": bool, "has_tax": bool}}."""
    log.info("Loading all Planfix Invoice tasks (template=%d)...", INVOICE_TEMPLATE_ID)
    fields = f"id,name,{PF_INVOICE_NUMBER_FIELD},{PF_PAYMENT_DATE_FIELD},dataTags"
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
            data_tags = task.get("dataTags") or []

            invoice_number = None
            has_date = False

            for entry in cfd:
                fid = (entry.get("field") or {}).get("id")
                val = entry.get("value")
                if fid == PF_INVOICE_NUMBER_FIELD:
                    invoice_number = (val or "").strip()
                elif fid == PF_PAYMENT_DATE_FIELD:
                    has_date = bool(val)

            # Проверяем наличие TAX аналитики
            has_tax = any(
                (dt.get("dataTag") or {}).get("id") == TAX_DATATAG_ID
                for dt in data_tags
            )

            if invoice_number:
                result[invoice_number] = {
                    "id": task_id,
                    "name": task.get("name", ""),
                    "has_date": has_date,
                    "has_tax": has_tax,
                }

        log.info("  fetched %d tasks (offset=%d)", len(tasks), offset)
        if len(tasks) < page_size:
            break
        offset += page_size

    log.info("Loaded %d Planfix invoices with invoice number", len(result))
    return result


# =============================================================================
# PLANFIX: записать дату оплаты
# =============================================================================

def set_payment_date(task_id: int, ts: int, dry_run: bool) -> str:
    date_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    if dry_run:
        log.info("  [DRY-RUN] task #%d → payment date = %s", task_id, date_str)
        return date_str
    pf_req("POST", f"/task/{task_id}", json={
        "customFieldData": [{"field": {"id": PF_PAYMENT_DATE_FIELD}, "value": ts}]
    })
    return date_str


# =============================================================================
# PLANFIX: создать запись аналитики TAX
# =============================================================================

def create_tax_entry(task_id: int, tax_rate: int, dry_run: bool) -> str:
    """Создаёт строку аналитики TAX для задачи. Возвращает описание результата."""
    directory_key = TAX_RATE_TO_KEY.get(tax_rate)
    if directory_key is None:
        log.warning("  task #%d: unknown tax rate %d%%, skipping TAX", task_id, tax_rate)
        return f"unknown_rate_{tax_rate}"

    if dry_run:
        log.info("  [DRY-RUN] task #%d → TAX entry: %d%% (directory key=%d)",
                 task_id, tax_rate, directory_key)
        return f"{tax_rate}%"

    pf_req("POST", f"/task/{task_id}/datatag/{TAX_DATATAG_ID}/entry/", json={
        "customFieldData": [
            {"field": {"id": TAX_TYPE_FIELD_ID}, "value": directory_key}
        ]
    })
    return f"{tax_rate}%"


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

    # 1. Получить список счетов из n8n
    n8n_invoices = fetch_n8n_invoices()

    # 2. Загрузить все Invoice задачи из Планфикс
    pf_invoices = load_planfix_invoices()

    stats = defaultdict(int)
    rows = []

    for item in n8n_invoices:
        mp_invoice_id = item.get("property_invoiseidmp")
        invoice_name  = item.get("property_invoice_name") or item.get("property_name") or ""

        row = {
            "mp_invoice_id": mp_invoice_id or "",
            "invoice_name":  invoice_name,
            "pf_task_id":    "",
            "status":        "",
            "date":          "",
            "tax":           "",
            "error":         "",
        }

        if not mp_invoice_id:
            log.info("Skip '%s': no property_invoiseidmp", invoice_name)
            stats["no_mp_id"] += 1
            row["status"] = "no_mp_id"
            rows.append(row)
            continue

        # 3. Получить счёт из Мегаплана
        log.info("Invoice '%s' (mp_id=%s)...", invoice_name, mp_invoice_id)
        mp_data = get_megaplan_invoice(mp_invoice_id)
        if not mp_data:
            stats["mp_error"] += 1
            row["status"] = "mp_error"
            rows.append(row)
            continue

        invoice_number  = mp_data.get("number") or mp_data.get("name") or ""
        payment_date_ts = parse_payment_date(mp_data)
        tax_rate        = get_tax_rate(mp_data)

        log.info("  MP: number=%s  paydate=%s  tax_rate=%s%%",
                 invoice_number,
                 datetime.datetime.fromtimestamp(payment_date_ts,
                     tz=datetime.timezone.utc).strftime("%Y-%m-%d") if payment_date_ts else "—",
                 tax_rate if tax_rate is not None else "—")

        # 4. Найти задачу в Планфикс по номеру счёта
        pf_task = pf_invoices.get(invoice_number)
        if not pf_task:
            log.warning("  Planfix task not found for invoice_number='%s'", invoice_number)
            stats["pf_not_found"] += 1
            row["status"] = "pf_not_found"
            row["error"] = f"no task with Invoice Number='{invoice_number}'"
            rows.append(row)
            continue

        task_id = pf_task["id"]
        row["pf_task_id"] = task_id
        errors = []

        # 5a. Дата оплаты
        if payment_date_ts and (not pf_task["has_date"] or args.overwrite):
            try:
                row["date"] = set_payment_date(task_id, payment_date_ts, dry_run)
            except Exception as e:
                log.error("  task #%d date write error: %s", task_id, e)
                errors.append(f"date: {str(e)[:100]}")
        elif pf_task["has_date"] and not args.overwrite:
            log.info("  task #%d: date already set, skipping", task_id)

        # 5b. TAX аналитика
        if tax_rate is not None and (not pf_task["has_tax"] or args.overwrite):
            try:
                row["tax"] = create_tax_entry(task_id, tax_rate, dry_run)
            except Exception as e:
                log.error("  task #%d TAX write error: %s", task_id, e)
                errors.append(f"tax: {str(e)[:100]}")
        elif pf_task["has_tax"] and not args.overwrite:
            log.info("  task #%d: TAX already set, skipping", task_id)

        if errors:
            stats["error"] += 1
            row["status"] = "error"
            row["error"] = "; ".join(errors)
        elif row["date"] or row["tax"]:
            stats["filled"] += 1
            row["status"] = "filled"
            log.info("  task #%d '%s' ✓  date=%s  tax=%s",
                     task_id, pf_task["name"][:40],
                     row["date"] or "—", row["tax"] or "—")
        else:
            stats["already_set"] += 1
            row["status"] = "already_set"

        rows.append(row)

    # Итог
    log.info("")
    log.info("=== DONE ===")
    log.info("filled=%d  already_set=%d  pf_not_found=%d  no_mp_id=%d  mp_error=%d  errors=%d",
             stats["filled"], stats["already_set"], stats["pf_not_found"],
             stats["no_mp_id"], stats["mp_error"], stats["error"])

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f,
            fieldnames=["mp_invoice_id","invoice_name","pf_task_id","status","date","tax","error"])
        writer.writeheader()
        writer.writerows(rows)
    log.info("Results: %s", RESULT_CSV)

    if dry_run:
        log.info("Re-run with --live to apply")


if __name__ == "__main__":
    main()
