"""Validated Stage 1 -> Stage 2 identity and evidence contracts."""
from __future__ import annotations

import math
import re


def _identifier(value, name, *, required=False):
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a string or integer identifier.")
    text = str(value).strip()
    if not text or len(text) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", text):
        raise ValueError(f"{name} is malformed.")
    return text


def _coordinate(value, name, low, high):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric.") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} is out of range.")
    return number


def validate_handoff(payload: dict) -> dict:
    """Validate and preserve the selected-listing identity without merging ID kinds."""
    if not isinstance(payload, dict):
        raise ValueError("Stage 2 handoff must be an object.")
    selected = payload.get("selected_listing")
    if not isinstance(selected, dict):
        raise ValueError("selected_listing is required.")
    property_id = _identifier(selected.get("property_id"), "property_id", required=True)
    listing_id = _identifier(selected.get("listing_id"), "listing_id")
    space_id = _identifier(selected.get("space_id"), "space_id")
    selection_id = _identifier(selected.get("stable_id"), "stable_id")
    facts = payload.get("property_facts") or {}
    if not isinstance(facts, dict):
        raise ValueError("property_facts must be an object.")
    facts_id = _identifier(facts.get("property_id"), "property_facts.property_id")
    if facts_id is not None and facts_id != property_id:
        raise ValueError("CoStar property identity differs between selection and property_facts.")
    address = selected.get("address") or facts.get("address")
    if not isinstance(address, str) or not address.strip() or len(address) > 300:
        raise ValueError("A selected property address is required.")
    latitude = _coordinate(facts.get("latitude", selected.get("latitude")), "latitude", -90, 90)
    longitude = _coordinate(facts.get("longitude", selected.get("longitude")), "longitude", -180, 180)
    intake = payload.get("intake") or {}
    if not isinstance(intake, dict):
        raise ValueError("intake must be an object.")
    # Preserve full original records. IDs remain separately named and typed as strings.
    normalized_selected = dict(selected)
    normalized_selected.update({"property_id": property_id, "listing_id": listing_id,
                                "space_id": space_id, "stable_id": selection_id,
                                "address": address.strip()})
    normalized_facts = dict(facts)
    normalized_facts.update({"property_id": facts_id or property_id,
                             "latitude": latitude, "longitude": longitude})
    return {"handoff_id": _identifier(payload.get("handoff_id"), "handoff_id") or "direct-fixture",
            "created_at": payload.get("created_at"), "stage": 2,
            "intake": dict(intake), "selected_listing": normalized_selected,
            "property_facts": normalized_facts,
            "property_facts_status": payload.get("property_facts_status", "unknown")}


def evidence_claim(*, claim_id, fact, value, unit=None, source, source_record_id,
                   scope_type, scope_id, semantics=None, observed_at=None,
                   retrieved_at=None, evidence_ref=None, quality=None,
                   identity_key=None, extra=None):
    """Common normalized evidence representation; null values remain unknown."""
    return {"claim_id": str(claim_id), "fact": str(fact), "value": value, "unit": unit,
            "source": str(source), "source_record_id": str(source_record_id),
            "scope_type": str(scope_type), "scope_id": str(scope_id),
            "measurement_semantics": semantics, "observed_at": observed_at,
            "retrieved_at": retrieved_at, "evidence_ref": evidence_ref,
            "quality": quality, "identity_key": identity_key,
            "status": "unknown" if value is None else "unassessed",
            "extra": extra or {}}
