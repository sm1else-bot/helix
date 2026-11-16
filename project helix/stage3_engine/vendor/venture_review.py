"""Deep review of one business in one listed space, for any of the 26 businesses (venture Stage 5).

A traced team of agents, the same trace shape as the neighborhood check:

  building      can this space host it? (neighborhood.building_fit)
  neighborhood  does the area support it? (neighborhood.investigate, all demand specialists)
  money         itemized cash to open from sourced benchmarks, scaled to this space; reserve; months of rent
                left after opening with zero revenue; what-if: opening delay
  deal          asking rent vs the rent model's range for this space; lease basis
  records       do CoStar and the linked scans agree? (address, suite, size, scan dates)
  permits       Houston's change-of-use path plus the business-specific licenses (official sources)
  verdict       GO / CONDITIONAL / NO-GO with the deciding reason, conditions and what would flip it

Everything is computed in code from cached data; the review never forecasts revenue.
"""
import json
import time
from functools import lru_cache
from pathlib import Path

import neighborhood as nb
from neighborhood_industries import INDUSTRIES
from reconcile import record

HOUSTON_STEPS = [  # Houston process (approvals_timeline.REFERENCES), applied to every business
    ("Allowed use and deed restrictions", "Houston has no conventional zoning, but recorded deed restrictions and "
     "development rules (parking, access) can limit a use. Ask the landlord and check the deed restrictions.",
     "houston-deeds"),
    ("Certificate of Occupancy (change of use)", "Houston requires a Certificate of Occupancy for a commercial lease "
     "space and for a change in occupancy classification. Retrieve the current CO and ask whether this use changes it.",
     "houston-occupancy"),
    ("Building, fire and accessibility review", "Plans for build-out, exits, accessible route and restrooms, fire "
     "protection and signage go through permit review; trade permits follow.", "houston-occupancy"),
    ("Final inspections and occupancy", "Final sign-offs before opening.", "houston-occupancy"),
]


HARRIS_STEPS = [  # unincorporated Harris County: the county permits, not the City of Houston
    ("Allowed use and deed restrictions", "Unincorporated Harris County has no zoning, but recorded deed "
     "restrictions can limit a use. Ask the landlord and check the deed restrictions."),
    ("Commercial building permit and plan review", "Outside city limits, Harris County's Office of the County "
     "Engineer handles commercial building permits; City of Houston permits don't apply."),
    ("Fire and accessibility review", "Fire review, exits and the accessible route go through the county; ask the "
     "county which reviews apply to this use."),
    ("Final inspections and occupancy", "Final sign-offs before opening."),
]
AUTHORITY = {"houston": ("City of Houston (Houston Permitting Center)",
                         "https://www.houstontx.gov/business/plan/permits-inspections.html"),
             "harris_unincorporated": ("Harris County Office of the County Engineer",
                                       "https://oce.harriscountytx.gov/Services/Permits/Permits-A-to-Z/Commercial-Building"),
             "los_angeles": ("City of Los Angeles Building and Safety / Planning",
                             "https://dbs.lacity.gov/services/search-online-building-records"),
             "la_unincorporated": ("LA County Public Works / Planning",
                                   "https://permits.lacounty.gov/permits/building-and-safety/")}
COMPLIANCE_FACTS = ("identity", "occupancy", "fire", "accessibility", "parking", "mep", "business_space_fit",
                    "official_process", "scan_address")


_COMPLIANCE = {}


def compliance(property_id, suite, key, kit=None):
    """Jayant's compliance engine (PR #17), called as is: building preflight + business check, with the official
    research replayed from its cache (live search only when COMPLIANCE_LIVE=1). Never raises: None if unavailable.
    Answers with a resolved jurisdiction are kept in memory (reading the kit for the context takes seconds)."""
    memo = (property_id, suite, key, str(kit))
    if memo in _COMPLIANCE:
        return _COMPLIANCE[memo]
    report = _compliance(property_id, suite, key, kit)
    if report and ((report.get("jurisdiction") or {}).get("verified") or (report.get("jurisdiction") or {}).get("place")):
        _COMPLIANCE[memo] = report
    return report


def _compliance(property_id, suite, key, kit=None):
    import os
    try:
        import compliance_engine as ce
        from jurisdiction import offline
    except ImportError:
        return None
    try:
        try:
            context = ce.context_from_kit(property_id, suite, str(kit) if kit else None)
        except ValueError:  # the suite label isn't a listed suite the engine can match: building scope
            context = ce.context_from_kit(property_id, None, str(kit) if kit else None)
        live = os.environ.get("COMPLIANCE_LIVE") == "1" and not offline()
        search = ce.cached_search(ce.tavily_search, ce.RESEARCH_CACHE, offline=not live)
        return ce.check_business(context, key, search)
    except Exception as error:  # noqa: BLE001  any engine failure leaves permits on the stated assumption
        return {"jurisdiction": {"code": None, "verified": False,
                                 "note": f"compliance engine unavailable ({type(error).__name__})"},
                "records": [], "research": [], "gates": []}


def jurisdiction_record(where):
    code = where.get("code")
    return record("jurisdiction", where.get("place") or where.get("county") or code, "public:census-geocoder",
                  "census-geocoder", (where.get("retrieved_at") or "")[:10] or None, 0.9 if where.get("verified") else 0.3,
                  where.get("evidence_url"), "agrees" if where.get("verified") or where.get("place") else "unknown",
                  note=where.get("note") or "")


SOURCE_NAMES = {"public:acs": "U.S. Census ACS 5-year", "costar": "CoStar kit", "computed": "computed in code",
                "public:census-geocoder": "U.S. Census geocoder", "venture_costs": "cost ledger (sourced benchmarks)"}
SIGNAL_AGENT = {"lookalike": "lookalike", "local supply": "competitors", "population trend": "residents",
                "business arrivals": "momentum"}


def money_text(x):
    return "unknown" if x is None else f"${x:,.0f}"


def signal_display(name, value):
    if value is None:
        return "unknown"
    if name == "lookalike":
        return f"{value}th percentile"
    if name == "local supply":
        return f"{value:g}x the Houston-area rate"
    if name == "population trend":
        return f"{value * 100:+.1f} pts vs the Houston area"
    if name == "business arrivals":
        return f"{value:+d} net arrivals"
    return str(value)


def signal_rule(name):
    return {
        "lookalike": f"+1 at the {nb.LOOKALIKE_PLUS}th percentile or above, -1 at the {nb.LOOKALIKE_MINUS}th or "
                     f"below; votes only if its held-out AUC >= {nb.VALIDATED_AUC}",
        "local supply": f"+1 at <= {nb.SUPPLY_UNDER:g}x the area rate, -1 at >= {nb.SUPPLY_OVER:g}x, only if "
                        f"Poisson p < {nb.SUPPLY_P:g}",
        "population trend": f"+/-1 beyond {nb.TREND_POINTS * 100:g} pts vs the Houston area and beyond the ACS "
                            "margin of error",
        "business arrivals": "+1 if more arrived than left in 3 years, -1 if fewer (needs 2+ moves)",
    }.get(name, "")


