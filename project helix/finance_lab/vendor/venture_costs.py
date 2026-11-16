"""Cash to open, per business, for a specific listed space. Sourced benchmarks, scaled to the space.

For each business the registry of opening costs (venture_cost_data.COSTS) holds the itemized
"Estimated Initial Investment" (Franchise Disclosure Document Item 7, or a cited industry study) for
a representative operator: leasehold improvements, equipment, signage, opening inventory, grand
opening, deposits/licenses, and the "additional funds" a new unit needs for its first months. Each
line keeps its source, URL and an exact quote.

For one space the lines become a ledger:
  build-out      leasehold improvements per sq ft of the benchmark's typical unit x this space's size
  equipment      per sq ft as well where it grows with capacity (courts, racks, machines, shelves of
                 inventory), else as listed; each business lists which lines scale
  fixed lines    signage, inventory, grand opening, deposits/licenses, working capital, and the
                 benchmark's other opening lines (pre-opening payroll, insurance, legal...): as listed
  this space     lease deposit (2 months, team convention) + rent during build-out (months x rent)
Franchise fees and franchisor-only lines (training travel) are left out and the fee shown as a note; the
benchmark's own rent lines are left out because this space's deposit and rent are added from the listing.

Low / base / high come from the benchmark's own range (base = midpoint). Rent comes from the listing,
else the rent model's typical estimate for the space, else it is unknown and the result says so.
Nothing here forecasts revenue: after opening we report how many months of rent the cash left over
would cover with zero revenue.
"""
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPOSIT_MONTHS = 2  # team convention in cost_ledger (two months' base rent), an assumption
LINES = [  # (key in the cost data, label); which lines scale with floor area is per business ("scales")
    ("leasehold_improvements", "Build-out (leasehold improvements)", None),
    ("equipment_ffe", "Equipment, furniture and fixtures", None),
    ("signage", "Signage", None),
    ("initial_inventory", "Opening inventory and supplies", None),
    ("grand_opening_marketing", "Grand opening marketing", None),
    ("deposits_licenses_permits", "Licenses, permits and utility deposits", None),
    ("additional_funds_working_capital", "Early operating cash (first months)", None),
    ("other_opening_costs", "Pre-opening payroll, insurance, legal and other", None),
    ("all_in_not_itemized", "Opening cost, all-in (benchmark not itemized)", None),
]


@lru_cache(maxsize=1)
def costs():
    from venture_cost_data import COSTS
    return COSTS


@lru_cache(maxsize=1)
def rent_estimates():
    try:
        return json.loads((ROOT / "output" / "rent-estimates.json").read_text())
    except (OSError, ValueError):
        return None


def rent_for(space):
    """Monthly base rent for a listed space: asking rent, else the rent model's typical estimate, else unknown."""
    if space.get("monthly_rent"):
        return {"monthly": space["monthly_rent"], "psf_year": space.get("rent_psf_year"),
                "source": f"CoStar asking rent ({space.get('lease_basis') or 'basis not recorded'})", "kind": "listed"}
    e = ((rent_estimates() or {}).get("spaces") or {}).get(str(space.get("space_id")))
    if e and not e.get("low_confidence") and space.get("sf"):
        return {"monthly": round(e["typical"] * space["sf"] / 12), "psf_year": e["typical"],
                "high_monthly": round(e["high"] * space["sf"] / 12),
                "source": f"No asking rent listed; rent model typical estimate (${e['low']:.2f}-${e['high']:.2f}/sq ft/yr)",
                "kind": "estimate"}
    return {"monthly": None, "psf_year": None, "source": "No asking rent listed and no confident estimate", "kind": "unknown"}


def _mid(pair):
    return (pair[0] + pair[1]) / 2


