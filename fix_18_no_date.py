#!/usr/bin/env python3
"""
fix_18_no_date.py — для задач без дати оплати копіює Дедлайн (121011) → Дата оплаты (138909).
Також тригерить сценарій Planfix (silent=false).

Запуск:
    python fix_18_no_date.py        # dry-run
    python fix_18_no_date.py --live
"""

import argparse
import logging
import os
import sqlite3
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PLANFIX_HOST  = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN = os.getenv("PLANFIX_TOKEN", "6ca06006655c6e695c495a4705609c85")
PF_HEADERS    = {"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"}

DEADLINE_FIELD     = 121011   # Дедлайн по оплате
PAYMENT_DATE_FIELD = 138909   # Дата оплаты
DB_PATH = "create_missing_expense.db"
DELAY   = 60  # секунд між задачами

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("fix_18_no_date.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def get_field_value(cfd: list, field_id: int):
    for entry in cfd:
        if (entry.get("field") or {}).get("id") == field_id:
            return entry.get("value")
    return None


def run(dry_run: bool):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT pf_task_id, mp_deal_id FROM created_tasks WHERE status='done' ORDER BY pf_task_id"
    ).fetchall()
    conn.close()

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info(f"=== fix_18_no_date.py [{mode}] — {len(rows)} задач у БД ===")

    processed = skipped = errors = 0

    for i, (pf_task_id, mp_deal_id) in enumerate(rows):
        # Отримуємо обидва поля з Planfix
        r = requests.get(
            f"{PLANFIX_HOST}/rest/task/{pf_task_id}",
            headers=PF_HEADERS,
            params={"fields": f"id,name,{DEADLINE_FIELD},{PAYMENT_DATE_FIELD}"},
            timeout=15,
        )
        if not r.ok:
            log.warning(f"[{pf_task_id}] GET error {r.status_code}")
            errors += 1
            continue

        data  = r.json()
        cfd   = data.get("customFieldData") or []
        name  = data.get("name", "")[:50]
        pay   = get_field_value(cfd, PAYMENT_DATE_FIELD)
        dead  = get_field_value(cfd, DEADLINE_FIELD)

        # Є дата оплати — пропускаємо
        if pay:
            skipped += 1
            continue

        # Немає дедлайну — нічого копіювати
        if not dead:
            log.warning(f"[{pf_task_id}] {name!r} — no deadline either, skip")
            skipped += 1
            continue

        from datetime import datetime, timezone
        dead_str = datetime.fromtimestamp(int(dead), tz=timezone.utc).strftime("%Y-%m-%d")
        log.info(f"[{pf_task_id}] {name!r} — deadline={dead_str} → set as payment date")

        if not dry_run:
            r2 = requests.post(
                f"{PLANFIX_HOST}/rest/task/{pf_task_id}?silent=false",
                headers=PF_HEADERS,
                json={"customFieldData": [{"field": {"id": PAYMENT_DATE_FIELD}, "value": int(dead)}]},
                timeout=15,
            )
            if not r2.ok:
                log.warning(f"  SET error {r2.status_code}: {r2.text[:100]}")
                errors += 1
                continue
            processed += 1
            if i < len(rows) - 1:
                log.info(f"  sleeping {DELAY}s...")
                time.sleep(DELAY)
        else:
            processed += 1

    log.info(f"\n=== DONE: set={processed}, skipped={skipped}, errors={errors} ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    run(dry_run=not args.live)
