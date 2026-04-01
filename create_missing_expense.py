#!/usr/bin/env python3
"""
create_missing_expense.py — створення розхідних задач Planfix з угод Megaplan,
які не були перенесені автоматично.

Для кожної угоди Megaplan:
  1. Отримуємо дані з Megaplan
  2. Визначаємо шаблон: 15 (Конфеты) або 7691 (безнал)
  3. Знаходимо контакт-постачальника в Planfix по Megaplan contractor ID
  4. Створюємо задачу в Planfix з усіма полями
  5. Переносимо файли з полів, коментарів та опису
  6. Прив'язуємо як підзадачу до батьківської CRM задачі

Запуск:
    python create_missing_expense.py           # dry-run
    python create_missing_expense.py --live    # реальна запис
    python create_missing_expense.py --live --deal 29657  # одна угода
"""

import argparse
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Налаштування ─────────────────────────────────────────────────────────────

PLANFIX_HOST   = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN  = os.getenv("PLANFIX_TOKEN", "6ca06006655c6e695c495a4705609c85")
MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST", "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN",
    "NzZkODNiOGUwMWNlMGIyMTY5NzlkMDkzOGEzOWFlOGI1MGYyNTk0YThmOWJkYWE5ZDFlMGMyNGU2YWQ2ZWI1ZA")

PLANFIX_DELAY  = float(os.getenv("PLANFIX_DELAY",  "0.5"))
MEGAPLAN_DELAY = float(os.getenv("MEGAPLAN_DELAY", "0.3"))

DB_PATH = "create_missing_expense.db"

# ─── Маппінг: Planfix батьківська задача → список Megaplan розхідних угод ─────

DEALS_MAP = {
    7847: [29657, 29656, 29655, 29654, 29653, 29652, 29651, 29648, 29588],
    3737: [28976, 28952, 28951, 28949, 28948, 28942, 28941, 28937, 28936, 28933,
           28932, 28930, 28893, 28886, 28884, 28883, 28881, 28880, 28878, 28877, 28876],
    7917: [29507, 29506, 29504, 29483, 29209, 29208, 29207, 28911],
    3746: [29583, 29479, 29431, 29312, 29267, 29090],
    3754: [29539, 29505, 29502, 29501, 29500, 29499, 29498, 29497, 29496, 29495,
           29494, 29493, 29492, 29491, 29490, 29489, 29488, 29487, 29486, 29375,
           29374, 29373, 29372, 29263, 29246, 29245, 29244, 29243, 29242, 29241,
           29240, 29239, 29238, 29237, 29236, 29235, 29234, 29233, 29232, 29231,
           29230, 29229, 29228],
    3750: [29485, 29484, 29265, 29264, 28913, 28912],
    3752: [29569, 29266, 29195],
}

# Megaplan поля файлів → Planfix поля (Program 35)
FILE_FIELD_MAP = [
    ("Category1000083CustomFieldSkanOriginalaDogovora", 121019, "Скан оригинала договора"),
    ("Category1000083CustomFieldSkanDogovora",          120579, "Скан договора"),
    ("Category1000083CustomFieldAktSkan",               121023, "Акт (скан)"),
]

PF_HEADERS    = {"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"}
PF_HDR_UPLOAD = {"Authorization": f"Bearer {PLANFIX_TOKEN}"}
MP_HEADERS    = {"Authorization": f"Bearer {MEGAPLAN_TOKEN}"}

# ─── Логування ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("create_missing_expense.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── SQLite ───────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS created_tasks (
            mp_deal_id   TEXT PRIMARY KEY,
            pf_task_id   INTEGER,
            pf_parent_id INTEGER,
            template_id  INTEGER,
            status       TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            mp_file_id  TEXT PRIMARY KEY,
            pf_file_id  INTEGER,
            filename    TEXT,
            uploaded_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def get_created_task(conn, mp_deal_id: str):
    row = conn.execute(
        "SELECT pf_task_id, status FROM created_tasks WHERE mp_deal_id = ?",
        (mp_deal_id,)
    ).fetchone()
    return row


def mark_created(conn, mp_deal_id: str, pf_task_id: int, pf_parent_id: int, template_id: int, status: str):
    conn.execute(
        """INSERT OR REPLACE INTO created_tasks
           (mp_deal_id, pf_task_id, pf_parent_id, template_id, status)
           VALUES (?,?,?,?,?)""",
        (mp_deal_id, pf_task_id, pf_parent_id, template_id, status)
    )
    conn.commit()


