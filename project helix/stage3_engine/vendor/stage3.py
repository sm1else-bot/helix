"""Helix Stage 3: business options for one selected building, ranked into a conditional, evidence-backed shortlist,
plus a Stage 4 handover.

    GET /api/stage3?property_id=4809147&budget=900000[&reserve=][&suite=]
    python3 playground/with_env.py python3 playground/stage3.py --property-id 4809147 --budget 900000 [--reserve N]

What it does, in code only (no AI, no web search, no gap-filling):
- Runs all 26 registry businesses through the hard gates for the selected listed space:
  1. building / physical fit: CoStar facts vs each business's stated space needs (neighborhood.building_fit);
  2. affordability: sourced opening costs (FDD Item 7, scaled to the space) + deposit + rent while building, against
     the budget. With no reserve given, the reserve is `unavailable` and the gate reports the largest reserve that
     would still pass, instead of assuming one;
  3. compliance preflight: Jayant's compliance_engine (jurisdiction from the pin, building and business gates). Official
     research is replayed from cache only; missing research is reported, never guessed.
- Neighborhood demand counts only where the model is calibrated (Houston). Elsewhere it is `unavailable`, with reason.
- Ranks the businesses that pass every hard gate: building fits > needs a check; affordable in the base case > only in
  the low case; (calibrated demand); more cash headroom; fewer open conditions. Top 3 or fewer. Every tie-break is
  written out.
- Lists `missing_evidence[]` for later research: exact question, jurisdiction, affected candidates, status, the
  authoritative source needed.

The shortlist is conditional: it is not a legal approval and not a market validation.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import neighborhood as nb  # noqa: E402
from neighborhood_industries import INDUSTRIES  # noqa: E402

SCHEMA = "stage3.v1"
OUT = Path(__file__).parent / "output" / "stage3"
GATE_ORDER = ("building", "affordability", "compliance")
AUTHORITY_SOURCES = {
    "los_angeles": {"zoning": "City of Los Angeles ZIMAS (zimas.lacity.org) zoning for the parcel, and LADBS records "
                              "for the current Certificate of Occupancy",
                    "licences": "City of Los Angeles Office of Finance (Business Tax Registration Certificate) and the "
                                "relevant California state board or agency for this business",
                    "fire": "Los Angeles Fire Department (LAFD) plan check / Fire Prevention",
                    "authority": "City of Los Angeles Department of Building and Safety (LADBS)"},
    "la_unincorporated": {"zoning": "LA County Department of Regional Planning (GIS-NET zoning)",
                          "licences": "LA County Treasurer and Tax Collector (business licence) and the relevant "
                                      "California state agency",
                          "fire": "LA County Fire Department", "authority": "LA County Public Works, Building and Safety"},
    "houston": {"zoning": "Recorded deed restrictions (Harris County Clerk) and City of Houston development rules",
                "licences": "City of Houston and Texas licensing agencies for this business",
                "fire": "Houston Fire Marshal", "authority": "City of Houston Permitting Center"},
}


def _git_rev():
    try:
        return subprocess.check_output(["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _src(name, url=None, date=None, note=None):
    return {k: v for k, v in {"name": name, "url": url, "retrieved_at": date, "note": note}.items() if v}


def run(property_id, budget, reserve=None, suite=None, kit=None, horizon_years=5, path="lease", save=True):
    import compliance_engine as ce
    import opportunity
    import venture_review as vr
    from reconcile import check_identity, check_scan_dates, check_size, check_suite_identity
    from venture_costs import DEPOSIT_MONTHS, cash_to_open, rent_for
    started = datetime.now(timezone.utc)
    property_id, budget = int(property_id), float(budget)
    reserve_known = reserve is not None
    reserve = float(reserve) if reserve_known else None
    building, market = opportunity.find_building(property_id, kit)
    if not building:
        raise ValueError("This property has no active lease listing in the CoStar kit.")
    spaces = building["spaces"]
    space = next((s for s in spaces if s["value"] == suite), None) or max(spaces, key=lambda s: s["sf"] or 0)
    facts = building["facts"]
    rent = rent_for(space) if market == "houston" else (
        {"monthly": space["monthly_rent"], "psf_year": space.get("rent_psf_year"),
         "source": f"CoStar asking rent ({space.get('lease_basis') or 'basis not recorded'})", "kind": "listed"}
        if space.get("monthly_rent") else {"monthly": None, "psf_year": None, "kind": "unknown",
                                           "source": "No asking rent listed (the rent model is Houston-trained and is "
                                                     "not applied here)"})
    calibrated = market == "houston"

    # building-level evidence, shared by every business
    context = ce.context_from_kit(property_id, None, str(kit) if kit else None)
    pre = ce.preflight(context)
    where = pre.get("jurisdiction") or {}
    code = where.get("code")
    auth = AUTHORITY_SOURCES.get(code, AUTHORITY_SOURCES["los_angeles" if market != "houston" else "houston"])
    search = ce.cached_search(ce.tavily_search, ce.RESEARCH_CACHE, offline=True)
    prop = vr.property_record(property_id, str(kit or ""))
    record_rows = (check_identity(prop) + check_suite_identity(prop, space.get("suite")) + check_size(prop)
                   + check_scan_dates(prop))
    try:
        import vision_assess
        profile = vision_assess.load_profile(property_id)
    except (ImportError, OSError, ValueError):
        profile = None
    index_demand = {}
    if calibrated:
        try:
            _, index = opportunity.load_index(kit)
            index_demand = (index.get(property_id) or {}).get("demand", {})
        except (nb.Offline, OSError, RuntimeError, ValueError, KeyError):
            index_demand = {}

    jur_src = _src("U.S. Census geocoder (incorporated places, counties)", where.get("evidence_url"),
                   (where.get("retrieved_at") or "")[:10] or None, where.get("note"))
    costar_src = _src("CoStar hackathon kit: property record and lease listing",
                      note=f"property {property_id}, listing space {space['value']}")

    gates = []
    for key, spec in INDUSTRIES.items():
        # 1. building
        fit = (nb.building_fit(key, facts, space["value"]) if calibrated
               else vr.outside_houston(key, building, space["value"])["decisions"][0]["building"])
        need = spec["space"]
        b_rule = (f"{'typical ' + format(need['min_sf'], ',') + '-' + format(need['max_sf'], ',') if need.get('max_sf') else 'at least ' + format(need['min_sf'], ',')} sq ft"
                  + (f"; clear height >= {need['min_clear_ft']} ft" if need.get("min_clear_ft") else "")
                  + ("; street level" if need.get("ground_floor") or need.get("prefers_street_level") else "")
                  + "; a building type that commonly hosts it")
        building_gate = {"status": {"fits": "pass", "check": "pass_with_checks", "no": "fail"}[fit["status"]],
                         "value": {"space_sf": space["sf"], "building_type": facts.get("type"),
                                   "ceiling_ft": facts.get("ceiling_ft"), "stories": facts.get("stories")},
                         "rule": b_rule, "reasons": fit["reasons"], "unknowns": fit["unknowns"],
                         "verify": fit.get("verify", []), "sources": [costar_src],
                         "basis": fit.get("basis")}
        # 2. affordability (reserve unavailable -> report the largest reserve that still passes)
        cost = cash_to_open(key, space["sf"], rent["monthly"], budget=budget, reserve=reserve or 0.0,
                            building_type=facts.get("type"))
        c = cost["cash_to_open"]
        if c["base"] is None:
            a_status, a_note = "unknown", "No comparable opening-cost benchmark for this business"
        elif not cost.get("rent_known"):
            a_status, a_note = "unknown", "No asking rent: the deposit and rent while building can't be priced"
        elif reserve_known:
            a_status = {"within": "pass", "stretch": "pass_low_case_only", "over": "fail"}.get(cost["affordability"],
                                                                                              "unknown")
            a_note = f"Cash to open + ${reserve:,.0f} reserve vs ${budget:,.0f} budget"
        else:
            if c["low"] > budget:
                a_status, a_note = "fail", f"Even the low case (${c['low']:,.0f}) exceeds the ${budget:,.0f} budget before any reserve"
            elif c["base"] <= budget:
                a_status = "pass_if_reserve_at_most"
                a_note = f"Passes in the base case if the reserve is at most ${budget - c['base']:,.0f}"
            else:
                a_status = "pass_low_case_only_if_reserve_at_most"
                a_note = f"Passes only in the low case, and only if the reserve is at most ${budget - c['low']:,.0f}"
        afford_gate = {"status": a_status, "note": a_note,
                       "cash_to_open": c, "required_with_reserve": cost["required"] if reserve_known else None,
                       "max_reserve_base_case": round(budget - c["base"]) if c["base"] is not None else None,
                       "max_reserve_low_case": round(budget - c["low"]) if c["low"] is not None else None,
                       "rule": "cash to open (base) + reserve <= budget",
                       "basis_quality": cost.get("basis_quality"), "unpriced": cost.get("unpriced") or [],
                       "sources": [_src(s["publisher_or_brand"] + " · " + s["document"], s.get("url"), note=s.get("quote"))
                                   for s in cost.get("sources") or []]}
        # 3. compliance preflight (Jayant's engine, as is)
        try:
            rep = ce.check_business(context, key, search)
        except Exception as error:  # noqa: BLE001  the engine stays authoritative; a failure is reported
            rep = {"status": "unavailable", "gates": [], "errors": [f"{type(error).__name__}: {error}"], "research": []}
        blocked = rep.get("status") == "blocked"
        c_status = "fail" if blocked else "pass_needs_verification" if where.get("verified") else "unknown"
        compliance_gate = {
            "status": c_status, "engine_status": rep.get("status"), "gates": rep.get("gates", []),
            "jurisdiction": {k: where.get(k) for k in ("code", "verified", "place", "county")},
            "authority": rep.get("authority") or auth["authority"],
            "category_limitations": ((rep.get("category_requirements") or {}).get("limitations") or []),
            "official_research": ("cached official pages: " + str(len(rep.get("research") or []))
                                  if rep.get("research") else "unavailable: official-source research not cached; "
                                  "not run without the team's OK"),
            "rule": "jurisdiction verified from the pin, building and business gates not blocked; nothing is an approval",
            "sources": [jur_src, _src("Jayant's compliance_engine.check_business (offline replay)")],
            "legal_approval": False}
        # neighborhood (only where calibrated)
        d = index_demand.get(key) if calibrated else None
        hood = ({"status": d["verdict"], "score": d.get("score"), "reasons": d.get("reasons"),
                 "sources": [_src("ACS 2020-24 vs 2015-19, OpenStreetMap / Texas HHSC, CoStar tenancies, lookalike model")]}
                if d else {"status": "unavailable", "reason": vr.NOT_CALIBRATED if not calibrated else
                           "No cached neighborhood decision for this building",
                           "sources": [_src("neighborhood.py calibration: Houston area (5 counties)")]})
        statuses = {"building": building_gate["status"], "affordability": a_status, "compliance": c_status}
        stopped = next((g for g in GATE_ORDER if statuses[g] in ("fail", "unknown")), None)
        gates.append({"business": key, "label": spec["label"], "group": spec["group"],
                      "building": building_gate, "affordability": afford_gate, "compliance": compliance_gate,
                      "neighborhood": hood, "hard_pass": stopped is None, "stopped_at": stopped})

    # ---------- ranking ----------
    def conditions_of(g):
        out = list(g["building"]["unknowns"])
        if g["affordability"]["status"].startswith("pass_low_case"):
            out.append("Affordable only in the low-cost case")
        if not reserve_known:
            out.append(g["affordability"]["note"])
        out += g["affordability"]["unpriced"]
        out += [f"Compliance gate: {x}" for x in g["compliance"]["gates"]]
        out += g["compliance"]["category_limitations"]
        out += [f"Records: {r['note'][:160]}" for r in record_rows if r["status"] == "conflict"]
        return out

    def rank_key(g):
        a = g["affordability"]["status"]
        demand_rank = {"supported": 0, "mixed": 1}.get(g["neighborhood"]["status"], 2) if calibrated else 0
        return (0 if g["building"]["status"] == "pass" else 1,
                0 if a in ("pass", "pass_if_reserve_at_most") else 1,
                demand_rank,
                -(g["affordability"]["max_reserve_base_case"] or -1e12),
                len(conditions_of(g)))
    passing = sorted([g for g in gates if g["hard_pass"]], key=rank_key)
    labels = ["building fits without open building checks", "affordable in the base case (not only the low case)",
              "stronger calibrated neighborhood demand", "more cash headroom (larger reserve possible)",
              "fewer open conditions"]

    def why(a, b):
        ka, kb = rank_key(a), rank_key(b)
        for i, (x, y) in enumerate(zip(ka, kb)):
            if x != y:
                detail = (f" (${-ka[3]:,.0f} vs ${-kb[3]:,.0f} of headroom in the base case)" if i == 3 else
                          f" ({ka[4]} vs {kb[4]} open conditions)" if i == 4 else "")
                return f"{a['label']} ranks above {b['label']}: {labels[i]}{detail}."
        return f"{a['label']} and {b['label']} tie on every criterion; kept in registry order."
    top = passing[:3]

    # ---------- missing evidence for later research ----------
    jname = where.get("place") or (where.get("county") and "unincorporated " + where["county"]) or "jurisdiction not verified"
    short = [g["business"] for g in top]
    missing = []

    def need(q, affects, status, source, why_=""):
        missing.append({"id": f"m{len(missing) + 1}", "question": q, "jurisdiction": jname, "affects": affects,
                        "status": status, "needed_source": source, "why": why_})
    need("Does the parcel's zoning allow each shortlisted use, and what is the current Certificate of Occupancy "
         "(use and occupancy group)?", short or "all", "unknown", auth["zoning"],
         "Permitted use is not in the CoStar kit; Los Angeles has zoning.")
    for g in top:
        art = "an" if g["label"][:1].lower() in "aeiou" else "a"
        need(f"Which licences and permits does {art} {g['label'].lower()} need here?", [g["business"]], "unavailable",
             auth["licences"], "The licence registry in this engine is Houston/Texas only.")
        need(f"Is there demand for {art} {g['label'].lower()} near {building['address']}?", [g["business"]],
             "unavailable" if not calibrated else g["neighborhood"]["status"],
             "ACS 2020-24 for LA County, local competitor counts, and a lookalike model trained and validated on LA",
             g["neighborhood"].get("reason", ""))
        for u in g["building"]["unknowns"]:
            need(f"{g['label']}: {u}", [g["business"]], "unknown", "Site visit or measured scan (Stage 2 profile)")
        for lim in g["compliance"]["category_limitations"]:
            need(f"{g['label']}: {lim}", [g["business"]], "unknown", auth["licences"])
    if facts.get("ceiling_ft") is not None:
        need(f"Is the usable clear height really {facts['ceiling_ft']:g} ft (CoStar-recorded, "
             f"{facts.get('ceiling_source') or 'source field unknown'}) across the space?", short or "all",
             "recorded, not verified", "Measured Matterport scan of this suite (Stage 2) or a site survey")
    if not (context.get("facts") or {}).get("parking_spaces"):
        need("How many parking spaces serve this space, and what does the use require?", short or "all",
             "unavailable", f"Landlord site plan and {auth['zoning']}", "CoStar records no parking count here.")
    need("Fire and life-safety requirements for the proposed occupancy (exits, sprinklers, occupant load)?",
         short or "all", "unknown", auth["fire"])
    if not reserve_known:
        need("What reserve will the founder keep untouched?", "all", "unavailable",
             "Founder input (Stage 0) or the bank account (Nessie) once verified",
             "Affordability is reported as 'passes if the reserve is at most $X' until this is set.")

    # ---------- per-candidate handover ----------
    def candidate(g, rank):
        a = g["affordability"]
        ledger = cash_to_open(g["business"], space["sf"], rent["monthly"], budget=budget, reserve=reserve or 0.0,
                              building_type=facts.get("type"))
        return {
            "rank": rank, "business": g["business"], "label": g["label"], "group": g["group"],
            "gates": {k: {"status": g[k]["status"], "rule": g[k].get("rule"), "sources": g[k].get("sources", [])}
                      for k in ("building", "affordability", "compliance")},
            "neighborhood": g["neighborhood"],
            "conditions": conditions_of(g),
            "unknowns": [m["question"] for m in missing if m["affects"] == "all" or g["business"] in m["affects"]],
            "verification_questions": g["building"]["verify"] + [
                f"Confirm with {g['compliance']['authority']} that this use is allowed and what plan check needs"],
            "stage4_inputs": {
                "property_id": property_id, "address": building["address"], "suite": space["value"],
                "space_sf": space["sf"],
                "monthly_rent": rent["monthly"], "rent_psf_year": rent.get("psf_year"), "rent_source": rent["source"],
                "lease_basis": space.get("lease_basis"), "available_on": space.get("available_on"),
                "cash_to_open": a["cash_to_open"],
                "cost_lines": [{k: l.get(k) for k in ("label", "low", "base", "high", "how", "source")}
                               for l in ledger["lines"]],
                "cost_sources": a["sources"], "unpriced": a["unpriced"],
                "build_months": ledger.get("build_months"), "deposit_months": DEPOSIT_MONTHS,
                "budget": budget, "reserve": reserve if reserve_known else "unavailable",
                "max_reserve_base_case": a["max_reserve_base_case"],
                "financing": {"path": path, "loan": "not assumed (Stage 4 decides)",
                              "horizon_years": horizon_years, "horizon_note": "context only; no lease term invented"},
            },
        }
    shortlist = [candidate(g, i + 1) for i, g in enumerate(top)]

    warnings = []
    if len(spaces) > 1 and suite is None:
        warnings.append(f"This building lists {len(spaces)} spaces; with no suite selected, Stage 3 evaluates the "
                        f"largest ({space['value']}, {space['sf']:,.0f} sq ft). Others: "
                        + ", ".join(f"{s['value']} ({s['sf']:,.0f} sq ft)" for s in spaces if s is not space)
                        + ". Pass suite= to evaluate a specific one.")
    if not calibrated:
        warnings.append("Neighborhood demand is not evaluated here: the model is calibrated for the Houston area only.")
    warnings.append("Licences: the engine's licence list is Houston/Texas only; no licence is evaluated for this "
                    "jurisdiction." if market != "houston" else "Licences are Houston/Texas business licences.")
    if not profile:
        warnings.append("No Stage 2 physical profile was provided to this engine (expected at "
                        f"playground/output/profiles/{property_id}.json); building fit uses CoStar-recorded facts only.")
    for r in record_rows:
        if r["status"] != "agrees":
            warnings.append(f"Scan scope ({r['fact']}): {r['note'][:200]}")
    if facts.get("listed") and any(str(x.get("floor")) not in ("None", "1", "1st", "GRND") for x in facts["listed"]):
        warnings.append(f"CoStar lists the space's floor code as {facts['listed'][0].get('floor')}; confirm it is at "
                        "grade before relying on street-level checks.")

    inputs = {"property_id": property_id, "address": building["address"], "market": market,
              "suite": space["value"], "space_sf": space["sf"], "budget": budget,
              "reserve": reserve if reserve_known else "unavailable", "path": path, "horizon_years": horizon_years}
    out = {
        "schema_version": SCHEMA, "generated_at": started.isoformat(), "inputs": inputs,
        "property": {"address": building["address"], "lat": building["lat"], "lon": building["lon"],
                     "facts": facts, "jurisdiction": compliance_gate["jurisdiction"], "authority": auth["authority"],
                     "preflight": {"status": pre.get("status"), "gates": pre.get("gates")},
                     "rent": rent, "listed_spaces": [{k: s.get(k) for k in ("value", "sf", "monthly_rent", "lease_basis")}
                                                     for s in spaces]},
        "method": {"hard_gates": list(GATE_ORDER), "neighborhood": "counts only where calibrated" +
                   ("" if calibrated else " (not calibrated here: excluded from ranking)"),
                   "rank_order": labels, "top_n": 3,
                   "nature": "Conditional, evidence-backed shortlist. Not a legal approval and not a market validation."},
        "summary": {"businesses": len(gates), "pass_all_hard_gates": len(passing),
                    "stopped_at": {g: sum(1 for x in gates if x["stopped_at"] == g) for g in GATE_ORDER},
                    "shortlist": [c["label"] for c in shortlist]},
        "shortlist": shortlist,
        "tie_breaks": [why(passing[i], passing[i + 1]) for i in range(len(passing) - 1)],
        "also_passed": [g["label"] for g in passing[3:]],
        "gates": gates,
        "missing_evidence": missing,
        "warnings": warnings,
        "stage2_profile": ({"source": profile.get("source"), "observations": len(profile.get("observations", []))}
                           if profile else "unavailable"),
        "assumptions": [
            "Opening costs are FDD Item 7 (or published benchmark) ranges scaled to the space's square footage; fixed "
            "lines (signage, licences) are not scaled down, so small spaces can be overstated.",
            f"Deposit = {DEPOSIT_MONTHS} months of asking rent; rent while building = build months x asking rent "
            "(no free-rent period assumed). NNN charges beyond base rent are not included.",
            "Reserve: " + (f"${reserve:,.0f} as given." if reserve_known else
                           "unavailable; affordability reports the largest reserve that still passes."),
            f"Lease path only; {horizon_years}-year horizon is context, no lease term is assumed.",
            "No revenue, operating costs or demand forecast is used anywhere in this stage.",
        ],
        "reproduce": {"command": f"python3 playground/with_env.py python3 playground/stage3.py --property-id "
                                 f"{property_id} --budget {budget:.0f}" + (f" --reserve {reserve:.0f}" if reserve_known else ""),
                      "endpoint": f"/api/stage3?property_id={property_id}&budget={budget:.0f}"
                                  + (f"&reserve={reserve:.0f}" if reserve_known else ""),
                      "code_version": _git_rev(), "kit": os.path.basename(str(opportunity.kit_file(kit)))},
    }
    out["reproduce"]["output_hash"] = hashlib.sha256(json.dumps(
        {k: v for k, v in out.items() if k not in ("generated_at", "reproduce")}, sort_keys=True, default=str
    ).encode()).hexdigest()[:16]
    if save:
        OUT.mkdir(parents=True, exist_ok=True)
        stem = f"stage3-{property_id}-{budget:.0f}-{'r' + format(reserve, '.0f') if reserve_known else 'reserve-unavailable'}"
        (OUT / f"{stem}.json").write_text(json.dumps(out, indent=2, default=str))
        (OUT / f"{stem}.md").write_text(summary_md(out))
        out["saved"] = {"json": str(OUT / f"{stem}.json"), "summary": str(OUT / f"{stem}.md")}
    return out


def summary_md(o):
    """A readable summary of the handover (same content as the JSON, no new claims)."""
    i, s = o["inputs"], o["summary"]
    money = lambda v: "unavailable" if v in (None, "unavailable") else f"${v:,.0f}"  # noqa: E731
    lines = [f"# Stage 3 handover: {o['property']['address']}", "",
             f"Property {i['property_id']} · {i['market']} · space {i['suite']} ({i['space_sf']:,.0f} sq ft) · "
             f"budget {money(i['budget'])} · reserve {money(i['reserve'])} · {i['path']} · {i['horizon_years']}-yr horizon (context)",
             f"Jurisdiction: {o['property']['jurisdiction'].get('place') or o['property']['jurisdiction'].get('county')} "
             f"({o['property']['jurisdiction'].get('code')}) · authority: {o['property']['authority']}",
             f"Rent: {money(o['property']['rent'].get('monthly'))}/mo · {o['property']['rent']['source']}", "",
             f"**{s['pass_all_hard_gates']} of {s['businesses']} pass every hard gate** (stopped at building: "
             f"{s['stopped_at']['building']}, affordability: {s['stopped_at']['affordability']}, compliance: "
             f"{s['stopped_at']['compliance']}). {o['method']['nature']}", "", "## Shortlist", ""]
    for c in o["shortlist"]:
        st = c["stage4_inputs"]
        lines += [f"### {c['rank']}. {c['label']}",
                  f"- Gates: building {c['gates']['building']['status']}, affordability {c['gates']['affordability']['status']}, "
                  f"compliance {c['gates']['compliance']['status']}; neighborhood {c['neighborhood']['status']}",
                  f"- Cash to open: {money(st['cash_to_open']['low'])} / {money(st['cash_to_open']['base'])} / "
                  f"{money(st['cash_to_open']['high'])} (low/base/high); largest reserve that still passes (base): "
                  f"{money(st['max_reserve_base_case'])}",
                  f"- Build months {st['build_months']} · deposit {st['deposit_months']} months · rent {money(st['monthly_rent'])}/mo",
                  "- Conditions: " + ("; ".join(c["conditions"]) or "none"),
                  "- To verify: " + "; ".join(c["verification_questions"]), ""]
    lines += ["## Tie-breaks", ""] + [f"- {t}" for t in o["tie_breaks"]] + ["", "## All 26 businesses", "",
              "| Business | Building | Affordability | Compliance | Neighborhood | Stopped at |", "|---|---|---|---|---|---|"]
    for g in o["gates"]:
        lines.append(f"| {g['label']} | {g['building']['status']} | {g['affordability']['status']} | "
                     f"{g['compliance']['status']} | {g['neighborhood']['status']} | {g['stopped_at'] or '—'} |")
    lines += ["", "## Missing evidence (for research)", ""] + [
        f"- **{m['id']}** [{m['status']}] {m['question']} · affects: {m['affects'] if isinstance(m['affects'], str) else ', '.join(m['affects'])} · "
        f"source needed: {m['needed_source']}" for m in o["missing_evidence"]]
    lines += ["", "## Warnings", ""] + [f"- {w}" for w in o["warnings"]] + ["", "## Assumptions", ""] + [
        f"- {a}" for a in o["assumptions"]] + ["", f"Reproduce: `{o['reproduce']['command']}` · code {o['reproduce']['code_version']} "
                                                   f"· output hash {o['reproduce']['output_hash']}"]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--property-id", type=int, required=True)
    p.add_argument("--budget", type=float, required=True)
    p.add_argument("--reserve", type=float)
    p.add_argument("--suite")
    p.add_argument("--kit")
    a = p.parse_args()
    os.environ.setdefault("JURISDICTION_OFFLINE", "1")
    out = run(a.property_id, a.budget, a.reserve, a.suite, a.kit)
    print(summary_md(out))
    print("saved:", out.get("saved"))


if __name__ == "__main__":
    main()
