"""Market-wide ranking: which listed spaces in a market best fit a use and a cash budget?

Screens every active for-lease space in the market with the same deterministic pieces the
single-property review uses, then returns a short, explained list:

  eligibility  CoStar property type must suit the use (alternatives.CATALOG); residential
               properties and dwelling tours are excluded
  fit          space_fit on CoStar facts only (no per-building Vexcel/Matterport calls), so many
               spaces come out "investigate" rather than "supported"
  rent         the listed asking rent for that space (deal.monthly_rent)
  cash         itemized planning cash to open incl. reserve (advisor_money.advisor_cash), the
               same editable allowances as /maximo; not quotes
  evidence     reconcile address and size conflicts are surfaced and ranked down, never hidden

Ranking (no synthetic score): physical-fit verdict, then budget (within / unknown because no rent
is listed / over), then no evidence flag, then lowest planning cash to open. One space per building.
The build-out allowance is scaled by size (the review's lump sum is sized for 10,000 sq ft).

Usage:
  python3 playground/market_rank.py --kit ~/Downloads/costar-hackathon-kit.zip --use "indoor pickleball" --capital 500000
"""
import argparse
import csv
import io
import json
import sys
from pathlib import Path
from zipfile import ZipFile

from advisor_money import advisor_cash, plan_inputs
from alternatives import CATALOG
from deal import SERVICE, load_rent_estimates, monthly_rent
from money_runway import DEFAULTS
from eligibility import DWELLING_CAPTION, DWELLING_TYPES, PROPERTY_TYPES
from reconcile import MARKETS, check_identity, check_size, costar_address, kit_password, unique_scans
from space_fit import space_fit

VERDICT_ORDER = {"supported": 0, "investigate": 1, "does_not_fit": 2}
TOP = 5
# Default smallest space worth ranking. Pickleball: 3 courts at the 30x60 ft minimum, because the cash
# scenario's revenue assumes 3 courts (3 x 12 h x 30 days); a 1-court suite can't earn that.
# Others are planning assumptions the user can override: a small boutique fitness studio, and a
# floor below which a space is an office cubicle rather than a business premises.
MIN_SF = {"indoor pickleball": 3 * 1800, "fitness studio": 1500}
DEFAULT_MIN_SF = 1000
# The review's default build-out allowance is a lump sum sized for the Tanner pilot's 10,000 sq ft suite
# ($200k, i.e. $20/sq ft; warehouse $25k). Ranking compares spaces of very different sizes, so the same
# allowance is scaled per sq ft; a lump sum would make a 48,600 sq ft space look as cheap as a 6,000 one.
REFERENCE_SF = 10000


def load_market(kit_path, market, password=None):
    """[(property, [listed lease spaces])] for every property in the market with an active lease listing.

    One pass over the listing summary and one over the properties, instead of one pass per building.
    Only properties the review page can open (commercial-properties.csv) are kept.
    """
    market_id = MARKETS[market]
    spaces = {}
    with ZipFile(kit_path) as kit:
        with kit.open("commercial-properties.csv", pwd=password) as stream:
            reviewable = {int(row["PropertyId"]) for row in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
                          if row["PropertyId"].isdigit()}
        with kit.open("data/commercial-listing-summary.jsonl", pwd=password) as stream:
            for line in stream:
                listing = json.loads(line)
                if listing.get("listingStatus") != 1:  # active only
                    continue
                for space in (listing.get("forLeaseSummary") or {}).get("spaces") or []:
                    pid = space.get("propertyId")
                    if pid in reviewable:
                        spaces.setdefault(pid, []).append({
                            "listing_id": listing["id"], "suite": space.get("suite"),
                            "square_feet": space.get("squareFeetMax"),
                            "available_on": (space.get("availableOnDate") or "")[:10] or None,
                            "on_market": (space.get("dateOnMarket") or "")[:10] or None,
                            "raw": space,
                        })
        out = []
        with kit.open("data/commercial-property.jsonl", pwd=password) as stream:
            for line in stream:
                prop = json.loads(line)
                if prop["id"] in spaces and (prop.get("primaryAddress") or {}).get("researchMarketId") == market_id:
                    out.append((prop, spaces[prop["id"]]))
    return out


