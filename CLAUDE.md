# ITCOMMS — Planfix Migration Project

## Инфраструктура
- Сервер: Hetzner `65.109.8.78`, контейнер: `itcomms-scripts`
- Запуск скриптов: `docker exec itcomms-scripts python <script>.py`
- Логи: `docker exec itcomms-scripts tail -f <script>.log`
- Git: ветка `claude/review-migration-S7AZD`
- После изменений: `git pull origin claude/review-migration-S7AZD` на сервере, затем `docker restart itcomms-scripts`

---

## Planfix
- Host: `https://itcomms.planfix.com`
- Token: `6ca06006655c6e695c495a4705609c85`
- API: `Bearer <token>`, base path `/rest/`
- Списки задач: `POST /rest/task/list`
- Filter type `325` = фильтр по шаблону задачи
- Filter type `4101` = фильтр контактов по текстовому полю
- Поля запрашивать по ID в параметре `fields` (не через "customFieldData")
- Создание datatag записи: `POST /task/{id}/datatags/` с телом `{"dataTag":{"id":X},"items":[{"customFieldData":[...]}]}`

## Megaplan
- Host: `https://likhtman.megaplan.ru`
- Token: `NzZkODNiOGUwMWNlMGIyMTY5NzlkMDkzOGEzOWFlOGI1MGYyNTk0YThmOWJkYWE5ZDFlMGMyNGU2YWQ2ZWI1ZA`
- API: `/api/v3`, `Bearer <token>` — использовать строку как есть (не декодировать)
- GET /api/v3/invoice/{id} — счёт (поля: number, actualPaymentDate, sum, taxTotal, rows)
- GET /api/v3/invoice — список (параметры как JSON-строка в query, limit макс ~100)
- GET /api/v3/contractor/{id} — контрагент
- Megaplan доступен только с сервера Hetzner (не с локальной машины без VPN)

---

## Шаблоны Planfix

| ID   | Название                      | Тип            |
|------|-------------------------------|----------------|
| 11   | Текущие клиенты               | CRM сделка     |
| 15   | Прочие поставщики Конфеты     | Расход нал     |
| 21   | Invoice                       | Доход / счёт   |
| 4519 | Новый клиент                  | Лид            |
| 7691 | Прочие поставщики безнал      | Расход безнал  |
| 7824 | Рабочая задача                | Задача         |
| 7959 | Отпуск                        | HR             |

---

## Ключевые поля

### Общие (шаблоны 11, 15, 7691)
| ID     | Название              | Тип | Описание                        |
|--------|-----------------------|-----|---------------------------------|
| 132121 | ID сделки             | 0   | Megaplan deal ID                |
| 130207 | ID контрагента        | 0   | Megaplan contractor ID          |
| 130209 | ID счета              | 0   | Megaplan invoice ID             |
| 130213 | ID плательщика        | 0   | Megaplan payer ID               |
| 138909 | Дата оплаты           | 3   | Фактическая дата оплаты         |
| 136609 | Поставщик (конф.)     | 10  | Контакт, только template 15     |
| 136611 | Поставщик (безн.)     | 10  | Контакт, только template 7691   |

### Invoice (шаблон 21)
| ID     | Название              | Тип | Описание                        |
|--------|-----------------------|-----|---------------------------------|
| 128167 | Invoice Number        | 0   | Номер счёта                     |
| 128157 | Invoice Payment Date  | 3   | Фактическая дата оплаты         |
| 128161 | Payment Amount        | 1   | Сумма оплаты                    |
| 128159 | TAX                   | 23  | Агрегат аналитики TAX           |
| 132121 | ID сделки             | 0   | Megaplan ID                     |

### TAX аналитика
| ID    | Описание                                             |
|-------|------------------------------------------------------|
| 9611  | Datatag ID аналитики TAX                            |
| 58393 | Поле Tax Type (справочник 17495): 1=16%, 2=12%, 3=0% |

### Контакты Planfix
| ID     | Название       | Описание                  |
|--------|----------------|---------------------------|
| 128997 | ID Megaplan    | Megaplan ID в карточке    |

---

## Скрипты

| Файл                            | Назначение                                          | Статус  |
|---------------------------------|-----------------------------------------------------|---------|
| fill_supplier.py                | Заполняет Поставщика в шаблонах 15 и 7691           | ✅ Done |
| fill_supplier_from_megaplan.py  | То же, через Megaplan API для not_found кейсов     | ✅ Done |
| fill_supplier_standalone.py     | Standalone версия fill_supplier                     | ✅ Done |
| fill_payment_date.py            | Дата оплаты (поле 138909) из Megaplan в 15 и 7691  | ✅ Done |
| fill_invoice_payment.py         | Дата оплаты (поле 128157) + TAX в Invoice (21)     | ✅ Done |
| export_megaplan_invoices.py     | Выгрузка счетов Megaplan за период + матч Planfix  | ✅ Done |
| server.py                       | Control API на порту 8002                           | ✅ Live |

### server.py endpoints (http://65.109.8.78:8002)
- `GET  /health` — статус
- `POST /run/{script}` — запуск скрипта (`?dry_run=false` для live)
- `GET  /jobs/{id}` — статус джоба
- `POST /invoice/fill` — записать дату/TAX в Invoice задачу напрямую
- Auth: `Bearer b082217377346689381200f53d6cab7c`

---

## Статус миграции

| Этап                    | Статус      | Детали                                    |
|-------------------------|-------------|-------------------------------------------|
| fill_supplier           | ✅ Done     | 2042 заполнено, 701 not_found             |
| fill_payment_date       | ✅ Done     | Поле 138909 в шаблонах 15 + 7691         |
| fill_invoice_payment    | ✅ Done     | Поле 128157 + TAX datatag в шаблоне 21   |
| migrate_files           | 🔲 TODO     | Файлы из Megaplan сделок → Planfix задачи |

### migrate_files — план
Шаблоны: 11 (Текущие клиенты), 15 (Конфеты), 7691 (Безнал)
Поле 132121 = Megaplan deal ID → берём файлы из полей сделки + комментариев
Megaplan API для файлов: TBD (ждём структуру от пользователя)

---

## n8n webhook
- Invoice list: `GET https://n8n.crmcustoms.com/webhook/ebcec118-f1fc-4214-9586-a539fb92a0e4`
- Возвращает 1025 счетов с полями: property_invoice_name, property_invoiseidmp, property_dedlinepay, property_sum_fact, property_invoicedate

---

## Типы полей Planfix
| type | Описание              |
|------|-----------------------|
| 0    | Текст                 |
| 1    | Число                 |
| 3    | Дата                  |
| 7    | Чекбокс               |
| 8    | Enum (список)         |
| 9    | Справочник            |
| 10   | Контакт               |
| 21   | Файлы (вложения)      |
| 23   | Агрегат datatag       |
| 24   | Формула               |
| 26   | Агрегат подзадач      |
