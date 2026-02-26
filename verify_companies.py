#!/usr/bin/env python3
"""Итерация 2 — проверка миграции контрагентов.

Алгоритм:
  1. Перебрать все расходы (шаблоны 15 и 7691) в Planfix.
  2. Собрать уникальные Planfix-ID из поля 130207 ("ID контрагента").
  3. Для каждого ID: GET /contact/{id} с customFieldData —
     проверить, что ключевые поля заполнены.
  4. Вывести отчёт: что нашлось, чего нет, какие поля пустые.

К Megaplan не обращаемся — всё внутри Planfix.

Usage:
    python verify_companies.py
    python verify_companies.py --template 15
    python verify_companies.py --template 7691
    python verify_companies.py --csv report.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Константы

CONTRACTOR_ID_FIELD = 130207   # "ID контрагента" в расходах — Planfix-ID контакта

TEMPLATES: dict[int, str] = {
    15:   "Прочие поставщики Конфеты",
    7691: "Прочие поставщики безнал",
}

PAGE_SIZE = 100

# Загружаем маппинг полей контактов
_MAPPING_PATH = Path(__file__).parent / "config" / "field_mapping.json"
_mapping = json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))
CONTACT_FIELDS: dict[str, dict] = _mapping["contact_fields"]

# Поля, которые проверяем на заполненность при аудите миграции
CHECK_KEYS: list[str] = _mapping["_fields_to_check_migration"]
# {id -> key} для быстрого поиска при разборе customFieldData
FIELD_ID_TO_KEY: dict[int, str] = {
    CONTACT_FIELDS[k]["id"]: k for k in CHECK_KEYS
}

# ---------------------------------------------------------------------------
# Logging


def _setup_logging(level: str) -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/verify_companies.log", encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses


@dataclass
class ContactInfo:
    planfix_id: int
    name: str = ""
    is_company: bool = False
    found: bool = False
    error: str = ""
    filled_fields: list[str] = field(default_factory=list)   # ключи заполненных полей
    empty_fields: list[str] = field(default_factory=list)     # ключи пустых полей
    expense_task_ids: list[int] = field(default_factory=list)


@dataclass
class Stats:
    total_expenses: int = 0
    expenses_no_contractor: int = 0
    unique_contractors: int = 0
    found_ok: int = 0
    not_found: int = 0
    has_empty_fields: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"расходы={self.total_expenses} "
            f"(без_контрагента={self.expenses_no_contractor}) | "
            f"уникальных={self.unique_contractors} | "
            f"найдено={self.found_ok} "
            f"не_найдено={self.not_found} "
            f"с_пустыми_полями={self.has_empty_fields}"
        )


# ---------------------------------------------------------------------------
# Helpers


def _get_expense_field(task: dict, field_id: int) -> str:
    for entry in task.get("customFieldData") or []:
        if (entry.get("field") or {}).get("id") == field_id:
            return (entry.get("stringValue") or entry.get("value") or "").strip()
    return ""


def _parse_contact_fields(contact: dict) -> tuple[list[str], list[str]]:
    """Вернуть (filled_keys, empty_keys) по CHECK_KEYS из customFieldData контакта."""
    values: dict[str, str] = {}
    for entry in contact.get("customFieldData") or []:
        fid = (entry.get("field") or {}).get("id")
        if fid in FIELD_ID_TO_KEY:
            key = FIELD_ID_TO_KEY[fid]
            values[key] = (entry.get("stringValue") or entry.get("value") or "").strip()

    filled = [k for k in CHECK_KEYS if values.get(k)]
    empty  = [k for k in CHECK_KEYS if not values.get(k)]
    return filled, empty


def _iter_tasks(planfix, template_id: int) -> Iterator[dict]:
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


def collect_contractor_ids(planfix, template_ids: list[int]) -> tuple[dict[int, ContactInfo], Stats]:
    contractors: dict[int, ContactInfo] = {}
    stats = Stats()

    for template_id in template_ids:
        logger.info("Перебираю расходы: шаблон %d — %s", template_id, TEMPLATES[template_id])
        for task in _iter_tasks(planfix, template_id):
            stats.total_expenses += 1
            task_id = task["id"]

            raw_id = _get_expense_field(task, CONTRACTOR_ID_FIELD)
            if not raw_id:
                stats.expenses_no_contractor += 1
                continue

            try:
                contractor_id = int(raw_id)
            except ValueError:
                stats.expenses_no_contractor += 1
                logger.warning("Расход #%d — некорректный ID контрагента: %r", task_id, raw_id)
                continue

            if contractor_id not in contractors:
                contractors[contractor_id] = ContactInfo(planfix_id=contractor_id)
            contractors[contractor_id].expense_task_ids.append(task_id)

    stats.unique_contractors = len(contractors)
    logger.info(
        "Расходов=%d (без контрагента=%d), уникальных контрагентов=%d",
        stats.total_expenses, stats.expenses_no_contractor, stats.unique_contractors,
    )
    return contractors, stats


def verify_contractors(planfix, contractors: dict[int, ContactInfo], stats: Stats) -> None:
    logger.info("Проверяю %d контрагентов в Planfix…", len(contractors))

    for i, (contractor_id, info) in enumerate(contractors.items(), 1):
        logger.debug("[%d/%d] GET /contact/%d", i, len(contractors), contractor_id)
        try:
            contact = planfix.get_contact(
                contractor_id,
                fields="id,name,isCompany,customFieldData",
            )
            info.found = True
            info.name = contact.get("name") or ""
            info.is_company = bool(contact.get("isCompany"))
            info.filled_fields, info.empty_fields = _parse_contact_fields(contact)

            if info.empty_fields:
                stats.has_empty_fields += 1
                empty_names = [CONTACT_FIELDS[k]["name"] for k in info.empty_fields]
                logger.warning(
                    "contact:%d  %-45s  → пустые поля: %s",
                    contractor_id, info.name, ", ".join(empty_names),
                )
            else:
                logger.debug("contact:%d  %-45s  → OK", contractor_id, info.name)

            stats.found_ok += 1

        except Exception as exc:
            info.found = False
            info.error = str(exc)
            stats.not_found += 1
            logger.warning("contact:%d  → НЕ НАЙДЕН: %s", contractor_id, exc)


def print_report(contractors: dict[int, ContactInfo], stats: Stats) -> None:
    print("\n" + "=" * 70)
    print("ОТЧЁТ ПРОВЕРКИ МИГРАЦИИ КОНТРАГЕНТОВ")
    print("=" * 70)
    print(f"  Всего расходов:                {stats.total_expenses}")
    print(f"  Расходов без ID контрагента:   {stats.expenses_no_contractor}")
    print(f"  Уникальных контрагентов:       {stats.unique_contractors}")
    print(f"  Найдено в Planfix:             {stats.found_ok}")
    print(f"  НЕ найдено (ошибка GET):       {stats.not_found}")
    print(f"  Найдено, но есть пустые поля:  {stats.has_empty_fields}")
    print()

    not_found = [(cid, info) for cid, info in contractors.items() if not info.found]
    if not_found:
        print(f"--- НЕ НАЙДЕНЫ В PLANFIX ({len(not_found)}) ---")
        for cid, info in not_found:
            expenses = ", ".join(f"#{t}" for t in info.expense_task_ids[:5])
            if len(info.expense_task_ids) > 5:
                expenses += f" (+{len(info.expense_task_ids) - 5})"
            print(f"  contact:{cid:<8}  расходы: {expenses}")
        print()

    with_empty = [(cid, info) for cid, info in contractors.items()
                  if info.found and info.empty_fields]
    if with_empty:
        print(f"--- ПУСТЫЕ ПОЛЯ (миграция неполная?) ({len(with_empty)}) ---")
        for cid, info in with_empty:
            kind = "Компания" if info.is_company else "Контакт"
            empty_names = [CONTACT_FIELDS[k]["name"] for k in info.empty_fields]
            print(f"  contact:{cid:<8}  {kind}  {info.name}")
            print(f"            нет: {', '.join(empty_names)}")
        print()

    print("=" * 70)


def save_csv(contractors: dict[int, ContactInfo], csv_path: str) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        header = ["planfix_id", "name", "is_company", "found", "expense_count"]
        header += [CONTACT_FIELDS[k]["name"] for k in CHECK_KEYS]
        header += ["error"]
        writer.writerow(header)

        for cid, info in contractors.items():
            row = [cid, info.name, info.is_company, info.found, len(info.expense_task_ids)]
            row += ["OK" if k in info.filled_fields else "ПУСТО" for k in CHECK_KEYS]
            row += [info.error]
            writer.writerow(row)
    logger.info("CSV сохранён: %s", csv_path)


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
                        help="Только один шаблон (по умолчанию — оба)")
    parser.add_argument("--csv", metavar="FILE", help="Сохранить отчёт в CSV")
    args = parser.parse_args()

    log_level = os.getenv("LOG_LEVEL", "INFO")
    _setup_logging(log_level)

    planfix_host  = _require_env("PLANFIX_HOST")
    planfix_token = _require_env("PLANFIX_TOKEN")
    pf_delay = float(os.getenv("PLANFIX_DELAY", "1.0"))

    from src.planfix.client import PlanfixClient
    planfix = PlanfixClient(planfix_host, planfix_token, delay=pf_delay)

    logger.info("verify_companies.py — аудит миграции (только Planfix)")

    template_ids = [args.template] if args.template else list(TEMPLATES)

    contractors, stats = collect_contractor_ids(planfix, template_ids)
    verify_contractors(planfix, contractors, stats)
    print_report(contractors, stats)
    logger.info("=== ИТОГО: %s ===", stats.summary())

    if args.csv:
        save_csv(contractors, args.csv)

    if stats.not_found or stats.has_empty_fields:
        sys.exit(1)


if __name__ == "__main__":
    main()
