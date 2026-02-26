#!/usr/bin/env python3
"""Заполнение полей Поставщик (конф.) / Поставщик (безн.) в расходах.

Алгоритм:
  1. Перебрать все задачи шаблона 15 (Конфеты) и 7691 (Безнал).
  2. Прочитать поле "ID контрагента" (id=130207) — там лежит Planfix-ID
     уже перенесённой компании или контакта.
  3. Проверить, что контрагент с таким ID существует в Planfix.
  4. Если поле Поставщик ещё не заполнено — записать.

Usage:
    # dry-run (только лог, без записи)
    python fill_supplier.py

    # реальная запись
    DRY_RUN=false python fill_supplier.py

    # только один шаблон
    python fill_supplier.py --template 15
    python fill_supplier.py --template 7691
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Константы

CONTRACTOR_ID_FIELD = 130207   # "ID контрагента"  type=0 (текст, хранит Planfix ID)

TEMPLATES: dict[int, dict] = {
    15:   {
        "name":           "Прочие поставщики Конфеты",
        "supplier_field": 136609,       # "Поставщик (конф.)"  type=10
    },
    7691: {
        "name":           "Прочие поставщики безнал",
        "supplier_field": 136611,       # "Поставщик (безн.)"  type=10
    },
}

PAGE_SIZE = 100

# ---------------------------------------------------------------------------
# Logging


def _setup_logging(level: str) -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/fill_supplier.log", encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stats


@dataclass
class Stats:
    total:    int = 0
    filled:   int = 0
    skipped_no_contractor: int = 0
    skipped_already_set:   int = 0
    skipped_contact_error: int = 0
    errors:   int = 0
    dry_run:  int = 0

    def summary(self) -> str:
        return (
            f"total={self.total} "
            f"filled={self.filled} "
            f"skip_no_contractor={self.skipped_no_contractor} "
            f"skip_already_set={self.skipped_already_set} "
            f"skip_contact_error={self.skipped_contact_error} "
            f"errors={self.errors}"
            + (f" dry_run={self.dry_run}" if self.dry_run else "")
        )


# ---------------------------------------------------------------------------
# Helpers


def _get_custom_field_value(task: dict, field_id: int) -> str:
    """Вернуть stringValue из customFieldData по field.id, или ''."""
    for entry in task.get("customFieldData") or []:
        if (entry.get("field") or {}).get("id") == field_id:
            return (entry.get("stringValue") or entry.get("value") or "").strip()
    return ""


def _iter_tasks(planfix, template_id: int) -> Iterator[dict]:
    """Постраничный перебор задач шаблона."""
    offset = 0
    while True:
        result = planfix.list_tasks(
            template_id=template_id,
            offset=offset,
            page_size=PAGE_SIZE,
            fields="id,name,customFieldData",
        )
        tasks = result.get("tasks") or result.get("data") or []
        if not tasks:
            break
        yield from tasks
        if len(tasks) < PAGE_SIZE:
            break
        offset += PAGE_SIZE


# ---------------------------------------------------------------------------
# Core


def fill_suppliers_for_template(planfix, template_id: int, *, dry_run: bool) -> Stats:
    cfg = TEMPLATES[template_id]
    tpl_name = cfg["name"]
    supplier_field_id = cfg["supplier_field"]

    stats = Stats()
    logger.info("=== Template %d — %s ===", template_id, tpl_name)

    for task in _iter_tasks(planfix, template_id):
        stats.total += 1
        task_id   = task["id"]
        task_name = task.get("name", "—")

        # 1. Читаем ID контрагента
        raw_id = _get_custom_field_value(task, CONTRACTOR_ID_FIELD)
        if not raw_id:
            logger.debug("Task #%d %-50s  → нет ID контрагента, пропуск", task_id, task_name)
            stats.skipped_no_contractor += 1
            continue

        try:
            contact_planfix_id = int(raw_id)
        except ValueError:
            logger.warning("Task #%d %-50s  → некорректный ID контрагента %r, пропуск",
                           task_id, task_name, raw_id)
            stats.skipped_contact_error += 1
            continue

        # 2. Проверяем, что поле Поставщик ещё не заполнено
        existing_supplier = _get_custom_field_value(task, supplier_field_id)
        if existing_supplier:
            logger.debug("Task #%d %-50s  → Поставщик уже задан (%s), пропуск",
                         task_id, task_name, existing_supplier)
            stats.skipped_already_set += 1
            continue

        # 3. Проверяем что контакт существует в Planfix
        try:
            planfix.get_contact(contact_planfix_id, fields="id,name,isCompany")
        except Exception as exc:
            logger.warning("Task #%d %-50s  → GET /contact/%d вернул ошибку: %s, пропуск",
                           task_id, task_name, contact_planfix_id, exc)
            stats.skipped_contact_error += 1
            continue

        # 4. Записываем
        if dry_run:
            logger.info("[DRY RUN] Task #%d %-50s  → Поставщик := contact:%d",
                        task_id, task_name, contact_planfix_id)
            stats.dry_run += 1
            continue

        try:
            planfix.update_task(task_id, {
                "customFieldData": [
                    {
                        "field": {"id": supplier_field_id},
                        "value": contact_planfix_id,
                    }
                ]
            })
            logger.info("OK  Task #%d %-50s  → Поставщик := contact:%d",
                        task_id, task_name, contact_planfix_id)
            stats.filled += 1
        except Exception as exc:
            logger.error("ERR Task #%d %-50s  → %s", task_id, task_name, exc)
            stats.errors += 1

    logger.info("Template %d done: %s", template_id, stats.summary())
    return stats


# ---------------------------------------------------------------------------
# Entry point


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"ERROR: env variable {name!r} is not set. Copy .env.example to .env",
              file=sys.stderr)
        sys.exit(1)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", type=int, choices=[15, 7691],
                        help="Обработать только один шаблон (по умолчанию — оба)")
    args = parser.parse_args()

    log_level = os.getenv("LOG_LEVEL", "INFO")
    _setup_logging(log_level)

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    pf_delay = float(os.getenv("PLANFIX_DELAY", "1.0"))

    planfix_host  = _require_env("PLANFIX_HOST")
    planfix_token = _require_env("PLANFIX_TOKEN")

    from src.planfix.client import PlanfixClient
    planfix = PlanfixClient(planfix_host, planfix_token, delay=pf_delay)

    mode = "DRY RUN — записи не будет" if dry_run else "LIVE WRITE"
    logger.info("fill_supplier.py запущен [%s]", mode)

    template_ids = [args.template] if args.template else list(TEMPLATES)

    total_stats = Stats()
    for tid in template_ids:
        s = fill_suppliers_for_template(planfix, tid, dry_run=dry_run)
        total_stats.total    += s.total
        total_stats.filled   += s.filled
        total_stats.skipped_no_contractor += s.skipped_no_contractor
        total_stats.skipped_already_set   += s.skipped_already_set
        total_stats.skipped_contact_error += s.skipped_contact_error
        total_stats.errors   += s.errors
        total_stats.dry_run  += s.dry_run

    logger.info("=== ИТОГО: %s ===", total_stats.summary())

    if total_stats.errors:
        logger.warning("Завершено с ошибками. Подробности: logs/fill_supplier.log")
        sys.exit(1)


if __name__ == "__main__":
    main()
