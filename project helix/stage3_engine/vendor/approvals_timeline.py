"""Deterministic approval pre-screen and an explicitly assumed opening schedule.

Official guidance was reviewed on 2026-11-14. References describe a process;
no reference page is treated as a parcel-specific approval. The Deep Agents
orchestrator may call ``analyze_approvals`` as a tool without an LLM doing math.
"""
from urllib.parse import urlsplit

REVIEWED_AT = "2026-11-14"
REFERENCES = {
    "houston-development": (
        "City of Houston development regulations",
        "https://www.houstontx.gov/planning/DevelopRegs/",
        "Houston has no conventional zoning; development review still covers parking, setbacks and access.",
    ),
    "houston-occupancy": (
        "Houston permits and inspections",
        "https://www.houstontx.gov/business/plan/permits-inspections.html",
        "Houston requires a Certificate of Occupancy for commercial buildings or lease spaces and changes in occupancy classification.",
    ),
    "houston-deeds": (
        "Houston deed restriction guidance",
        "https://www.houstontx.gov/legal/dr-faq.html",
        "Recorded deed restrictions can limit uses independently of municipal development review.",
    ),
    "la-zimas": (
        "Los Angeles City Planning zoning search",
        "https://planning.lacity.gov/zoning/zoning-search",
        "ZIMAS provides parcel zoning, overlays, planning cases and building permit history for City of Los Angeles properties.",
    ),
    "la-permits": (
        "LADBS plan check and permit guidance",
        "https://www.ladbs.org/services/core-services/plan-check-permit",
        "LADBS lists changes of use or occupancy and interior floor-plan modifications among work requiring building plan approval.",
    ),
    "la-records": (
        "LADBS property records",
        "https://dbs.lacity.gov/services/search-online-building-records",
        "LADBS provides access to permit, building and Certificate of Occupancy records for properties within LA City.",
    ),
}


def _reference(key):
    title, url, summary = REFERENCES[key]
    return {
        "fact": title, "value": summary, "source": "public:official-guidance",
        "source_id": key, "captured_at": None, "reviewed_at": REVIEWED_AT,
        "confidence": None, "evidence_url": url, "status": "agrees",
        "needs_human": True, "scope": "general municipal guidance",
        "note": "Reference guidance reviewed on the stated date; applicability to this parcel and suite is unverified.",
    }


def _months(assumptions, key, default=None):
    value = assumptions.get(key, default)
    if value is None:
        return None
    # bools are ints in Python, but not valid month counts. Reject nonfinite,
    # negative and fractional numbers rather than silently changing a scenario.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a whole number from 0 to 120")
    if not 0 <= value <= 120 or int(value) != value:
        raise ValueError(f"{key} must be a whole number from 0 to 120")
    return int(value)


def build_timeline(assumptions=None):
    """Same monthly convention as nessie_pilot.simulate: revenue starts next month."""
    assumptions = assumptions or {}
    permit = _months(assumptions, "permit_months")
    build = _months(assumptions, "build_months")
    delay = _months(assumptions, "delay_months", 0)
    if delay is None:
        delay = 0
    complete = permit is not None and build is not None
    phases = []
    if complete:
        permit_end = permit + delay
        if permit_end:
            phases.append({"name": "Approval allowance including delay", "start_month": 1,
                           "end_month": permit_end, "status": "assumption"})
        if build:
            phases.append({"name": "Build-out allowance", "start_month": permit_end + 1,
                           "end_month": permit_end + build, "status": "assumption"})
    return {
        "status": "assumption" if complete else "needs_inputs",
        "permit_months": permit, "build_months": build, "delay_months": delay,
        "opening_month": permit + build + delay + 1 if complete else None,
        "baseline_opening_month": permit + build + 1 if complete else None,
        "phases": phases,
        "note": "Editable planning allowances, not agency forecasts. The schedule assumes approvals precede build-out; revenue starts the following month. Inspections and corrections must fit these allowances or extend the delay.",
    }


def _market(address):
    # Only select relevant guidance; never establish jurisdiction from an address.
    parts = [part.strip().casefold() for part in str(address).split(",")]
    if len(parts) >= 3 and parts[-2] == "houston" and parts[-1].startswith("tx"):
        return "Houston"
    if len(parts) >= 3 and parts[-2] in ("los angeles", "la") and parts[-1].startswith("ca"):
        return "Los Angeles"
    return None


def _jurisdiction(records, market):
    """Accept only an explicit official boundary result, never a Census tract."""
    hosts = {"Houston": ("houstontx.gov", "arcgis.com"),
             "Los Angeles": ("lacity.gov", "lacity.org", "arcgis.com")}
    expected = {"Houston": "City of Houston", "Los Angeles": "City of Los Angeles"}
    if market not in hosts:
        return None
    for row in records:
        value = row.get("value")
        if not isinstance(value, dict) or row.get("source_id") != "municipal-jurisdiction":
            continue
        host = urlsplit(row.get("evidence_url") or "").hostname or ""
        official = any(host == suffix or host.endswith("." + suffix) for suffix in hosts[market] if suffix != "arcgis.com")
        # Generic ArcGIS hosts are not proof that a service belongs to the city.
        if (official and str(row.get("source", "")).startswith("public:")
                and row.get("status") == "agrees" and value.get("verified") is True
                and value.get("jurisdiction") == expected[market]
                and value.get("method") == "point_in_polygon"):
            return row
    return None


