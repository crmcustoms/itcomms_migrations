#!/usr/bin/env python3
"""
trigger_payment_date.py — проставляє/тригерить дату оплати (поле 138909)
у щойно створених розхідних задачах Planfix.

Логіка:
  - Якщо дата НЕ встановлена → тягнемо з Megaplan і ставимо → сценарій спрацьовує
  - Якщо дата ВЖЕ стоїть → ставимо date+1день, чекаємо BUMP_WAIT сек, повертаємо оригінал
    → сценарій спрацьовує двічі, фінальне значення — правильна дата

Сценарій Planfix реагує на зміну поля 138909.
Затримка між задачами: TASK_DELAY секунд (за замовчуванням 60).

Запуск:
    python trigger_payment_date.py           # dry-run, покаже план
    python trigger_payment_date.py --live    # реальний запис
    python trigger_payment_date.py --live --delay 30   # менша затримка
"""

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

PLANFIX_HOST   = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN  = os.getenv("PLANFIX_TOKEN", "6ca06006655c6e695c495a4705609c85")
MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST", "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN",
    "NzZkODNiOGUwMWNlMGIyMTY5NzlkMDkzOGEzOWFlOGI1MGYyNTk0YThmOWJkYWE5ZDFlMGMyNGU2YWQ2ZWI1ZA")

PF_HEADERS = {"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"}
MP_HEADERS = {"Authorization": f"Bearer {MEGAPLAN_TOKEN}"}

PAYMENT_DATE_FIELD = 138909   # Дата оплаты
MP_DATE_FIELD = "Category1000083CustomFieldDataOplati"

DB_PATH    = "create_missing_expense.db"
BUMP_WAIT  = 30   # секунд між date+1 і поверненням назад
TASK_DELAY = 60   # секунд між задачами

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("trigger_payment_date.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Конвертація дат ──────────────────────────────────────────────────────────

def parse_dateonly(obj) -> int | None:
    """DateOnly Megaplan → Unix timestamp UTC. Month 0-indexed."""
    if not obj or not isinstance(obj, dict):
        return None
    if obj.get("contentType") != "DateOnly":
        return None
    try:
        dt = datetime(obj["year"], obj["month"] + 1, obj["day"], tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def ts_to_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ─── Planfix API ──────────────────────────────────────────────────────────────

def pf_get_task_date(pf_task_id: int) -> int | None:
    """Повертає поточне значення поля 138909 або None."""
    r = requests.get(
        f"{PLANFIX_HOST}/rest/task/{pf_task_id}",
        headers=PF_HEADERS,
        params={"fields": f"id,{PAYMENT_DATE_FIELD}"},
        timeout=15,
    )
    if not r.ok:
        log.warning(f"  GET task {pf_task_id}: {r.status_code}")
        return None
    cfd = r.json().get("customFieldData") or []
    for entry in cfd:
        if (entry.get("field") or {}).get("id") == PAYMENT_DATE_FIELD:
            val = entry.get("value")
            if val:
                return int(val)
    return None


def pf_set_date(pf_task_id: int, ts: int, dry_run: bool) -> bool:
    if dry_run:
        log.info(f"    DRY SET task {pf_task_id} date={ts_to_str(ts)}")
        return True
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{pf_task_id}?silent=false",
        headers=PF_HEADERS,
        json={"customFieldData": [{"field": {"id": PAYMENT_DATE_FIELD}, "value": ts}]},
        timeout=15,
    )
    if not r.ok:
        log.warning(f"    SET task {pf_task_id}: {r.status_code} {r.text[:100]}")
        return False
    return True


# ─── Megaplan API ─────────────────────────────────────────────────────────────

def mp_get_payment_date(mp_deal_id: str) -> int | None:
    """Дата оплати з Megaplan (CustomFieldDataOplati)."""
    try:
        r = requests.get(
            f"{MEGAPLAN_HOST}/api/v3/deal/{mp_deal_id}",
            headers=MP_HEADERS, timeout=20,
        )
        r.raise_for_status()
        deal = r.json().get("data", {})
        return parse_dateonly(deal.get(MP_DATE_FIELD))
    except Exception as e:
        log.warning(f"  Megaplan deal {mp_deal_id}: {e}")
        return None


# ─── Обробка задачі ───────────────────────────────────────────────────────────

def process_task(pf_task_id: int, mp_deal_id: str, dry_run: bool, bump_wait: int) -> str:
    log.info(f"  Task {pf_task_id} (deal {mp_deal_id})")

    # Поточна дата оплати
    current_ts = pf_get_task_date(pf_task_id)

    if current_ts is None:
        # Дати немає — тягнемо з Megaplan
        log.info(f"    no date → fetching from Megaplan")
        mp_ts = mp_get_payment_date(mp_deal_id)
        if mp_ts is None:
            log.info(f"    Megaplan also has no date — skip")
            return "no_date_anywhere"
        log.info(f"    Megaplan date: {ts_to_str(mp_ts)} → setting")
        ok = pf_set_date(pf_task_id, mp_ts, dry_run)
        return "set_from_megaplan" if ok else "error"
    else:
        # Дата є — bump на 1 день і повернути
        bump_ts = current_ts + 86400
        log.info(f"    date exists: {ts_to_str(current_ts)} → bump to {ts_to_str(bump_ts)}")
        ok1 = pf_set_date(pf_task_id, bump_ts, dry_run)
        if not ok1:
            return "error"
        if not dry_run:
            log.info(f"    waiting {bump_wait}s for scenario to fire...")
            time.sleep(bump_wait)
        log.info(f"    restoring to {ts_to_str(current_ts)}")
        ok2 = pf_set_date(pf_task_id, current_ts, dry_run)
        return "bumped_and_restored" if ok2 else "error_on_restore"


# ─── Основний цикл ────────────────────────────────────────────────────────────

def run(dry_run: bool, task_delay: int, bump_wait: int, single: int | None):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT pf_task_id, mp_deal_id FROM created_tasks WHERE status='done' ORDER BY pf_task_id"
    ).fetchall()
    conn.close()

    if not rows:
        log.error("No tasks in DB — run create_missing_expense.py first")
        return

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info(f"=== trigger_payment_date.py [{mode}] ===")
    log.info(f"Tasks: {len(rows)}, delay: {task_delay}s, bump_wait: {bump_wait}s")

    stats = {"set_from_megaplan": 0, "bumped_and_restored": 0,
             "no_date_anywhere": 0, "error": 0, "error_on_restore": 0}

    for i, (pf_task_id, mp_deal_id) in enumerate(rows):
        if single and pf_task_id != single:
            continue

        log.info(f"\n[{i+1}/{len(rows)}]")
        result = process_task(pf_task_id, str(mp_deal_id), dry_run, bump_wait)
        stats[result] = stats.get(result, 0) + 1
        log.info(f"  → {result}")

        if i < len(rows) - 1 and not single:
            if not dry_run:
                log.info(f"  sleeping {task_delay}s...")
                time.sleep(task_delay)

    log.info(f"\n=== DONE ===")
    for k, v in stats.items():
        if v:
            log.info(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",      action="store_true")
    parser.add_argument("--delay",     type=int, default=TASK_DELAY,  help="Seconds between tasks (default 60)")
    parser.add_argument("--bump-wait", type=int, default=BUMP_WAIT,   help="Seconds between bump and restore (default 30)")
    parser.add_argument("--task",      type=int, default=None,        help="Process single Planfix task ID")
    args = parser.parse_args()

    run(
        dry_run=not args.live,
        task_delay=args.delay,
        bump_wait=args.bump_wait,
        single=args.task,
    )
