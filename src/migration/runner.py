"""Migration runner: orchestrates Megaplan → Planfix data transfer."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from src.megaplan.client import MegaplanClient, MegaplanError
from src.planfix.client import PlanfixClient, PlanfixError
from src.migration.transformer import transform_contractor, transform_contact

logger = logging.getLogger(__name__)

_MAPPING_FILE = os.path.join("data", "id_mapping.json")


@dataclass
class Stats:
    companies_created: int = 0
    companies_skipped: int = 0
    contacts_created: int = 0
    contacts_skipped: int = 0
    errors: int = 0
    dry_run_items: int = 0

    def summary(self) -> str:
        return (
            f"Companies: created={self.companies_created} skipped={self.companies_skipped} | "
            f"Contacts: created={self.contacts_created} skipped={self.contacts_skipped} | "
            f"Errors: {self.errors}"
            + (f" | DryRun: {self.dry_run_items}" if self.dry_run_items else "")
        )


class MigrationRunner:
    """Runs the full Megaplan → Planfix migration.

    ID Mapping:
        Stores megaplan_id → planfix_id in data/id_mapping.json.
        On subsequent runs already-migrated records are skipped.
        Delete id_mapping.json to force a full re-migration.

    Dry Run:
        When dry_run=True no write requests are sent to Planfix.
        The transformed payloads are logged at DEBUG level.
    """

    def __init__(
        self,
        megaplan: MegaplanClient,
        planfix: PlanfixClient,
        field_mapping: dict,
        *,
        dry_run: bool = True,
        only_active: bool = True,
    ):
        self.megaplan = megaplan
        self.planfix = planfix
        self.field_mapping = field_mapping
        self.dry_run = dry_run
        self.only_active = only_active
        self.stats = Stats()
        self._mapping: dict[str, dict[str, int]] = self._load_mapping()

    # ------------------------------------------------------------------
    # ID mapping persistence

    def _load_mapping(self) -> dict:
        try:
            with open(_MAPPING_FILE, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"companies": {}, "contacts": {}}

    def _save_mapping(self) -> None:
        os.makedirs("data", exist_ok=True)
        with open(_MAPPING_FILE, "w", encoding="utf-8") as f:
            json.dump(self._mapping, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Company migration

    def _migrate_single_company(self, contractor: dict) -> None:
        megaplan_id = str(contractor["id"])
        name = contractor.get("name") or contractor.get("humanName") or megaplan_id

        if megaplan_id in self._mapping["companies"]:
            logger.debug("Skip company %s (already in mapping)", name)
            self.stats.companies_skipped += 1
            return

        # Fetch detail + payer (best-effort)
        payer: dict | None = None
        try:
            detail = self.megaplan.get_contractor_detail(megaplan_id)
            payer_ref = detail.get("payer") or {}
            payer_id = payer_ref.get("id")
            if payer_id:
                payer = self.megaplan.get_payer(str(payer_id))
        except MegaplanError as exc:
            logger.warning("Could not fetch detail/payer for %s: %s", name, exc)

        planfix_data = transform_contractor(contractor, payer, self.field_mapping)

        if self.dry_run:
            logger.info("[DRY RUN] company → %s", name)
            logger.debug("[DRY RUN] payload: %s", json.dumps(planfix_data, ensure_ascii=False))
            self.stats.dry_run_items += 1
            return

        try:
            result = self.planfix.create_contact(planfix_data)
            planfix_id: int = result["id"]
            self._mapping["companies"][megaplan_id] = planfix_id
            self._save_mapping()
            self.stats.companies_created += 1
            logger.info("Company ✓  %-50s  Megaplan#%s → Planfix#%s", name, megaplan_id, planfix_id)
        except PlanfixError as exc:
            logger.error("Company ✗  %s — %s", name, exc)
            self.stats.errors += 1

    def migrate_companies(self) -> None:
        logger.info("=== Migrating companies (contractors) ===")
        total = self.megaplan.total_contractors(self.only_active)
        logger.info("Total contractors in Megaplan: %d", total)

        for contractor in self.megaplan.get_contractors(self.only_active):
            self._migrate_single_company(contractor)

        logger.info(
            "Companies done. created=%d skipped=%d errors=%d",
            self.stats.companies_created,
            self.stats.companies_skipped,
            self.stats.errors,
        )

    # ------------------------------------------------------------------
    # Contact migration

    def _migrate_single_contact(self, contact: dict) -> None:
        megaplan_id = str(contact["id"])
        first = contact.get("firstName", "")
        last = contact.get("lastName", "")
        name = f"{first} {last}".strip() or megaplan_id

        if megaplan_id in self._mapping["contacts"]:
            logger.debug("Skip contact %s (already in mapping)", name)
            self.stats.contacts_skipped += 1
            return

        # Resolve linked company in Planfix
        company_planfix_id: int | None = None
        contractor_ref = contact.get("contractor") or {}
        contractor_megaplan_id = str(contractor_ref.get("id") or "")
        if contractor_megaplan_id:
            company_planfix_id = self._mapping["companies"].get(contractor_megaplan_id)
            if not company_planfix_id:
                logger.debug(
                    "Contact %s: linked company %s not in mapping (not migrated yet?)",
                    name, contractor_megaplan_id,
                )

        planfix_data = transform_contact(contact, company_planfix_id, self.field_mapping)

        if self.dry_run:
            logger.info("[DRY RUN] contact  → %s", name)
            logger.debug("[DRY RUN] payload: %s", json.dumps(planfix_data, ensure_ascii=False))
            self.stats.dry_run_items += 1
            return

        try:
            result = self.planfix.create_contact(planfix_data)
            planfix_id: int = result["id"]
            self._mapping["contacts"][megaplan_id] = planfix_id
            self._save_mapping()
            self.stats.contacts_created += 1
            logger.info("Contact  ✓  %-50s  Megaplan#%s → Planfix#%s", name, megaplan_id, planfix_id)
        except PlanfixError as exc:
            logger.error("Contact  ✗  %s — %s", name, exc)
            self.stats.errors += 1

    def migrate_contacts(self) -> None:
        logger.info("=== Migrating contacts ===")
        total = self.megaplan.total_contacts()
        logger.info("Total contacts in Megaplan: %d", total)

        for contact in self.megaplan.get_contacts():
            self._migrate_single_contact(contact)

        logger.info(
            "Contacts done. created=%d skipped=%d errors=%d",
            self.stats.contacts_created,
            self.stats.contacts_skipped,
            self.stats.errors,
        )

    # ------------------------------------------------------------------
    # Full run

    def run(self) -> Stats:
        mode = "DRY RUN — no writes to Planfix" if self.dry_run else "LIVE"
        logger.info("Starting migration [%s]", mode)

        self.migrate_companies()
        self.migrate_contacts()

        logger.info("Migration finished. %s", self.stats.summary())
        return self.stats
