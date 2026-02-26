#!/usr/bin/env python3
"""Заполнение поля Поставщик в расходных сделках Planfix.

Логика (только внутри Planfix, Megaplan не нужен):
  1. Перебрать все задачи шаблонов 15 (Конфеты) и 7691 (Безнал) постранично.
  2. Если поле Поставщик уже заполнено — пропустить.
  3. Прочитать поле "ID контрагента" (id=130207) — там Megaplan-ID контрагента,
     записанный при миграции.
  4. Найти контакт/компанию в Planfix у которого поле "ID Megaplan" (id=128997)
     содержит это же значение.
  5. Если найден — записать в поле Поставщик.

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
from dataclasses import dataclass
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Константы

# Поле в расходе — Megaplan-ID контрагента, записанный при миграции
CONTRACTOR_MEGAPLAN_ID_FIELD = 130207   # "ID контрагента", type=0 (текст)

# Поле в контакте/компании — Megaplan-ID, записанный при миграции
CONTACT_MEGAPLAN_ID_FIELD = 128997      # "ID Megaplan", type=0 (текст)

TEMPLATES: dict[int, dict] = {
    15: {
        "name":           "Прочие поставщики Конфеты",
        "supplier_field": 136609,   # "Поставщик (конф.)", type=10
    },
    7691: {
        "name":           "Прочие поставщики безнал",
        "supplier_field": 136611,   # "Поставщик (безн.)", type=10
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
    total:                    int = 0
    skipped_already_set:      int = 0
    skipped_no_megaplan_id:   int = 0
    skipped_contact_not_found:int = 0
    filled:                   int = 0
    dry_run:                  int = 0
    errors:                   int = 0

    def summary(self) -> str:
        return (
            f"total={self.total} "
            f"already_set={self.skipped_already_set} "
            f"no_megaplan_id={self.skipped_no_megaplan_id} "
            f"contact_not_found={self.skipped_contact_not_found} "
            f"filled={self.filled} "
            f"errors={self.errors}"
            + (f" dry_run={self.dry_run}" if self.dry_run else "")
        )


# ---------------------------------------------------------------------------
# Helpers


def _get_field_value(task: dict, field_id: int) -> str:
    """Вернуть stringValue кастомного поля по его id, или ''."""
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
    supplier_field_id = cfg["supplier_field"]
    stats = Stats()

    logger.info("=== Шаблон %d — %s ===", template_id, cfg["name"])

    for task in _iter_tasks(planfix, template_id):
        stats.total += 1
        task_id   = task["id"]
        task_name = task.get("name", "—")

        # 1. Если Поставщик уже заполнен — пропускаем
        existing = _get_field_value(task, supplier_field_id)
        if existing:
            logger.debug("Task #%d %-50s  → Поставщик уже заполнен, пропуск", task_id, task_name)
            stats.skipped_already_set += 1
            continue

        # 2. Читаем Megaplan-ID контрагента из расхода
        megaplan_id = _get_field_value(task, CONTRACTOR_MEGAPLAN_ID_FIELD)
        if not megaplan_id:
            logger.debug("Task #%d %-50s  → нет ID контрагента, пропуск", task_id, task_name)
            stats.skipped_no_megaplan_id += 1
            continue

        # 3. Ищем контакт/компанию в Planfix по полю "ID Megaplan"
        try:
            contact = planfix.find_contact_by_custom_field(
                field_id=CONTACT_MEGAPLAN_ID_FIELD,
                value=megaplan_id,
                fields="id,name,isCompany",
            )
        except Exception as exc:
            logger.error("Task #%d %-50s  → ошибка поиска контакта: %s", task_id, task_name, exc)
            stats.errors += 1
            continue

        if contact is None:
            logger.warning(
                "Task #%d %-50s  → контакт с ID Megaplan=%r не найден в Planfix",
                task_id, task_name, megaplan_id,
            )
            stats.skipped_contact_not_found += 1
            continue

        contact_id   = contact["id"]
        contact_name = contact.get("name", "—")

        # 4. Записываем в поле Поставщик
        if dry_run:
            logger.info(
                "[DRY RUN] Task #%d %-50s  → Поставщик := contact:%d (%s)",
                task_id, task_name, contact_id, contact_name,
            )
            stats.dry_run += 1
            continue

        try:
            planfix.update_task(task_id, {
                "customFieldData": [
                    {
                        "field": {"id": supplier_field_id},
                        "value": contact_id,
                    }
                ]
            })
            logger.info(
                "OK  Task #%d %-50s  → Поставщик := contact:%d (%s)",
                task_id, task_name, contact_id, contact_name,
            )
            stats.filled += 1
        except Exception as exc:
            logger.error("ERR Task #%d %-50s  → %s", task_id, task_name, exc)
            stats.errors += 1

    logger.info("Шаблон %d готово: %s", template_id, stats.summary())
    return stats


# ---------------------------------------------------------------------------
# Entry point


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"ERROR: env variable {name!r} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--template", type=int, choices=[15, 7691],
                        help="Обработать только один шаблон (по умолчанию — оба)")
    args = parser.parse_args()

    log_level = os.getenv("LOG_LEVEL", "INFO")
    _setup_logging(log_level)

    dry_run       = os.getenv("DRY_RUN", "true").lower() == "true"
    pf_delay      = float(os.getenv("PLANFIX_DELAY", "1.0"))
    planfix_host  = _require_env("PLANFIX_HOST")
    planfix_token = _require_env("PLANFIX_TOKEN")

    from src.planfix.client import PlanfixClient
    planfix = PlanfixClient(planfix_host, planfix_token, delay=pf_delay)

    mode = "DRY RUN — записи не будет" if dry_run else "LIVE WRITE"
    logger.info("fill_supplier.py запущен [%s]", mode)

    template_ids = [args.template] if args.template else list(TEMPLATES)

    total = Stats()
    for tid in template_ids:
        s = fill_suppliers_for_template(planfix, tid, dry_run=dry_run)
        total.total                     += s.total
        total.skipped_already_set       += s.skipped_already_set
        total.skipped_no_megaplan_id    += s.skipped_no_megaplan_id
        total.skipped_contact_not_found += s.skipped_contact_not_found
        total.filled                    += s.filled
        total.dry_run                   += s.dry_run
        total.errors                    += s.errors

    logger.info("=== ИТОГО: %s ===", total.summary())

    if total.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
