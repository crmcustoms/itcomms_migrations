#!/usr/bin/env python3
"""
migrate_files.py — перенос файлов из Megaplan сделок в Planfix задачи.

Правила:
  - Файлы из полей сделки → в соответствующие поля задачи (по маппингу)
  - Файлы из комментариев → создаём новый комментарий в задаче с этими файлами
  - Прямые вложения сделки (attaches) → в описание задачи (files)
  - Дублей нет: SQLite DB хранит mp_file_id → pf_file_id и статус по каждой source

Шаблоны: 11 (Текущие клиенты), 15 (Конфеты нал), 7691 (Безнал)
Связь: поле 132121 (ID сделки) в Planfix ↔ deal.id в Megaplan

Запуск:
    python migrate_files.py              # dry-run, все шаблоны
    python migrate_files.py --live       # реальная запись
    python migrate_files.py --template 15            # один шаблон
    python migrate_files.py --live --limit 10        # test на 10 задачах
    python migrate_files.py --live --deal 29008      # одна сделка
"""

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Настройки ────────────────────────────────────────────────────────────────

PLANFIX_HOST   = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN  = os.getenv("PLANFIX_TOKEN", "6ca06006655c6e695c495a4705609c85")
MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST", "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN",
    "NzZkODNiOGUwMWNlMGIyMTY5NzlkMDkzOGEzOWFlOGI1MGYyNTk0YThmOWJkYWE5ZDFlMGMyNGU2YWQ2ZWI1ZA")

PLANFIX_DELAY  = float(os.getenv("PLANFIX_DELAY",  "0.4"))
MEGAPLAN_DELAY = float(os.getenv("MEGAPLAN_DELAY", "0.3"))

DB_PATH = "migrate_files.db"

# ─── Маппинг шаблонов ─────────────────────────────────────────────────────────

TEMPLATES = [11, 15, 7691]

# Megaplan program_id → list[tuple(mp_field, pf_field_id, pf_field_name)]
FILE_FIELD_MAP = {
    14: [  # Program 14 → шаблон 11
        ("Category1000061CustomFieldBrif",                  120571, "Бриф"),
        ("Category1000061CustomFieldSkanDogovora",          120579, "Скан договора"),
        ("Category1000061CustomFieldSkanPrilozheniyaIliDs", 120581, "Скан Приложения (или ДС)"),
        ("Category1000061CustomFieldAktVipolnennihRabot",   120585, "Акт выполненных работ"),
    ],
    35: [  # Program 35 → шаблоны 15 и 7691
        ("Category1000083CustomFieldSkanOriginalaDogovora", 121019, "Скан оригинала договора"),
        ("Category1000083CustomFieldSkanDogovora",          120579, "Скан договора"),
        ("Category1000083CustomFieldAktSkan",               121023, "Акт (скан)"),
    ],
}

PF_HEADERS     = {"Authorization": f"Bearer {PLANFIX_TOKEN}", "Content-Type": "application/json"}
PF_HDR_UPLOAD  = {"Authorization": f"Bearer {PLANFIX_TOKEN}"}  # без Content-Type для multipart
MP_HEADERS     = {"Authorization": f"Bearer {MEGAPLAN_TOKEN}"}

DEAL_ID_FIELD  = 132121  # поле "ID сделки" в Planfix

