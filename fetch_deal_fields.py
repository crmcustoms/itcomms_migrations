#!/usr/bin/env python3
"""
fetch_deal_fields.py — виводить всі custom field ключі Megaplan угоди Program 35.
Потрібно один раз запустити щоб дізнатись точні назви полів.

Запуск:
    python fetch_deal_fields.py
    python fetch_deal_fields.py --deal 29657
"""

import argparse
import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

MEGAPLAN_HOST  = os.getenv("MEGAPLAN_HOST",  "https://likhtman.megaplan.ru")
MEGAPLAN_TOKEN = os.getenv("MEGAPLAN_TOKEN",
    "NzZkODNiOGUwMWNlMGIyMTY5NzlkMDkzOGEzOWFlOGI1MGYyNTk0YThmOWJkYWE5ZDFlMGMyNGU2YWQ2ZWI1ZA")

MP_HEADERS = {"Authorization": f"Bearer {MEGAPLAN_TOKEN}"}

INTERESTING_FIELDS = [
    "Category1000083CustomFieldSummaOplaty",
    "Category1000083CustomFieldSumma",
    "Category1000083CustomFieldSummaKotoruyuPlatim",
    "Category1000083CustomFieldValyuta",
    "Category1000083CustomFieldTipPlatezha",
    "Category1000083CustomFieldBrendKlienta",
    "Category1000083CustomFieldBrend",
    "Category1000083CustomFieldStatyaRaskhodov",
    "Category1000083CustomFieldStatyaRashodov",
    "Category1000083CustomFieldKursNaMomentOplaty",
    "Category1000083CustomFieldKurs",
    "Category1000083CustomFieldDataOplati",
    "Category1000083CustomFieldDedlaynPoOplate",
    "Category1000083CustomFieldStranaKudaOtpravlyaemPlatezh",
    "Category1000083CustomFieldStatusOplati1",
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal", type=int, default=29657)
    args = parser.parse_args()

    r = requests.get(
        f"{MEGAPLAN_HOST}/api/v3/deal/{args.deal}",
        headers=MP_HEADERS, timeout=30,
    )
    r.raise_for_status()
    deal = r.json().get("data", {})

    print(f"\n=== Deal {args.deal}: ALL KEYS ({len(deal)} total) ===")
    all_keys = sorted(deal.keys())
    for k in all_keys:
        print(f"  {k}")

    print(f"\n=== Custom fields (Category1000083...) with VALUES ===")
    for k, v in deal.items():
        if "CustomField" in k:
            print(f"  {k} = {json.dumps(v, ensure_ascii=False)[:200]}")

    print(f"\n=== Standard fields ===")
    for f in ["name", "contentName", "contractor", "payer", "invoices",
              "attaches", "attachesCountInComments", "sum", "currency",
              "actualPaymentDate", "paymentDate"]:
        if f in deal:
            v = deal[f]
            if isinstance(v, dict):
                print(f"  {f} = {json.dumps(v, ensure_ascii=False)[:200]}")
            else:
                print(f"  {f} = {v!r}")

    print(f"\n=== Checking known interesting fields ===")
    for f in INTERESTING_FIELDS:
        key = f.replace("Category1000083", "")
        exists = f in deal
        val = deal.get(f, "NOT_FOUND")
        print(f"  {'✅' if exists else '❌'} {key} = {json.dumps(val, ensure_ascii=False)[:100] if exists else 'NOT FOUND'}")

if __name__ == "__main__":
    main()