def because_rows(*, key, budget, reserve, fit, cost, stress, rent, decision, signals, deal_step, records_step, permits_step,
            blockers, conditions, verdict, scans_step=None):
    """Every reason behind the verdict as rows {text, effect, decides, agent, signal{name, value, rule}, refs}.

    effect: blocks | check | supports | unknown. `decides` marks the rows the verdict rule actually used (every
    blocker and condition, and the GO criteria); the others inform. `refs` are (agent, fact prefix) pairs that
    review() turns into indexes into evidence[]. Built from the same values as the verdict, so the UI never
    re-derives logic.
    """
    rows = []

    def add(effect, agent, text, name, value, rule, refs=(), decides=False):
        rows.append({"text": text, "effect": effect, "decides": decides, "agent": agent,
                     "signal": {"name": name, "value": value, "rule": rule}, "refs": list(refs)})
    need = INDUSTRIES[key]["space"]
    typical = (f"typical {need['min_sf']:,}-{need['max_sf']:,} sq ft" if need.get("max_sf")
               else f"at least {need['min_sf']:,} sq ft")
    size = f"{fit['space_sf']:,.0f} sq ft" if fit.get("space_sf") else "size not recorded"
    fit_refs = [("building", f"{key}.building_fit")]
    if fit["status"] == "fits":
        add("supports", "building", "The space fits: " + "; ".join(fit["reasons"])
            + (f". Room for {fit['capacity']}" if fit.get("capacity") else ""), "building fit", size,
            f"{typical}; a building type that commonly hosts it", fit_refs, decides=True)
    elif fit["status"] == "check":
        for u in fit["unknowns"]:
            add("check", "building", u, "building fit", size, typical, fit_refs, decides=True)
    else:
        text = next((b for b in blockers if b.startswith("The space can't host it")),
                    "The space can't host it: " + "; ".join(fit["reasons"]))
        add("blocks", "building", text, "building fit", size, typical, fit_refs, decides=True)

    afford = cost.get("affordability", "unknown")
    req, total = cost.get("required") or {}, cost.get("cash_to_open") or {}
    money_refs = [("money", f"{key}.cash_to_open"), ("money", f"{key}.budget_check")]
    rule = f"cash to open + {money_text(reserve)} reserve <= {money_text(budget)} budget"
    if afford == "within":
        months = cost.get("months_of_rent_after_opening")
        add("supports", "money", f"Affordable: {money_text(total.get('base'))} to open + {money_text(reserve)} "
            f"reserve = {money_text(req.get('base'))}, {money_text(cost.get('headroom'))} under budget"
            + (f" (about {months:g} months of rent to spare with zero revenue)" if months else ""),
            "cash to open + reserve (base)", money_text(req.get("base")), rule, money_refs, decides=True)
    elif afford == "stretch":
        add("check", "money", f"Only affordable in the low-cost case: base case needs {money_text(req.get('base'))}"
            f" (low case {money_text(req.get('low'))})", "cash to open + reserve (base)", money_text(req.get("base")), rule,
            money_refs, decides=True)
    elif afford == "over":
        add("blocks", "money", f"Cash to open plus reserve ({money_text(req.get('low'))} even in the low case) "
            f"exceeds the {money_text(budget)} budget", "cash to open + reserve (low)", money_text(req.get("low")),
            rule.replace("cash to open", "low-case cash to open"), money_refs, decides=True)
    elif afford == "rent_unknown":
        add("check", "money", "The listing has no asking rent: cash excludes the deposit and rent while building; "
            "ask the broker", "monthly rent", "not listed", rule, money_refs, decides=True)
    else:
        add("check", "money", "Cash to open is unknown (no comparable cost benchmark for this business): get quotes",
            "cash to open", "unknown", rule, money_refs, decides=True)
    for gap in cost.get("unpriced") or []:
        add("check", "money", gap, "unpriced cost line", "not priced", "every cost line priced from a source",
            money_refs, decides=True)
    if afford == "within" and stress and stress.get("required", {}).get("base") is not None:
        late = stress["required"]["base"]
        add("supports" if stress["affordability"] == "within" else "check", "money",
            (f"Survives a 3-month opening delay: {money_text(late)} needed, still within budget"
             if stress["affordability"] == "within" else
             f"A 3-month opening delay raises the need to {money_text(late)}, over budget: keep a buffer"),
            "3-month delay", money_text(late), rule, [("money", f"{key}.three_month_delay")])

    demand = (decision or {}).get("verdict", "unknown")
    for r in (decision or {}).get("reasons", []):
        if r["stance"] == 0:
            continue  # neutral: shown in the agent run, not a reason
        agent = SIGNAL_AGENT.get(r["signal"], "coordinator")
        effect = "supports" if r["stance"] > 0 else "blocks" if demand == "weak" else "check"
        add(effect, agent, r["reason"], r["signal"], signal_display(r["signal"], (signals.get(r["signal"]) or {}).get("value")),
            signal_rule(r["signal"]), signal_refs(key, r["signal"]))
    for u in (decision or {}).get("unknown", []):
        add("unknown", SIGNAL_AGENT.get(u["signal"], "coordinator"), u["reason"], u["signal"], "unknown",
            signal_rule(u["signal"]), signal_refs(key, u["signal"]))
    verdict_rule = (f"supported: total >= +{nb.VERDICT_TOTAL} incl. a business-specific +1 (lookalike or supply), "
                    "no lookalike -1")
    score = (f"{demand} (total {decision['score']:+d})" if decision and decision.get("score") is not None
             else demand)
    voted = [ref for r in (decision or {}).get("reasons", []) if r["stance"] for ref in signal_refs(key, r["signal"])]
    if demand == "supported":
        add("supports", "coordinator", decision["headline"], "neighborhood verdict", score, verdict_rule, voted,
            decides=True)
    elif demand == "weak":
        add("blocks", "coordinator", next((b for b in blockers if b.startswith("The neighborhood does not")),
                                          decision["headline"]), "neighborhood verdict", score, verdict_rule, voted,
            decides=True)
    else:
        add("check", "coordinator", f"Neighborhood demand is {demand}: "
            + (decision["headline"] if decision else "no verdict"), "neighborhood verdict", score, verdict_rule,
            voted, decides=True)

    deal_findings = (deal_step or {}).get("findings", [])
    vs = next((f for f in deal_findings if f["fact"] == "asking_rent_vs_market"), None)
    if vs:
        inside = vs["status"] == "agrees"
        add("supports" if inside else "check", "deal", ("Asking rent is inside the market range: " if inside else
            "Asking rent is outside the market range (negotiate or ask why): ") + vs["note"], "asking rent",
            f"${vs['value']:.2f}/sq ft/yr", "inside the 10-90% range of similar signed deals",
            [("deal", "asking_rent_vs_market")])
    elif rent.get("kind") == "estimate":
        add("unknown", "deal", rent["source"] + ": rent is an estimate until the broker quotes it", "asking rent",
            "not listed", "asking rent from the listing", [("deal", "monthly_base_rent")])
    if any("lease is pending" in c for c in conditions):
        add("check", "deal", "A lease is pending on this space: confirm it is still available", "lease pending",
            "yes", "space available to lease", [("deal", "monthly_base_rent")], decides=True)

    rec = (records_step or {}).get("findings", [])
    conflicts = [r for r in rec if r["status"] == "conflict"]
    for r in conflicts[:3]:
        add("check", "records", "Records disagree: " + r["note"], r["fact"].replace("_", " "), str(r["value"]),
            "CoStar and the linked scans agree", [("records", r["fact"], r["value"])], decides=True)
    if rec and not conflicts:
        add("supports", "records", f"CoStar's record and the linked scans agree ({len(rec)} checks, no conflicts)",
            "records agree", f"0 of {len(rec)} conflict", "no conflicts between CoStar and the scans",
            [("records", "")])
    unknown_records = [r for r in rec if r["status"] == "unknown"]
    if unknown_records:
        add("unknown", "records", f"{len(unknown_records)} record fact(s) can't be confirmed from the data, e.g. "
            + unknown_records[0]["note"][:140], "unconfirmed records", str(len(unknown_records)),
            "each fact confirmed by a second record", [("records", unknown_records[0]["fact"])])

    for r in (scans_step or {}).get("findings", []):  # Helix Stage 2 physical profile
        if str(r["source"]).startswith("ai:"):
            add("check" if "effect=check" in r["note"] else "unknown", "scans", "AI-flagged, not verified: " + str(r["value"]),
                "AI assessment", r["note"].split(".")[0], "informs only; a person must verify", [("scans", r["fact"], r["value"])])
        elif r["status"] == "conflict":
            add("check", "scans", "Scans and CoStar disagree: " + r["note"].split("] ", 1)[-1], r["fact"].replace("scan.", ""),
                str(r["value"]), "scan observation agrees with CoStar", [("scans", r["fact"], r["value"])], decides=True)
        elif r["status"] == "agrees":
            add("supports", "scans", "Scans confirm CoStar: " + r["note"].split("] ", 1)[-1], r["fact"].replace("scan.", ""),
                str(r["value"]), "scan observation agrees with CoStar", [("scans", r["fact"], r["value"])])
    where = (permits_step or {}).get("jurisdiction") or {}
    j_rule = "permits follow the official boundary (Census), not the postal city"
    j_refs = [("permits", "jurisdiction")]
    if where.get("code") == "houston":
        add("supports", "permits", "Inside City of Houston limits (Census boundary): Houston's permit path applies",
            "jurisdiction", where.get("place") or "Houston", j_rule, j_refs)
    elif where.get("code") == "los_angeles":
        add("supports", "permits", "Inside City of Los Angeles limits (Census boundary): the City's permit path "
            "applies", "jurisdiction", where.get("place") or "Los Angeles", j_rule, j_refs)
    elif where.get("code") == "la_unincorporated":
        add("check", "permits", "Outside Los Angeles city limits (unincorporated LA County): LA County permits it, "
            "not the City of Los Angeles", "jurisdiction", "unincorporated " + (where.get("county") or "LA County"),
            j_rule, j_refs, decides=True)
    elif where.get("code") == "harris_unincorporated":
        add("check", "permits", "Outside Houston city limits (unincorporated Harris County): City of Houston permits "
            "don't apply; Harris County does", "jurisdiction", "unincorporated " + (where.get("county") or "Harris County"), j_rule, j_refs,
            decides=True)
    elif where.get("place"):
        add("check", "permits", f"Inside {where['place']}, which has its own permitting office", "jurisdiction",
            where["place"], j_rule, j_refs, decides=True)
    else:
        add("unknown", "permits", "Jurisdiction not verified: Houston's path is shown as an assumption ("
            + (where.get("note") or "not looked up") + ")", "jurisdiction", "not verified", j_rule, j_refs)
    if "commercial_conversion_unestablished" in ((permits_step or {}).get("gates") or []) and fit["status"] != "no":
        add("blocks", "permits", "CoStar records this as a residential building: commercial use is not established",
            "building gate", "residential", "a commercial building", [("permits", "compliance.")], decides=True)

    # Every blocker and condition must appear as a deciding row (guards against drift between the two lists).
    shown = " ".join(r["text"] for r in rows if r["decides"])
    for text in blockers:
        if text[:40] not in shown:
            add("blocks", "verdict", text, "blocker", "", "", decides=True)
    for text in conditions:
        if text[:40] not in shown:
            add("check", "verdict", text, "condition", "", "", decides=True)
    order = {"blocks": 0, "check": 1, "supports": 2, "unknown": 3}
    rows.sort(key=lambda r: (order[r["effect"]], not r["decides"]))
    return rows


