#!/usr/bin/env python3
"""Итерация 2 — проверка миграции контрагентов.

Алгоритм:
  1. Перебрать все расходы (шаблоны 15 и 7691) в Planfix.
  2. Собрать уникальные Planfix-ID контрагентов из поля 130207 ("ID контрагента").
  3. Для каждого ID: GET /contact/{id} — проверить существование и полноту данных.
  4. Вывести отчёт: что нашлось, чего нет, у кого пустое описание.

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
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Константы

CONTRACTOR_ID_FIELD = 130207   # "ID контрагента" — Planfix-ID перенесённого контрагента

TEMPLATES: dict[int, str] = {
    15:   "Прочие поставщики Конфеты",
    7691: "Прочие поставщики безнал",
}

# Ключевые маркеры в description компании (из миграции реквизитов)
DESCRIPTION_MARKERS = ["ИНН", "КПП", "ОГРН", "Банк", "БИК", "Р/с", "К/с", "Директор"]

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
    description: str = ""
    found: bool = False
    error: str = ""
    # расходы, которые ссылаются на этого контрагента
    expense_task_ids: list[int] = field(default_factory=list)

    def description_markers_found(self) -> list[str]:
        """Какие маркеры реквизитов есть в description."""
        return [m for m in DESCRIPTION_MARKERS if m in (self.description or "")]

    def description_markers_missing(self) -> list[str]:
        """Какие маркеры реквизитов отсутствуют в description."""
        return [m for m in DESCRIPTION_MARKERS if m not in (self.description or "")]


@dataclass
class Stats:
    total_expenses: int = 0
    expenses_no_contractor: int = 0
    unique_contractors: int = 0
    found_ok: int = 0
    not_found: int = 0
    found_empty_description: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"расходы={self.total_expenses} "
            f"(без_контрагента={self.expenses_no_contractor}) | "
            f"уникальных_контрагентов={self.unique_contractors} | "
            f"найдено={self.found_ok} "
            f"не_найдено={self.not_found} "
            f"пустое_описание={self.found_empty_description} "
            f"ошибки={self.errors}"
        )


# ---------------------------------------------------------------------------
# Helpers


def _get_custom_field_value(task: dict, field_id: int) -> str:
    for entry in task.get("customFieldData") or []:
        if (entry.get("field") or {}).get("id") == field_id:
            return (entry.get("stringValue") or entry.get("value") or "").strip()
    return ""


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
    """Шаг 1: перебрать расходы, собрать уникальные ID контрагентов."""
    contractors: dict[int, ContactInfo] = {}
    stats = Stats()

    for template_id in template_ids:
        tpl_name = TEMPLATES[template_id]
        logger.info("Перебираю расходы: шаблон %d — %s", template_id, tpl_name)

        for task in _iter_tasks(planfix, template_id):
            stats.total_expenses += 1
            task_id = task["id"]

            raw_id = _get_custom_field_value(task, CONTRACTOR_ID_FIELD)
            if not raw_id:
                stats.expenses_no_contractor += 1
                logger.debug("Расход #%d — нет ID контрагента", task_id)
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
        "Собрано: расходов=%d (без контрагента=%d), уникальных контрагентов=%d",
        stats.total_expenses, stats.expenses_no_contractor, stats.unique_contractors,
    )
    return contractors, stats


def verify_contractors(planfix, contractors: dict[int, ContactInfo], stats: Stats) -> None:
    """Шаг 2: для каждого ID проверить контакт в Planfix."""
    logger.info("Проверяю %d контрагентов в Planfix…", len(contractors))

    for i, (contractor_id, info) in enumerate(contractors.items(), 1):
        logger.debug("[%d/%d] GET /contact/%d", i, len(contractors), contractor_id)
        try:
            contact = planfix.get_contact(
                contractor_id,
                fields="id,name,isCompany,description",
            )
            info.found = True
            info.name = contact.get("name") or ""
            info.is_company = bool(contact.get("isCompany"))
            info.description = contact.get("description") or ""

            if not info.description.strip():
                stats.found_empty_description += 1
                logger.warning(
                    "  contact:%d  %-50s  → описание ПУСТО (реквизиты не мигрировали?)",
                    contractor_id, info.name,
                )
            else:
                missing = info.description_markers_missing()
                if missing:
                    logger.info(
                        "  contact:%d  %-50s  → описание есть, отсутствуют маркеры: %s",
                        contractor_id, info.name, ", ".join(missing),
                    )
                else:
                    logger.debug(
                        "  contact:%d  %-50s  → OK, все маркеры найдены",
                        contractor_id, info.name,
                    )
            stats.found_ok += 1

        except Exception as exc:
            info.found = False
            info.error = str(exc)
            stats.not_found += 1
            logger.warning(
                "  contact:%d  → НЕ НАЙДЕН в Planfix: %s", contractor_id, exc
            )


def print_report(contractors: dict[int, ContactInfo], stats: Stats) -> None:
    """Итоговый отчёт в stdout."""
    print("\n" + "=" * 70)
    print("ИТОГОВЫЙ ОТЧЁТ ПРОВЕРКИ МИГРАЦИИ КОНТРАГЕНТОВ")
    print("=" * 70)
    print(f"  Всего расходов обработано:      {stats.total_expenses}")
    print(f"  Расходов без ID контрагента:    {stats.expenses_no_contractor}")
    print(f"  Уникальных контрагентов:        {stats.unique_contractors}")
    print(f"  Найдено в Planfix:              {stats.found_ok}")
    print(f"  НЕ найдено (ошибка GET):        {stats.not_found}")
    print(f"  Найдено, но описание пустое:    {stats.found_empty_description}")
    print()

    not_found = [(cid, info) for cid, info in contractors.items() if not info.found]
    if not_found:
        print(f"--- НЕ НАЙДЕНЫ В PLANFIX ({len(not_found)}) ---")
        for cid, info in not_found:
            expense_list = ", ".join(f"#{t}" for t in info.expense_task_ids[:5])
            if len(info.expense_task_ids) > 5:
                expense_list += f" … (+{len(info.expense_task_ids) - 5})"
            print(f"  contact:{cid:<8}  расходы: {expense_list}")
            print(f"            ошибка: {info.error}")
        print()

    empty_desc = [(cid, info) for cid, info in contractors.items()
                  if info.found and not info.description.strip()]
    if empty_desc:
        print(f"--- ПУСТОЕ ОПИСАНИЕ (реквизиты не мигрировали?) ({len(empty_desc)}) ---")
        for cid, info in empty_desc:
            kind = "Компания" if info.is_company else "Контакт"
            print(f"  contact:{cid:<8}  {kind}  {info.name}")
        print()

    partial_desc = [(cid, info) for cid, info in contractors.items()
                    if info.found and info.description.strip() and info.description_markers_missing()]
    if partial_desc:
        print(f"--- НЕПОЛНОЕ ОПИСАНИЕ (часть маркеров отсутствует) ({len(partial_desc)}) ---")
        for cid, info in partial_desc:
            kind = "Компания" if info.is_company else "Контакт"
            missing = ", ".join(info.description_markers_missing())
            print(f"  contact:{cid:<8}  {kind}  {info.name}")
            print(f"            нет маркеров: {missing}")
        print()

    print("=" * 70)


def save_csv(contractors: dict[int, ContactInfo], csv_path: str) -> None:
    """Сохранить полный отчёт в CSV."""
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "planfix_id", "name", "is_company", "found",
            "description_empty", "markers_found", "markers_missing",
            "expense_count", "expense_task_ids", "error",
        ])
        for cid, info in contractors.items():
            writer.writerow([
                cid,
                info.name,
                info.is_company,
                info.found,
                not bool(info.description.strip()) if info.found else "",
                "|".join(info.description_markers_found()),
                "|".join(info.description_markers_missing()),
                len(info.expense_task_ids),
                "|".join(str(t) for t in info.expense_task_ids),
                info.error,
            ])
    logger.info("CSV сохранён: %s", csv_path)


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
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--template", type=int, choices=[15, 7691],
                        help="Проверить только один шаблон расходов (по умолчанию — оба)")
    parser.add_argument("--csv", metavar="FILE",
                        help="Сохранить результат в CSV-файл")
    args = parser.parse_args()

    log_level = os.getenv("LOG_LEVEL", "INFO")
    _setup_logging(log_level)

    pf_delay = float(os.getenv("PLANFIX_DELAY", "1.0"))
    planfix_host  = _require_env("PLANFIX_HOST")
    planfix_token = _require_env("PLANFIX_TOKEN")

    from src.planfix.client import PlanfixClient
    planfix = PlanfixClient(planfix_host, planfix_token, delay=pf_delay)

    logger.info("verify_companies.py — проверка миграции контрагентов (только Planfix)")

    template_ids = [args.template] if args.template else list(TEMPLATES)

    # Шаг 1 — собрать ID из расходов
    contractors, stats = collect_contractor_ids(planfix, template_ids)

    # Шаг 2 — проверить каждый контакт в Planfix
    verify_contractors(planfix, contractors, stats)

    # Отчёт
    print_report(contractors, stats)
    logger.info("=== ИТОГО: %s ===", stats.summary())

    if args.csv:
        save_csv(contractors, args.csv)

    # Выход с ошибкой если есть проблемы
    if stats.not_found or stats.found_empty_description:
        sys.exit(1)


if __name__ == "__main__":
    main()