def get_cached_pf_file(conn, mp_file_id: str):
    row = conn.execute(
        "SELECT pf_file_id FROM uploaded_files WHERE mp_file_id = ?", (mp_file_id,)
    ).fetchone()
    return row[0] if row else None


def cache_pf_file(conn, mp_file_id: str, pf_file_id: int, filename: str):
    conn.execute(
        "INSERT OR REPLACE INTO uploaded_files (mp_file_id, pf_file_id, filename) VALUES (?,?,?)",
        (mp_file_id, pf_file_id, filename),
    )
    conn.commit()

# ─── Megaplan API ─────────────────────────────────────────────────────────────

def mp_get_deal(deal_id) -> dict:
    r = requests.get(
        f"{MEGAPLAN_HOST}/api/v3/deal/{deal_id}",
        headers=MP_HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def mp_get_comments(deal_id) -> list:
    r = requests.get(
        f"{MEGAPLAN_HOST}/api/v3/deal/{deal_id}/comments",
        headers=MP_HEADERS, timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    if isinstance(data, list):
        return data
    return data.get("comments", [])


def mp_download_file(mp_file: dict) -> tuple:
    url = f"{MEGAPLAN_HOST}{mp_file['path']}"
    r = requests.get(url, headers=MP_HEADERS, timeout=120)
    r.raise_for_status()
    filename = mp_file.get("name", "file")
    mimetype = mp_file.get("mimeType", "application/octet-stream")
    return r.content, filename, mimetype

# ─── Planfix API ──────────────────────────────────────────────────────────────

def pf_find_contact_by_mp_id(mp_contractor_id) -> int | None:
    """Знайти Planfix контакт по Megaplan contractor ID (поле 128997)."""
    r = requests.post(
        f"{PLANFIX_HOST}/rest/contact/list",
        headers=PF_HEADERS,
        json={
            "offset": 0,
            "pageSize": 10,
            "fields": "id,name,isCompany",
            "filters": [{
                "type": 4101,
                "field": "128997",
                "operator": "equal",
                "value": str(mp_contractor_id),
            }]
        },
        timeout=30,
    )
    if r.status_code == 500:
        log.warning(f"  contact search 500 for mp_id={mp_contractor_id}, skip")
        return None
    r.raise_for_status()
    contacts = r.json().get("contacts", [])
    if contacts:
        return contacts[0].get("id")
    return None


def pf_upload_file(content: bytes, filename: str, mimetype: str) -> int:
    safe_filename = filename.encode("ascii", errors="replace").decode("ascii").replace("?", "_")
    files = {"file": (safe_filename, content, mimetype)}
    r = requests.post(
        f"{PLANFIX_HOST}/rest/file/",
        headers=PF_HDR_UPLOAD,
        files=files,
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        raise ValueError(f"Planfix file upload failed: {data}")
    return data["id"]


def pf_create_task(title: str, template_id: int, parent_id: int,
                   custom_fields: list, dry_run: bool) -> int | None:
    """Створити задачу в Planfix. Повертає pf_task_id."""
    if dry_run:
        log.info(f"  DRY CREATE task title={title!r} template={template_id} parent={parent_id}")
        log.info(f"    fields: {custom_fields}")
        return 0

    import json as _json

    # Створити задачу з усіма кастомними полями одразу (121011 — обов'язкове!)
    payload = {
        "name": title,
        "template": {"id": template_id},
        "parent": {"id": parent_id},
        "status": {"id": 101},
    }
    if custom_fields:
        payload["customFieldData"] = custom_fields
    log.info(f"  payload: {_json.dumps(payload, ensure_ascii=False)[:600]}")
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/",
        headers=PF_HEADERS,
        json=payload,
        timeout=30,
    )
    if r.status_code != 200:
        log.error(f"  CREATE task error {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    data = r.json()
    task_id = data.get("id") or data.get("task", {}).get("id")
    if not task_id:
        raise ValueError(f"No task id in response: {data}")
    log.info(f"  task created id={task_id}")

    return task_id


def pf_update_field_files(task_id: int, pf_field_id: int, file_ids: list, dry_run: bool):
    if dry_run:
        log.info(f"  DRY field {pf_field_id} <- {file_ids}")
        return
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{task_id}?silent=true",
        headers=PF_HEADERS,
        json={"customFieldData": [{"field": {"id": pf_field_id}, "value": file_ids}]},
        timeout=30,
    )
    if r.status_code == 400:
        log.warning(f"  400 task {task_id} field {pf_field_id}: {r.text[:100]}")
        return
    r.raise_for_status()


def pf_update_description_files(task_id: int, file_ids: list, dry_run: bool):
    if dry_run:
        log.info(f"  DRY description files <- {file_ids}")
        return
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{task_id}?silent=true",
        headers=PF_HEADERS,
        json={"files": [{"id": i} for i in file_ids]},
        timeout=30,
    )
    r.raise_for_status()


def pf_create_comment_with_files(task_id: int, text: str, file_ids: list, dry_run: bool):
    if dry_run:
        log.info(f"  DRY comment files={file_ids}")
        return
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{task_id}/comments/",
        headers=PF_HEADERS,
        json={"description": text, "files": [{"id": i} for i in file_ids]},
        timeout=30,
    )
    r.raise_for_status()