def signal_refs(key, name):
    agent = SIGNAL_AGENT.get(name, "coordinator")
    return [(agent, {"lookalike": f"{key}.lookalike_percentile", "local supply": f"{key}.supply",
                     "population trend": f"{key}.residents_" if INDUSTRIES[key]["demand_band"]
                     else "residents_growth", "business arrivals": f"{key}.kit_net"}.get(name, ""))]


def tours_from(findings, suite):
    """The building's linked Matterport tours, from the records agent's findings (links only; nothing fetched)."""
    tours = {}
    for f in findings:
        url = f.get("evidence_url") or ""
        if "matterport.com" not in url:
            continue
        t = tours.setdefault(url, {"url": url, "model_id": url.split("m=")[-1], "caption": None, "suites": [],
                                   "captured_at": None, "status": "agrees"})
        if f["fact"] == "scan_address":
            t["caption"] = f["value"]
        if f["fact"] == "scan_suite" and isinstance(f["value"], list):
            t["suites"] = f["value"]
        t["captured_at"] = t["captured_at"] or f.get("captured_at")
        if f["status"] == "conflict" or (f["status"] == "unknown" and t["status"] == "agrees"):
            t["status"] = f["status"]
    for t in tours.values():
        other = t["suites"] and suite and str(suite) not in t["suites"]
        t["note"] = (f"Captioned for suite {', '.join(t['suites'])}, not the listed suite {suite}: use it to see the "
                     "building, not this space's measurements." if other else
                     "Caption is not a surveyed join: confirm it shows this space." if t["status"] != "agrees" else
                     "Linked to this property in CoStar.")
    return sorted(tours.values(), key=lambda t: t["captured_at"] or "", reverse=True)


