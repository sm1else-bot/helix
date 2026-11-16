"""Which permitting authority covers a point? Official boundaries, never the postal city.

resolve_jurisdiction(lat, lon) asks the U.S. Census geocoder (geographies by coordinates, no key) for the
incorporated place and county containing the point, then maps it to one of the four authorities the
compliance engine knows:

    houston                 inside the City of Houston (Census incorporated place 4835000)
    harris_unincorporated   in Harris County (48201) and inside no incorporated place
    los_angeles             inside the City of Los Angeles (0644000)
    la_unincorporated       in Los Angeles County (06037) and inside no incorporated place

Anything else (another city such as Bellaire, Pasadena TX or Pico Rivera, another county, or a failed
lookup) returns code=None: the compliance engine then keeps its jurisdiction gate closed. A postal city
of "Houston" is not evidence: many Houston-addressed parcels are in other cities or unincorporated county.

Caveats, stated in every result: Census TIGER boundaries are updated annually and can lag a recent
annexation, and a point near a boundary can fall on the wrong side; confirm edge parcels with the city's
own GIS before relying on the answer.

Responses are cached in playground/output/jurisdiction/ (gitignored) by point rounded to 5 decimals
(about 1 m), so the demo runs offline after one online run. Offline with no cache, or any error, returns
code=None with the reason; it never raises and never guesses.

    python3 playground/jurisdiction.py 29.8540 -95.5317
"""
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CACHE = Path(__file__).parent / "output" / "jurisdiction"
LAYERS = "Incorporated Places,Counties"
USER_AGENT = "hackprinceton-compliance-jurisdiction/0.1 (research prototype)"

PLACES = {"4835000": "houston", "0644000": "los_angeles"}
COUNTIES = {"48201": "harris_unincorporated", "06037": "la_unincorporated"}
CAVEAT = ("Census TIGER boundaries (updated yearly) can lag recent annexations, and points near a boundary "
          "can land on the wrong side; confirm edge parcels with the city's GIS.")


def offline():
    return os.environ.get("JURISDICTION_OFFLINE", "") not in ("", "0", "false")


def query_url(lat, lon):
    return GEOCODER + "?" + urlencode({"x": f"{lon:.5f}", "y": f"{lat:.5f}", "benchmark": "Public_AR_Current",
                                       "vintage": "Current_Current", "layers": LAYERS, "format": "json"})


def fetch_census(lat, lon, timeout=20):
    """Raw Census geocoder geographies for the point, cached on disk."""
    url = query_url(lat, lon)
    path = CACHE / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if path.exists():
        return json.loads(path.read_text())
    if offline():
        raise RuntimeError("offline and no cached jurisdiction lookup for this point")
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
        body = response.read()
        if "json" not in (response.headers.get_content_type() or ""):
            raise RuntimeError(f"Census geocoder returned {response.headers.get_content_type()}, not JSON")
    geographies = json.loads(body)["result"]["geographies"]
    saved = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "geographies": geographies}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved))
    return saved


def classify(geographies):
    """(code, place, county, place_geoid, county_geoid) from Census geographies."""
    places = geographies.get("Incorporated Places") or []
    counties = geographies.get("Counties") or []
    place = places[0] if places else None
    county = counties[0] if counties else None
    place_id = (place or {}).get("GEOID")
    county_id = (county or {}).get("GEOID")
    if place:
        code = PLACES.get(place_id)
    else:
        code = COUNTIES.get(county_id)
    return code, (place or {}).get("NAME"), (county or {}).get("NAME"), place_id, county_id


def resolve_jurisdiction(lat, lon, fetch=fetch_census):
    """{code, verified, evidence_url, county, place, note, retrieved_at} for a point. Never raises."""
    base = {"code": None, "verified": False, "evidence_url": None, "county": None, "place": None,
            "retrieved_at": None}
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {**base, "note": "No usable coordinates; jurisdiction not looked up."}
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return {**base, "note": "Coordinates out of range; jurisdiction not looked up."}
    try:
        saved = fetch(lat, lon)
    except Exception as error:  # network, offline, bad payload: report, never guess
        return {**base, "note": f"Jurisdiction lookup unavailable ({type(error).__name__}: {error}). "
                               "Confirm with the city or county before any permit research."}
    code, place, county, place_id, county_id = classify(saved.get("geographies") or {})
    result = {**base, "code": code, "verified": code is not None, "evidence_url": saved.get("url"),
              "county": county, "place": place, "retrieved_at": saved.get("retrieved_at")}
    if code in ("houston", "los_angeles"):
        result["note"] = f"Inside {place} ({county}) per Census incorporated-place boundaries. {CAVEAT}"
    elif code:
        result["note"] = f"In unincorporated {county}: no incorporated place contains the point. {CAVEAT}"
    elif place:
        result["note"] = (f"Inside {place} ({county or 'county unknown'}), which has its own permitting authority "
                          "that the compliance engine does not model. The postal city may differ.")
    elif county:
        result["note"] = f"In {county}, outside the counties the compliance engine models."
    else:
        result["note"] = "Census returned no county or place for this point."
    return result


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: python3 playground/jurisdiction.py LAT LON")
    print(json.dumps(resolve_jurisdiction(sys.argv[1], sys.argv[2]), indent=2))


if __name__ == "__main__":
    main()