def space_value(space):
    """The suite value the review page's suite picker uses (maximo.space_option)."""
    suite = str(space["suite"] or "").strip()
    if suite:
        return suite
    return f"listing:{space['listing_id']}:{space['square_feet']:g}" if space["square_feet"] else None


def deciding_reason(fit):
    judged = [r for r in fit["records"] if r["source"] != "checklist"
              and (r["fact"].startswith(f"{fit['use']}_") or r["fact"].startswith("req_"))]
    row = (next((r for r in judged if r["status"] == "conflict"), None)
           or next((r for r in judged if r["status"] == "unknown"), None)
           or next(iter(judged), None))
    return row["note"] if row else "No use-specific threshold can be checked from CoStar data; see the review checklist."


def evidence_flags(prop):
    flags = []
    if any(r["status"] == "conflict" for r in check_identity(prop)):
        flags.append("A linked tour is captioned for a different address.")
    if any(r["status"] == "conflict" for r in check_size(prop)):
        flags.append("CoStar's own size fields don't add up.")
    return flags


LOW_RENT_PER_SF_YEAR = 3.0  # below this an asking rent is more likely a data-entry error than a bargain


def screen_space(prop, space, use, min_sf=None, max_sf=None):
    """Cheap screen for one space (eligibility, size, fit, rent), or None if the use is ruled out.

    Cash to open is added later, only for the best space per building (add_cash)."""
    spec = CATALOG[use]
    size = space["square_feet"] or 0
    if (min_sf and size < min_sf) or (max_sf and size > max_sf):
        return None
    kind = str(prop.get("propertyType", "")).strip()
    if kind.casefold() in DWELLING_TYPES or kind not in spec["types"]:
        return None
    if any(DWELLING_CAPTION.search(c) for scan in unique_scans(prop) for c in scan["captions"]):
        return None
    if not space["square_feet"] or not space_value(space):
        return None
    fit = space_fit(prop, spec["fit"], spaces=[space], suite=space["suite"],
                    requirements={"min_area_sf": 1} if spec["fit"] == "custom" else None)
    verdict = fit["verdict"]
    if verdict == "does_not_fit":
        return None
    if spec["fit"] == "custom" and verdict == "supported":
        verdict = "investigate"  # same rule as the single-property review: area alone doesn't prove a layout
    monthly, per_sf_year = monthly_rent(space["raw"])
    flags = []
    if per_sf_year is not None and per_sf_year < LOW_RENT_PER_SF_YEAR:
        flags.append(f"Asking rent of ${per_sf_year:.2f}/sq ft/year is implausibly low; confirm the rate and its "
                     "period with the broker.")
    basis = SERVICE.get(space["raw"].get("serviceType"), ("lease basis not recorded",))[0]
    return {
        "property_id": prop["id"], "address": costar_address(prop)["display"],
        "property_type": PROPERTY_TYPES.get(kind, kind), "suite": space_value(space),
        "suite_label": f"Suite {space['suite'].strip()}" if str(space["suite"] or "").strip()
        else f"{space['square_feet']:,.0f} sq ft (unnamed)",
        "square_feet": space["square_feet"], "available_on": space["available_on"],
        "monthly_rent": monthly, "rent_per_sf_year": per_sf_year, "lease_basis": basis,
        "rent_source": "CoStar asking rent" if monthly else "not listed; default assumption used",
        "verdict": verdict, "fit_reason": deciding_reason(fit), "evidence_flags": flags,
        "build_cost": build_allowance(use, space["square_feet"]), "space_id": space["raw"].get("id"),
        "tour_count": len(unique_scans(prop)),
    }