def cash_months(cost, rent, budget, reserve, months=12, delay=3, extra_bills=None, cash_source=None):
    """12 months of cash with ZERO revenue, from the real cost ledger and rent, run through Jayant's Nessie-pilot
    simulator (nessie_pilot.simulate). Opening costs are paid up front, base rent every month; a delay moves the
    opening, not the cash line (with no revenue, rent is owed either way), so it shortens the months you're open
    before the reserve is touched. No revenue, payroll or utilities are modelled: real runway is shorter."""
    from nessie_pilot import simulate
    monthly = (rent or {}).get("monthly")
    lines = cost.get("lines") or []
    if budget is None or not monthly or not lines or (cost.get("cash_to_open") or {}).get("base") is None:
        return None
    building = next((l for l in lines if l["label"].startswith("Rent while you build")), None)
    build_months = cost.get("build_months") or 3
    buyer = {"starting_cash": budget, "recurring_bills": [{"payee": "Base rent (CoStar asking)", "monthly": monthly}]
             + [{"payee": b["payee"], "monthly": b["monthly"]} for b in extra_bills or []],
             "source": cash_source or "your budget"}
    cases = {}
    for case in ("low", "base", "high"):
        upfront = cost["cash_to_open"][case] - (building[case] if building else 0)  # rent is billed monthly instead
        scenario = {"name": case, "upfront_cost": upfront, "permit_months": build_months, "build_months": 0,
                    "build_cost": 0, "monthly_income": 0}
        run = simulate(scenario, buyer, months, reserve)
        late = simulate(scenario, buyer, months, reserve, permit_delay=delay)
        cases[case] = {"cash": [budget] + [r["cash"] for r in run["rows"]], "upfront": round(upfront),
                       "first_month_below_reserve": run["first_month_below_reserve"],
                       "first_month_below_zero": next((r["month"] for r in run["rows"] if r["cash"] < 0), None),
                       "opens": run["opening_month"], "opens_if_delayed": late["opening_month"]}
    # Finance option (Jayant's nessie_loan): if cash alone can't cover opening + reserve, borrow the gap and rerun.
    loan = None
    try:
        import nessie_loan
        need = cost["cash_to_open"]["base"]
        option = nessie_loan.plan(need, budget, reserve)
        if option["needed"]:
            funded = nessie_loan.with_loan(buyer, option)
            runs = {}
            for case in ("low", "base", "high"):
                upfront = cost["cash_to_open"][case] - (building[case] if building else 0)
                scenario = {"name": case, "upfront_cost": upfront, "permit_months": build_months, "build_months": 0,
                            "build_cost": 0, "monthly_income": 0}
                run = simulate(scenario, funded, months, reserve)
                runs[case] = {"cash": [budget + option["amount"]] + [r["cash"] for r in run["rows"]],
                              "first_month_below_reserve": run["first_month_below_reserve"],
                              "first_month_below_zero": next((r["month"] for r in run["rows"] if r["cash"] < 0), None),
                              "opens": run["opening_month"]}
            loan = {k: option[k] for k in ("amount", "apr", "term_months", "monthly_payment", "first_year_interest",
                                           "label")} | {"cases": runs, "engine": "nessie_loan.plan + with_loan (Jayant)"}
    except ImportError:
        loan = None
    return {"months": months, "reserve": reserve, "budget": budget, "monthly_rent": monthly, "delay": delay,
            "loan": loan,
            "other_bills": buyer["recurring_bills"][1:], "cash_source": buyer["source"],
            "cases": cases, "engine": "nessie_pilot.simulate (Jayant's Nessie pilot), zero revenue",
            "note": "Zero revenue: only base rent is charged after the opening costs. No payroll, utilities or "
                    "revenue are modelled, so read it as 'how long could rent alone be covered'."}


NOT_CALIBRATED = ("The neighborhood model is calibrated for the Houston area only (its ACS percentiles, supply rates, "
                  "Texas licensed-childcare data and lookalike models). It is not applied to Los Angeles, so demand "
                  "stays unknown here.")
LA_STEPS = [  # City / County of Los Angeles: generic process only; no Houston or Texas rule is reused
    ("Allowed use (zoning) for this parcel", "Los Angeles has zoning: confirm the parcel's zone allows this use "
     "(ZIMAS for the City of LA; LA County Planning outside city limits)."),
    ("Building permit and plan check for the tenant build-out", "Change of use or occupancy and build-out go through "
     "plan check; trade permits follow."),
    ("Fire and accessibility review", "Exits, occupant load and accessible route are reviewed with the plans."),
    ("Final inspections and Certificate of Occupancy", "Final sign-offs before opening."),
]


def outside_houston(key, building, suite):
    """Review inputs for a building outside Houston: building fit from CoStar facts (national planning ranges), and an
    explicit 'not calibrated' neighborhood instead of Houston-calibrated signals."""
    fit = nb.building_fit(key, building["facts"], suite)
    if INDUSTRIES[key]["space"].get("outdoor_sf_per_unit") or key == "childcare":  # Texas licensing ratios
        fit = dict(fit, capacity=None,
                   reasons=[r for r in fit["reasons"] if "Room for" not in r],
                   unknowns=[u for u in fit["unknowns"] if "outdoor play space" not in u]
                   + ["California childcare licensing ratios are not modelled here: confirm capacity and outdoor "
                      "space with California Community Care Licensing"])
        fit["status"] = "check" if fit["status"] == "fits" else fit["status"]
    city = building["address"].split(",")[-2].strip() if building["address"].count(",") >= 2 else "this market"
    step = lambda agent, role, summary, findings=(), status="ok": {  # noqa: E731
        "agent": agent, "role": role, "computed_by": "code", "inputs": {}, "sources": [], "findings": list(findings),
        "summary": summary, "confidence": None, "handoff": "", "signal": None, "per_industry": {}, "status": status,
        "ms": 0}
    trace = [
        step("planner", "Maps the request to a business", f"{INDUSTRIES[key]['label']} at a {city} listing"),
        step("site", "Finds the point", f"{building['address']} ({building['lat']:.5f}, {building['lon']:.5f})",
             [record("site_point", {"lat": building["lat"], "lon": building["lon"], "address": building["address"]},
                     "costar", f"property-{building['property_id']}", None, 0.9, None, "agrees",
                     note="CoStar property pin.")]),
        step("building", "Can the building host it?", f"{fit['status']}: " + "; ".join(fit["reasons"] + fit["unknowns"]),
             [record(f"{key}.building_fit", {"status": fit["status"], "space_sf": fit["space_sf"]}, "costar",
                     f"property-{building['property_id']}", None, 0.6, None,
                     "unknown" if fit["status"] == "check" else "agrees",
                     note="; ".join(fit["reasons"] + fit["unknowns"]) + ". " + fit["basis"])]),
        step("coordinator", "Combines neighborhood signals", "Not calibrated for " + city + ": demand unknown",
             [record(f"{key}.neighborhood", None, "computed", "neighborhood-model", note=NOT_CALIBRATED)], "unknown"),
    ]
    decision = {"industry": key, "use": INDUSTRIES[key]["label"], "group": INDUSTRIES[key]["group"],
                "verdict": "unknown", "headline": f"Not calibrated for {city}", "score": None,
                "basis": NOT_CALIBRATED, "insight": None, "reasons": [],
                "unknown": [{"signal": "neighborhood model", "reason": NOT_CALIBRATED}],
                "lookalike_percentile": None, "lookalike_validated": False, "building": fit}
    return {"decisions": [decision], "trace": trace, "narration": None, "narration_label": None}


