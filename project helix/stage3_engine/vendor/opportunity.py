"""Opportunity scan: "I have a budget; what could I open, and where?"

Every active for-lease space in the Houston market x every business in the registry, narrowed by
hard gates in order and then ranked by neighborhood demand:

  1 building   can this building (and this listed space) host the business?   neighborhood.building_fit
  2 money      does cash to open + the user's reserve fit the budget?          venture_costs.cash_to_open
  3 demand     does the neighborhood support the business?                     neighborhood.investigate

Efficiency. The expensive parts do not depend on the budget, so they are built once into an index
(gitignored, under playground/output/neighborhood/): one pass over the kit for the listed spaces, then
the 26-business neighborhood check and the building gate for every listed building. A budget query is
then arithmetic over the index (well under a second for ~2,400 spaces x 26 businesses).

Usage (from the repo root):
  python3.12 playground/with_env.py python3.12 playground/opportunity.py --build
  python3.12 playground/with_env.py python3.12 playground/opportunity.py --budget 500000 --reserve 50000
"""
import argparse
import csv
import io
import json
import os
import time
from functools import lru_cache
from pathlib import Path

import neighborhood as nb
from neighborhood_industries import INDUSTRIES

INDEX_VERSION = 2
MARKET_ID = 14  # Houston (reconcile.MARKETS)
MARKET_IDS = {"houston": 14, "los_angeles": 20}
LOW_RENT_PER_SF_YEAR = 3.0  # below this an asking rent is more likely a data-entry error (market_rank)


def password():
    return os.environ.get("COSTAR_KIT_PASSWORD", "").encode() or None


def kit_file(kit=None):
    path = Path(kit) if kit else nb.kit_path()
    if not path:
        raise ValueError("CoStar kit not found. Set COSTAR_KIT or pass --kit.")
    return path


def market_path(kit=None, market="houston"):
    # derived from the kit; same data whether zip or folder
    return nb.CACHE / ("market-houston-v2.json" if market == "houston" else f"market-{market}-v2.json")


