#!/usr/bin/env python3
"""Entry point for Megaplan → Planfix migration.

Usage:
    # 1. Copy and fill environment file
    cp .env.example .env
    # edit .env — set tokens, set DRY_RUN=true first

    # 2. Install dependencies
    pip install -r requirements.txt

    # 3. Dry run (validates transform, no writes to Planfix)
    DRY_RUN=true python run_migration.py

    # 4. Real run
    DRY_RUN=false python run_migration.py

    # 5. To re-run from scratch, delete the ID mapping:
    rm data/id_mapping.json
"""

import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/migration.log", encoding="utf-8"),
        ],
    )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"ERROR: environment variable {name!r} is not set.", file=sys.stderr)
        print("Copy .env.example to .env and fill in the values.", file=sys.stderr)
        sys.exit(1)
    return value


def _load_field_mapping() -> dict:
    path = os.path.join("config", "field_mapping.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    _setup_logging()
    logger = logging.getLogger("migration")

    # Lazy imports so logging is configured first
    from src.megaplan.client import MegaplanClient
    from src.planfix.client import PlanfixClient
    from src.migration.runner import MigrationRunner

    megaplan_host  = _require_env("MEGAPLAN_HOST")
    megaplan_token = _require_env("MEGAPLAN_TOKEN")
    planfix_host   = _require_env("PLANFIX_HOST")
    planfix_token  = _require_env("PLANFIX_TOKEN")

    dry_run      = os.getenv("DRY_RUN", "true").lower() == "true"
    only_active  = os.getenv("MEGAPLAN_ONLY_ACTIVE", "true").lower() == "true"
    mp_delay     = float(os.getenv("MEGAPLAN_DELAY", "0.5"))
    pf_delay     = float(os.getenv("PLANFIX_DELAY", "1.0"))

    logger.info("Megaplan host : %s", megaplan_host)
    logger.info("Planfix host  : %s", planfix_host)
    logger.info("Dry run       : %s", dry_run)
    logger.info("Only active   : %s", only_active)

    megaplan = MegaplanClient(megaplan_host, megaplan_token, delay=mp_delay)
    planfix  = PlanfixClient(planfix_host, planfix_token, delay=pf_delay)

    # Verify Planfix connectivity before starting
    if not dry_run:
        logger.info("Checking Planfix connectivity…")
        if not planfix.ping():
            logger.error("Cannot reach Planfix API. Check PLANFIX_HOST and PLANFIX_TOKEN.")
            sys.exit(1)
        logger.info("Planfix API OK")

    field_mapping = _load_field_mapping()

    runner = MigrationRunner(
        megaplan=megaplan,
        planfix=planfix,
        field_mapping=field_mapping,
        dry_run=dry_run,
        only_active=only_active,
    )

    stats = runner.run()

    if stats.errors:
        logger.warning("Finished with %d error(s). Check logs/migration.log", stats.errors)
        sys.exit(1)


if __name__ == "__main__":
    main()
