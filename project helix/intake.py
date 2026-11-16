"""Deterministic Stage 0 intake validation and Stage 1 handoff."""
from decimal import Decimal, InvalidOperation

CITIES = {"Houston, TX", "Los Angeles, CA"}
LEASE_THRESHOLD_USD = Decimal("900000")


def build_intake(payload):
    if not isinstance(payload, dict):
        raise ValueError("Submit an intake object.")
    city = payload.get("city")
    if city not in CITIES:
        raise ValueError("Choose Houston, TX or Los Angeles, CA.")
    try:
        budget = Decimal(str(payload.get("total_budget_usd", ""))).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid total budget in USD.")
    if not budget.is_finite() or budget <= 0:
        raise ValueError("Total budget must be greater than $0.")
    financing = payload.get("financing")
    if financing not in ("cash", "financing"):
        raise ValueError("Choose cash or financing.")
    preferences = payload.get("business_preferences", "")
    if not isinstance(preferences, str):
        raise ValueError("Business preferences must be text.")
    preferences = preferences.strip()
    open_to_ideas = payload.get("open_to_ideas") is True
    return {
        "city": city,
        "total_budget_usd": float(budget),
        "financing": financing,
        "business_preferences": preferences,
        "open_to_ideas": open_to_ideas,
        "transaction_types": ["lease"] if city == "Los Angeles, CA" and budget < LEASE_THRESHOLD_USD else ["sale", "lease"],
        "acquisition_allocation": None,
        "lease_horizon_years": None,
    }