def load_market(kit=None, market="houston"):
    """Buildings with active lease listings in one market (Houston by default), reduced to what the scan needs.
    One kit pass, cached. The neighborhood model is calibrated for Houston only; other markets get building fit and
    cost, and say so."""
    from deal import SERVICE, monthly_rent
    from reconcile import costar_ceiling_ft, unique_scans
    from space_fit import building_numbers, kit_lines
    from market_rank import evidence_flags
    kit = kit_file(kit)
    path = market_path(kit, market)
    if path.exists():
        return json.loads(path.read_text())
    pw = password()
    reviewable = {int(row["PropertyId"]) for row in csv.DictReader(io.StringIO("".join(
        kit_lines(kit, "commercial-properties.csv", pw)).lstrip("﻿"))) if row["PropertyId"].isdigit()}
    spaces = {}
    for line in kit_lines(kit, "data/commercial-listing-summary.jsonl", pw):
        listing = json.loads(line)
        if listing.get("listingStatus") != 1:
            continue
        for sp in (listing.get("forLeaseSummary") or {}).get("spaces") or []:
            pid = sp.get("propertyId")
            if pid not in reviewable or not sp.get("squareFeetMax"):
                continue
            monthly, per_year = monthly_rent(sp)
            if per_year is not None and per_year < LOW_RENT_PER_SF_YEAR:
                monthly = per_year = None  # implausible asking rent: treat as not listed
            basis = SERVICE.get(sp.get("serviceType"), ("basis not recorded", ""))[0]
            row = {"listing_id": listing["id"], "space_id": sp.get("id"), "suite": sp.get("suite"),
                   "floor": sp.get("floorName"), "executive_suite": bool(sp.get("isExecutiveSuite")),
                   "space_use": sp.get("spaceUse"),
                   "sf": sp.get("squareFeetMax"), "monthly_rent": monthly, "rent_psf_year": per_year,
                   "lease_basis": basis, "available_on": (sp.get("availableOnDate") or "")[:10] or None,
                   "lease_pending": bool(sp.get("isLeasePending"))}
            row["value"] = nb.space_value({**row, "square_feet": row["sf"]})
            spaces.setdefault(pid, {})[row["value"]] = row  # one row per suite value
    buildings = []
    for line in kit_lines(kit, "data/commercial-property.jsonl", pw):
        prop = json.loads(line)
        a = prop.get("primaryAddress") or {}
        if prop["id"] not in spaces or a.get("researchMarketId") != MARKET_IDS[market] or a.get("latitude") is None:
            continue
        ceiling, ceiling_source = costar_ceiling_ft(prop)
        numbers = building_numbers(prop)
        street = " ".join(str(a.get(k) or "") for k in ("streetNumber", "streetDirection", "streetName", "streetType"))
        buildings.append({
            "property_id": prop["id"], "address": f"{' '.join(street.split())}, {a.get('city')}, {a.get('state')}",
            "lat": a["latitude"], "lon": a["longitude"], "primary_address": a,
            "facts": {"property_id": prop["id"], "type": str(prop.get("propertyType") or ""),
                      "subtype": str(prop.get("propertySubtype") or ""), "gba": prop.get("grossBuildingAreaSquareFeet"),
                      "stories": prop.get("numStoriesAboveGrade"), "ceiling_ft": ceiling,
                      "ceiling_source": ceiling_source, "drive_in_bays": numbers.get("drive_in_bays"),
                      "docks": numbers.get("docks"),
                      "listed": [{"value": v, "suite": s["suite"], "sf": s["sf"], "floor": s["floor"],
                                  "executive_suite": s["executive_suite"]} for v, s in spaces[prop["id"]].items()]},
            "spaces": list(spaces[prop["id"]].values()),
            "evidence_flags": evidence_flags(prop), "tours": len(unique_scans(prop)), "market": market,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(buildings))
    return buildings


def find_building(property_id, kit=None):
    """(building, market) from the Houston market first, then Los Angeles; (None, None) if not listed for lease."""
    for market in MARKET_IDS:
        try:
            b = next((b for b in load_market(kit, market) if b["property_id"] == int(property_id)), None)
        except (OSError, RuntimeError, ValueError, KeyError):
            b = None
        if b:
            return b, market
    return None, None


def write_seed(buildings):
    """Bulk-write property pins and building facts into the neighborhood caches (one write each)."""
    pins_path, facts_path = nb.CACHE / "property-pins.json", nb.CACHE / "building-facts.json"
    pins = json.loads(pins_path.read_text()) if pins_path.exists() else {}
    facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
    for b in buildings:
        pins.setdefault(str(b["property_id"]), b["primary_address"])
        facts[str(b["property_id"])] = b["facts"]  # the listing pass is the freshest view of listed spaces
    pins_path.parent.mkdir(parents=True, exist_ok=True)
    pins_path.write_text(json.dumps(pins))
    facts_path.write_text(json.dumps(facts))
    nb.property_pin.cache_clear()
    nb.building_facts.cache_clear()


def index_path(kit=None):
    return nb.CACHE / f"opportunity-index-v{INDEX_VERSION}.json"


def build_index(kit=None, log=print):
    """Neighborhood decisions (26 businesses) and building fit for every listed space. Budget-independent."""
    kit = kit_file(kit)
    started = time.perf_counter()
    buildings = load_market(kit)
    log(f"market: {len(buildings)} buildings, {sum(len(b['spaces']) for b in buildings)} listed spaces "
        f"({time.perf_counter() - started:.1f}s)")
    write_seed(buildings)
    out = []
    t = time.perf_counter()
    for i, b in enumerate(buildings):
        report = nb.investigate("suggest", property_id=b["property_id"], kit=kit)
        demand = {}
        for d in report["decisions"]:
            demand[d["industry"]] = {
                "verdict": d["verdict"], "score": d["score"], "insight": d["insight"], "basis": d["basis"],
                "reasons": [r["reason"] for r in d["reasons"] if r["stance"]][:3],
                "lookalike": d["lookalike_percentile"], "validated": d["lookalike_validated"]}
        fits = {}
        for sp in b["spaces"]:
            fits[sp["value"]] = {}
            for key in INDUSTRIES:
                f = nb.building_fit(key, b["facts"], sp["value"])
                fits[sp["value"]][key] = {"status": f["status"], "capacity": f["capacity"],
                                          "reasons": f["reasons"], "unknowns": f["unknowns"]}
        out.append({"property_id": b["property_id"], "demand": demand, "fits": fits})
        if (i + 1) % 50 == 0:
            log(f"  {i + 1}/{len(buildings)} buildings ({time.perf_counter() - t:.1f}s)")
    path = index_path(kit)
    path.write_text(json.dumps({"version": INDEX_VERSION, "built_at": nb.now_iso(), "buildings": out}))
    log(f"index: {len(out)} buildings x {len(INDUSTRIES)} businesses in {time.perf_counter() - started:.1f}s")
    return out


@lru_cache(maxsize=2)
def load_index(kit=None):
    kit = kit_file(kit)
    path = index_path(kit)
    if not path.exists():
        if nb.offline():
            raise nb.Offline("No opportunity index; run opportunity.py --build online first.")
        build_index(kit, log=lambda *_: None)
    index = {row["property_id"]: row for row in json.loads(path.read_text())["buildings"]}
    market = {b["property_id"]: b for b in load_market(kit)}
    return market, index


def promising(demand):
    return demand["verdict"] == "supported" or (demand["verdict"] == "mixed" and demand["score"] > 0)


def jurisdiction_cached(lat, lon):
    """Which authority permits this point, from the jurisdiction cache only (never a network call in a scan)."""
    try:
        from jurisdiction import fetch_census, resolve_jurisdiction
    except ImportError:
        return {"code": None, "verified": False, "place": None}

    def cached(la, lo):
        import os
        old = os.environ.get("JURISDICTION_OFFLINE")
        os.environ["JURISDICTION_OFFLINE"] = "1"
        try:
            return fetch_census(la, lo)
        finally:
            if old is None:
                os.environ.pop("JURISDICTION_OFFLINE", None)
            else:
                os.environ["JURISDICTION_OFFLINE"] = old
    return resolve_jurisdiction(lat, lon, fetch=cached)


def scan(budget, reserve=0, kit=None, center=None, radius_mi=None, exclude=(), include=None, top=8, property_ids=None):
    """Every listed space x every business through the gates; returns the funnel, directions and best pairs.

    include limits the businesses (I know what I want to open); property_ids limits the buildings (I have a space).
    The last gate is the compliance preflight from the jurisdiction cache: who permits each surviving building."""
    from venture_costs import cash_to_open, rent_for
    started = time.perf_counter()
    if property_ids:
        outside = [b for b in load_market(kit, "los_angeles") if b["property_id"] in property_ids]
        if outside:
            return scan_outside(budget, reserve, kit, outside, exclude=exclude, include=include, top=top)
    market, index = load_index(kit)
    budget, reserve = float(budget), float(reserve or 0)
    keys = [k for k in INDUSTRIES if k not in set(exclude) and (not include or k in set(include))]
    excluded_groups = sorted({INDUSTRIES[k]["group"] for k in INDUSTRIES if k not in keys})
    funnel = {"spaces": 0, "combinations": 0, "building_can_host": 0, "affordable": 0, "neighborhood_support": 0,
              "cost_not_estimated": 0, "rent_not_listed": 0}
    pairs, unpriced = [], {}
    where, tally = {}, {"houston": 0, "harris_unincorporated": 0, "other_city": 0, "unverified": 0}
    fate = {}  # business -> (furthest gate 0-3, reason), filled only when scanning chosen buildings
    reach = {}  # building -> furthest gate any business x space there passed (0 none, 1 building, 2 cash, 3 neighborhood)
    for pid, b in market.items():
        if center and radius_mi and nb.miles(center[0], center[1], b["lat"], b["lon"]) > radius_mi:
            continue
        if property_ids and pid not in property_ids:
            continue
        row = index.get(pid)
        if not row:
            continue
        reach[pid] = 0
        for sp in b["spaces"]:
            funnel["spaces"] += 1
            rent = rent_for(sp)
            for key in keys:
                funnel["combinations"] += 1
                fit = row["fits"].get(sp["value"], {}).get(key)
                if not fit or fit["status"] == "no":
                    if property_ids and fate.get(key, (-1,))[0] < 0:
                        fate[key] = (0, "; ".join((fit or {}).get("reasons") or ["No space needs recorded"]))
                    continue
                funnel["building_can_host"] += 1
                reach[pid] = max(reach[pid], 1)
                cost = cash_to_open(key, sp["sf"], rent["monthly"], budget=budget, reserve=reserve,
                                    building_type=b["facts"].get("type"))
                if cost["affordability"] == "over":
                    if property_ids and fate.get(key, (-1,))[0] < 1:
                        fate[key] = (1, f"Cash to open + reserve is ${cost['required']['low']:,.0f} even in the low case, "
                                        f"over the ${budget:,.0f} budget")
                    continue
                demand = row["demand"].get(key)
                if cost["affordability"] == "unknown":
                    # No cash figure (no comparable benchmark, or no rent): never counted as affordable.
                    funnel["cost_not_estimated"] += 1
                    if demand and promising(demand):
                        u = unpriced.setdefault(key, {"business": key, "label": INDUSTRIES[key]["label"], "places": 0,
                                                      "why": cost["note"]})
                        u["places"] += 1
                    continue
                funnel["affordable"] += 1
                reach[pid] = max(reach[pid], 2)
                funnel["rent_not_listed"] += cost["affordability"] == "rent_unknown"
                if not demand or not promising(demand):
                    if property_ids and fate.get(key, (-1,))[0] < 2:
                        verdict = (demand or {}).get("verdict") or "unknown"
                        fate[key] = (2, "Neighborhood: not enough evidence to call it" if verdict == "unknown" else
                                     f"Neighborhood: {verdict}" + (": " + demand["reasons"][0].lower()
                                                                    if demand.get("reasons") else ""))
                    continue
                funnel["neighborhood_support"] += 1
                reach[pid] = max(reach[pid], 3)
                if pid not in where:
                    where[pid] = jurisdiction_cached(b["lat"], b["lon"])
                w = where[pid]
                tally[w.get("code") if w.get("code") in tally else "other_city" if w.get("place") else "unverified"] += 1
                pairs.append({
                    "business": key, "label": INDUSTRIES[key]["label"], "group": INDUSTRIES[key]["group"],
                    "property_id": pid, "address": b["address"], "lat": b["lat"], "lon": b["lon"],
                    "suite": sp["value"], "suite_label": f"Suite {sp['suite']}" if sp.get("suite") else f"{sp['sf']:,.0f} sq ft space",
                    "sf": sp["sf"], "rent": rent, "building": fit, "demand": demand,
                    "cost": {k: cost.get(k) for k in ("affordability", "cash_to_open", "required", "headroom",
                                                       "months_of_rent_after_opening", "basis_quality", "excludes",
                                                       "note")},
                    "evidence_flags": b["evidence_flags"], "tours": b["tours"],
                })
    tier = {"supported": 0, "mixed": 1}
    afford = {"within": 0, "stretch": 1, "rent_unknown": 2}
    pairs.sort(key=lambda p: (tier[p["demand"]["verdict"]], afford[p["cost"]["affordability"]],
                              p["building"]["status"] != "fits", -p["demand"]["score"], bool(p["evidence_flags"]),
                              -(p["cost"]["headroom"] or -1e12)))
    # One best space per building per business keeps the shortlist diverse.
    best, seen = [], set()
    for p in pairs:
        if (p["business"], p["property_id"]) in seen:
            continue
        seen.add((p["business"], p["property_id"]))
        best.append(p)
    directions = {}
    for p in best:
        d = directions.setdefault(p["business"], {"business": p["business"], "label": p["label"], "group": p["group"],
                                                  "places": 0, "supported": 0, "within_budget": 0,
                                                  "only_low_case": 0, "rent_not_listed": 0, "cash_to_open": [],
                                                  "pairs": [],
                                                  "excludes": p["cost"].get("excludes"),
                                                  "cost_basis": p["cost"]["basis_quality"]})
        d["places"] += 1
        d["supported"] += p["demand"]["verdict"] == "supported"
        d["within_budget"] += p["cost"]["affordability"] == "within"
        d["only_low_case"] += p["cost"]["affordability"] == "stretch"
        d["rent_not_listed"] += p["cost"]["affordability"] == "rent_unknown"
        if p["cost"]["cash_to_open"]["base"] is not None and p["cost"]["affordability"] != "rent_unknown":
            d["cash_to_open"].append(p["cost"]["cash_to_open"]["base"])
        if len(d["pairs"]) < 3:
            d["pairs"].append(p)
    ranked = sorted(directions.values(), key=lambda d: (-min(d["supported"], d["within_budget"]), -d["supported"],
                                                        -d["within_budget"], -d["places"]))
    for d in ranked:
        costs = sorted(d.pop("cash_to_open"))
        d["cash_to_open_range"] = [costs[0], costs[-1]] if costs else None
        d["why"] = (d["pairs"][0]["demand"]["reasons"] or ["Neighborhood signals are mixed"])[0]
    # Compliance preflight (Jayant's jurisdiction lookup, cached): who permits each surviving building. Residential
    # buildings already failed the building gate. Another city's or an unverified jurisdiction is kept but flagged.
    for p in pairs:
        w = where.get(p["property_id"]) or {}
        p["jurisdiction"] = {"code": w.get("code"), "place": w.get("place"), "verified": bool(w.get("verified"))}
    funnel["compliance"] = tally
    funnel["authority_identified"] = tally["houston"] + tally["harris_unincorporated"]
    for pid, w in where.items():
        if w.get("verified"):
            reach[pid] = 4
    dots = [[round(market[pid]["lat"], 4), round(market[pid]["lon"], 4), stage] for pid, stage in reach.items()
            if market[pid].get("lat") is not None and market[pid].get("lon") is not None]
    return {"budget": budget, "reserve": reserve, "funnel": funnel, "directions": ranked,
            "map": {"buildings": dots, "after_gate": [sum(1 for d in dots if d[2] >= k) for k in range(5)],
                    "note": "One dot per building with listed space; it stays lit while any business x space there "
                            "passes the gate."},
            "cost_not_estimated": sorted(unpriced.values(), key=lambda u: -u["places"]),
            "pairs": best[:top * 5], "all_pairs": len(best),
            "not_here": sorted(({"business": k, "label": INDUSTRIES[k]["label"], "group": INDUSTRIES[k]["group"],
                                 "stopped_at": ["building", "cash", "neighborhood"][g], "why": why}
                                for k, (g, why) in fate.items() if k not in {p["business"] for p in pairs}),
                               key=lambda r: (["neighborhood", "cash", "building"].index(r["stopped_at"]), r["label"])), "excluded_groups": excluded_groups, "businesses_considered": len(keys),
            "elapsed_ms": round(1000 * (time.perf_counter() - started)),
            "basis": ("Gates in order: the building can host the business (CoStar facts vs space needs), cash to open "
                      "plus your reserve fits the budget (sourced opening-cost benchmarks, labelled; where the listing "
                      "has no rent, the cost before rent must fit and the pair is labelled), and the "
                      "neighborhood supports it (supported, or mixed with a positive total), then the compliance "
                      "preflight says who permits it (Census boundary, not the postal city). Lease only. One best "
                      "space per building per business.")}


def scan_outside(budget, reserve, kit, buildings, exclude=(), include=None, top=8):
    """Chosen buildings outside Houston: building fit and cash for every business, in code. The neighborhood model is
    Houston-calibrated, so it is not applied: demand is 'unknown' on every pair and that gate is not counted."""
    from venture_costs import cash_to_open, rent_for
    from venture_review import NOT_CALIBRATED
    started = time.perf_counter()
    budget, reserve = float(budget), float(reserve or 0)
    keys = [k for k in INDUSTRIES if k not in set(exclude) and (not include or k in set(include))]
    funnel = {"spaces": 0, "combinations": 0, "building_can_host": 0, "affordable": 0, "neighborhood_support": 0,
              "neighborhood_not_calibrated": 0, "cost_not_estimated": 0, "rent_not_listed": 0,
              "compliance": {}, "authority_identified": 0}
    pairs, fate = [], {}
    demand = {"verdict": "unknown", "score": 0, "insight": None, "basis": NOT_CALIBRATED,
              "reasons": ["Neighborhood not calibrated for Los Angeles: demand unknown"], "lookalike": None,
              "validated": False}
    for b in buildings:
        for sp in b["spaces"]:
            funnel["spaces"] += 1
            rent = rent_for(sp)
            for key in keys:
                funnel["combinations"] += 1
                fit = nb.building_fit(key, b["facts"], sp["value"])
                if fit["status"] == "no":
                    fate.setdefault(key, (0, "; ".join(fit["reasons"])))
                    continue
                funnel["building_can_host"] += 1
                cost = cash_to_open(key, sp["sf"], rent["monthly"], budget=budget, reserve=reserve,
                                    building_type=b["facts"].get("type"))
                if cost["affordability"] == "over":
                    fate[key] = (1, f"Cash to open + reserve is ${cost['required']['low']:,.0f} even in the low case, "
                                    f"over the ${budget:,.0f} budget")
                    continue
                if cost["affordability"] == "unknown":
                    funnel["cost_not_estimated"] += 1
                    fate[key] = (1, "No comparable opening-cost benchmark")
                    continue
                funnel["affordable"] += 1
                funnel["neighborhood_not_calibrated"] += 1
                funnel["rent_not_listed"] += cost["affordability"] == "rent_unknown"
                pairs.append({
                    "business": key, "label": INDUSTRIES[key]["label"], "group": INDUSTRIES[key]["group"],
                    "property_id": b["property_id"], "address": b["address"], "lat": b["lat"], "lon": b["lon"],
                    "suite": sp["value"], "suite_label": f"Suite {sp['suite']}" if sp.get("suite") else f"{sp['sf']:,.0f} sq ft space",
                    "sf": sp["sf"], "rent": rent, "demand": demand,
                    "building": {k: fit[k] for k in ("status", "capacity", "reasons", "unknowns")},
                    "cost": {k: cost.get(k) for k in ("affordability", "cash_to_open", "required", "headroom",
                                                       "months_of_rent_after_opening", "basis_quality", "excludes",
                                                       "note")},
                    "evidence_flags": b["evidence_flags"], "tours": b["tours"], "market": b.get("market")})
    afford = {"within": 0, "stretch": 1, "rent_unknown": 2}
    pairs.sort(key=lambda p: (afford[p["cost"]["affordability"]], p["building"]["status"] != "fits",
                              -(p["cost"]["headroom"] or -1e12)))
    best, seen = [], set()
    for p in pairs:
        if (p["business"], p["property_id"]) not in seen:
            seen.add((p["business"], p["property_id"]))
            best.append(p)
    directions = []
    for p in best:
        c = p["cost"]["cash_to_open"]["base"]
        directions.append({"business": p["business"], "label": p["label"], "group": p["group"], "places": 1,
                           "supported": 0, "within_budget": int(p["cost"]["affordability"] == "within"),
                           "only_low_case": int(p["cost"]["affordability"] == "stretch"),
                           "rent_not_listed": int(p["cost"]["affordability"] == "rent_unknown"),
                           "excludes": p["cost"].get("excludes"), "cost_basis": p["cost"]["basis_quality"],
                           "cash_to_open_range": [c, c] if c is not None else None,
                           "why": "Fits physically and financially; " + demand["reasons"][0][0].lower() + demand["reasons"][0][1:],
                           "pairs": [p]})
    return {"budget": budget, "reserve": reserve, "funnel": funnel, "directions": directions,
            "map": {"buildings": [[round(b["lat"], 4), round(b["lon"], 4), 2] for b in buildings],
                    "after_gate": [len(buildings)] * 3 + [0, 0], "note": "Los Angeles: neighborhood not calibrated"},
            "cost_not_estimated": [], "pairs": best[:top * 5], "all_pairs": len(best),
            "not_here": sorted(({"business": k, "label": INDUSTRIES[k]["label"], "group": INDUSTRIES[k]["group"],
                                 "stopped_at": ["building", "cash"][g], "why": why}
                                for k, (g, why) in fate.items() if k not in {p["business"] for p in pairs}),
                               key=lambda r: (["cash", "building"].index(r["stopped_at"]), r["label"])),
            "excluded_groups": [], "businesses_considered": len(keys), "market": "los_angeles",
            "elapsed_ms": round(1000 * (time.perf_counter() - started)),
            "basis": "Los Angeles: building fit (CoStar facts vs space needs) and sourced cash to open are checked; "
                     "the neighborhood model is calibrated for Houston and is not applied, so demand is unknown."}


def warm(kit=None):
    """Load the market and index in the background so the first scan is quick."""
    try:
        load_index(kit)
    except (nb.Offline, RuntimeError, OSError, ValueError, KeyError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kit", type=Path)
    parser.add_argument("--build", action="store_true", help="build the market index (once per laptop)")
    parser.add_argument("--budget", type=float)
    parser.add_argument("--reserve", type=float, default=0)
    parser.add_argument("--exclude", default="", help="comma-separated business keys to leave out")
    args = parser.parse_args()
    if args.build:
        build_index(args.kit)
        return
    result = scan(args.budget or 500000, args.reserve, args.kit,
                  exclude=[k for k in args.exclude.split(",") if k])
    print(json.dumps(result["funnel"]), f"{result['elapsed_ms']} ms")
    for d in result["directions"][:12]:
        print(f"{d['label']:32} {d['places']:3} places, {d['supported']} supported, {d['within_budget']} within budget "
              f"(+{d['only_low_case']} low case, +{d['rent_not_listed']} rent not listed) cash {d['cash_to_open_range']}"
              + (f" excl. {d['excludes']}" if d["excludes"] else "") + f" | {d['why'][:60]}")
    for u in result["cost_not_estimated"]:
        print(f"  cost not estimated: {u['label']} ({u['places']} places with neighborhood support)")


if __name__ == "__main__":
    main()