def analyze_approvals(address, proposed_use, records, assumptions=None):
    """Return actionable municipal guidance and editable schedule, never clearance.

    Inputs use the common evidence record contract. Optional jurisdiction proof:
    source_id='municipal-jurisdiction', source='public:<authority>', status='agrees',
    evidence_url on an official city domain, value={verified: true,
    jurisdiction: 'City of ...', method: 'point_in_polygon'}. Census matches and
    CoStar zoning labels do not meet that contract.
    """
    market = _market(address)
    timeline = build_timeline(assumptions)
    jurisdiction = _jurisdiction(records, market)
    findings, actions = [], []
    evidence = []

    def finding(statement, *ids):
        findings.append({"statement": statement, "source_ids": list(ids)})

    def action(title, why, owner, request, *ids):
        actions.append({"title": title, "why": why, "owner": owner,
                        "request": request, "source_ids": list(ids)})

    if jurisdiction:
        finding(f"An official municipal boundary lookup places the address point in {jurisdiction['value']['jurisdiction']}; parcel and suite approvals still need review.", jurisdiction["source_id"])
    else:
        action("Confirm which authority controls this parcel",
               "A postal city and Census tract do not establish the permitting jurisdiction.",
               "Broker and municipal planning desk",
               f"Provide the parcel/APN and a municipal boundary result for {address}; confirm the building and advertised suite.")

    if market == "Houston":
        evidence = [_reference(key) for key in ("houston-development", "houston-occupancy", "houston-deeds")]
        finding("If this parcel is within Houston, the absence of conventional zoning does not remove development review: parking, setbacks and access still matter.", "houston-development")
        finding("Houston's published process requires a Certificate of Occupancy for commercial lease space and changes in occupancy classification. This suite's approved use has not been retrieved.", "houston-occupancy")
        action("Match the proposed business to the suite's approved use",
               "An occupancy change can add plan review, construction and inspection work before opening.",
               "Houston Permitting Center — Occupancy Inspections and commercial plan review",
               f"Request the current Certificate of Occupancy and approved floor plan for this exact suite. Ask which reviews and inspections apply to {proposed_use}.", "houston-occupancy")
        action("Check parking rights and private use restrictions",
               "Listed parking counts do not establish allocated parking rights or permission for the proposed use.",
               "Broker/landlord, title professional and Houston Planning",
               "Request recorded deed restrictions, the lease's permitted-use clause, an allocated parking plan and an applicable development review checklist.", "houston-development", "houston-deeds")
    elif market == "Los Angeles":
        evidence = [_reference(key) for key in ("la-zimas", "la-permits", "la-records")]
        finding("For a parcel in the City of Los Angeles, ZIMAS can identify zoning and overlays. A CoStar zoning label or an LA postal address does not establish that this use is allowed.", "la-zimas")
        finding("LADBS lists use/occupancy changes and interior floor-plan modifications among work requiring building plan approval. No permit or occupancy record for this suite has been verified.", "la-permits", "la-records")
        action("Confirm the use against the parcel's zoning and overlays",
               "The applicable zoning code, overlays and use permissions can change the project scope.",
               "Los Angeles City Planning zoning desk",
               f"Obtain the APN and current ZIMAS property report, including zone, overlays and planning cases. Ask whether {proposed_use} needs discretionary approval at this exact parcel.", "la-zimas")
        action("Reconcile existing permits and the proposed floor plan",
               "The previous business or a listing is not evidence that the proposed occupancy is approved.",
               "LADBS plan check and broker/landlord",
               "Retrieve the exact suite's permit history, approved plans and Certificate of Occupancy through LADBS records. Confirm change-of-use, accessibility, fire, parking and final-inspection requirements.", "la-permits", "la-records")
    else:
        action("Obtain the local change-of-use checklist",
               "This address does not select either supported municipal guidance set.",
               "The parcel's permitting authority",
               f"Request approved occupancy records and the permit checklist for {proposed_use} at the exact suite.")

    # Sponsor labels remain helpful recorded context without becoming approvals.
    for row in records:
        value = row.get("value")
        if row.get("source") == "costar" and isinstance(value, dict) and str(value.get("zoningDescription") or "").strip().casefold() not in {"", "none", "null", "unknown", "n/a"}:
            finding(f"CoStar records zoning as {value['zoningDescription']}. Confirm its current meaning and applicability with the planning authority.", row["source_id"])
            break
    use = str(proposed_use).casefold()
    if any(word in use for word in ("restaurant", "food", "cafe", "café")):
        action("Scope food-service reviews before pricing the kitchen",
               "Ventilation, grease handling and health review may change both cost and sequencing.",
               "Local health authority and licensed kitchen/MEP designer",
               "Request the food-facility review checklist and assess the proposed menu, hood/exhaust, grease interceptor, utilities and plumbing. Obtain a written scope before committing build-out costs.")
    elif any(word in use for word in ("daycare", "childcare", "child care")):
        action("Confirm childcare licensing and site requirements",
               "Childcare adds licensing and age/capacity-specific requirements to the building review.",
               "State childcare licensing authority and local fire/building reviewer",
               "Request the licensing checklist for the intended ages and capacity; confirm outdoor space, supervision, occupancy and inspection requirements for this site.")
    else:
        action("Confirm the operating layout before estimating review time",
               "Capacity, access, restrooms and fire protection can change the required work and opening date.",
               "Licensed architect and local building/fire reviewer",
               f"Review a measured suite plan for {proposed_use}, intended peak users, exits, accessibility, restrooms and fire protection; obtain a written permit/inspection scope.",
               *( ["houston-occupancy"] if market == "Houston" else ["la-permits"] if market == "Los Angeles" else []))

    return {
        "status": "needs_verification", "jurisdiction_verified": jurisdiction is not None,
        "guidance_market": market, "summary": "Confirm the exact suite's approved use and required reviews before committing to an opening date.",
        "findings": findings, "actions": actions, "evidence": evidence, "timeline": timeline,
        "site_approval_verified": False,
    }