# ─── Завантаження файлу з дедублікацією ──────────────────────────────────────

def upload_mp_file(conn, mp_file: dict, dry_run: bool) -> int | None:
    mp_id = str(mp_file.get("id", ""))
    if not mp_id:
        return None

    cached = get_cached_pf_file(conn, mp_id)
    if cached:
        log.info(f"    cached {mp_file.get('name')} pf_id={cached}")
        return cached

    if dry_run:
        log.info(f"    DRY upload {mp_file.get('name')}")
        return 0

    try:
        content, filename, mimetype = mp_download_file(mp_file)
        time.sleep(MEGAPLAN_DELAY)
        pf_id = pf_upload_file(content, filename, mimetype)
        cache_pf_file(conn, mp_id, pf_id, filename)
        log.info(f"    uploaded {filename} pf_id={pf_id}")
        time.sleep(PLANFIX_DELAY)
        return pf_id
    except Exception as e:
        log.error(f"    upload error {mp_file.get('name')}: {e}")
        return None

# ─── Конвертація дати ─────────────────────────────────────────────────────────

TIP_PLATEZHA_MAP = {
    "Конфеты":       "Конфеты/Оплата на карту",
    "Оплата на карту": "Конфеты/Оплата на карту",
    "Безнал":        "Безнал",
    "Крипта":        "Крипта",
}


