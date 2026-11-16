"""Conservative eligibility checks using listing scope and original kit evidence."""
import re
from collections import defaultdict

from reconcile import record, tour_url, unique_scans

# PropertyType from schemas/enums.json in the CoStar kit.
PROPERTY_TYPES = {
    "0": "Unknown", "1": "Hospitality", "2": "Industrial", "3": "Land", "5": "Office",
    "6": "Retail", "7": "Flex", "8": "Sports and entertainment", "9": "Mixed use",
    "10": "Specialty", "11": "Multifamily", "13": "Healthcare",
    "14": "Unknown", "15": "Residential", "16": "Homes",
}
DWELLING_TYPES = {"11", "15", "16", "multifamily", "residential", "homes"}
DWELLING_CAPTION = re.compile(r"\b\d+\s*(?:br\b|beds?\b|bedrooms?\b).*?\b\d+(?:\.\d+)?\s*(?:ba\b|baths?\b|bathrooms?\b)", re.I)

ISSUES = {
    "scan_address": "Scan-to-building match",
    "scan_suite": "Scan-to-suite match",
    "scan_attachment_date": "Scan date provenance",
    "ceiling_height_ft": "Clear height",
    "suite_occupancy_at_scan": "Occupancy at scan date",
    "gross_area_vs_floors": "Building area consistency",
    "rentable_vs_gross_area": "Rentable and gross area",
}
PRIORITY = {"conflict": 0, "unknown": 1, "agrees": 2}
UNKNOWN_SUMMARIES = {
    "scan_address": "At least one tour cannot be matched to the building by its recorded caption. Ask which address the tour covers.",
    "scan_suite": "At least one linked tour has unverified suite coverage. Request a plan and the exact tour for the advertised premises.",
    "ceiling_height_ft": "Clear height for the selected premises is unverified. Measure below beams, lights and sprinklers.",
    "scan_attachment_date": "At least one tour lacks usable attachment date provenance. Confirm when the space was scanned before comparing tenancy.",
}


def review_eligibility(prop, suite, selected, lease_spaces, offers, ownership):
    code = str(prop.get("propertyType", "")).strip()
    kind = PROPERTY_TYPES.get(code, code or "Unknown")
    residential = code.casefold() in DWELLING_TYPES
    dwelling_scans = [scan for scan in unique_scans(prop)
                      if any(DWELLING_CAPTION.search(caption) for caption in scan["captions"])]
    confirmed_terms = len(selected) == 1 and len(lease_spaces) == 1 if ownership == "lease" else bool(offers)
    facts = [record("property_type", kind, "costar", prop["id"], confidence=0.8,
                    status="agrees" if kind != "Unknown" else "unknown",
                    note=f"CoStar classifies the property as {kind.lower()}.")]
    for scan in dwelling_scans:
        caption = next(c for c in scan["captions"] if DWELLING_CAPTION.search(c))
        facts.append(record("residential_scan_caption", caption, "costar", scan["model_id"],
                            scan["captured_at"], confidence=0.7, status="agrees", needs_human=True,
                            evidence_url=tour_url(scan["model_id"]),
                            note=f'Linked Matterport tour caption describes a dwelling (“{caption}”). Its scope is limited to the scanned unit.'))
    if residential and not confirmed_terms:
        state, headline = "blocked", "Residential property. Commercial space unverified."
        reason = "The kit identifies residential use and does not establish an available commercial space for this deal."
    elif not confirmed_terms:
        state, headline = "unverified", "Verify a commercial space before modeling this deal"
        if ownership == "lease" and not suite:
            reason = "Building scope does not identify which space can be leased. Select an advertised suite or get the broker’s listing and floor plan."
        else:
            reason = "The kit did not supply one matching active lease space or an active sale offer for this deal."
    elif residential or dwelling_scans:
        state, headline = "unverified", "Separate the commercial listing from the residential evidence"
        reason = "Confirm the advertised premises are commercial and distinct from the residential space before using the listing or tour to model a conversion."
    else:
        state, headline = "conditional", "Commercial listing found. Verify fit and approvals."
        reason = "The kit supplies an active listing. Confirm the premises, proposed use and fit before relying on a cash scenario."
    return {"status": state, "headline": headline, "reason": reason, "records": facts}


def evidence_overview(records):
    groups = defaultdict(list)
    for row in records:
        if row["status"] in PRIORITY:
            groups[row["fact"]].append(row)
    summaries = []
    for fact, rows in groups.items():
        row = min(rows, key=lambda item: PRIORITY[item["status"]])
        if row["status"] == "agrees":
            continue
        summaries.append({"title": ISSUES.get(fact, fact.replace("_", " ").capitalize()),
                          "status": row["status"],
                          "note": UNKNOWN_SUMMARIES.get(fact, row["note"]) if row["status"] == "unknown" else row["note"],
                          "count": len(rows)})
    return sorted(summaries, key=lambda row: PRIORITY[row["status"]])