def licenses_for(key):
    try:
        from venture_license_data import LICENSES
    except ImportError:
        return []
    return LICENSES.get(key, [])


@lru_cache(maxsize=256)
def property_record(property_id, kit=""):
    """The full CoStar property record (cached on disk: reading the encrypted zip is slow)."""
    path = nb.CACHE / "property-records" / f"{property_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    from space_fit import load_property
    kit_file = Path(kit) if kit else nb.kit_path()
    import os
    prop = load_property(kit_file, int(property_id), os.environ.get("COSTAR_KIT_PASSWORD", "").encode() or None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prop))
    return prop


def step(trace, agent, role, fn):
    """Run one review agent and record it in the trace (inputs are the review's)."""
    started = time.perf_counter()
    entry = {"agent": agent, "role": role, "computed_by": "code", "inputs": {}, "sources": [], "findings": [],
             "summary": "", "confidence": None, "handoff": "", "signal": None, "per_industry": {}, "status": "ok"}
    try:
        entry.update(fn() or {})
    except nb.Offline as error:
        entry.update(status="unknown", summary=f"Offline and not cached: {error}")
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        entry.update(status="error", summary=f"{type(error).__name__}: {error}")
    entry["ms"] = round(1000 * (time.perf_counter() - started))
    trace.append(entry)
    return entry


def find_space(property_id, suite, kit, key=None):
    """The building and the listed space to review: the chosen suite, else the listed space that best suits the
    business's typical size (not the biggest one: costs scale with floor area)."""
    from opportunity import find_building
    building, _ = find_building(property_id, kit)
    if not building:
        return None, None
    space = next((s for s in building["spaces"] if s["value"] == suite), None)
    if space is None and building["spaces"]:
        pool = [s for s in building["spaces"] if not s.get("executive_suite")] or building["spaces"]
        need = INDUSTRIES[key]["space"] if key else {}
        lo, hi = need.get("min_sf", 0), need.get("max_sf") or need.get("min_sf", 0) * 3 or 1e12
        target = (lo + hi) / 2
        inside = [s for s in pool if lo <= (s["sf"] or 0) <= hi]
        above = [s for s in pool if (s["sf"] or 0) > hi]
        space = (min(inside, key=lambda s: abs(s["sf"] - target)) if inside else
                 min(above, key=lambda s: s["sf"]) if above else max(pool, key=lambda s: s["sf"] or 0))
    return building, space