def build_allowance(use, square_feet):
    """The review's default build-out allowance for this use, scaled from 10,000 sq ft to this space."""
    lump = plan_inputs({}, use).get("build_cost", DEFAULTS["build_cost"])
    return round(lump * square_feet / REFERENCE_SF, -2)


def space_key(row):
    """Best space within one building. Approximates planning cash to open (build-out + contingency, deposit
    and pre-opening rent) so the full cash model only runs once per building."""
    upfront = 1.15 * row["build_cost"] + 5 * (row["monthly_rent"] or 0)
    return (VERDICT_ORDER[row["verdict"]], bool(row["evidence_flags"]), row["monthly_rent"] is None, upfront)


def budget_order(row):
    """Within budget on a listed rent, within even at the high rent estimate, unknown, over budget."""
    if row["within_budget"] is True:
        return 0 if row["budget_basis"] == "listed rent" else 1
    return 2 if row["within_budget"] is None else 3


def cash_for(row, use, capital, reserve, monthly):
    inputs = {"starting_cash": capital, "reserve": reserve, "build_cost": row["build_cost"]}
    if monthly is not None:
        inputs["monthly_rent"] = monthly
    cash = advisor_cash(inputs, use, "lease", area=row["square_feet"],
                        rent_source="CoStar asking rent" if row["monthly_rent"] else "Editable assumption")
    totals = cash["costs"]["totals"]["base"]
    gap = ((cash.get("money") or {}).get("conversion") or {}).get("extra_funding_needed") or 0
    return totals, gap, not totals["shortfall"] and not gap


def usable_estimate(row, rent_estimates):
    """The market rent range for a space with no listed rent, if it rests on enough similar recent deals."""
    e = ((rent_estimates or {}).get("spaces") or {}).get(str(row.get("space_id")))
    return e if e and not e.get("low_confidence") else None


def add_cash(row, use, capital, reserve, rent_estimates=None):
    if row["monthly_rent"] is not None:
        totals, gap, fits = cash_for(row, use, capital, reserve, row["monthly_rent"])
        row.update(within_budget=fits, budget_basis="listed rent", assumed_rent=None, rent_estimate=None)
    elif (e := usable_estimate(row, rent_estimates)):
        # No listed rent: test the budget at the HIGH end of what similar spaces asked, so "within budget"
        # holds even if the broker quotes high; "over" only when even the typical rent breaks it.
        typical, high = (round(e[k] * row["square_feet"] / 12) for k in ("typical", "high"))
        _, _, fits_high = cash_for(row, use, capital, reserve, high)
        totals, gap, fits_typical = cash_for(row, use, capital, reserve, typical)
        row.update(within_budget=True if fits_high else (False if not fits_typical else None),
                   budget_basis="rent estimate", assumed_rent=typical,
                   rent_estimate={"low": e["low"], "typical": e["typical"], "high": e["high"],
                                  "method": e["method"], "high_monthly": high})
    else:
        # Without a listed rent or a usable estimate, the cash figure rests on the default rent.
        totals, gap, _ = cash_for(row, use, capital, reserve, None)
        row.update(within_budget=None, budget_basis="default rent", assumed_rent=DEFAULTS["monthly_rent"],
                   rent_estimate=None)
    row.update(cash_to_open=totals["cash_required"], shortfall=totals["shortfall"] or 0, reserve_gap=gap)
    return row


def rank_key(row):
    return (VERDICT_ORDER[row["verdict"]], budget_order(row), bool(row["evidence_flags"]),
            row["cash_to_open"] if row["cash_to_open"] is not None else float("inf"))


