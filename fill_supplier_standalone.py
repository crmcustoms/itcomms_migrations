#!/usr/bin/env python3
"""
fill_supplier_standalone.py — заполнение поля Поставщик в расходах Planfix.

Запуск:
    pip install requests
    python fill_supplier_standalone.py

Для реальной записи (по умолчанию dry-run):
    python fill_supplier_standalone.py --live
"""

import argparse
import logging
import sys
import time

import requests

# =============================================================================
# НАСТРОЙКИ — вставь свои значения
# =============================================================================

PLANFIX_HOST  = "https://itcomms.planfix.com"
PLANFIX_TOKEN = "6ca06006655c6e695c495a4705609c85"

# Задержка между запросами к API (секунд)
PLANFIX_DELAY = 1.0

# =============================================================================
# Константы (не менять)
# =============================================================================

# Поле в расходе — Megaplan-ID контрагента, записанный при миграции
CONTRACTOR_MEGAPLAN_ID_FIELD = 130207

# Поле в контакте/компании — Megaplan-ID, записанный при миграции
CONTACT_MEGAPLAN_ID_FIELD = 128997

TEMPLATES = {
    15:   {"name": "Прочие поставщики Конфеты", "supplier_field": 136609},
    7691: {"name": "Прочие поставщики безнал",  "supplier_field": 136611},
}

PAGE_SIZE = 100

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fill_supplier.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# Planfix API
# =============================================================================

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {PLANFIX_TOKEN}",
    "Content-Type": "application/json",
})


def _request(method, path, **kwargs):
    url = f"{PLANFIX_HOST.rstrip('/')}/rest/{path.lstrip('/')}"
    for attempt in range(4):
        time.sleep(PLANFIX_DELAY)
        resp = session.request(method, url, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            log.warning("Rate limit, жду %ss…", wait)
            time.sleep(wait)
            continue
        if resp.status_code in (200, 201, 202):
            return resp.json() if resp.text.strip() else {}
        raise RuntimeError(f"{method} {url} → {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError(f"{method} {url} — не удалось после retry")


def list_tasks(template_id, offset):
    return _request("POST", "/task/list", json={
        "offset": offset,
        "pageSize": PAGE_SIZE,
        "fields": "id,name,customFieldData",
        "filters": [{"type": 4008, "operator": "equal", "value": {"id": template_id}}],
    })


def find_contact_by_megaplan_id(megaplan_id):
    result = _request("POST", "/contact/list", json={
        "offset": 0,
        "pageSize": 10,
        "fields": "id,name,isCompany",
        "filters": [{
            "type": 5006,
            "field": {"id": CONTACT_MEGAPLAN_ID_FIELD},
            "operator": "equal",
            "value": megaplan_id,
        }],
    })
    contacts = result.get("contacts") or result.get("data") or []
    return contacts[0] if contacts else None


def update_task_supplier(task_id, supplier_field_id, contact_id):
    _request("POST", f"/task/{task_id}", json={
        "customFieldData": [
            {"field": {"id": supplier_field_id}, "value": contact_id}
        ]
    })


def get_field_value(task, field_id):
    for entry in task.get("customFieldData") or []:
        if (entry.get("field") or {}).get("id") == field_id:
            return (entry.get("stringValue") or entry.get("value") or "").strip()
    return ""


# =============================================================================
# Основная логика
# =============================================================================

def process_template(template_id, dry_run):
    cfg = TEMPLATES[template_id]
    supplier_field_id = cfg["supplier_field"]

    total = already_set = no_id = not_found = filled = errors = dry = 0

    log.info("=== Шаблон %d — %s ===", template_id, cfg["name"])
    offset = 0

    while True:
        result = list_tasks(template_id, offset)
        tasks = result.get("tasks") or result.get("data") or []
        if not tasks:
            break

        for task in tasks:
            total += 1
            task_id   = task["id"]
            task_name = task.get("name", "—")

            # Если Поставщик уже заполнен — пропускаем
            if get_field_value(task, supplier_field_id):
                already_set += 1
                log.debug("Task #%d %-50s → уже заполнен", task_id, task_name)
                continue

            # Читаем Megaplan-ID контрагента
            megaplan_id = get_field_value(task, CONTRACTOR_MEGAPLAN_ID_FIELD)
            if not megaplan_id:
                no_id += 1
                log.debug("Task #%d %-50s → нет ID контрагента", task_id, task_name)
                continue

            # Ищем контакт в Planfix по Megaplan-ID
            try:
                contact = find_contact_by_megaplan_id(megaplan_id)
            except Exception as e:
                log.error("Task #%d %-50s → ошибка поиска: %s", task_id, task_name, e)
                errors += 1
                continue

            if contact is None:
                log.warning("Task #%d %-50s → контакт megaplan_id=%r не найден",
                            task_id, task_name, megaplan_id)
                not_found += 1
                continue

            contact_id   = contact["id"]
            contact_name = contact.get("name", "—")

            # Записываем Поставщика
            if dry_run:
                log.info("[DRY RUN] Task #%d %-50s → Поставщик := contact:%d (%s)",
                         task_id, task_name, contact_id, contact_name)
                dry += 1
                continue

            try:
                update_task_supplier(task_id, supplier_field_id, contact_id)
                log.info("OK  Task #%d %-50s → Поставщик := contact:%d (%s)",
                         task_id, task_name, contact_id, contact_name)
                filled += 1
            except Exception as e:
                log.error("ERR Task #%d %-50s → %s", task_id, task_name, e)
                errors += 1

        if len(tasks) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    log.info(
        "Шаблон %d: total=%d already_set=%d no_id=%d not_found=%d filled=%d errors=%d%s",
        template_id, total, already_set, no_id, not_found, filled, errors,
        f" dry_run={dry}" if dry else "",
    )
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Реальная запись (без этого флага — только dry-run)")
    parser.add_argument("--template", type=int, choices=[15, 7691])
    args = parser.parse_args()

    dry_run = not args.live
    mode = "DRY RUN — записи не будет" if dry_run else "LIVE WRITE — пишем в Planfix"
    log.info("Запуск [%s]", mode)

    template_ids = [args.template] if args.template else list(TEMPLATES)
    total_errors = sum(process_template(tid, dry_run) for tid in template_ids)

    log.info("=== ГОТОВО ===")
    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