def cash_to_open(key, space_sf, monthly_rent, *, budget=None, reserve=0.0, build_months=None, building_type=None):
    """Itemized cash to open this business in a space of `space_sf` (lease). Returns ledger, totals, affordability.

    A benchmark can carry building-type variants (coworking in an office building is a conversion of
    fitted-out space, not a new build); the variant replaces the lines it names.
    """
    data = costs().get(key)
    variant = next((v for v in (data or {}).get("variants", {}).values()
                    if building_type and str(building_type) in v["types"]), None)
    if variant:
        data = {**data, "line_items": variant["line_items"], "scales": variant.get("scales", data.get("scales")),
                "notes": variant["note"] + " " + data.get("notes", "")}
    if not data or not data.get("comparable", True):
        return {"affordability": "unknown", "lines": [], "cash_to_open": dict.fromkeys(("low", "base", "high")),
                "required": dict.fromkeys(("low", "base", "high")), "headroom": None,
                "months_of_rent_after_opening": None, "basis_quality": "none",
                "note": (data or {}).get("note") or "No comparable opening-cost benchmark for this business yet.",
                "sources": (data or {}).get("sources", [])}
    typical = _mid(data["typical_sf"]) if data.get("typical_sf") else None
    lines = []
    for field, label, scales in LINES:
        pair = data["line_items"].get(field)
        if not pair:
            continue
        per_sf = field in data.get("scales", ["leasehold_improvements"])
        if field in data.get("per_sf", {}) and space_sf:
            rate = data["per_sf"][field]
            amounts = {"low": rate[0] * space_sf, "high": rate[1] * space_sf}
            how = f"${rate[0]:,.0f}-${rate[1]:,.0f} per sq ft x {space_sf:,.0f} sq ft"
        elif field in data.get("per_sf", {}):
            continue
        elif per_sf and typical and space_sf:
            factor = space_sf / typical
            amounts = {"low": pair[0] * factor, "high": pair[1] * factor}
            how = f"${pair[0] / typical:,.0f}-${pair[1] / typical:,.0f} per sq ft x {space_sf:,.0f} sq ft"
        else:
            amounts = {"low": pair[0], "high": pair[1]}
            how = ("as listed for a typical unit" if not per_sf else
                   "as listed (benchmark unit size unknown, so not scaled to this space)")
        amounts["base"] = (amounts["low"] + amounts["high"]) / 2
        lines.append({"id": field, "label": label, "how": how, "source": data["sources"][0]["publisher_or_brand"],
                      **{k: round(v) for k, v in amounts.items()}})
    months = build_months if build_months is not None else data.get("build_months", 3)
    if monthly_rent:
        deposit = DEPOSIT_MONTHS * monthly_rent
        lines.append({"id": "lease_deposit", "label": "Lease security deposit", "how":
                      f"{DEPOSIT_MONTHS} months x ${monthly_rent:,.0f} (assumption)", "source": "team convention",
                      "low": round(deposit), "base": round(deposit), "high": round(deposit)})
        lines.append({"id": "rent_during_buildout", "label": "Rent while you build out", "how":
                      f"{months} months x ${monthly_rent:,.0f} (no free-rent period assumed)",
                      "source": "build months: assumption", "low": round(months * monthly_rent * 0.5),
                      "base": round(months * monthly_rent), "high": round(months * monthly_rent * 1.5)})
    totals = {case: sum(line[case] for line in lines) for case in ("low", "base", "high")}
    required = {case: totals[case] + reserve for case in totals}
    rent_known = bool(monthly_rent)
    if budget is None:
        affordability = "unknown"
    elif required["low"] > budget:
        affordability = "over"  # over even before rent if rent is unknown
    elif not rent_known:
        affordability = "rent_unknown"  # fits before rent; the listing has no rent to add
    elif required["base"] <= budget:
        affordability = "within"
    else:
        affordability = "stretch"
    headroom = None if budget is None or not rent_known else round(budget - required["base"])
    months_left = (round(headroom / monthly_rent, 1) if headroom is not None and monthly_rent and headroom > 0
                   else 0 if headroom is not None else None)
    itemized = sum(1 for f, _, _ in LINES if data["line_items"].get(f))
    unpriced = data.get("unpriced", [])
    return {"affordability": affordability, "lines": lines,
            "cash_to_open": {k: round(v) for k, v in totals.items()},
            "required": {k: round(v) for k, v in required.items()}, "reserve": reserve,
            "headroom": headroom, "months_of_rent_after_opening": months_left,
            "build_months": months, "rent_known": rent_known,
            "basis_quality": "weak" if data.get("weak") else "partial" if unpriced or itemized < 4 else "itemized",
            "unpriced": unpriced, "excludes": "build-out" if unpriced else None,
            "franchise_fee_excluded": data.get("franchise_fee"),
            "sources": data["sources"],
            "note": (data.get("notes") or "") + (" Not priced: " + " ".join(unpriced) if unpriced else "")
            + (" Rent unknown: cash excludes the deposit and build-out rent." if not rent_known else "")}


def flip(cost, budget):
    """The smallest change that makes an unaffordable or stretch option affordable (cash only)."""
    if cost["affordability"] in ("within", "unknown") or budget is None:
        return None
    needed = cost["required"]["base"] - budget
    return {"extra_cash": round(needed, -3) if needed > 0 else 0,
            "text": f"About ${needed:,.0f} more cash (or a smaller reserve) covers the base case."}
