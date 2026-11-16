"""Interior evidence from Matterport scans linked to a CoStar property.

Rules that keep this honest:
  - A scan whose caption names a different address than the CoStar record is not used for
    measurements (it may be a sibling building); it is reported instead.
  - The scan date is CoStar's attachment originationDate, never Matterport's `created`.
  - A height of 0 means "not measured" (common in large open spaces), never a real height.
  - Measured area covers only what was scanned, usually one suite, not the building.

Room responses are cached in playground/output/matterport-<model id>.json (gitignored).
"""
import json
import re
from pathlib import Path

from reconcile import check_identity, matterport_rooms, parse_caption, record, tour_url, unique_scans

CACHE = Path(__file__).parent / "output"
M2_TO_SF = 10.7639
M_TO_FT = 3.28084


def fetch_rooms(model_id, offline=False):
    cache = CACHE / f"matterport-{model_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    if offline:
        raise RuntimeError("no cached Matterport rooms (run once online)")
    rooms = matterport_rooms(model_id)
    CACHE.mkdir(exist_ok=True)
    cache.write_text(json.dumps(rooms))
    return rooms


def summarize(rooms):
    """Total measured area and the largest space (area and height) in sq ft / ft."""
    sized = [(r, r.get("dimensionEstimates") or {}) for r in rooms or []]
    total_sf = sum((d.get("area") or 0) for _, d in sized) * M2_TO_SF
    main_room, main = max(sized, key=lambda rd: rd[1].get("area") or 0, default=(None, {}))
    height_m = main.get("height") or 0
    return {
        "total_sf": round(total_sf) if total_sf else None,
        "main_label": (main_room or {}).get("label") or "unlabeled space",
        "main_sf": round((main.get("area") or 0) * M2_TO_SF) or None,
        "main_height_ft": round(height_m * M_TO_FT, 1) if height_m > 0 else None,
    }


SF_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{3,6})\s*(?:sq\.?\s*ft|sf|square feet)", re.I)


def match_listed(caption, spaces):
    """The listed space a scan caption refers to: by suite name, else by an advertised size within 2%
    ("Warehouse Space - 9,900SF" -> the 9,900 sq ft space)."""
    suite = parse_caption(caption)["suite"]
    by_name = next((s for s in spaces if suite and str(s["suite"]).lower() == str(suite).lower()), None)
    if by_name:
        return by_name
    size = SF_RE.search(caption or "")
    if size:
        sf = float(size.group(1).replace(",", ""))
        hits = [s for s in spaces if s["square_feet"] and abs(s["square_feet"] - sf) <= 0.02 * sf]
        if len(hits) == 1:
            return hits[0]
    return None


def scanned_suites(prop, spaces):
    """Listed spaces that have a usable (same-address) scan, as {suite key: model id}."""
    out = {}
    for scan, ident in usable_scans(prop):
        if ident and ident["status"] == "conflict":
            continue
        listed = match_listed(scan["caption"], spaces)
        if listed:
            out.setdefault(space_key(listed), scan["model_id"])
    return out


def space_key(space):
    return f"{space.get('listing_id')}:{str(space.get('suite')).lower()}:{space.get('square_feet')}"


def usable_scans(prop):
    """Scans with their identity verdict: (scan, identity_record)."""
    identity = {r["source_id"]: r for r in check_identity(prop)}
    return [(scan, identity.get(scan["model_id"])) for scan in unique_scans(prop)]


def interior_records(prop, rooms_by_model, spaces=None):
    """Evidence records for the scans.

    Returns (records, suite_heights): suite_heights maps a listed suite name to the measured main-space
    height of the scan captioned with that suite. Heights from scans of other spaces are reported but
    never applied to a suite, so an office scan's 9 ft ceiling cannot rule out a warehouse suite."""
    spaces = spaces or []
    out, suite_heights = [], {}
    for scan, ident in usable_scans(prop):
        model = scan["model_id"]
        common = dict(source="matterport", source_id=model, captured_at=scan["captured_at"],
                      evidence_url=tour_url(model))
        if ident and ident["status"] == "conflict":
            out.append(record("scan_used_for_measurements", False, confidence=0.7, status="conflict", needs_human=True,
                              note=f"Not used: the scan is captioned '{scan['caption']}', a different address than "
                                   "the CoStar record. Confirm which building was scanned first.", **common))
            continue
        if model not in rooms_by_model:
            out.append(record("scan_measured_area_sf", None, confidence=0.1, status="unknown",
                              note=f"Rooms for scan '{scan['caption']}' were not loaded.", **common))
            continue
        summary = summarize(rooms_by_model[model])
        listed = match_listed(scan["caption"], spaces)
        when = f"scanned by {scan['captured_at']}" if scan["captured_at"] else "scan date unknown"
        if summary["total_sf"]:
            note = (f"'{scan['caption'].strip()}' ({when}) measures {summary['total_sf']:,} sq ft across its rooms; the largest "
                    f"space is {summary['main_label']} at {summary['main_sf'] or 0:,} sq ft. A scan covers only what was "
                    "walked, usually one suite.")
            status = "agrees"
            if listed and listed["square_feet"]:
                ratio = summary["total_sf"] / listed["square_feet"]
                close = 0.75 <= ratio <= 1.25
                note += (f" The listing advertises {('suite ' + str(listed['suite'])) if listed['suite'] else 'this space'} at {listed['square_feet']:,.0f} sq ft "
                         f"({ratio:.0%} measured)" + ("." if close else "; the advertised size or the scan coverage "
                                                                           "needs checking."))
                status = "agrees" if close else "conflict"
            out.append(record("scan_measured_area_sf", summary["total_sf"], confidence=0.6, status=status,
                              needs_human=status != "agrees", note=note, **common))
        if summary["main_height_ft"]:
            out.append(record("measured_clear_height_ft", summary["main_height_ft"], confidence=0.6, status="agrees",
                              needs_human=True,
                              note=(f"Measured height of the largest scanned space ({summary['main_label']}); "
                                    "clear height under lights, beams and sprinklers is lower."), **common))
            if listed:
                suite_heights[space_key(listed)] = (summary["main_height_ft"], model)
        elif summary["main_sf"]:
            out.append(record("measured_clear_height_ft", None, confidence=0.1, status="unknown", needs_human=True,
                              note=(f"The largest scanned space ({summary['main_label']}, {summary['main_sf']:,} sq ft) "
                                    "has no measured height; Matterport reports 0 in large open spaces."), **common))
    return out, suite_heights