def review(business, property_id, suite=None, budget=None, reserve=0.0, kit=None, delay_months=0, explain=False,
           bank=None):
    """Full review of `business` (registry key or description) in a listed space."""
    from venture_costs import cash_to_open, flip, rent_estimates, rent_for
    started = time.perf_counter()
    key = business if business in INDUSTRIES else nb.resolve(business)[0]
    if not key:
        raise ValueError(f"Could not match '{business}' to a business type.")
    spec = INDUSTRIES[key]
    building, space = find_space(property_id, suite, kit, key)
    if not building or not space:
        raise ValueError("This property has no active lease listing in the kit.")
    suite = space["value"]
    trace, conditions, blockers = [], [], []

    market = building.get("market", "houston")
    hood = (nb.investigate(key, property_id=int(property_id), kit=kit, suite=suite, explain=explain)
            if market == "houston" else outside_houston(key, building, suite))
    decision = hood["decisions"][0] if hood["decisions"] else None
    fit = (decision or {}).get("building") or nb.building_fit(key, building["facts"], suite)
    trace.extend(s for s in hood["trace"] if s["agent"] in ("planner", "site", "building"))

    rent = rent_for(space)
    money_holder = {}

    def money():
        btype = building["facts"].get("type")
        cost = cash_to_open(key, space["sf"], rent["monthly"], budget=budget, reserve=reserve, building_type=btype)
        delayed = (cash_to_open(key, space["sf"], rent["monthly"], budget=budget, reserve=reserve, building_type=btype,
                                build_months=cost["build_months"] + delay_months) if delay_months else None)
        stress = cash_to_open(key, space["sf"], rent["monthly"], budget=budget, reserve=reserve, building_type=btype,
                              build_months=cost.get("build_months", 3) + 3)
        money_holder.update(cost=cost, delayed=delayed, stress=stress, flip=flip(cost, budget))
        total = cost["cash_to_open"]
        findings = [record(f"{key}.cash_to_open", total, "computed", "venture_costs", None,
                           0.5 if cost["basis_quality"] == "itemized" else 0.3, None,
                           "agrees" if total["base"] else "unknown",
                           note=f"Low/base/high from {cost['sources'][0]['publisher_or_brand'] if cost['sources'] else 'no source'}"
                                f", scaled to {space['sf']:,.0f} sq ft. {cost['note']}".strip())]
        if budget is not None and cost["required"]["base"] is not None:
            findings.append(record(f"{key}.budget_check", cost["affordability"], "computed", "venture_costs", None,
                                   0.5, None, "agrees" if cost["affordability"] != "unknown" else "unknown",
                                   note=f"Cash to open ${total['base']:,.0f} + reserve ${reserve:,.0f} = "
                                        f"${cost['required']['base']:,.0f} vs budget ${budget:,.0f}."))
        if stress["required"]["base"] is not None and budget is not None:
            findings.append(record(f"{key}.three_month_delay", stress["required"]["base"], "computed", "venture_costs",
                                   None, 0.4, None, "agrees",
                                   note=f"If opening slips 3 months, rent while building adds "
                                        f"${stress['cash_to_open']['base'] - total['base']:,.0f}; affordability "
                                        f"becomes '{stress['affordability']}'."))
        summary = (f"Cash to open ${total['base']:,.0f} (range ${total['low']:,.0f}-${total['high']:,.0f}); "
                   f"{cost['affordability']} for a ${budget:,.0f} budget with ${reserve:,.0f} reserve"
                   + (f"; about {cost['months_of_rent_after_opening']} months of rent left after opening"
                      if cost["months_of_rent_after_opening"] else "")
                   if total["base"] is not None and budget is not None else cost["note"])
        return {"summary": summary, "findings": findings, "confidence": 0.5,
                "sources": [{"name": s["publisher_or_brand"] + " · " + s["document"], "url": s["url"],
                             "retrieved_at": None, "cached": True, "note": s.get("quote", "")} for s in cost["sources"]],
                "handoff": "Cash verdict and the 3-month delay stress to the verdict."}
    step(trace, "money", "What it costs to open here, from sourced opening-cost benchmarks scaled to this space",
         money)

    def deal():
        est = ((rent_estimates() or {}).get("spaces") or {}).get(str(space.get("space_id")))
        findings, summary = [], rent["source"]
        if rent["kind"] == "listed" and est and not est.get("low_confidence"):
            psf = rent["psf_year"]
            where = "inside" if est["low"] <= psf <= est["high"] else "above" if psf > est["high"] else "below"
            findings.append(record("asking_rent_vs_market", psf, "costar", space.get("listing_id"), None, 0.6, None,
                                   "agrees" if where == "inside" else "conflict",
                                   note=f"Asking ${psf:.2f}/sq ft/yr vs ${est['low']:.2f}-${est['high']:.2f} that "
                                        f"similar spaces were asking when they leased ({est['method']})."))
            summary = f"Asking ${psf:.2f}/sq ft/yr ({space['lease_basis']}) is {where} the market range"
        findings.append(record("monthly_base_rent", rent["monthly"], "costar" if rent["kind"] == "listed" else "computed",
                               space.get("listing_id"), space.get("available_on"), 0.6 if rent["kind"] == "listed" else 0.4,
                               None, "agrees" if rent["monthly"] else "unknown",
                               note=rent["source"] + (". A lease is pending on this space." if space.get("lease_pending") else "")
                               + (" NNN: taxes, insurance and common-area costs come on top." if "NNN" in str(space.get("lease_basis")) else "")))
        if space.get("lease_pending"):
            conditions.append("A lease is pending on this space: confirm it is still available.")
        return {"summary": summary, "findings": findings, "confidence": 0.6,
                "handoff": "Rent feeds the money agent; a pending lease becomes a condition."}
    deal_step = step(trace, "deal", "Is the asking rent fair for this space?", deal)

    def records():
        from reconcile import check_identity, check_scan_dates, check_size, check_suite_identity
        prop = property_record(int(property_id), str(kit or ""))
        rows = check_identity(prop) + check_suite_identity(prop, space.get("suite")) + check_size(prop) + check_scan_dates(prop)
        conflicts = [r for r in rows if r["status"] == "conflict"]
        for r in conflicts[:3]:
            conditions.append(f"Records disagree: {r['note'][:160]}")
        return {"findings": rows, "confidence": 0.7,
                "summary": (f"{len(conflicts)} conflict(s) between CoStar and the linked scans" if conflicts
                            else f"No conflicts in {len(rows)} checks") + f"; {building['tours']} linked tour(s).",
                "handoff": "Conflicts become conditions on the verdict."}
    records_step = step(trace, "records", "Do CoStar's record and the linked scans agree?", records)

    scans_step = None
    try:
        import vision_assess
        profile = vision_assess.load_profile(property_id)
    except (ImportError, OSError, ValueError):
        profile = None
    if profile:  # Helix Stage 2 physical profile (Matterport + Vexcel): deterministic checks, then the bounded AI layer
        def scans():
            facts = dict(building["facts"])
            recs = vision_assess.deterministic(profile, facts)
            ai, meta = vision_assess.ai_assess(profile, key)
            for r in recs:
                if r["status"] == "conflict":
                    conditions.append("Scans and CoStar disagree: " + r["note"].split("] ", 1)[-1][:150])
            agree = sum(r["status"] == "agrees" for r in recs)
            return {"findings": recs + ai, "confidence": 0.5, "ai": meta,
                    "inputs": {"profile": profile.get("source") or "Helix Stage 2",
                               "observations": len(profile.get("observations", []))},
                    "sources": [{"name": f"{profile.get('source') or 'Helix Stage 2'} physical profile",
                                 "url": None, "retrieved_at": profile.get("generated_at"), "cached": True,
                                 "note": "Matterport + Vexcel observations"}],
                    "summary": (f"{len(recs)} observation(s) from the scans: {agree} agree with CoStar, "
                                f"{sum(r['status'] == 'conflict' for r in recs)} conflict"
                                + (f"; {len(ai)} AI-flagged constraint(s), not verified" if ai else "")
                                + (f" ({meta['error']})" if meta.get("error") else "")),
                    "handoff": "Scan vs CoStar conflicts become conditions. AI findings are for a person to verify; "
                               "they never decide the verdict."}
        scans_step = step(trace, "scans", "What do the Matterport and Vexcel scans show about the space?", scans)

    compliance_holder = {}

    def permits():
        from approvals_timeline import REFERENCES
        report = compliance(int(property_id), space.get("suite"), key, kit)
        compliance_holder["report"] = report
        where = (report or {}).get("jurisdiction") or {}
        code = where.get("code")
        authority, authority_url = AUTHORITY.get(code, AUTHORITY["houston" if market == "houston" else "los_angeles"])
        if code == "harris_unincorporated":
            steps = [{"title": t, "detail": d, "authority": authority, "url": authority_url, "status": "to verify"}
                     for t, d in HARRIS_STEPS]
        elif market != "houston" or code in ("los_angeles", "la_unincorporated"):
            steps = [{"title": t, "detail": d, "authority": authority, "url": authority_url, "status": "to verify"}
                     for t, d in LA_STEPS]
        else:
            steps = [{"title": t, "detail": d, "authority": "City of Houston", "url": REFERENCES[ref][1],
                      "status": "to verify"} for t, d, ref in HOUSTON_STEPS]
        licenses = licenses_for(key) if market == "houston" else []
        if market != "houston":
            steps.insert(-1, {"title": "Business licences for this business (California / Los Angeles)",
                              "detail": "Not modelled here: the licence list is Houston and Texas only. Check the "
                                        "City of LA Office of Finance and the relevant state board for this business.",
                              "authority": authority, "url": authority_url, "status": "to verify"})
        for lic in licenses:
            city_only = code == "harris_unincorporated" and "Houston" in lic["authority"]
            steps.insert(-1, {"title": lic["name"], "detail": (lic.get("applies_when") or "") + (
                ". This is a City of Houston permit; outside city limits ask Harris County for the county "
                "equivalent." if city_only else ""), "authority": lic["authority"], "url": lic["url"],
                "status": "to verify"})
        findings = [jurisdiction_record(where)] if report else []
        findings += [record(f"{key}.license.{i}", lic["name"], "public:" + lic["authority"], lic["url"], None, 0.7,
                            lic["url"], "agrees", note=f"{lic.get('applies_when') or 'Required'}: \"{lic.get('quote', '')}\"")
                     for i, lic in enumerate(licenses)]
        for r in (report or {}).get("records", []):  # the compliance engine's own records, as it returned them
            if r["fact"] in COMPLIANCE_FACTS:
                findings.append({**r, "fact": "compliance." + r["fact"]})
        for i, page in enumerate((report or {}).get("research", [])[:4]):
            findings.append(record(f"compliance.official_page.{i}", page["title"] or page["url"],
                                   "public:" + (report.get("authority") or "official site"), page["url"],
                                   (page.get("retrieved_at") or "")[:10], 0.5, page["url"], "unknown",
                                   needs_human=True, note="Official process page (applicability unverified): "
                                                          + page["excerpt"][:240]))
        gates = (report or {}).get("gates") or []
        where_text = ("not looked up (" + (where.get("note") or "compliance engine unavailable") + "): Houston's "
                      "path is shown as an assumption" if not where.get("verified") and not where.get("place")
                      else where.get("note", ""))
        sources = []
        if where.get("evidence_url"):
            sources.append({"name": "U.S. Census geocoder (incorporated places, counties)",
                            "url": where["evidence_url"], "retrieved_at": where.get("retrieved_at"), "cached": True,
                            "note": where.get("note", "")})
        sources += [{"name": lic["authority"], "url": lic["url"], "retrieved_at": None, "cached": True,
                     "note": lic["name"]} for lic in licenses]
        sources += [{"name": page["title"][:80] or "Official page", "url": page["url"],
                     "retrieved_at": page.get("retrieved_at"), "cached": True, "note": "applicability unverified"}
                    for page in (report or {}).get("research", [])[:4]]
        return {"findings": findings, "steps": steps, "confidence": 0.6, "sources": sources,
                "inputs": {"lat": building["lat"], "lon": building["lon"], "suite": space.get("suite")},
                "jurisdiction": {k: where.get(k) for k in ("code", "verified", "place", "county", "note",
                                                         "evidence_url", "retrieved_at")},
                "authority": {"name": authority, "url": authority_url}, "gates": gates,
                "summary": f"{where_text.split('. Census TIGER')[0]}. {len(steps)} steps with {authority} + "
                           f"{len(licenses)} business-specific license(s). No timelines claimed; nothing is "
                           "counted as approved.",
                "handoff": "Steps go to the plan; none are counted as cleared. Jurisdiction and building gates go "
                           "to the verdict."}
    permits_step = step(trace, "permits", "Who permits this address, and what must be approved before opening?",
                        permits)
    where = (permits_step.get("jurisdiction") or {})
    gates = permits_step.get("gates") or []
    if where.get("code") == "harris_unincorporated":
        conditions.append("Outside Houston city limits (unincorporated Harris County): City of Houston permits "
                          "don't apply; Harris County does")
    elif where.get("code") == "la_unincorporated":
        conditions.append("Outside Los Angeles city limits (unincorporated LA County): LA County permits it, not the "
                          "City of Los Angeles")
    elif not where.get("code") and where.get("place"):
        conditions.append(f"Inside {where['place']}, which has its own permitting office: Houston's path does "
                          "not apply")
    if "commercial_conversion_unestablished" in gates and fit["status"] != "no":
        blockers.append("CoStar records this as a residential building: commercial use is not established")
    if "identity_conflict" in gates:
        conditions.append("The compliance check found an identity conflict: confirm the building and suite")

    # --- Verdict -------------------------------------------------------------------------
    cost = money_holder.get("cost") or {}
    demand = decision["verdict"] if decision else "unknown"
    afford = cost.get("affordability", "unknown")
    reasons = []
    if fit["status"] == "no":
        blockers.append("The space can't host it: " + "; ".join(r for r in fit["reasons"] if any(
            w in r for w in ("below", "Not a typical", "Residential"))))
    elif fit["status"] == "check":
        conditions.extend(fit["unknowns"])
    if afford == "over":
        blockers.append(f"Cash to open plus reserve (${cost['required']['low']:,.0f} even in the low case) exceeds "
                        f"the ${budget:,.0f} budget")
    elif afford == "stretch":
        conditions.append(f"Only affordable in the low-cost case: base case needs ${cost['required']['base']:,.0f}")
    elif afford == "unknown":
        conditions.append("Cash to open is unknown (no comparable cost benchmark for this business): get quotes")
    elif afford == "rent_unknown":
        conditions.append("The listing has no asking rent: cash excludes the deposit and rent while building; ask the broker")
    for gap in cost.get("unpriced") or []:
        conditions.append(gap)
    if demand == "weak":
        blockers.append("The neighborhood does not look like it supports this business: "
                        + (decision["reasons"][0]["reason"] if decision and decision["reasons"] else ""))
    elif demand in ("mixed", "unknown"):
        conditions.append(f"Neighborhood demand is {demand}: "
                          + (decision["headline"] if decision else "no verdict"))
    verdict = "NO-GO" if blockers else ("GO" if not conditions and demand == "supported" and afford == "within"
                                        and fit["status"] == "fits" else "CONDITIONAL")
    if verdict == "NO-GO":
        reasons = blockers
    elif verdict == "GO":
        reasons = [f"Fits ({fit.get('capacity') or 'size and type'}), affordable with "
                   f"{cost.get('months_of_rent_after_opening')} months of rent to spare, and the neighborhood supports it"]
    else:
        reasons = conditions[:3]
    flips = []
    if money_holder.get("flip"):
        flips.append(money_holder["flip"]["text"])
    if fit["status"] != "fits" and fit.get("unknowns"):
        flips.append("Confirm: " + fit["unknowns"][0])
    if demand == "mixed" and decision:
        unknown = [u["signal"] for u in decision["unknown"]]
        if unknown:
            flips.append("More evidence on " + ", ".join(unknown[:2]) + " could settle demand")
    trace.append({"agent": "verdict", "role": "Combines space, money, demand, records and permits into one call",
                  "computed_by": "code", "inputs": {}, "sources": [], "findings": [], "signal": None,
                  "per_industry": {}, "status": "ok", "confidence": None, "ms": 0,
                  "summary": f"{verdict}: " + (reasons[0] if reasons else ""),
                  "handoff": "Verdict, conditions and next checks to the person."})
    neighborhood_steps = [s for s in hood["trace"] if s["agent"] in ("residents", "competitors", "momentum", "lookalike",
                                                                     "coordinator", "narrator")]
    order = ["planner", "site", "building", "residents", "competitors", "momentum", "lookalike", "coordinator",
             "money", "deal", "records", "scans", "permits", "narrator", "verdict"]
    trace = sorted(trace + neighborhood_steps, key=lambda s: order.index(s["agent"]) if s["agent"] in order else 99)
    checks = list(dict.fromkeys(fit.get("unknowns", []) + fit.get("verify", []) + conditions))
    for s in trace:  # lift this business's signal onto its neighborhood step, for the readable agent run
        own = (s.get("per_industry") or {}).get(key) or {}
        if own.get("signal") and not s.get("signal"):
            s["signal"] = own["signal"]
    signals = {s["signal"]["name"]: s["signal"] for s in trace if s.get("signal")}
    because = because_rows(key=key, budget=budget, reserve=reserve, fit=fit, cost=cost, stress=money_holder.get("stress"),
                      rent=rent, decision=decision, signals=signals, deal_step=deal_step, records_step=records_step,
                      permits_step=permits_step, blockers=blockers, conditions=conditions, verdict=verdict,
                      scans_step=scans_step)
    evidence, owner = [], []
    for s in trace:
        for r in s.get("findings", []):
            evidence.append(r)
            owner.append(s["agent"])
    steps_by_agent = {s["agent"]: s for s in trace}
    for row in because:
        hits = []
        for agent, prefix, *value in row.pop("refs"):
            hits += [i for i, r in enumerate(evidence) if owner[i] == agent and r["fact"].startswith(prefix)
                     and (not value or r["value"] == value[0]) and i not in hits]
        row["evidence"] = hits[:4]
        first = evidence[hits[0]] if hits else {}
        sources = (steps_by_agent.get(owner[hits[0]] if hits else row["agent"]) or {}).get("sources") or []
        src = next((x for x in sources if first.get("evidence_url") and x.get("url") == first["evidence_url"]),
                   sources[0] if sources and not first.get("evidence_url") else {})
        row["source"] = {"name": src.get("name") or SOURCE_NAMES.get(first.get("source"), first.get("source"))
                         or "computed in code",
                         "url": first.get("evidence_url") or src.get("url"),
                         "retrieved_at": first.get("captured_at") or (src.get("retrieved_at") or "")[:10] or None,
                         "confidence": first.get("confidence")}
    return {
        "business": key, "label": spec["label"], "group": spec["group"],
        "property_id": int(property_id), "address": building["address"], "lat": building["lat"], "lon": building["lon"],
        "suite": suite, "space": {k: space.get(k) for k in ("suite", "sf", "available_on", "lease_basis", "lease_pending")},
        "budget": budget, "reserve": reserve,
        "verdict": verdict, "because": because, "reasons": reasons, "blockers": blockers, "conditions": conditions, "flips": flips,
        "pillars": {
            "space": {"status": fit["status"], "capacity": fit.get("capacity"), "reasons": fit["reasons"],
                      "unknowns": fit.get("unknowns", []), "verify": fit.get("verify", [])},
            "money": {"affordability": afford, "cash_to_open": cost.get("cash_to_open"), "required": cost.get("required"),
                      "months_of_rent_after_opening": cost.get("months_of_rent_after_opening"),
                      "lines": cost.get("lines"), "sources": cost.get("sources"), "rent": rent,
                      "delay_what_if": money_holder.get("delayed"), "three_month_delay": (money_holder.get("stress") or {}).get("required"),
                      "cash_12m": cash_months(cost, rent, budget, reserve, extra_bills=(bank or {}).get("bills"),
                                              cash_source=(bank or {}).get("source")) if cost else None},
            "demand": decision,
            "permits": permits_step.get("steps", []),
            "records": [r for r in next((s for s in trace if s["agent"] == "records"), {}).get("findings", [])
                        if r["status"] != "agrees"],
            "tours": tours_from((records_step or {}).get("findings", []), space.get("suite")),
        },
        "checks": checks, "narration": hood.get("narration"), "narration_label": hood.get("narration_label"),
        "jurisdiction": permits_step.get("jurisdiction"), "permit_authority": permits_step.get("authority"),
        "trace": trace, "evidence": evidence,
        "elapsed_ms": round(1000 * (time.perf_counter() - started)),
        "computed_by": "All numbers and verdicts are computed in code. Only the optional summary is LLM-written.",
    }