def why(row):
    parts = [f"{row['square_feet']:,.0f} sq ft"]
    e = row.get("rent_estimate")
    if row["monthly_rent"]:
        parts.append(f"${row['monthly_rent']:,}/mo asking ({row['lease_basis']})")
    elif e:
        source = "trained model" if e["method"] == "model" else "similar recent deals"
        parts.append(f"no asking rent listed; similar spaces asked ${e['low']:,.2f}–${e['high']:,.2f}/sq ft/yr "
                     f"({source}), ~${row['assumed_rent']:,}/mo typical")
    else:
        parts.append("no asking rent listed")
    parts.append(f"${row['build_cost']:,.0f} build-out allowance")
    parts.append(f"~${row['cash_to_open']:,.0f} to open incl. reserve" if row["cash_to_open"] is not None
                 else "cash to open incomplete")
    if e:
        parts.append({True: "within budget even at the high rent estimate",
                      False: "over budget even at the typical rent estimate",
                      None: "within budget at the typical rent estimate but not the high one; get a quote"}
                     [row["within_budget"]])
    elif row["within_budget"] is None:
        parts.append(f"budget fit unknown until the broker quotes a rent (cash assumes ${row['assumed_rent']:,.0f}/mo)")
    else:
        parts.append("within budget" if row["within_budget"] else "over budget under planning allowances")
    return ", ".join(parts) + "."


def rank_market(candidates, use, capital, reserve, top=TOP, min_sf=None, max_sf=None, rent_estimates=None):
    """Pure ranking over load_market() output."""
    if use not in CATALOG:
        raise ValueError(f"use must be one of {', '.join(CATALOG)}")
    min_sf = MIN_SF.get(use, DEFAULT_MIN_SF) if min_sf is None else min_sf
    best = {}
    screened = 0
    for prop, spaces in candidates:
        building_flags = None
        for space in spaces:
            screened += 1
            row = screen_space(prop, space, use, min_sf, max_sf)
            if not row:
                continue
            if building_flags is None:
                building_flags = evidence_flags(prop)
            row["evidence_flags"] = building_flags + row["evidence_flags"]
            if prop["id"] not in best or space_key(row) < space_key(best[prop["id"]]):
                best[prop["id"]] = row
    rows = sorted((add_cash(row, use, capital, reserve, rent_estimates) for row in best.values()), key=rank_key)
    for row in rows:
        row["why"] = why(row)
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ("supported", "investigate")}
    return {
        "use": use, "capital": capital, "reserve": reserve, "ownership": "lease", "min_sf": min_sf, "max_sf": max_sf,
        "spaces_screened": screened, "buildings_screened": len(candidates), "buildings_passing": len(rows),
        "by_verdict": counts, "within_budget": sum(r["within_budget"] is True for r in rows),
        "budget_unknown": sum(r["within_budget"] is None for r in rows),
        "rent_from_estimate": sum(r["budget_basis"] == "rent estimate" for r in rows),
        "results": rows[:top],
        "ranking_basis": ("Physical-fit verdict from CoStar facts first, then budget (within on a listed rent, within "
                          "even at the high market-rent estimate for spaces with no listed rent, unknown, over), then "
                          "no evidence flag, then the lowest planning cash to open. One space per building. No numeric "
                          "score."),
        "limits": ("CoStar data only: no per-building aerial or scan measurements, so most spaces are 'investigate'. "
                   "Cash to open uses the review's unverified planning allowances, with the build-out scaled by size "
                   f"(${build_allowance(use, REFERENCE_SF) / REFERENCE_SF:g}/sq ft); not quotes. "
                   "Lease only. Demand and permission are not screened. Open a result for the full review."),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--market", choices=sorted(MARKETS), default="Houston")
    parser.add_argument("--use", choices=sorted(CATALOG), default="indoor pickleball")
    parser.add_argument("--capital", type=float, default=320000)
    parser.add_argument("--reserve", type=float, default=50000)
    parser.add_argument("--top", type=int, default=TOP)
    parser.add_argument("--min-sf", type=float, help="Smallest space to rank (default: 3 courts for pickleball).")
    parser.add_argument("--max-sf", type=float)
    args = parser.parse_args()
    try:
        candidates = load_market(args.kit, args.market, kit_password())
        result = rank_market(candidates, args.use, args.capital, args.reserve, args.top, args.min_sf, args.max_sf,
                             load_rent_estimates())
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