# ─── Логирование ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("migrate_files.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── SQLite ───────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            mp_file_id  TEXT PRIMARY KEY,
            pf_file_id  INTEGER,
            filename    TEXT,
            uploaded_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # source: 'field:120571', 'comment:310089', 'attaches'
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_sources (
            mp_deal_id  TEXT,
            source      TEXT,
            pf_task_id  INTEGER,
            status      TEXT,
            files_count INTEGER DEFAULT 0,
            processed_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (mp_deal_id, source)
        )
    """)
    conn.commit()
    return conn


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


def is_source_done(conn, mp_deal_id: str, source: str) -> bool:
    row = conn.execute(
        "SELECT status FROM processed_sources WHERE mp_deal_id=? AND source=?",
        (mp_deal_id, source),
    ).fetchone()
    return row is not None and row[0] == "done"


def mark_source(conn, mp_deal_id: str, source: str, pf_task_id: int, status: str, count: int):
    conn.execute(
        """INSERT OR REPLACE INTO processed_sources
           (mp_deal_id, source, pf_task_id, status, files_count)
           VALUES (?,?,?,?,?)""",
        (mp_deal_id, source, pf_task_id, status, count),
    )
    conn.commit()

# ─── Megaplan API ─────────────────────────────────────────────────────────────

def mp_get_deal(deal_id: str) -> dict:
    r = requests.get(
        f"{MEGAPLAN_HOST}/api/v3/deal/{deal_id}",
        headers=MP_HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def mp_get_comments(deal_id: str) -> list:
    """Получить все комментарии сделки (с вложениями включены в ответе)."""
    r = requests.get(
        f"{MEGAPLAN_HOST}/api/v3/deal/{deal_id}/comments",
        headers=MP_HEADERS, timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    # Ответ может быть списком или объектом с ключом comments
    if isinstance(data, list):
        return data
    return data.get("comments", [])


def mp_download_file(mp_file: dict) -> tuple[bytes, str, str]:
    """Скачать файл из Megaplan. Возвращает (content, filename, mimetype)."""
    url = f"{MEGAPLAN_HOST}{mp_file['path']}"
    r = requests.get(url, headers=MP_HEADERS, timeout=120)
    r.raise_for_status()
    filename = mp_file.get("name", "file")
    mimetype = mp_file.get("mimeType", "application/octet-stream")
    return r.content, filename, mimetype

# ─── Planfix API ──────────────────────────────────────────────────────────────

def pf_get_tasks(template_id: int, offset: int = 0, page_size: int = 100) -> dict:
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/list",
        headers=PF_HEADERS,
        json={
            "offset": offset,
            "pageSize": page_size,
            "filters": [{"type": 325, "operator": "equal", "value": template_id}],
            "fields": str(DEAL_ID_FIELD),
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def pf_upload_file(content: bytes, filename: str, mimetype: str) -> int:
    """Загрузить файл в Planfix. Возвращает pf_file_id."""
    # Sanitize filename: залишаємо тільки ASCII-сумісні символи
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


def pf_get_field_file_ids(task_id: int, pf_field_id: int) -> list[int]:
    """Получить ID файлов в кастомном поле типа 21."""
    r = requests.get(
        f"{PLANFIX_HOST}/rest/task/{task_id}",
        headers=PF_HEADERS,
        params={"fields": f"customField{pf_field_id}"},
        timeout=30,
    )
    r.raise_for_status()
    for field in r.json().get("customFieldData", []):
        if field.get("field", {}).get("id") == pf_field_id:
            val = field.get("value")
            if isinstance(val, list):
                # value може бути [int, ...] або [{"id": int}, ...]
                result = []
                for v in val:
                    if isinstance(v, int):
                        result.append(v)
                    elif isinstance(v, dict) and v.get("id"):
                        result.append(v["id"])
                return result
    return []


def pf_update_field_files(task_id: int, pf_field_id: int, file_ids: list[int], dry_run: bool):
    if dry_run:
        log.info(f"  DRY field {pf_field_id} ← {file_ids}")
        return
    # Правильний формат для type 21 (Files): customFieldData + value = [int, int, ...]
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{task_id}?silent=true",
        headers=PF_HEADERS,
        json={"customFieldData": [{"field": {"id": pf_field_id}, "value": file_ids}]},
        timeout=30,
    )
    if r.status_code == 400:
        log.warning(f"  ⚠ 400 task {task_id} field {pf_field_id} — поле не належить шаблону, пропуск")
        return
    r.raise_for_status()


def pf_get_description_file_ids(task_id: int) -> list[int]:
    """Получить файлы из описания задачи."""
    r = requests.get(
        f"{PLANFIX_HOST}/rest/task/{task_id}/files",
        headers=PF_HEADERS,
        params={"onlyFromDescription": "true"},
        timeout=30,
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return [f["id"] for f in files if f.get("id")]


def pf_update_description_files(task_id: int, file_ids: list[int], dry_run: bool):
    if dry_run:
        log.info(f"  DRY description files ← {file_ids}")
        return
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{task_id}?silent=true",
        headers=PF_HEADERS,
        json={"files": [{"id": i} for i in file_ids]},
        timeout=30,
    )
    r.raise_for_status()


def pf_create_comment_with_files(task_id: int, text: str, file_ids: list[int], dry_run: bool):
    if dry_run:
        log.info(f"  DRY comment files={file_ids} text={text[:60]}")
        return
    r = requests.post(
        f"{PLANFIX_HOST}/rest/task/{task_id}/comments/",
        headers=PF_HEADERS,
        json={
            "description": text,
            "files": [{"id": i} for i in file_ids],
        },
        timeout=30,
    )
    r.raise_for_status()

# ─── Загрузка файла с дедупликацией ──────────────────────────────────────────

def upload_mp_file(conn: sqlite3.Connection, mp_file: dict, dry_run: bool) -> int | None:
    """
    Загружает файл из Megaplan в Planfix с кешированием по mp_file_id.
    В dry_run возвращает фейковый ID 0.
    """
    mp_id = str(mp_file.get("id", ""))
    if not mp_id:
        log.warning(f"  Файл без id: {mp_file.get('name')}")
        return None

    # Проверить кеш
    cached = get_cached_pf_file(conn, mp_id)
    if cached:
        log.info(f"    ↩ {mp_file.get('name')} (cached pf_id={cached})")
        return cached

    if dry_run:
        log.info(f"    DRY upload: {mp_file.get('name')} ({mp_file.get('size', 0)} bytes)")
        return 0  # фейковый ID для dry_run

    try:
        content, filename, mimetype = mp_download_file(mp_file)
        time.sleep(MEGAPLAN_DELAY)
        pf_id = pf_upload_file(content, filename, mimetype)
        cache_pf_file(conn, mp_id, pf_id, filename)
        log.info(f"    ✓ {filename} → pf_file_id={pf_id}")
        time.sleep(PLANFIX_DELAY)
        return pf_id
    except Exception as e:
        log.error(f"    ✗ Ошибка загрузки {mp_file.get('name')}: {e}")
        return None

# ─── Обработка одной сделки ───────────────────────────────────────────────────

def process_deal(conn: sqlite3.Connection, pf_task_id: int, deal_id: str, dry_run: bool):
    log.info(f"Task {pf_task_id} | deal {deal_id}")

    try:
        deal = mp_get_deal(deal_id)
        time.sleep(MEGAPLAN_DELAY)
    except Exception as e:
        log.error(f"  ✗ Megaplan GET deal {deal_id}: {e}")
        return

    program_id = int(deal.get("program", {}).get("id", 0))
    field_map = FILE_FIELD_MAP.get(program_id, [])

    stats = {"fields": 0, "comments": 0, "attaches": 0, "skipped": 0}

    # ── 1. Файлы из полей сделки → в соответствующие поля задачи ─────────────
    for mp_field, pf_field_id, pf_field_name in field_map:
        source = f"field:{pf_field_id}"

        if is_source_done(conn, deal_id, source):
            log.info(f"  ⏭ {pf_field_name} — уже обработано")
            stats["skipped"] += 1
            continue

        mp_files = deal.get(mp_field)
        if not mp_files:
            mark_source(conn, deal_id, source, pf_task_id, "done", 0)
            continue

        log.info(f"  → {pf_field_name} ({len(mp_files)} файлов)")

        # Получить существующие файлы в поле (чтобы не затереть)
        existing_ids = pf_get_field_file_ids(pf_task_id, pf_field_id) if not dry_run else []
        time.sleep(PLANFIX_DELAY)

        new_ids = []
        for f in mp_files:
            pf_id = upload_mp_file(conn, f, dry_run)
            if pf_id is not None and pf_id != 0:
                new_ids.append(pf_id)

        if new_ids or dry_run:
            all_ids = list(dict.fromkeys(existing_ids + new_ids))  # дедуп порядок
            pf_update_field_files(pf_task_id, pf_field_id, all_ids, dry_run)
            time.sleep(PLANFIX_DELAY)

        mark_source(conn, deal_id, source, pf_task_id, "done", len(new_ids))
        stats["fields"] += len(new_ids)

    # ── 2. Файлы из комментариев → новый комментарий в задаче ────────────────
    if deal.get("attachesCountInComments", 0) > 0:
        try:
            comments = mp_get_comments(deal_id)
            time.sleep(MEGAPLAN_DELAY)
        except Exception as e:
            log.error(f"  ✗ Megaplan GET comments {deal_id}: {e}")
            comments = []

        for comment in comments:
            comment_id = str(comment.get("id", ""))
            attaches = comment.get("attaches", [])
            if not attaches:
                continue

            source = f"comment:{comment_id}"
            if is_source_done(conn, deal_id, source):
                log.info(f"  ⏭ comment {comment_id} — уже обработано")
                stats["skipped"] += 1
                continue

            log.info(f"  → comment {comment_id} ({len(attaches)} файлов)")

            new_ids = []
            for f in attaches:
                pf_id = upload_mp_file(conn, f, dry_run)
                if pf_id is not None and pf_id != 0:
                    new_ids.append(pf_id)

            if new_ids or dry_run:
                # Формируем текст комментария
                date_val = comment.get("timeCreated", {}).get("value", "")[:10]
                original = comment.get("content", "").strip()
                # Убираем HTML-теги для читабельности
                import re
                original_text = re.sub(r"<[^>]+>", "", original).strip()
                text_parts = [f"📎 Файлы из Megaplan (комментарий от {date_val})"]
                if original_text:
                    text_parts.append(original_text)
                comment_text = "\n".join(text_parts)

                pf_create_comment_with_files(pf_task_id, comment_text, new_ids, dry_run)
                time.sleep(PLANFIX_DELAY)

            mark_source(conn, deal_id, source, pf_task_id, "done", len(new_ids))
            stats["comments"] += len(new_ids)

    # ── 3. Прямые вложения сделки → в описание задачи ────────────────────────
    direct_attaches = deal.get("attaches", [])
    if direct_attaches:
        source = "attaches"
        if is_source_done(conn, deal_id, source):
            log.info(f"  ⏭ attaches — уже обработано")
            stats["skipped"] += 1
        else:
            log.info(f"  → attaches ({len(direct_attaches)} файлов)")

            existing_ids = pf_get_description_file_ids(pf_task_id) if not dry_run else []
            time.sleep(PLANFIX_DELAY)

            new_ids = []
            for f in direct_attaches:
                pf_id = upload_mp_file(conn, f, dry_run)
                if pf_id is not None and pf_id != 0:
                    new_ids.append(pf_id)

            if new_ids or dry_run:
                all_ids = list(dict.fromkeys(existing_ids + new_ids))
                pf_update_description_files(pf_task_id, all_ids, dry_run)
                time.sleep(PLANFIX_DELAY)

            mark_source(conn, deal_id, source, pf_task_id, "done", len(new_ids))
            stats["attaches"] += len(new_ids)

    total = stats["fields"] + stats["comments"] + stats["attaches"]
    log.info(
        f"  ✓ Done: fields={stats['fields']}, comments={stats['comments']}, "
        f"attaches={stats['attaches']}, skipped={stats['skipped']}, total={total}"
    )

# ─── Основной цикл ────────────────────────────────────────────────────────────

def run(templates: list[int], dry_run: bool, limit: int | None, single_deal: str | None):
    conn = init_db(DB_PATH)
    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info(f"=== migrate_files.py [{mode}] templates={templates} limit={limit} ===")

    total_tasks = 0
    total_with_files = 0
    total_skipped = 0

    for template_id in templates:
        log.info(f"\n── Шаблон {template_id} ──")
        offset = 0
        page_size = 100

        while True:
            try:
                resp = pf_get_tasks(template_id, offset, page_size)
            except Exception as e:
                log.error(f"Planfix task list error: {e}")
                break

            tasks = resp.get("tasks", [])
            if not tasks:
                break

            for task in tasks:
                if limit and total_tasks >= limit:
                    log.info(f"Достигнут лимит {limit} задач")
                    return

                task_id = task.get("id")
                deal_id = ""
                for cf in task.get("customFieldData", []):
                    if cf.get("field", {}).get("id") == DEAL_ID_FIELD:
                        deal_id = str(cf.get("value", "")).strip()
                        break

                if not deal_id:
                    log.debug(f"Task {task_id}: нет deal_id, пропуск")
                    total_skipped += 1
                    total_tasks += 1
                    continue

                if single_deal and deal_id != single_deal:
                    total_tasks += 1
                    continue

                total_tasks += 1
                total_with_files += 1

                try:
                    process_deal(conn, task_id, deal_id, dry_run)
                except Exception as e:
                    log.error(f"Ошибка task {task_id} deal {deal_id}: {e}", exc_info=True)

                time.sleep(PLANFIX_DELAY)

            if len(tasks) < page_size:
                break
            offset += page_size

    log.info(f"\n=== ИТОГО: tasks={total_tasks}, with_deal={total_with_files}, no_deal={total_skipped} ===")
    conn.close()

# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Megaplan files to Planfix")
    parser.add_argument("--live",     action="store_true", help="Реальная запись (без --live = dry-run)")
    parser.add_argument("--template", type=int, choices=[11, 15, 7691], help="Обработать один шаблон")
    parser.add_argument("--limit",    type=int, help="Ограничить кол-во задач")
    parser.add_argument("--deal",     type=str, help="Обработать одну конкретную сделку (mp deal id)")
    args = parser.parse_args()

    templates = [args.template] if args.template else TEMPLATES
    run(
        templates=templates,
        dry_run=not args.live,
        limit=args.limit,
        single_deal=args.deal,
    )
