"""Typed, deterministic MAXIMUS Stage 1 policy, normalization and ranking."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
import re
import json
import urllib.request
import urllib.error

from costar_service import PROPERTY_TYPES, RATE_BASIS

CITY_CODES = {"Houston, TX": ("Houston", "TX"), "Los Angeles, CA": ("Los Angeles", "CA")}
LEASE_HORIZONS = (3, 5, 10)
SALE_THRESHOLD_USD = Decimal("900000")
SALE_STRETCH = Decimal("1.10")


@dataclass
class RentNormalization:
    status: str
    raw_low: float | None
    raw_high: float | None
    raw_basis_code: int | None
    raw_basis: str | None
    currency: str | None
    area_min_sqft: float | None
    area_max_sqft: float | None
    monthly_low_usd: float | None
    monthly_high_usd: float | None
    annual_low_usd: float | None
    annual_high_usd: float | None
    known_additional_charges: list[dict[str, Any]]
    total_occupancy_cost_status: str
    missing_fields: list[str]


@dataclass
class EvidenceRecord:
    claim_id: str
    subject_id: str
    source: str
    source_reference: str
    source_url: str | None
    observed_at: str | None
    retrieved_at: str
    value: Any
    unit: str | None
    evidence_type: str
    independence: str
    contradiction_status: str


@dataclass
class EvidenceAssessment:
    listing_id: str
    records: list[EvidenceRecord]
    contradictions: list[str]
    retrieved_at: str


def ingest_evidence(listing_id: str, evidence_records: list[dict]) -> EvidenceAssessment:
    """Stable ingestion boundary for future evidence modules; no team adapters yet."""
    normalized = []
    contradictions = []
    now = datetime.now(timezone.utc).isoformat()
    for item in evidence_records:
        required = ("claim_id", "subject_id", "source", "source_reference", "retrieved_at", "evidence_type")
        if not isinstance(item, dict) or any(not item.get(k) for k in required):
            raise ValueError("Evidence record is missing a required identity/provenance field.")
        if str(item["subject_id"]) != str(listing_id):
            raise ValueError("Evidence record subject does not match the selected listing.")
        if item.get("contradiction_status") == "conflicting":
            contradictions.append(str(item["claim_id"]))
        normalized.append(EvidenceRecord(
            claim_id=str(item["claim_id"]), subject_id=str(item["subject_id"]),
            source=str(item["source"]), source_reference=str(item["source_reference"]),
            source_url=item.get("source_url"), observed_at=item.get("observed_at"),
            retrieved_at=str(item["retrieved_at"]), value=item.get("value"),
            unit=item.get("unit"), evidence_type=str(item["evidence_type"]),
            independence=str(item.get("independence", "unknown")),
            contradiction_status=str(item.get("contradiction_status", "unknown")),
        ))
    return EvidenceAssessment(str(listing_id), normalized, contradictions, now)


def _number(value):
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def policy_for_intake(intake: dict) -> dict:
    """Recompute all purchase and lease policy server-side."""
    budget = _number(intake["total_budget_usd"])
    if budget is None or budget <= 0:
        raise ValueError("Budget must be a positive USD amount.")
    transaction_types = ["lease"] if intake["city"] == "Los Angeles, CA" and budget < SALE_THRESHOLD_USD else ["sale", "lease"]
    ceiling = None
    allocation = intake.get("acquisition_allocation")
    occupancy_budget = budget if allocation is None else budget * Decimal(str(allocation)) / Decimal(100)
    if "sale" in transaction_types:
        ceiling = float(occupancy_budget * SALE_STRETCH)
    return {
        "total_budget_usd": float(budget),
        "transaction_types": transaction_types,
        "maximum_sale_price_usd": ceiling,
        "occupancy_budget_usd": float(occupancy_budget),
        "monthly_rent_caps_usd": {
            str(years): float(occupancy_budget / Decimal(15 * years)) for years in LEASE_HORIZONS
        },
    }


def _float(value):
    n = _number(value)
    return float(n) if n is not None else None


def normalize_rent(raw: dict) -> RentNormalization:
    """Normalize only explicit supported CoStar rate-basis enums; never infer units."""
    low = _float(raw.get("rate_low"))
    high = _float(raw.get("rate_high"))
    basis_code = raw.get("rate_basis_code")
    try:
        basis_code = int(basis_code) if basis_code is not None else None
    except (TypeError, ValueError):
        basis_code = None
    basis = RATE_BASIS.get(basis_code)
    area_min = _float(raw.get("area_min_sqft"))
    area_max = _float(raw.get("area_max_sqft"))
    missing = []
    flag = raw.get("asking_rate_has_value")
    currency_code = raw.get("monetary_unit")
    currency = "USD" if currency_code in (1, "1", "USD", "usd") else None
    if flag is not True or low is None or high is None or low <= 0 or high <= 0:
        missing.append("asking rent not supplied")
    if basis is None:
        missing.append("rent basis unknown")
    if currency is None:
        missing.append("currency not confirmed as USD")
    per_sf = basis_code in (1, 2)
    valid_rate_range = low is not None and high is not None and 0 < low <= high
    valid_area_range = (area_min is not None and area_max is not None
                        and 0 < area_min <= area_max)
    if low is not None and high is not None and not valid_rate_range:
        missing.append("asking rent range is invalid")
    if per_sf and not valid_area_range:
        missing.append("positive, ordered area bounds required for per-square-foot rent")
    if basis and flag is True and valid_rate_range and currency and (not per_sf or valid_area_range):
        if basis_code == 1:  # USD/SF/month
            monthly_low, monthly_high = low * area_min, high * area_max
        elif basis_code == 2:  # USD/SF/year
            monthly_low, monthly_high = low * area_min / 12, high * area_max / 12
        elif basis_code == 3:  # USD/month total
            monthly_low, monthly_high = low, high
        elif basis_code == 4:  # USD/year total
            monthly_low, monthly_high = low / 12, high / 12
        else:
            monthly_low = monthly_high = None
        if monthly_low is not None:
            return RentNormalization(
                "normalized", low, high, basis_code, basis, currency, area_min, area_max,
                round(monthly_low, 2), round(monthly_high, 2), round(monthly_low * 12, 2),
                round(monthly_high * 12, 2), [], "unknown",
                ["total occupancy charges (CAM/taxes/insurance/utilities) not provided"],
            )
    return RentNormalization(
        "unknown", low, high, basis_code, basis, currency, area_min, area_max,
        None, None, None, None, [], "unknown", missing or ["rent could not be normalized"],
    )


def _date_age_days(value, now):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt.astimezone(timezone.utc)).days)
    except (ValueError, TypeError):
        return None


def classify_sale_price(price, budget, ceiling):
    p = _number(price)
    if p is None or p <= 0:
        return {"classification": "unknown", "asking_price_usd": None}
    if p <= Decimal(str(budget)):
        classification = "within_budget"
    elif p <= Decimal(str(ceiling)):
        classification = "stretch"
    else:
        classification = "over_budget"
    return {"classification": classification, "asking_price_usd": float(p)}


def classify_lease(rent: RentNormalization, caps: dict) -> dict:
    results = {}
    for years in LEASE_HORIZONS:
        cap = caps[str(years)]
        if rent.monthly_high_usd is None or rent.monthly_low_usd is None:
            status, fit = "unknown", None
        elif rent.monthly_high_usd <= cap:
            status, fit = "within_budget", 100.0
        elif rent.monthly_low_usd > cap:
            status, fit = "over_budget", round(cap / rent.monthly_low_usd * 100, 1) if rent.monthly_low_usd else None
        else:
            status, fit = "unknown", None
        results[str(years)] = {"classification": status, "monthly_cap_usd": round(cap, 2), "budget_fit": fit}
    return results


def _property_search_fit(record, intake):
    # City, transaction type, and active status are hard discovery gates; scoring
    # them again would double-count eligibility. No general user constraints exist.
    if (record.get("city") or "").casefold() != CITY_CODES[intake["city"]][0].casefold():
        return {"score": None, "status": "eligibility_mismatch", "basis": "Listing city does not match the requested market."}
    if record.get("transaction_type") not in policy_for_intake(intake)["transaction_types"]:
        return {"score": None, "status": "eligibility_mismatch", "basis": "Transaction type is outside the server-authorized search."}
    return {"score": 100, "status": "matched", "basis": "The record matches the requested city and an authorized transaction type. No additional general property constraints were collected."}


def confidence_assessment(record, intake, verified=None):
    """Transparent rubric. Numeric values are evidence dimensions, never success probabilities."""
    now = datetime.now(timezone.utc)
    source_id = record.get("listing_id") and record.get("property_id")
    reliability = None
    reliability_reason = "Stable CoStar listing identity is incomplete."
    if source_id and record.get("listing_status") == "active":
        reliability = 75  # 50 traceable first-party source + 25 provider-active status
        age = _date_age_days(record.get("source_updated_at"), now)
        if age is not None and age <= 180:
            reliability += 15
        reliability_reason = "50 points for traceable first-party CoStar provenance, 25 for active provider status, and 15 when source update is within the documented 180-day recency window. Not independently corroborated."
    if verified and verified.get("independent_exact_claims", 0) > 0:
        reliability = min(100, (reliability or 0) + 10)
        reliability_reason += " An independently assessed exact-match claim added 10 points."
    fields = [
        record.get("property_id") is not None,
        record.get("listing_id") is not None,
        record.get("transaction_type") in ("sale", "lease"),
        record.get("listing_status") == "active",
    ]
    if not all(fields):
        completeness = None
    else:
        extras = [record.get("area_min_sqft") is not None, record.get("area_max_sqft") is not None,
                  record.get("source_updated_at") is not None]
        rent_or_sale = record.get("asking_price_usd") is not None if record["transaction_type"] == "sale" else record.get("rent_normalization", {}).get("status") == "normalized"
        completeness = round(100 * (4 + sum(extras) + int(rent_or_sale)) / 8)
    budget_fit = None
    budget_basis = "Price/rent or rent basis is unknown."
    if record["transaction_type"] == "sale":
        price = record.get("asking_price_usd")
        if price:
            allocation = intake.get("acquisition_allocation")
            acquisition_budget = float(intake["total_budget_usd"]) if allocation is None else float(intake["total_budget_usd"]) * float(allocation) / 100
            budget_fit = round(min(100, acquisition_budget / price * 100), 1)
            budget_basis = "Acquisition-budget-to-asking-price ratio; stretch is not affordability."
    else:
        high = record.get("rent_normalization", {}).get("monthly_high_usd")
        if high is not None and high > 0:
            caps = policy_for_intake(intake)["monthly_rent_caps_usd"]
            horizon = intake.get("lease_horizon_years")
            cap = caps[str(horizon)] if horizon in LEASE_HORIZONS else max(caps.values())
            budget_fit = round(min(100, cap / high * 100), 1)
            budget_basis = f"${cap:,.2f} monthly cap divided by normalized base rent; additional charges unknown."
    search_fit = _property_search_fit(record, intake)
    factors = (reliability, completeness, budget_fit, search_fit["score"])
    overall = round(sum(factors) / 4) if all(value is not None for value in factors) else None
    return {
        "source_reliability": {"score": reliability, "status": "scored" if reliability is not None else "unknown", "basis": reliability_reason},
        "data_completeness_consistency": {"score": completeness, "status": "scored" if completeness is not None else "insufficient_evidence", "basis": "Four required identity/status fields plus four completeness checks (area bounds, source update, price or normalized rent)."},
        "budget_fit": {"score": budget_fit, "status": "scored" if budget_fit is not None else "unknown", "basis": budget_basis},
        "property_search_fit": search_fit,
        "factor_weights_percent": {"source_reliability": 25, "data_completeness_consistency": 25, "budget_fit": 25, "property_search_fit": 25},
        "overall_confidence": overall,
        "overall_status": "complete" if overall is not None else "incomplete_confidence",
        "is_probability": False,
    }


def normalize_listing(raw: dict, intake: dict, retrieved_at: str | None = None) -> dict:
    retrieved_at = retrieved_at or datetime.now(timezone.utc).isoformat()
    txn = raw["transaction_type"]
    props = raw.get("property") or {}
    rent = normalize_rent(raw) if txn == "lease" else None
    budget = policy_for_intake(intake)
    raw_sale_low, raw_sale_high = _number(raw.get("asking_price_low")), _number(raw.get("asking_price_high"))
    sale_price = raw_sale_low if raw_sale_low is not None and raw_sale_low == raw_sale_high else None
    allocation = intake.get("acquisition_allocation")
    acquisition_budget = float(intake["total_budget_usd"]) if allocation is None else float(intake["total_budget_usd"]) * float(allocation) / 100
    sale = classify_sale_price(sale_price, acquisition_budget, budget["maximum_sale_price_usd"] or 0) if txn == "sale" else None
    lease_affordability = classify_lease(rent, budget["monthly_rent_caps_usd"]) if rent else None
    status = sale["classification"] if sale else (
        "within_budget" if any(x["classification"] == "within_budget" for x in lease_affordability.values()) else
        "over_budget" if all(x["classification"] == "over_budget" for x in lease_affordability.values()) else "unknown"
    )
    fresh = raw.get("certified_fresh_date") or raw.get("listing_status_updated_at") or raw.get("date_on_market")
    now = datetime.now(timezone.utc)
    record = {
        "property_id": raw.get("property_id"),
        "listing_id": str(raw.get("listing_id")) if raw.get("listing_id") is not None else None,
        "space_id": str(raw.get("space_id")) if raw.get("space_id") is not None else None,
        "suite": raw.get("suite"),
        "stable_id": str(raw.get("stable_id")),
        "address": props.get("address") or raw.get("address"),
        "city": props.get("city") or raw.get("city"),
        "state": props.get("state") or raw.get("state"),
        "property_type": PROPERTY_TYPES.get(raw.get("property_type_code"), "Unknown"),
        "transaction_type": txn,
        "listing_status": "active" if raw.get("listing_status") == 1 else "unknown",
        "listing_status_raw": raw.get("listing_status"),
        "availability_status": raw.get("availability_status"),
        "area_min_sqft": _float(raw.get("area_min_sqft")),
        "area_max_sqft": _float(raw.get("area_max_sqft")),
        "asking_price_raw": raw.get("asking_price_raw"),
        "asking_price_usd": sale["asking_price_usd"] if sale else None,
        "sale_classification": sale["classification"] if sale else None,
        "rent_raw": {"low": raw.get("rate_low"), "high": raw.get("rate_high"), "basis_code": raw.get("rate_basis_code"), "currency_code": raw.get("monetary_unit") } if txn == "lease" else None,
        "rent_normalization": asdict(rent) if rent else None,
        "lease_affordability": lease_affordability,
        "budget_classification": status,
        "known_additional_charges": rent.known_additional_charges if rent else [],
        "additional_charges_evidence": raw.get("known_additional_charges_raw") if txn == "lease" else None,
        "total_occupancy_cost_status": rent.total_occupancy_cost_status if rent else "unknown",
        "source_updated_at": fresh,
        "date_on_market": raw.get("date_on_market"),
        "available_at": raw.get("available_at"),
        "retrieved_at": retrieved_at,
        "source": "CoStar",
        "source_reference": raw.get("source_reference"),
        "original_evidence": raw.get("original_evidence", {}),
        "missing_fields": (rent.missing_fields if rent and rent.status == "unknown" else []) + ([] if raw.get("area_min_sqft") is not None else ["area missing"]),
        "lease_horizon_years": None,
        "verification": None,
    }
    record["confidence"] = confidence_assessment(record, intake)
    return record


def apply_allocation_and_horizon(records: list[dict], intake: dict, acquisition_percent: int = 100, preferred_horizon: int | None = None) -> list[dict]:
    if not isinstance(acquisition_percent, int) or not 0 <= acquisition_percent <= 100:
        raise ValueError("Acquisition/occupancy allocation must be between 0 and 100 percent.")
    if preferred_horizon not in (None, *LEASE_HORIZONS):
        raise ValueError("Lease horizon must be 3, 5, or 10 years.")
    revised_intake = {**intake, "acquisition_allocation": acquisition_percent,
                      "lease_horizon_years": preferred_horizon}
    budget = float(intake["total_budget_usd"])
    occupancy = budget * acquisition_percent / 100
    caps = {str(years): occupancy / (15 * years) for years in LEASE_HORIZONS}
    output = []
    for source in records:
        record = dict(source)
        record["acquisition_allocation_percent"] = acquisition_percent
        record["occupancy_budget_usd"] = occupancy
        record["monthly_rent_caps_usd"] = {k: round(v, 2) for k, v in caps.items()}
        record["lease_horizon_years"] = preferred_horizon
        if record["transaction_type"] == "lease":
            norm = record.get("rent_normalization") or {}
            rn = RentNormalization(**norm)
            record["lease_affordability"] = classify_lease(rn, caps)
            if preferred_horizon:
                cls = record["lease_affordability"][str(preferred_horizon)]["classification"]
                record["budget_classification"] = cls
            else:
                classes = [x["classification"] for x in record["lease_affordability"].values()]
                record["budget_classification"] = "within_budget" if "within_budget" in classes else "over_budget" if all(x == "over_budget" for x in classes) else "unknown"
        else:
            sale = classify_sale_price(record.get("asking_price_usd"), occupancy, occupancy * float(SALE_STRETCH))
            record["sale_classification"] = sale["classification"]
            record["budget_classification"] = sale["classification"]
        record["confidence"] = confidence_assessment(record, revised_intake)
        output.append(record)
    return output


def _diverse_order(records):
    priority = {"within_budget": 0, "stretch": 1, "unknown": 2, "over_budget": 3}
    records = sorted(records, key=lambda x: (
        priority.get(x["budget_classification"], 4),
        -(x["confidence"].get("source_reliability", {}).get("score") if x["confidence"].get("source_reliability", {}).get("score") is not None else -1),
        -(x["confidence"].get("data_completeness_consistency", {}).get("score") if x["confidence"].get("data_completeness_consistency", {}).get("score") is not None else -1),
        x.get("address") or "", x.get("stable_id") or "",
    ))
    # Apply identical type diversity to every intake; Stage 0 business fields
    # never influence candidate selection or ordering.
    groups = {}
    for r in records:
        groups.setdefault(r["budget_classification"], {}).setdefault(r.get("property_type") or "Unknown", []).append(r)
    out = []
    for cls in ("within_budget", "stretch", "unknown", "over_budget"):
        kinds = groups.get(cls, {})
        while any(kinds.values()):
            for kind in sorted(kinds):
                if kinds[kind]: out.append(kinds[kind].pop(0))
    return out


def make_discovery_response(intake, raw_records, model_explanations=None, acquisition_percent=100, preferred_horizon=None):
    budget = policy_for_intake(intake)
    normalized, seen = [], set()
    for raw in raw_records:
        key = raw["stable_id"]
        if key in seen: continue
        seen.add(key)
        # Sale is rejected unless the server policy granted sale discovery.
        if raw["transaction_type"] == "sale" and "sale" not in budget["transaction_types"]:
            continue
        record = normalize_listing(raw, intake)
        normalized.append(record)
    eligible = [r for r in normalized if r["budget_classification"] != "over_budget"]
    excluded = [r for r in normalized if r["budget_classification"] == "over_budget"]
    explanations = model_explanations or {}
    for r in normalized:
        note = explanations.get(str(r["stable_id"]))
        if isinstance(note, str): r["explanation"] = note
        else: r["explanation"] = deterministic_explanation(r)
    eligible = _diverse_order(eligible)
    excluded = _diverse_order(excluded)
    sorted_records = apply_allocation_and_horizon(eligible + excluded, intake, acquisition_percent, preferred_horizon)
    occupancy_budget = budget["total_budget_usd"] * acquisition_percent / 100
    return {
        "policy": {**budget, "acquisition_allocation_percent": acquisition_percent,
                   "occupancy_budget_usd": occupancy_budget,
                   "maximum_sale_price_usd": occupancy_budget * float(SALE_STRETCH) if "sale" in budget["transaction_types"] else None},
        "results": sorted_records,
        "qualifying_count": sum(1 for r in sorted_records if r["budget_classification"] in ("within_budget", "stretch")),
        "excluded_count": sum(1 for r in sorted_records if r["budget_classification"] == "over_budget"),
        "trace": {"provider": "CoStar", "source_listing_count": len(raw_records), "deduplicated_count": len(seen), "returned_count": len(sorted_records), "tavily_calls": 0},
    }


def deterministic_explanation(record):
    txn = "sale" if record["transaction_type"] == "sale" else "lease"
    area = "area not supplied" if record.get("area_min_sqft") is None else f"{record['area_min_sqft']:,.0f}–{record['area_max_sqft']:,.0f} SF" if record.get("area_max_sqft") != record.get("area_min_sqft") else f"{record['area_min_sqft']:,.0f} SF"
    if txn == "sale":
        price = f"CoStar asks ${record['asking_price_usd']:,.0f}." if record.get("asking_price_usd") is not None else "Asking price is unknown."
        return f"Active CoStar sale listing for {area}. {price} Budget class: {record['budget_classification']}."
    rent = record.get("rent_normalization") or {}
    rent_note = f"Normalized base rent is ${rent['monthly_low_usd']:,.0f}–${rent['monthly_high_usd']:,.0f}/month from explicit {rent['raw_basis']} pricing." if rent.get("status") == "normalized" else "Rent and affordability are unknown because the recorded basis or amount is incomplete."
    return f"Active CoStar lease listing for {area}. {rent_note} Additional occupancy charges are unknown."


def nemotron_explanations(records, intake, api_key, base_url, model):
    """One bounded, tool-free explanation call. Any error leaves canonical records intact."""
    if not api_key or not records:
        return {}, {"model_calls": 0, "status": "deterministic_fallback", "error": "model unavailable"}
    allowed = {str(r["stable_id"]) for r in records}
    payload_records = [{k: r.get(k) for k in (
        "stable_id", "listing_id", "property_id", "space_id", "address", "city", "state",
        "property_type", "transaction_type", "area_min_sqft", "area_max_sqft",
        "asking_price_usd", "sale_classification", "rent_raw", "rent_normalization",
        "lease_affordability", "budget_classification", "known_additional_charges",
        "source", "source_reference", "source_updated_at", "missing_fields")}
        for r in records[:8]]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "You are the MAXIMUS Property Discovery Explanation Agent. Explain supplied canonical CoStar records only. "
                "Never invent or alter facts, costs, locations, source URLs, affordability, zoning, feasibility, demand, or profitability. "
                "Do not make tool calls or discuss business suitability, business ideas, or conversion feasibility. Preserve unknowns. "
                "Return a JSON object with an 'explanations' array of {stable_id, text}; stable_id must exactly match an input record."
            )},
            {"role": "user", "content": json.dumps({"search": {
                "city": intake.get("city"),
                "total_budget_usd": intake.get("total_budget_usd"),
                "transaction_types": policy_for_intake(intake)["transaction_types"],
            }, "records": payload_records}, ensure_ascii=False)},
        ],
        "stream": False,
        "temperature": 0,
        # Nemotron's reasoning tokens count against max_tokens on OpenRouter.
        # Reserve enough output budget for reasoning plus the bounded JSON answer.
        "max_tokens": 4096,
        "reasoning": {"effort": "low", "exclude": True},
    }
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://hackprinceton.com", "X-Title": "MAXIMUS Stage 1"},
        data=json.dumps(body).encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0]
        finish_reason = choice.get("finish_reason")
        text = (choice.get("message") or {}).get("content") or ""
        if not text:
            return {}, {"model_calls": 1, "status": "deterministic_fallback",
                        "error": f"Nemotron returned no explanation content (finish_reason={finish_reason or 'unknown'})"}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Some compatible routes wrap structured output in prose or code fences.
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(text[start:end + 1])
        explanations = {}
        for item in parsed.get("explanations", []):
            stable_id, explanation = str(item.get("stable_id", "")), item.get("text")
            if stable_id in allowed and isinstance(explanation, str) and len(explanation) <= 700:
                explanations[stable_id] = explanation.strip()
        return explanations, {"model_calls": 1, "status": "ok" if explanations else "deterministic_fallback", "error": None if explanations else "No valid grounded explanations returned"}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as exc:
        return {}, {"model_calls": 1, "status": "deterministic_fallback", "error": f"Nemotron explanation unavailable ({type(exc).__name__})"}


def attach_explanations(response, explanations, model_trace):
    for record in response["results"]:
        explanation = explanations.get(str(record["stable_id"]))
        # Explanation text is strictly supplemental; deterministic financial/source content stays canonical.
        if explanation:
            record["explanation"] = explanation
    response["trace"]["nemotron"] = model_trace
    return response


def _identity_match(record, result):
    address = re.findall(r"[a-z0-9]+", (record.get("address") or "").casefold())
    text = f"{result.get('title', '')} {result.get('snippet', '')}".casefold()
    significant = [x for x in address if x not in {"street", "st", "road", "rd", "avenue", "ave", "boulevard", "blvd", "drive", "dr"}]
    return bool(significant) and sum(1 for token in significant if token in text) >= min(3, len(significant))


def _claim_value_matches(value, text):
    if value is None:
        return False
    raw = str(value).strip()
    if raw and raw.casefold() in text.casefold():
        return True
    number = _number(value)
    if number is None:
        return False
    candidates = re.findall(r"(?<![a-z])\$?\s*\d[\d,]*(?:\.\d+)?", text.casefold())
    return any((n := _number(c.replace("$", "").replace(",", "").strip())) is not None and n == number for c in candidates)


def _verification_candidates(claim_type, text):
    if claim_type in ("asking_price", "asking_rent_low", "asking_rent_high"):
        return [float(n.replace(",", "")) for n in re.findall(r"\$\s*(\d[\d,]*(?:\.\d+)?)", text)]
    if claim_type == "area":
        return [float(n.replace(",", "")) for n in re.findall(r"\b(\d[\d,]*(?:\.\d+)?)\s*(?:sq\.?\s*ft\.?|square feet|sf)\b", text, re.I)]
    if claim_type == "rent_basis":
        lower = text.casefold()
        candidates = []
        for basis, patterns in {
            "$/SF/month": ("$/sf/month", "per sf per month", "per square foot per month"),
            "$/SF/year": ("$/sf/year", "per sf per year", "per square foot per year"),
            "$/month": ("per month", "monthly rent"),
            "$/year": ("per year", "annual rent"),
        }.items():
            if any(pattern in lower for pattern in patterns):
                candidates.append(basis)
        return candidates
    return []


def reconcile_verification(record, search_results, retrieved_at=None):
    """Conservative claim-level comparison; snippets alone never establish source independence."""
    retrieved_at = retrieved_at or datetime.now(timezone.utc).isoformat()
    matching = [r for r in search_results if _identity_match(record, r) and str(r.get("url", "")).startswith("https://")]
    claims = [{"claim_type": "address", "costar_value": record.get("address"), "unit": None}]
    if record["transaction_type"] == "sale":
        claims += [{"claim_type": "asking_price", "costar_value": record.get("asking_price_usd"), "unit": "USD"}]
    else:
        rent = record.get("rent_normalization") or {}
        claims += [
            {"claim_type": "asking_rent_low", "costar_value": rent.get("raw_low"), "unit": rent.get("raw_basis")},
            {"claim_type": "asking_rent_high", "costar_value": rent.get("raw_high"), "unit": rent.get("raw_basis")},
            {"claim_type": "rent_basis", "costar_value": rent.get("raw_basis"), "unit": None},
        ]
    claims.append({"claim_type": "area", "costar_value": record.get("area_max_sqft"), "unit": "SF"})
    output = []
    independent_exact = 0
    for claim in claims:
        sources = []
        claim_conflict = False
        claim_match = False
        for result in matching:
            text = f"{result.get('title', '')} {result.get('snippet', '')}"
            matches = _claim_value_matches(claim["costar_value"], text)
            candidates = _verification_candidates(claim["claim_type"], text)
            external_value = claim["costar_value"] if matches else None
            if not matches and claim["costar_value"] is not None and candidates:
                expected = _number(claim["costar_value"])
                distinct = [value for value in candidates if (
                    (_number(value) != expected) if expected is not None and _number(value) is not None
                    else str(value).casefold() != str(claim["costar_value"]).casefold()
                )]
                if distinct:
                    claim_conflict = True
                    external_value = distinct[0]
                elif candidates:
                    matches = True
                    external_value = candidates[0]
            claim_match = claim_match or matches
            sources.append({"url": result.get("url"), "title": result.get("title"),
                            "source_date": result.get("source_date"), "retrieved_at": retrieved_at,
                            "external_value": external_value,
                            "observation_type": "exact_match" if matches else "candidate_discrepancy" if external_value is not None else "not_observed",
                            "independence": "unconfirmed; syndicated copies are not ruled out"})
            if matches and claim["claim_type"] not in ("address", "rent_basis"):
                independent_exact += 0
        verdict = "conflict" if claim_conflict else "corroborated" if claim_match else "unverified"
        explanation = ("A same-identity search result contains a potentially conflicting value; verify the source directly."
                       if verdict == "conflict" else "Exact text match found, but search-result metadata does not establish independent sourcing."
                       if verdict == "corroborated" else "No same-identity exact claim match was established from retrieved results.")
        output.append({**claim, "sources": sources, "verdict": verdict,
                       "explanation": explanation, "source_independence": "not established"})
    return {
        "stable_id": record["stable_id"], "listing_id": record["listing_id"],
        "property_id": record["property_id"], "retrieved_at": retrieved_at,
        "query_status": "results" if search_results else "unavailable",
        "claims": output, "search_results": search_results,
        "independent_exact_claims": independent_exact,
        "overall_status": "conflict" if any(claim["verdict"] == "conflict" for claim in output) else "unverified" if search_results else "unavailable",
    }
