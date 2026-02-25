"""Transform Megaplan entities to Planfix REST API request bodies.

Megaplan                           Planfix
--------                           -------
contractor (ContractorCompany)  →  POST /contact/ {isCompany: true}
contractor (ContractorHuman)    →  POST /contact/ {isCompany: true}  (ИП)
contact    (Human)              →  POST /contact/ {isCompany: false}
payer      (PayerCompany …)     →  customFieldData on company contact
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers

def _clean_phone(number: str) -> str:
    """Strip non-digits and prefix with +."""
    digits = "".join(c for c in str(number) if c.isdigit())
    if not digits:
        return ""
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


def _phone_type(description: str, phone_type_map: dict) -> int:
    """Map Megaplan phone description → Planfix phone type int."""
    for key, ptype in phone_type_map.items():
        if key == "default":
            continue
        if key.lower() in description.lower():
            return ptype
    return phone_type_map.get("default", 4)


def extract_phones(phones_raw: list, phone_type_map: dict) -> list[dict]:
    """Convert Megaplan phones array → Planfix PhoneRequest array."""
    result = []
    for p in phones_raw or []:
        number = _clean_phone(p.get("number", ""))
        if not number:
            continue
        result.append(
            {
                "number": number,
                "type": _phone_type(p.get("description", ""), phone_type_map),
            }
        )
    return result


def extract_primary_email(emails_raw: list) -> str:
    """Return primary email address string or empty string."""
    if not emails_raw:
        return ""
    primary = next((e for e in emails_raw if e.get("isPrimary")), emails_raw[0])
    return (primary.get("address") or "").lower().strip()


def extract_additional_emails(emails_raw: list) -> list[str]:
    """Return non-primary email addresses."""
    if not emails_raw:
        return []
    return [
        e["address"].lower().strip()
        for e in emails_raw[1:]
        if e.get("address")
    ]


def _build_custom_field_data(
    kv: dict[str, str | None], mapping: dict
) -> list[dict]:
    """Build customFieldData from {mapping_key: value} pairs.

    If planfix_field_id is set → add to customFieldData.
    Otherwise the value is skipped here (caller appends to description).
    """
    result = []
    for key, value in kv.items():
        if not value:
            continue
        cfg = mapping.get(key, {})
        field_id = cfg.get("planfix_field_id")
        if field_id:
            result.append({"field": {"id": field_id}, "value": value})
    return result


def _append_to_description(base: str, kv: dict[str, str | None], mapping: dict) -> str:
    """Append values that have no Planfix field ID into the description text."""
    lines = []
    for key, value in kv.items():
        if not value:
            continue
        cfg = mapping.get(key, {})
        if not cfg.get("planfix_field_id"):
            label = cfg.get("label", key)
            lines.append(f"{label}: {value}")
    if lines:
        separator = "\n" + ("─" * 40) + "\n"
        return (base + separator + "\n".join(lines)).strip()
    return base.strip()


# ---------------------------------------------------------------------------
# Public transformers

def transform_contractor(
    contractor: dict,
    payer: dict | None,
    field_mapping: dict,
) -> dict:
    """Build Planfix ContactRequest for a company/ИП contractor.

    Args:
        contractor:    Raw contractor dict from Megaplan GET /contractor
        payer:         Raw payer dict from Megaplan GET /payer/{id} or None
        field_mapping: Parsed config/field_mapping.json
    """
    company_mapping = field_mapping.get("company", {})
    phone_type_map = field_mapping.get("phone_type_map", {"default": 4})

    description = contractor.get("comment") or ""

    # Gather payer details
    payer_kv: dict[str, str | None] = {}
    phones_raw: list = list(contractor.get("phones") or [])
    emails_raw: list = list(contractor.get("emails") or [])
    website: str = ""
    address: str = ""

    if payer:
        payer_kv = {
            "inn":                   payer.get("inn"),
            "kpp":                   payer.get("kpp"),
            "ogrn":                  payer.get("ogrn"),
            "okpo":                  payer.get("okpo"),
            "okved":                 payer.get("okved"),
            "legal_address":         payer.get("legalAddress"),
            "actual_address":        payer.get("actualAddress"),
            "bank_name":             payer.get("bank"),
            "bic":                   payer.get("bic"),
            "current_account":       payer.get("currentAccount"),
            "correspondent_account": payer.get("correspondentAccount"),
            "director_name":         payer.get("directorName"),
            "director_post":         payer.get("directorPost"),
        }
        # Prefer payer phones/emails when contractor has none
        if not phones_raw:
            phones_raw = list(payer.get("phones") or [])
        if not emails_raw:
            emails_raw = list(payer.get("emails") or [])
        website = payer.get("website") or ""
        address = payer.get("legalAddress") or payer.get("actualAddress") or ""

    # Megaplan metadata fields
    category_name = ""
    if isinstance(contractor.get("type"), dict):
        category_name = contractor["type"].get("name", "")

    meta_kv = {
        "megaplan_id":       str(contractor["id"]),
        "megaplan_type":     contractor.get("contentType", ""),
        "megaplan_category": category_name,
    }

    all_kv = {**payer_kv, **meta_kv}

    description = _append_to_description(description, all_kv, company_mapping)
    custom_field_data = _build_custom_field_data(all_kv, company_mapping)

    result: dict = {
        "isCompany": True,
        "name": contractor.get("name") or contractor.get("humanName") or "",
        "sourceObjectId": str(contractor["id"]),
        "description": description,
    }

    if address:
        result["address"] = address
    if website:
        result["site"] = website

    phones = extract_phones(phones_raw, phone_type_map)
    if phones:
        result["phones"] = phones

    primary_email = extract_primary_email(emails_raw)
    if primary_email:
        result["email"] = primary_email
        additional = extract_additional_emails(emails_raw)
        if additional:
            result["additionalEmailAddresses"] = additional

    if custom_field_data:
        result["customFieldData"] = custom_field_data

    return result


def transform_contact(
    contact: dict,
    company_planfix_id: int | None,
    field_mapping: dict,
) -> dict:
    """Build Planfix ContactRequest for a physical person contact.

    Args:
        contact:           Raw contact dict from Megaplan GET /contact
        company_planfix_id: Planfix ID of the linked company (or None)
        field_mapping:     Parsed config/field_mapping.json
    """
    phone_type_map = field_mapping.get("phone_type_map", {"default": 4})
    contact_mapping = field_mapping.get("contact", {})

    description = contact.get("comment") or ""

    meta_kv = {"megaplan_id": str(contact["id"])}
    description = _append_to_description(description, meta_kv, contact_mapping)
    custom_field_data = _build_custom_field_data(meta_kv, contact_mapping)

    result: dict = {
        "isCompany": False,
        "name": contact.get("firstName") or "",
        "midname": contact.get("middleName") or "",
        "lastname": contact.get("lastName") or "",
        "sourceObjectId": str(contact["id"]),
        "position": contact.get("position") or "",
        "description": description,
    }

    birthday = contact.get("birthday")
    if birthday:
        result["birthDate"] = {"date": birthday}

    phones = extract_phones(contact.get("phones") or [], phone_type_map)
    if phones:
        result["phones"] = phones

    emails_raw = contact.get("emails") or []
    primary_email = extract_primary_email(emails_raw)
    if primary_email:
        result["email"] = primary_email
        additional = extract_additional_emails(emails_raw)
        if additional:
            result["additionalEmailAddresses"] = additional

    if company_planfix_id:
        result["companies"] = [{"id": company_planfix_id}]

    if custom_field_data:
        result["customFieldData"] = custom_field_data

    return result