def parse_dateonly(mp_date) -> int | None:
    """Конвертує Megaplan DateOnly об'єкт або рядок в Unix timestamp (UTC).

    Megaplan DateOnly: {"contentType":"DateOnly","year":2025,"month":11,"day":12}
    ⚠️ Місяці 0-індексовані: month=0 → січень, month=11 → грудень
    """
    if not mp_date:
        return None
    if isinstance(mp_date, dict):
        if mp_date.get("contentType") == "DateOnly":
            year  = mp_date.get("year")
            month = mp_date.get("month")   # 0-indexed
            day   = mp_date.get("day")
            if year is None or month is None or day is None:
                return None
            try:
                dt = datetime(year, month + 1, day, tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                return None
        # Якщо dict з "value" — рядок дати
        mp_date = mp_date.get("value", "")
    if not mp_date:
        return None
    date_str = str(mp_date)[:10]
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

# ─── Обробка однієї угоди ─────────────────────────────────────────────────────

def process_deal(conn, mp_deal_id: int, pf_parent_id: int, dry_run: bool):
    log.info(f"\n--- Deal {mp_deal_id} -> parent task {pf_parent_id} ---")

    # Перевірити чи вже оброблено
    existing = get_created_task(conn, str(mp_deal_id))
    if existing and existing[1] == "done":
        log.info(f"  already done, pf_task={existing[0]}")
        return existing[0]

    # Отримати з Megaplan
    try:
        deal = mp_get_deal(mp_deal_id)
        time.sleep(MEGAPLAN_DELAY)
    except Exception as e:
        log.error(f"  Megaplan error: {e}")
        return None

    # Визначити шаблон
    tip = (
        deal.get("Category1000083CustomFieldTipPlatezha") or
        deal.get("TipPlatezha") or ""
    )
    if isinstance(tip, dict):
        tip = tip.get("value", "") or tip.get("name", "")
    tip_str = str(tip).strip()
    template_id = 15 if tip_str == "Конфеты" else 7691
    log.info(f"  TipPlatezha={tip_str!r} -> template={template_id}")

    # Назва задачі
    contractor = deal.get("contractor") or {}
    if isinstance(contractor, dict):
        contractor_name = contractor.get("name", "") or contractor.get("contentName", "")
        mp_contractor_id = contractor.get("id")
    else:
        contractor_name = ""
        mp_contractor_id = None

    deal_name = deal.get("name", "") or deal.get("contentName", "")
    title = deal_name or contractor_name or f"Розхід #{mp_deal_id}"
    log.info(f"  title={title!r} contractor_id={mp_contractor_id}")

    # ── Поля ──────────────────────────────────────────────────────────────────
    custom_fields = []

    def add(field_id, value):
        if value is not None and value != "" and value != 0:
            custom_fields.append({"field": {"id": field_id}, "value": value})

    # 132121 — Megaplan deal ID
    add(132121, str(mp_deal_id))

    # 130207 — Megaplan contractor ID
    if mp_contractor_id:
        add(130207, str(mp_contractor_id))

    # 130213 — Megaplan payer ID
    payer = deal.get("payer") or deal.get("responsible") or {}
    if isinstance(payer, dict) and payer.get("id"):
        add(130213, str(payer["id"]))

    # 130209 — ID рахунку (перший invoice якщо є)
    invoices = deal.get("invoices") or []
    if isinstance(invoices, list) and invoices:
        inv_id = invoices[0].get("id") if isinstance(invoices[0], dict) else invoices[0]
        if inv_id:
            add(130209, str(inv_id))

    # 121011 — ⭐ Дедлайн по оплаті (DateOnly, 0-indexed months)
    deadline_ts = parse_dateonly(deal.get("Category1000083CustomFieldDedlaynPoOplate"))
    if deadline_ts:
        add(121011, deadline_ts)
        log.info(f"  deadline={datetime.fromtimestamp(deadline_ts, tz=timezone.utc).strftime('%Y-%m-%d')}")

    # 138909 — Дата оплати (DateOnly, 0-indexed months)
    payment_ts = parse_dateonly(deal.get("Category1000083CustomFieldDataOplati"))
    if payment_ts:
        add(138909, payment_ts)
        log.info(f"  payment_date={datetime.fromtimestamp(payment_ts, tz=timezone.utc).strftime('%Y-%m-%d')}")

    # 120997 — Сумма + 138907 — Курс (з Money об'єкта CustomFieldSumma)
    summa_obj = deal.get("Category1000083CustomFieldSumma") or {}
    if isinstance(summa_obj, dict):
        summa_val = summa_obj.get("value")
        kurse_val = summa_obj.get("rate")
        if summa_val:
            add(120997, summa_val)
            log.info(f"  summa={summa_val}")
        if kurse_val:
            add(138907, kurse_val)
            log.info(f"  kurs={kurse_val}")

    # 120987 — Валюта (enum: USD/EUR/RUB/UAH/KZT/UZS/CNY)
    valuta = deal.get("Category1000083CustomFieldBuhgalteriyaValyuta") or ""
    if valuta:
        add(120987, valuta)

    # 120981 — Тип платежа (enum) — mapped from TipPlatezha
    tip_pf = TIP_PLATEZHA_MAP.get(tip_str)
    if tip_pf:
        add(120981, tip_pf)

    # 120999 — Страна куда отправляем
    strana = deal.get("Category1000083CustomFieldStranaKudaOtpravlyaemPlatezh") or ""
    if strana:
        add(120999, strana)

    # 120983 — Бренд клиента
    brend = deal.get("Category1000083CustomFieldBrend") or ""
    if brend:
        add(120983, brend)

    # 120995 — Статья расходов (enum)
    statya = deal.get("Category1000083CustomFieldStatyaRashodov") or ""
    if statya and statya != "ВЫБЕРИ СТАТЬЮ":
        add(120995, statya)

    # 121059 — Статус документа (enum)
    status_dok = deal.get("Category1000083CustomFieldStatusOplati1") or ""
    if status_dok:
        add(121059, status_dok)

    # Template 15 only fields
    if template_id == 15:
        # 120985 — Наше Юр лицо (enum)
        nashe_yur = deal.get("Category1000083CustomFieldBuhgalteriyaNasheYurLitso") or ""
        if nashe_yur:
            add(120985, nashe_yur)

        # 121001 — Перевод на карту? (checkbox bool)
        perevod = deal.get("Category1000083CustomFieldPerevodNaKartu")
        if perevod is True:
            add(121001, True)
            # Карткові поля
            fio = deal.get("Category1000083CustomFieldFamiliyaImyaPoluchatelyaNaLatinitseK") or ""
            if fio:
                add(121003, fio)
            karta = deal.get("Category1000083CustomFieldNomerKarti") or ""
            if karta:
                add(121005, karta)
            telefon = deal.get("Category1000083CustomFieldNomerTelefona") or ""
            if telefon:
                add(121007, telefon)
            bank = deal.get("Category1000083CustomFieldNazvanieBanka") or ""
            if bank:
                add(121009, bank)

    # 136609 / 136611 — Постачальник (Planfix контакт)
    supplier_field_id = 136609 if template_id == 15 else 136611
    if mp_contractor_id and not dry_run:
        pf_contact_id = pf_find_contact_by_mp_id(mp_contractor_id)
        time.sleep(PLANFIX_DELAY)
        if pf_contact_id:
            add(supplier_field_id, pf_contact_id)
            log.info(f"  supplier contact pf_id={pf_contact_id}")
        else:
            log.warning(f"  supplier contact not found for mp_id={mp_contractor_id}")

    # Створити задачу
    try:
        pf_task_id = pf_create_task(title, template_id, pf_parent_id, custom_fields, dry_run)
        time.sleep(PLANFIX_DELAY)
        log.info(f"  created pf_task={pf_task_id}")
    except Exception as e:
        log.error(f"  create task error: {e}")
        return None

    if dry_run:
        return 0

    # ── Файли з полів угоди ───────────────────────────────────────────────────
    for mp_field, pf_field_id, field_name in FILE_FIELD_MAP:
        mp_files = deal.get(mp_field)
        if not mp_files:
            continue
        log.info(f"  files field {field_name}: {len(mp_files)}")
        file_ids = []
        for f in mp_files:
            pf_id = upload_mp_file(conn, f, dry_run)
            if pf_id:
                file_ids.append(pf_id)
        if file_ids:
            pf_update_field_files(pf_task_id, pf_field_id, file_ids, dry_run)
            time.sleep(PLANFIX_DELAY)

    # ── Файли з коментарів ────────────────────────────────────────────────────
    if deal.get("attachesCountInComments", 0) > 0:
        try:
            comments = mp_get_comments(mp_deal_id)
            time.sleep(MEGAPLAN_DELAY)
        except Exception as e:
            log.error(f"  comments error: {e}")
            comments = []

        for comment in comments:
            attaches = comment.get("attaches", [])
            if not attaches:
                continue
            file_ids = []
            for f in attaches:
                pf_id = upload_mp_file(conn, f, dry_run)
                if pf_id:
                    file_ids.append(pf_id)
            if file_ids:
                date_val = (comment.get("timeCreated") or {}).get("value", "")[:10]
                original = re.sub(r"<[^>]+>", "", comment.get("content", "")).strip()
                text = f"Файли з Megaplan (коментар від {date_val})"
                if original:
                    text += "\n" + original
                pf_create_comment_with_files(pf_task_id, text, file_ids, dry_run)
                time.sleep(PLANFIX_DELAY)

    # ── Прямі вкладення угоди → в опис ───────────────────────────────────────
    direct_attaches = deal.get("attaches", [])
    if direct_attaches:
        log.info(f"  attaches: {len(direct_attaches)}")
        file_ids = []
        for f in direct_attaches:
            pf_id = upload_mp_file(conn, f, dry_run)
            if pf_id:
                file_ids.append(pf_id)
        if file_ids:
            pf_update_description_files(pf_task_id, file_ids, dry_run)
            time.sleep(PLANFIX_DELAY)

    mark_created(conn, str(mp_deal_id), pf_task_id, pf_parent_id, template_id, "done")
    log.info(f"  done pf_task={pf_task_id}")
    return pf_task_id

# ─── Основний цикл ────────────────────────────────────────────────────────────

def run(dry_run: bool, single_deal: int | None):
    conn = init_db(DB_PATH)
    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info(f"=== create_missing_expense.py [{mode}] ===")

    total = created = errors = 0

    for pf_parent_id, mp_deal_ids in DEALS_MAP.items():
        log.info(f"\n=== Parent task {pf_parent_id} ({len(mp_deal_ids)} deals) ===")

        for mp_deal_id in mp_deal_ids:
            if single_deal and mp_deal_id != single_deal:
                continue

            total += 1
            try:
                result = process_deal(conn, mp_deal_id, pf_parent_id, dry_run)
                if result is not None:
                    created += 1
                else:
                    errors += 1
            except Exception as e:
                log.error(f"Unhandled error deal {mp_deal_id}: {e}", exc_info=True)
                errors += 1

            time.sleep(PLANFIX_DELAY)

    log.info(f"\n=== DONE: total={total}, created={created}, errors={errors} ===")
    conn.close()

# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",  action="store_true", help="Реальна запис")
    parser.add_argument("--deal",  type=int, help="Обробити одну угоду")
    args = parser.parse_args()

    run(dry_run=not args.live, single_deal=args.deal)
