"""Deterministic, scope-aware reconciliation informed by Asmit's data-pilot engine."""
from __future__ import annotations

from datetime import date
import math

STATUSES = {"agrees", "conflict", "possible_conflict", "resolved_by_timeline", "not_comparable", "unknown"}


def _day(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def compare_claims(left, right):
    """Compare only same identity/fact/scope/unit/semantics and defensible time windows."""
    ids = [left.get("claim_id"), right.get("claim_id")]
    base = {"fact": left.get("fact"), "evidence_ids": ids}
    if left.get("fact") != right.get("fact"):
        return {**base, "status": "not_comparable", "reason": "Different fact types."}
    if left.get("identity_key") and right.get("identity_key") and left["identity_key"] != right["identity_key"]:
        return {**base, "status": "not_comparable", "reason": "Property/building identity differs; quarantine the cross-identity comparison.", "identity_conflict": True}
    if left.get("scope_type") != right.get("scope_type") or left.get("scope_id") != right.get("scope_id"):
        return {**base, "status": "not_comparable", "reason": "Evidence scopes differ (parcel, building, floor, suite or image)."}
    if left.get("unit") != right.get("unit") or left.get("measurement_semantics") != right.get("measurement_semantics"):
        return {**base, "status": "not_comparable", "reason": "Units or measurement meanings differ; values are not directly comparable."}
    if left.get("value") is None or right.get("value") is None:
        return {**base, "status": "unknown", "reason": "At least one source does not establish a value."}
    if left.get("fact") == "suite_occupancy" and left.get("value") != right.get("value"):
        scan_date = _day(left.get("observed_at"))
        tenant_start = _day(right.get("extra", {}).get("move_in_date"))
        if scan_date is None or tenant_start is None:
            return {**base, "status": "possible_conflict", "reason": "Occupancy differs, but a verified scan capture date or tenant start date is missing."}
        if scan_date < tenant_start:
            return {**base, "status": "resolved_by_timeline", "reason": "The scan predates the recorded tenant move-in; it does not establish later occupancy."}
        tenant_end = _day(right.get("extra", {}).get("move_out_date"))
        if tenant_end and scan_date > tenant_end:
            return {**base, "status": "resolved_by_timeline", "reason": "The scan postdates the recorded tenant move-out."}
        if left.get("extra", {}).get("date_semantics") != "verified_capture_date":
            return {**base, "status": "possible_conflict", "reason": "Attachment provenance is not a verified camera capture date."}
    lv, rv = left.get("value"), right.get("value")
    if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
        if isinstance(lv, bool) or isinstance(rv, bool) or not (math.isfinite(float(lv)) and math.isfinite(float(rv))):
            return {**base, "status": "unknown", "reason": "A numeric value is invalid or non-finite."}
        equal = math.isclose(float(lv), float(rv), rel_tol=0.01, abs_tol=1e-6)
    else:
        equal = str(lv).strip().casefold() == str(rv).strip().casefold()
    return {**base, "status": "agrees" if equal else "conflict",
            "reason": "Comparable claims agree." if equal else "Comparable claims differ; human review is required."}


def reconcile_claims(claims):
    """Build pairwise verdicts for claims sharing a fact; missing claims are never conflicts."""
    verdicts = []
    by_fact = {}
    for claim in claims:
        by_fact.setdefault(claim.get("fact"), []).append(claim)
    for group in by_fact.values():
        # Duplicate records within one provider are not independent evidence.
        # Only produce comparisons across sources to keep the dossier actionable.
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                if left.get("source") == right.get("source"):
                    continue
                verdicts.append(compare_claims(left, right))
    return verdicts