GATE_TEXT = {
    "commercial_conversion_unestablished": "CoStar records a residential building: commercial use is not established, "
                                           "so no business is suggested here",
    "identity_conflict": "The records disagree on which building or suite this is: confirm before any business check",
    "identity_incomplete": "The building or suite identity is incomplete",
    "jurisdiction_unverified": "Who permits this address isn't verified from the pin (Census boundary): confirm with "
                               "the city or county",
}


def site_preflight(property_id, kit=None):
    """'I have a space': the building check before any business (Jayant's compliance_engine.preflight, as is), plus
    the listed spaces a business review can use. A residential building or an identity conflict stops here."""
    import compliance_engine as ce
    from opportunity import find_building
    context = ce.context_from_kit(int(property_id), None, str(kit) if kit else None)
    report = ce.preflight(context)
    where = report.get("jurisdiction") or {}
    building, _ = find_building(property_id, kit)
    spaces = [{"value": sp["value"], "suite": sp.get("suite"), "sf": sp.get("sf"),
               "label": f"Suite {sp['suite']}" if sp.get("suite") else f"{sp['sf']:,.0f} sq ft space",
               "monthly_rent": sp.get("monthly_rent"), "lease_basis": sp.get("lease_basis")}
              for sp in (building or {}).get("spaces", [])]
    stops = [GATE_TEXT.get(g, g) for g in report["gates"] if g in ("commercial_conversion_unestablished",
                                                                   "identity_conflict")]
    if not stops and not spaces:
        stops.append("Nothing is listed for lease here in CoStar, so there is no space to price or review")
    checks = [GATE_TEXT.get(g, g) for g in report["gates"] if g not in ("commercial_conversion_unestablished",
                                                                        "identity_conflict")]
    if where.get("code") == "harris_unincorporated":
        checks.append("Outside Houston city limits (unincorporated Harris County): City of Houston permits don't "
                      "apply; Harris County does")
    authority = AUTHORITY.get(where.get("code"))
    return {"property_id": int(property_id), "address": context.get("address"), "lat": context.get("lat"),
            "lon": context.get("lon"), "facts": context.get("facts"),
            "jurisdiction": {k: where.get(k) for k in ("code", "verified", "place", "county", "note", "evidence_url",
                                                      "retrieved_at")},
            "authority": {"name": authority[0], "url": authority[1]} if authority else None,
            "gates": report["gates"], "status": report["status"], "stops": stops, "checks": checks,
            "can_continue": not stops, "spaces": spaces, "tours": (building or {}).get("tours"),
            "records": [r for r in report["records"] if r["fact"] in ("jurisdiction", "scan_address") or r["value"]
                        is not None],
            "to_verify": [r["note"] for r in report["records"] if r["fact"] in ce.TOPICS],
            "legal_approval": False,
            "computed_by": "Jayant's compliance engine (building preflight) and the CoStar kit; no approval is implied."}
