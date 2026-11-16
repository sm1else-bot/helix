"""Exterior evidence from Vexcel for the building under the CoStar pin.

One Vexcel parcel can hold several buildings, so every fact here comes from the
structure whose footprint contains the CoStar pin (or the nearest one, flagged).
Requests ask for include=precomputed,calculated: "calculated" adds roof height and
ground elevation, which "precomputed" alone leaves empty.

Responses are cached in playground/output/vexcel-<property id>.json (gitignored) so
the demo can run with --offline.
"""
import json
import math
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from reconcile import record

VEXCEL = "https://api.vexcelgroup.com/v2"
LAYERS = "urban,urban-r,wide-area,urban-g"
CACHE = Path(__file__).parent / "output"
MIN_FT_PER_STORY = 8  # a roof lower than this per story is a data error, not a fact
DEFECTS = {  # Vexcel roof-defect flags worth turning into inspection questions
    "missing-material": "missing roofing material", "tarp": "tarps on the roof",
    "discoloration.types.water-pooling": "water pooling", "patch": "patched areas",
    "structural-damage": "structural damage", "rust": "rust", "debris": "debris",
    "discoloration.types.vegetation-on-roof": "vegetation on the roof",
}


def fetch_property(lat, lon, property_id, offline=False):
    """Vexcel property/extract at the CoStar pin, cached per property."""
    cache = CACHE / f"vexcel-{property_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    if offline:
        raise RuntimeError("no cached Vexcel response (run once online)")
    key = os.environ.get("VEXCEL_API_KEY")
    if not key:
        raise RuntimeError("VEXCEL_API_KEY not set")
    params = urlencode({"wkt": f"POINT({lon} {lat})", "srid": 4326, "layer": LAYERS, "latest": "true",
                        "include": "precomputed,calculated", "unit-system": "imperial"})
    try:
        with urlopen(Request(f"{VEXCEL}/property/extract?{params}", headers={"X-API-KEY": key}), timeout=120) as r:
            data = json.loads(r.read())
    except HTTPError as error:
        raise RuntimeError(f"Vexcel HTTP {error.code}") from None
    except URLError as error:
        raise RuntimeError(f"Vexcel unreachable: {error.reason}") from None
    CACHE.mkdir(exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def polygon(wkt):
    return [(float(x), float(y)) for x, y in re.findall(r"(-?\d+\.?\d*) (-?\d+\.?\d*)", wkt or "")]


def inside(point, poly):
    x, y = point
    hit, j = False, len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi:
            hit = not hit
        j = i
    return hit


def distance_m(point, poly):
    cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
    return math.hypot((point[0] - cx) * 111_320 * math.cos(math.radians(point[1])), (point[1] - cy) * 111_320)


def structure_under_pin(structures, lat, lon):
    """(structure, how): the footprint containing the pin, else the nearest (flagged)."""
    pin = (lon, lat)
    shaped = [(s, polygon(s.get("footprint.wkt"))) for s in structures]
    shaped = [(s, p) for s, p in shaped if len(p) >= 3]
    for s, p in shaped:
        if inside(pin, p):
            return s, "pin inside footprint"
    if not shaped:
        return None, "no footprints"
    s, p = min(shaped, key=lambda sp: distance_m(pin, sp[1]))
    return s, f"nearest footprint, {distance_m(pin, p):.0f} m from the pin"


def exterior_records(prop, data, expected_floor_sf=None):
    """Evidence records for the building under the CoStar pin."""
    pid = prop["id"]
    address = prop.get("primaryAddress") or {}
    lat, lon = address.get("latitude"), address.get("longitude")
    features = (data or {}).get("features") or []
    if not features:
        return [record("vexcel_structure", None, "vexcel", None, confidence=0.1, status="unknown", needs_human=True,
                       note="Vexcel returned no parcel at the CoStar pin.")], None
    attrs = features[0].get("properties") or {}
    date = (attrs.get("metadata.ortho-date-estimate") or "")[:10] or None
    source_id = attrs.get("metadata.collection")
    structures = attrs.get("structures") or []
    s, how = structure_under_pin(structures, lat, lon)
    rec = lambda fact, value, status, note, confidence=0.6, needs_human=False: record(  # noqa: E731
        fact, value, "vexcel", source_id, date, confidence, None, status, needs_human, note)
    out = []
    if s is None:
        return [rec("vexcel_structure", None, "unknown", "Parcel has no building footprints in Vexcel.", 0.1, True)], None
    pinned = how == "pin inside footprint"
    footprint = s.get("footprint.area")
    out.append(rec("footprint_sf", round(footprint) if footprint else None, "agrees" if pinned else "unknown",
                   f"Vexcel footprint of the building under the CoStar pin ({how}); the parcel has "
                   f"{len(structures)} structure(s). Footprint includes canopies and attached wings.",
                   0.7 if pinned else 0.3, not pinned))
    if footprint and expected_floor_sf and not pinned:
        out.append(rec("footprint_vs_costar_floor", None, "unknown",
                       "Not compared: no Vexcel footprint contains the CoStar pin, so the nearest structure may be a "
                       "different building. Check the pin and the parcel on the aerial.", 0.1, True))
    elif footprint and expected_floor_sf:
        ratio = footprint / expected_floor_sf
        ok = 0.8 <= ratio <= 1.2
        out.append(rec("footprint_vs_costar_floor", round(ratio, 2), "agrees" if ok else "conflict",
                       f"Footprint {footprint:,.0f} sq ft vs CoStar floor size {expected_floor_sf:,.0f} sq ft "
                       f"({ratio:.2f}x). "
                       + ("Within 20%." if ok else "Outside 20%: check for a podium, garage or wing in the footprint, "
                                                   "a different building under the pin, or a stale CoStar size."),
                       0.6 if pinned else 0.3, not ok))
    roof_h = s.get("roof-characteristics.height") or s.get("roof-characteristics.maximum-roof-height")
    stories = prop.get("numStoriesAboveGrade") or 1
    if roof_h and not pinned:
        out.append(rec("roof_height_ft", None, "unknown",
                       f"Vexcel reports {roof_h:.1f} ft for the nearest structure, which may not be this building; "
                       "not used.", 0.1, True))
    elif roof_h and roof_h < MIN_FT_PER_STORY * stories:
        out.append(rec("roof_height_ft", None, "unknown",
                       f"Vexcel reports a roof height of {roof_h:.1f} ft, implausible for {stories} stor"
                       f"{'y' if stories == 1 else 'ies'} (under {MIN_FT_PER_STORY} ft per story); treated as unknown "
                       "rather than used to rule anything out.", 0.1, True))
    else:
        out.append(rec("roof_height_ft", round(roof_h, 1) if roof_h else None, "agrees" if roof_h else "unknown",
                       ("Height of the roof above ground from Vexcel's elevation model. Interior clear height is "
                        "lower (roof deck, beams, lights), so this is an upper bound.") if roof_h
                       else "Vexcel has no roof height for this structure.", 0.6 if roof_h else 0.1))
    ground = attrs.get("ground-elevation.value") or s.get("ground-elevation.value")
    if ground is not None:
        out.append(rec("ground_elevation_ft", round(ground, 1), "agrees",
                       "Ground elevation above sea level at the building (Vexcel). Context for flood risk, not a "
                       "finished-floor elevation.", 0.6))
    material = s.get("roof-characteristics.material.class")
    shape = s.get("roof-characteristics.shape.value")
    material_score = s.get("roof-characteristics.material.score")
    if material and (material_score or 0) < 0.5:
        material = None  # low-confidence classification; do not report it as a fact
    if material or shape:
        out.append(rec("roof_type", " / ".join(x for x in (material, shape) if x), "agrees",
                       "Roof " + ("material and shape" if material else "shape") + " classified from imagery"
                       + (f" (material score {material_score:.2f})." if material else "."), 0.6))
    found = []
    for key, label in DEFECTS.items():
        if s.get(f"roof-defects.{key}.detected"):
            score = s.get(f"roof-defects.{key}.score")
            pct = s.get(f"roof-defects.{key}.percent")
            found.append(f"{label}" + (f" ({pct:g}% of roof)" if pct else "") + (f" score {score:.2f}" if score else ""))
    if not found and s.get("roof-defects.discoloration.types.water-pooling.score"):
        found.append(f"water pooling score {s['roof-defects.discoloration.types.water-pooling.score']:.2f}")
    out.append(rec("roof_defects", "; ".join(found) or "none detected", "unknown" if found else "agrees",
                   ("Visible on the latest aerial: " + "; ".join(found) + ". Ask for the roof's age, warranty and "
                    "a roofer's inspection; aerial detection does not establish leaks.") if found
                   else "No roof defects detected on the latest aerial. Not a roof inspection.",
                   0.5, bool(found)))
    condition = s.get("roof-characteristics.condition.precise-value") or s.get("roof-characteristics.condition")
    if condition is not None:
        out.append(rec("roof_condition_score", condition, "unknown",
                       "Vexcel roof condition score. The scale's direction is not documented in the sponsor "
                       "material, so it is shown for reference and not used to judge fit.", 0.2))
    ac = s.get("roof-objects.ac-unit.count")
    if ac is not None:
        out.append(rec("rooftop_ac_units", ac, "agrees",
                       "Rooftop AC units counted on the aerial (whole building). Capacity serving a suite needs an "
                       "HVAC contractor.", 0.6))
    vehicles = attrs.get("vehicles.count")
    spaces = sum((p.get("totalSpaces") or 0) for p in prop.get("parking") or [])
    if vehicles is not None:
        share = f" against {spaces} CoStar spaces ({vehicles / spaces:.0%})" if spaces else ""
        out.append(rec("vehicles_on_parcel", vehicles, "agrees",
                       f"Vehicles counted on the parcel in one aerial ({date}){share}. One snapshot: time of day "
                       "and weekday are unknown, so this is not an occupancy measure.", 0.4))
    return out, s


def roof_height_ft(records):
    return next((r["value"] for r in records if r["fact"] == "roof_height_ft" and r["value"]), None)
