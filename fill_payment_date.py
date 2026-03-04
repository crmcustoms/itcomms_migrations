#!/usr/bin/env python3
"""
fill_payment_date.py — заполнение поля "Дата оплаты" в расходных сделках Планфикс.

Алгоритм:
  1. Перебирает все задачи в шаблонах 15 и 7691 (Прочие поставщики)
  2. Читает поле 132121 "ID сделки" — Megaplan deal ID
  3. Если поле заполнено и дата оплаты ещё не стоит — идёт в Мегаплан
  4. Получает сделку GET /api/v3/deal/{id}
  5. Берёт поле Category1000083CustomFieldDataOplati (DateOnly)
  6. Записывает дату в поле 138909 "Дата оплаты" в Планфикс

Формат DateOnly в Мегаплане: {"contentType":"DateOnly","year":2026,"month":0,"day":26}
Месяцы 0-индексированные (0 = январь).

Запуск:
    python fill_payment_date.py           # dry-run
    python fill_payment_date.py --live    # реальная запись
    python fill_payment_date.py --live --overwrite  # перезаписать уже заполненные
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

# Планфикс: шаблоны расходных сделок
TEMPLATES = [
    {"id": 15,   "deal_id_field": 132121, "payment_date_field": 138909},
    {"id": 7691, "deal_id_field": 132121, "payment_date_field": 138909},
]

# Мегаплан: поле даты оплаты в сделке
MP_DATE_FIELD = "Category1000083CustomFieldDataOplati"

RESULT_CSV = "fill_payment_date_result.csv"

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fill_payment_date.log", encoding="utf-8"),
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
# DATE CONVERSION
# =============================================================================

def parse_mp_date(date_obj: dict | None) -> int | None:
    """Конвертирует DateOnly из Мегаплана в Unix timestamp (UTC полночь).

    Мегаплан: {"contentType":"DateOnly","year":2026,"month":0,"day":26}
    Месяцы 0-индексированные: 0=январь, 1=февраль, ..., 11=декабрь
    """
    if not date_obj or date_obj.get("contentType") != "DateOnly":
        return None
    year  = date_obj.get("year")
    month = date_obj.get("month")  # 0-indexed
    day   = date_obj.get("day")
    if not all([year, day is not None, month is not None]):
        return None
    try:
        # Convert 0-indexed month to 1-indexed
        dt = datetime.datetime(year, month + 1, day, tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError) as e:
        log.warning("Invalid date %s: %s", date_obj, e)
        return None


def timestamp_to_str(ts: int) -> str:
    """Для логов: Unix timestamp → читаемая дата."""
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


# =============================================================================
# PLANFIX: перебор задач
# =============================================================================

def iter_tasks(template_id: int, deal_id_field: int, payment_date_field: int):
    """Перебирает все задачи шаблона, возвращает (task_id, task_name, deal_id, has_payment_date)."""
    fields = f"id,name,{deal_id_field},{payment_date_field}"
    offset = 0
    page_size = 100

    while True:
        resp = pf_req("POST", "/task/list", json={
            "offset": offset,
            "pageSize": page_size,
            "fields": fields,
            "filters": [{"type": 325, "operator": "equal", "value": template_id}],
        })
        tasks = resp.get("tasks") or resp.get("data") or []
        if not tasks:
            break

        for task in tasks:
            task_id   = task["id"]
            task_name = task.get("name", "")
            cfd       = task.get("customFieldData") or []

            deal_id         = None
            has_payment_date = False

            for entry in cfd:
                fid = (entry.get("field") or {}).get("id")
                if fid == deal_id_field:
                    deal_id = (entry.get("value") or "").strip()
                elif fid == payment_date_field:
                    val = entry.get("value")
                    has_payment_date = bool(val)

            yield task_id, task_name, deal_id, has_payment_date

        log.info("  fetched %d tasks (offset=%d)", len(tasks), offset)
        if len(tasks) < page_size:
            break
        offset += page_size


# =============================================================================
# MEGAPLAN: получить дату оплаты из сделки
# =============================================================================

_mp_deal_cache: dict[str, int | None] = {}


def get_payment_date_from_megaplan(deal_id: str) -> int | None:
    """Возвращает Unix timestamp даты оплаты из Мегаплана, или None."""
    if deal_id in _mp_deal_cache:
        return _mp_deal_cache[deal_id]

    try:
        data = mp_req(f"/deal/{deal_id}")
        deal = data.get("data", {})
        date_obj = deal.get(MP_DATE_FIELD)
        ts = parse_mp_date(date_obj)
        _mp_deal_cache[deal_id] = ts
        if ts:
            log.debug("  Megaplan deal %s: payment date = %s", deal_id, timestamp_to_str(ts))
        else:
            log.debug("  Megaplan deal %s: no payment date", deal_id)
        return ts
    except Exception as e:
        log.warning("  Megaplan deal %s error: %s", deal_id, e)
        _mp_deal_cache[deal_id] = None
        return None


# =============================================================================
# PLANFIX: записать дату оплаты
# =============================================================================

def set_payment_date(task_id: int, payment_date_field: int, timestamp: int, dry_run: bool) -> bool:
    if dry_run:
        log.info("  [DRY-RUN] task #%d → дата оплаты = %s", task_id, timestamp_to_str(timestamp))
        return True
    pf_req("POST", f"/task/{task_id}", json={
        "customFieldData": [{"field": {"id": payment_date_field}, "value": timestamp}]
    })
    return True


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",      action="store_true", help="Write to Planfix")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite already filled dates")
    args = parser.parse_args()
    dry_run = not args.live

    if dry_run:
        log.info("=== DRY-RUN (use --live to write) ===")
    else:
        log.info("=== LIVE mode ===")

    stats = defaultdict(int)
    results = []

    for tpl in TEMPLATES:
        template_id        = tpl["id"]
        deal_id_field      = tpl["deal_id_field"]
        payment_date_field = tpl["payment_date_field"]

        log.info("── Template %d ──", template_id)

        for task_id, task_name, deal_id, has_payment_date in iter_tasks(
            template_id, deal_id_field, payment_date_field
        ):
            row = {
                "template_id": template_id,
                "task_id": task_id,
                "task_name": task_name,
                "deal_id": deal_id or "",
                "status": "",
                "date": "",
                "error": "",
            }

            # Пропускаем если нет ID сделки
            if not deal_id:
                stats["no_deal_id"] += 1
                row["status"] = "no_deal_id"
                results.append(row)
                continue

            # Пропускаем если дата уже стоит (если не --overwrite)
            if has_payment_date and not args.overwrite:
                stats["already_set"] += 1
                row["status"] = "already_set"
                results.append(row)
                continue

            # Получаем дату из Мегаплана
            try:
                ts = get_payment_date_from_megaplan(deal_id)
            except Exception as e:
                log.error("Task #%d deal %s: %s", task_id, deal_id, e)
                stats["error"] += 1
                row["status"] = "error"
                row["error"] = str(e)[:200]
                results.append(row)
                continue

            if ts is None:
                log.info("Task #%d %-40s deal=%s → нет даты в Мегаплане", task_id, task_name[:40], deal_id)
                stats["no_date_in_mp"] += 1
                row["status"] = "no_date_in_mp"
                results.append(row)
                continue

            # Записываем
            try:
                set_payment_date(task_id, payment_date_field, ts, dry_run)
                date_str = timestamp_to_str(ts)
                log.info("Task #%d %-40s deal=%s → %s ✓", task_id, task_name[:40], deal_id, date_str)
                stats["filled"] += 1
                row["status"] = "filled"
                row["date"] = date_str
            except Exception as e:
                log.error("Task #%d write error: %s", task_id, e)
                stats["error"] += 1
                row["status"] = "error"
                row["error"] = str(e)[:200]

            results.append(row)

    # Итог
    log.info("")
    log.info("=== DONE ===")
    log.info("filled=%d  already_set=%d  no_deal_id=%d  no_date_in_mp=%d  errors=%d",
             stats["filled"], stats["already_set"],
             stats["no_deal_id"], stats["no_date_in_mp"], stats["error"])

    # CSV
    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["template_id","task_id","task_name",
                                                "deal_id","status","date","error"])
        writer.writeheader()
        writer.writerows(results)
    log.info("Results: %s", RESULT_CSV)

    if dry_run:
        log.info("Re-run with --live to apply")


if __name__ == "__main__":
    main()
