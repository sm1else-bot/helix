"""Neighborhood demand and trends: does the area around an address support a business?

A small team of specialist agents investigates, and every step is recorded in a
trace (inputs -> sources -> findings with confidence -> hand-off). Each specialist
runs once per site and covers every industry asked about:

  planner      maps the request to industries (registry + synonyms, code; an LLM can be layered on)
  site         resolves the address or CoStar property to a point
  building     can this building (or suite) host each business? CoStar type, listed sizes and
               ceiling vs the registry's space needs; the hard gate before demand (properties only)
  residents    "Who lives here": ACS 2020-2024 vs 2015-2019 around the site
  competitors  "Who already serves it": OpenStreetMap facilities within the radius
  momentum     "What is moving in": CoStar kit tenant arrivals / departures by industry
  lookalike    "Does it look like places that already have this business?"
               (logistic model, measured on held-out real facilities per industry)
  coordinator  verdict per industry from the signals, with the deciding reasons
  narrator     optional plain-English summary written by an LLM from the coordinator output

Industries live in neighborhood_industries.py. Numbers are computed in code; the LLM
only writes the optional summary (labelled, and rejected if it adds a number).

Efficiency: everything heavy is prepared once and cached under
playground/output/neighborhood/ (gitignored): Census and Overpass responses, the
Houston-area reference profiles, kit tenancies, and one evaluated model per industry
(`--prepare`). A request is then in-memory math over grid indexes.
NEIGHBORHOOD_OFFLINE=1 (or --offline) reads only the cache.

Usage (from the repo root):
  python3.12 playground/with_env.py python3.12 playground/neighborhood.py --prepare all
  ... playground/neighborhood.py --property-id 13890701 --use "indoor pickleball"
  ... playground/neighborhood.py --address "9700 Tanner Rd, Houston, TX 77041" --use suggest
  ... playground/neighborhood.py --property-id 13890701 --use "bouldering gym" --explain
  ... playground/neighborhood.py --report        # evaluation table for every industry
  ... playground/neighborhood.py --bench         # cold and warm request timings
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from neighborhood_industries import (INDUSTRIES, NOT_RESIDENT_DRIVEN, RESIDENTIAL_TYPES, SPACE_SOURCES,
                                     industries_for_tenant, resolve)
from reconcile import record

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "output" / "neighborhood"
USER_AGENT = "hackprinceton-neighborhood-demand/0.2 (research prototype)"

# Houston area = the five core Houston-metro counties (Census FIPS, Texas = 48).
STATE = "48"
COUNTIES = {"201": "Harris", "157": "Fort Bend", "339": "Montgomery", "039": "Brazoria", "167": "Galveston"}
AREA_NAME = "Houston area (Harris, Fort Bend, Montgomery, Brazoria, Galveston counties)"
BBOX = (28.9, -96.1, 30.65, -94.35)  # south, west, north, east around those counties
RADIUS_MI = 3.0
MOMENTUM_RADIUS_MI = 5.0  # wider than the residents radius: the kit is a sample of buildings
MIN_FACILITIES = 30       # fewer mapped facilities than this: no lookalike model for the industry
VALIDATED_AUC = 0.6       # below this on the spatial hold-out, the lookalike is shown but does not vote
# Vote thresholds (signal_guide.py serves these to the UI legend, so the legend cannot drift from the code).
TREND_POINTS = 0.02       # population trend votes beyond +/-2 points vs the Houston area (and beyond the ACS MOE)
LOOKALIKE_PLUS, LOOKALIKE_MINUS = 70, 30  # percentile cut-offs for a validated lookalike
SUPPLY_UNDER, SUPPLY_OVER = 0.5, 2.0      # local supply rate vs the Houston-area rate: underserved / crowded
SUPPLY_P = 0.1            # supply votes only when the gap is unlikely by chance (Poisson tail p below this)
VERDICT_TOTAL = 2         # coordinator: supported at total >= +2, weak at total <= -2 (with business-specific evidence)
MAX_TRAINING_POSITIVES = 600  # subsample big industries (restaurants): same signal, bounded training time

ACS = "https://api.census.gov/data/{year}/acs/acs5"
GAZETTEER = {2024: "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_gaz_tracts_48.txt"}
# ACS 2015-2019 is published on 2010 tracts and 2020-2024 on 2020 tracts. This Census relationship
# file (land area of every 2010/2020 tract piece) moves 2015-2019 counts onto 2020 tracts.
RELATIONSHIP = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/tab20_tract20_tract10_st48.txt"
GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
OVERPASS = "https://overpass-api.de/api/interpreter"

# B01001 sex-by-age cells (male 003-025, female 027-049, same order) grouped into bands.
AGE_BANDS = {
    "under_5": [3], "age_5_17": [4, 5, 6], "age_18_24": [7, 8, 9, 10], "age_25_34": [11, 12],
    "age_35_54": [13, 14, 15, 16], "age_55_64": [17, 18, 19], "age_65_plus": [20, 21, 22, 23, 24, 25],
}
BAND_LABEL = {"under_5": "Under-5", "age_5_17": "School-age (5-17)", None: "Population"}
AGE_VARS = [f"B01001_{n + offset:03d}E" for band in AGE_BANDS.values() for n in band for offset in (0, 24)]
# 90% margins of error for the bands whose trends vote (summed in quadrature).
MOE_BANDS = ("under_5", "age_5_17")
MOE_VARS = [f"B01001_{n + offset:03d}M" for band in MOE_BANDS for n in AGE_BANDS[band] for offset in (0, 24)]
# population (+MOE), households, median household income, occupied units, renter-occupied units
BASE_VARS = ["B01003_001E", "B01003_001M", "B11001_001E", "B19013_001E", "B25003_001E", "B25003_003E"]


class Offline(RuntimeError):
    """Raised when a response is needed but offline mode allows only the cache."""


def offline():
    return os.environ.get("NEIGHBORHOOD_OFFLINE", "") not in ("", "0", "false")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fmt_pct(value, signed=False, points=False):
    if value is None:
        return "unknown"
    if points:
        return f"{value * 100:+.1f} pts"
    return f"{value:+.1%}" if signed else f"{value:.1%}"


# --- Cached fetching -----------------------------------------------------------

def public_url(url, params=None):
    """The URL a person can open as evidence: never includes an API key."""
    shown = {k: v for k, v in (params or {}).items() if k != "key"}
    return url + ("?" + urlencode(shown) if shown else "")


def fetch(url, params=None, form=None, kind="json", timeout=180):
    """GET/POST with a disk cache. Returns (payload, meta). The key never enters the cache key."""
    shown = public_url(url, params)
    digest = hashlib.sha1((shown + "|" + json.dumps(form, sort_keys=True)).encode()).hexdigest()[:20]
    path = CACHE / "http" / f"{digest}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        return saved["payload"], {"url": shown, "retrieved_at": saved["retrieved_at"], "cached": True}
    if offline():
        raise Offline(f"Offline mode and no cached response for {shown[:160]}")
    full = url + ("?" + urlencode(params) if params else "")
    body = urlencode(form).encode() if form else None
    last = None
    for attempt in range(4):
        try:
            with urlopen(Request(full, data=body, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
            break
        except HTTPError as error:
            last = RuntimeError(f"HTTP {error.code} from {url}")
            if error.code not in (429, 502, 503, 504):
                raise last from None
        except (URLError, TimeoutError) as error:
            if "CERTIFICATE_VERIFY_FAILED" in str(getattr(error, "reason", error)):
                raw = curl(full, body, timeout)  # Python's CA bundle lacks this chain; curl uses the system store
                break
            last = RuntimeError(f"Could not reach {url}: {getattr(error, 'reason', error)}")
        time.sleep(5 * (attempt + 1))
    else:
        raise last
    payload = json.loads(raw) if kind == "json" else raw
    retrieved = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": shown, "retrieved_at": retrieved, "payload": payload}))
    return payload, {"url": shown, "retrieved_at": retrieved, "cached": False}


def curl(url, body=None, timeout=180):
    """Fetch with the system curl (still verifies TLS, against the OS trust store)."""
    import subprocess
    command = ["curl", "-sS", "--fail", "-m", str(timeout), "-A", USER_AGENT, url]
    if body is not None:
        command[1:1] = ["--data-binary", "@-"]
    result = subprocess.run(command, input=body, capture_output=True, timeout=timeout + 5)
    if result.returncode:
        raise RuntimeError(f"Could not reach {url.split('?')[0]}: {result.stderr.decode(errors='replace')[:200]}")
    return result.stdout.decode("utf-8", "replace")


def source(name, meta, note=""):
    return {"name": name, "url": meta.get("url"), "retrieved_at": meta.get("retrieved_at"),
            "cached": meta.get("cached", True), "note": note}


# --- Geometry ------------------------------------------------------------------

def miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


class GridIndex:
    """Rows bucketed into 0.05-degree cells (~3.5 miles) so radius queries touch only nearby cells."""
    CELL = 0.05

    def __init__(self, rows):
        self.rows = list(rows)
        self.cells = {}
        for row in self.rows:
            self.cells.setdefault(self.key(row["lat"], row["lon"]), []).append(row)

    def key(self, lat, lon):
        return math.floor(lat / self.CELL), math.floor(lon / self.CELL)

    def near(self, lat, lon, radius):
        """[(distance, row)] within `radius` miles."""
        dlat = radius / 69.0
        dlon = radius / (69.0 * math.cos(math.radians(lat)))
        (a, b), (c, d) = self.key(lat - dlat, lon - dlon), self.key(lat + dlat, lon + dlon)
        out = []
        for i in range(a, c + 1):
            for j in range(b, d + 1):
                for row in self.cells.get((i, j), ()):
                    if abs(row["lat"] - lat) <= dlat and abs(row["lon"] - lon) <= dlon:
                        dist = miles(lat, lon, row["lat"], row["lon"])
                        if dist <= radius:
                            out.append((dist, row))
        return out


def near(rows, lat, lon, radius):
    """Rows within radius miles (builds a throwaway index; use GridIndex for repeated queries)."""
    return GridIndex(rows).near(lat, lon, radius)


# --- Census tables -------------------------------------------------------------

@lru_cache(maxsize=None)
def tracts(year):
    """{GEOID: {lat, lon, land_sqmi}} for the Houston-area counties, from the Census Gazetteer."""
    text, meta = fetch(GAZETTEER[year], kind="text")
    out = {}
    for line in text.splitlines()[1:]:
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) < 8 or cells[1][2:5] not in COUNTIES:
            continue
        out[cells[1]] = {"geoid": cells[1], "lat": float(cells[6]), "lon": float(cells[7]),
                         "land_sqmi": float(cells[4])}
    return out, meta


def number(value):
    """ACS uses large negative sentinels for 'not available'; those become None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def census_key_params(query):
    key = os.environ.get("CENSUS_API_KEY")
    return {**query, "key": key} if key else query


@lru_cache(maxsize=None)
def acs(year):
    """{GEOID: {variable: value}} for every Houston-area tract in the ACS 5-year release ending `year`."""
    table, metas = {}, []
    for variables in (BASE_VARS, AGE_VARS, MOE_VARS):
        query = census_key_params({"get": ",".join(variables), "for": "tract:*",
                                   "in": f"state:{STATE} county:{','.join(COUNTIES)}"})
        rows, meta = fetch(ACS.format(year=year), query)
        metas.append(meta)
        header = rows[0]
        for row in rows[1:]:
            cells = dict(zip(header, row))
            geoid = cells["state"] + cells["county"] + cells["tract"]
            table.setdefault(geoid, {}).update({v: number(cells.get(v)) for v in variables})
    return table, metas


def counts(v):
    """Population and age-band counts from one ACS tract row, with squared 90% MOEs ("<name>_moe2")."""
    out = {"pop": v["B01003_001E"], "pop_moe2": (v.get("B01003_001M") or 0) ** 2}
    for band, cells in AGE_BANDS.items():
        out[band] = sum((v.get(f"B01001_{n:03d}E") or 0) + (v.get(f"B01001_{n + 24:03d}E") or 0) for n in cells)
    for band in MOE_BANDS:
        out[f"{band}_moe2"] = sum((v.get(f"B01001_{n:03d}M") or 0) ** 2 + (v.get(f"B01001_{n + 24:03d}M") or 0) ** 2
                                  for n in AGE_BANDS[band])
    return out


@lru_cache(maxsize=None)
def tract_table(year=2024):
    """2020 tracts joined to their 2020-2024 ACS values (tuple of rows, tuple of source metas)."""
    points, gaz_meta = tracts(year)
    values, acs_metas = acs(year)
    rows = []
    for geoid, point in points.items():
        v = values.get(geoid)
        if not v or v.get("B01003_001E") is None:
            continue
        rows.append({**point, **counts(v), "pop_moe": v.get("B01003_001M"),
                     "households": v.get("B11001_001E"), "income": v.get("B19013_001E"),
                     "occupied": v.get("B25003_001E"), "renters": v.get("B25003_003E")})
    return tuple(rows), tuple(tuple(sorted(m.items())) for m in [gaz_meta, *acs_metas])


@lru_cache(maxsize=None)
def old_on_new():
    """2015-2019 ACS counts moved onto 2020 tracts by land-area share. Returns ({geoid20: counts}, metas).

    Each 2010 tract's counts are split across the 2020 tracts it overlaps in proportion to
    shared land area (assumes residents are spread evenly within a 2010 tract). County
    totals are preserved, and a site's trend compares the same 2020 tracts in both periods.
    """
    text, rel_meta = fetch(RELATIONSHIP, kind="text")
    values, acs_metas = acs(2019)
    out = {}
    for line in text.splitlines()[1:]:
        c = line.split("|")
        if len(c) < 16 or c[1][2:5] not in COUNTIES:
            continue
        geoid20, geoid10 = c[1], c[8]
        land10, part = float(c[10] or 0), float(c[14] or 0)
        v = values.get(geoid10)
        if not v or v.get("B01003_001E") is None or land10 <= 0 or part <= 0:
            continue
        share = part / land10
        dst = out.setdefault(geoid20, {})
        for k, value in counts(v).items():
            dst[k] = dst.get(k, 0.0) + value * (share ** 2 if k.endswith("_moe2") else share)
    return out, tuple(tuple(sorted(m.items())) for m in [rel_meta, *acs_metas])


@lru_cache(maxsize=None)
def tract_index(year=2024):
    return GridIndex(tract_table(year)[0])


def census_sources(year):
    if year == 2019:
        _, metas = old_on_new()
        names = ["Census 2010-to-2020 tract relationship file (land-area shares)",
                 "ACS 2015-2019 5-year (population, households, income, tenure)",
                 "ACS 2015-2019 5-year (sex by age, B01001)"]
    else:
        _, metas = tract_table(year)
        names = ["Census Gazetteer tract points", f"ACS {year - 4}-{year} 5-year (population, households, income, tenure)",
                 f"ACS {year - 4}-{year} 5-year (sex by age, B01001)"]
    return [source(n, dict(m)) for n, m in zip(names, metas)]


@lru_cache(maxsize=65536)
def area_profile(lat, lon, radius=RADIUS_MI):
    """Residents of the Census tracts whose internal point lies within `radius` miles.

    Tracts are the unit, so the area is approximate at its edge. Median household
    income is the household-weighted mean of tract medians (medians do not add up
    exactly), and is labelled that way wherever it is shown.
    """
    hits = [row for _, row in tract_index(2024).near(lat, lon, radius)]
    if not hits:
        return None
    pop = sum(r["pop"] for r in hits)
    land = sum(r["land_sqmi"] for r in hits)
    weighted = [(r["income"], r["households"] or 0) for r in hits if r["income"] is not None]
    hh_weight = sum(w for _, w in weighted)
    occupied = sum(r["occupied"] or 0 for r in hits)
    out = {
        "tracts": len(hits), "population": pop,
        "population_moe": round(math.sqrt(sum((r["pop_moe"] or 0) ** 2 for r in hits))),
        "households": sum(r["households"] or 0 for r in hits),
        "land_sqmi": round(land, 2), "density_per_sqmi": round(pop / land) if land else None,
        "median_income_approx": round(sum(i * w for i, w in weighted) / hh_weight) if hh_weight else None,
        "share_renters": round(sum(r["renters"] or 0 for r in hits) / occupied, 4) if occupied else None,
    }
    for band in AGE_BANDS:
        out[band] = sum(r[band] for r in hits)
        out[f"share_{band}"] = round(out[band] / pop, 4) if pop else None
    before, _ = old_on_new()  # 2015-2019 counts on the same 2020 tracts
    old = [before.get(r["geoid"], {}) for r in hits]
    for name, growth in (("pop", "growth"), ("under_5", "under_5_growth"), ("age_5_17", "age_5_17_growth")):
        now_, prior = sum(r[name] for r in hits), sum(o.get(name, 0) for o in old)
        out[growth] = round(now_ / prior - 1, 4) if prior else None
        # 90% MOE of the growth ratio: ratio x sqrt((MOE_new/new)^2 + (MOE_old/old)^2), sums in quadrature
        moe_new = math.sqrt(sum(r.get(f"{name}_moe2", 0) for r in hits))
        moe_old = math.sqrt(sum(o.get(f"{name}_moe2", 0) for o in old))
        out[f"{growth}_moe"] = (round(now_ / prior * math.sqrt((moe_new / now_) ** 2 + (moe_old / prior) ** 2), 4)
                                if prior and now_ else None)
    out["population_2019"] = round(sum(o.get("pop", 0) for o in old)) or None
    return out


@lru_cache(maxsize=None)
def area_totals():
    """Whole Houston-area totals (county sums are comparable across tract vintages)."""
    new, _ = tract_table(2024)
    before, _ = old_on_new()
    old = [before.get(r["geoid"], {}) for r in new]
    out = {"population": sum(r["pop"] for r in new), "population_2019": round(sum(o.get("pop", 0) for o in old))}
    out["growth"] = round(out["population"] / out["population_2019"] - 1, 4)
    for band in ("under_5", "age_5_17"):
        now_, prior = sum(r[band] for r in new), sum(o.get(band, 0) for o in old)
        out[band] = now_
        out[f"{band}_growth"] = round(now_ / prior - 1, 4)
    return out


@lru_cache(maxsize=None)
def reference_profiles(radius=RADIUS_MI):
    """The same profile around every Houston-area tract point: the distribution a site is compared to."""
    path = CACHE / f"reference-profiles-v4-{radius}.json"
    if path.exists():
        return json.loads(path.read_text())
    rows, _ = tract_table(2024)
    out = [{"geoid": r["geoid"], "lat": r["lat"], "lon": r["lon"], **(area_profile(r["lat"], r["lon"], radius) or {})}
           for r in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    return out


@lru_cache(maxsize=None)
def reference_values(field, radius=RADIUS_MI):
    return tuple(sorted(p[field] for p in reference_profiles(radius) if p.get(field) is not None))


def percentile(value, population):
    """Share of the reference values strictly below `value`, plus half the ties (0-100)."""
    values = [v for v in population if v is not None]
    if value is None or not values:
        return None
    below = sum(v < value for v in values)
    ties = sum(v == value for v in values)
    return round(100 * (below + ties / 2) / len(values))


def houston_percentile(field, value, radius=RADIUS_MI):
    return percentile(value, reference_values(field, radius))


@lru_cache(maxsize=None)
def us_population_6_plus_record():
    """US residents aged 6+ from the ACS 2020-2024 national table (SFIA surveys ages 6+)."""
    query = census_key_params({"get": "B01003_001E,B01001_003E,B01001_027E,B01001_004E,B01001_028E", "for": "us:1"})
    rows, meta = fetch(ACS.format(year=2024), query)
    cells = dict(zip(rows[0], rows[1]))
    total = float(cells["B01003_001E"])
    under5 = float(cells["B01001_003E"]) + float(cells["B01001_027E"])
    age5to9 = float(cells["B01001_004E"]) + float(cells["B01001_028E"])
    # Age 5 is one fifth of the 5-9 band (assumes even ages within the band).
    return meta, total - under5 - age5to9 / 5


# --- OpenStreetMap facilities ----------------------------------------------------

def overpass_query(selectors):
    s, w, n_, e = BBOX
    body = "".join(f"{sel}({s},{w},{n_},{e});" for sel in selectors)
    return f"[out:json][timeout:180];({body});out center tags;"


def element_point(element):
    if "lat" in element:
        return element["lat"], element["lon"]
    center = element.get("center") or {}
    return center.get("lat"), center.get("lon")


def is_indoor(tags):
    return (tags.get("indoor") == "yes" or tags.get("leisure") in ("sports_centre", "sports_hall", "fitness_centre")
            or tags.get("building") not in (None, "no", "roof"))


def is_commercial(tags):
    return tags.get("fee") == "yes" or tags.get("access") == "customers" or tags.get("leisure") == "sports_centre"


def cluster(points, radius_mi):
    """Merge OSM features into facilities: courts in one park become one facility.

    Features merge when within `radius_mi` and their names do not conflict (same
    name, or one unnamed). A floor of 0.02 mi merges duplicates of one place (a shop
    mapped as a point and as a building outline).
    """
    radius_mi = max(radius_mi, 0.02)
    facilities_out, index = [], {}
    for p in sorted(points, key=lambda p: (p["name"] is None, p["name"] or "")):
        cell = (round(p["lat"] / 0.01), round(p["lon"] / 0.01))
        span = max(1, math.ceil(radius_mi / 0.6))  # 0.01 degree is ~0.6-0.7 miles
        nearby = [f for i in range(-span, span + 1) for j in range(-span, span + 1)
                  for f in index.get((cell[0] + i, cell[1] + j), ())]
        for f in nearby:
            if miles(p["lat"], p["lon"], f["lat"], f["lon"]) <= radius_mi and (
                    p["name"] is None or f["name"] is None or p["name"].casefold() == f["name"].casefold()):
                f["features"] += 1
                f["osm_ids"].append(p["osm_id"])
                f["indoor"] = f["indoor"] or p["indoor"]
                f["commercial"] = f["commercial"] or p["commercial"]
                f["name"] = f["name"] or p["name"]
                break
        else:
            f = {**p, "features": 1, "osm_ids": [p["osm_id"]]}
            facilities_out.append(f)
            index.setdefault(cell, []).append(f)
    return facilities_out


TX_CHILDCARE = "https://data.texas.gov/resource/bc5r-88dy.json"
TX_CHILDCARE_PAGE = "https://data.texas.gov/dataset/HHSC-CCL-Daycare-and-Residential-Operations-Data/bc5r-88dy"
BATCH_GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
COUNTY_NAMES = {"201": "HARRIS", "157": "FORT BEND", "339": "MONTGOMERY", "039": "BRAZORIA", "167": "GALVESTON"}


def batch_geocode(rows):
    """Census batch geocoder (one POST, up to 10,000 addresses). rows: [(id, street, city, zip)] -> {id: (lat, lon)}."""
    csv_text = "".join(f'{i},"{street.replace(chr(34), "")}","{city}",TX,{zip_}\n' for i, street, city, zip_ in rows)
    digest = hashlib.sha1(csv_text.encode()).hexdigest()[:20]
    path = CACHE / "http" / f"geocode-batch-{digest}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        return {k: tuple(v) for k, v in saved["payload"].items()}, {"url": BATCH_GEOCODER,
                                                                     "retrieved_at": saved["retrieved_at"],
                                                                     "cached": True}
    if offline():
        raise Offline("Offline mode and no cached batch geocode")
    boundary = "----neighborhood" + digest
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"a.csv\"\r\n"
            f"Content-Type: text/csv\r\n\r\n{csv_text}\r\n--{boundary}--\r\n").encode()
    request = Request(BATCH_GEOCODER, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=600) as response:
        text = response.read().decode("utf-8", "replace")
    import csv
    import io
    out = {}
    for cells in csv.reader(io.StringIO(text)):
        if len(cells) >= 6 and cells[2] == "Match" and "," in cells[5]:
            lon, lat = (float(v) for v in cells[5].split(","))
            out[cells[0]] = (lat, lon)
    retrieved = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": BATCH_GEOCODER, "retrieved_at": retrieved, "payload": out}))
    return out, {"url": BATCH_GEOCODER, "retrieved_at": retrieved, "cached": False}


def tx_childcare_centers():
    """Texas HHSC licensed childcare centers (active) in the Houston-area counties, geocoded by the Census."""
    counties = ",".join(f"'{c}'" for c in COUNTY_NAMES.values())
    params = {"$select": "operation_id,operation_name,address_line,city,zipcode,total_capacity,licensed_to_serve_ages",
              "$where": f"county in({counties}) AND operation_type='Licensed Center' AND operation_status='Y'",
              "$limit": "10000"}
    records, meta = fetch(TX_CHILDCARE, params)
    wanted = [(r["operation_id"], r.get("address_line") or "", r.get("city") or "", (r.get("zipcode") or "")[:5])
              for r in records if r.get("address_line")]
    points, geo_meta = batch_geocode(wanted)
    rows = []
    for r in records:
        point = points.get(r["operation_id"])
        if not point:
            continue
        rows.append({"lat": point[0], "lon": point[1], "name": (r.get("operation_name") or "").strip() or None,
                     "indoor": True, "commercial": True, "features": 1,
                     "capacity": int(float(r["total_capacity"])) if r.get("total_capacity") else None,
                     "ref": r["operation_id"], "url": f"{TX_CHILDCARE}?operation_id={r['operation_id']}"})
    meta = {**meta, "url": TX_CHILDCARE_PAGE, "geocoded": len(rows), "listed": len(records),
            "geocoder": geo_meta["url"]}
    return rows, meta


DATASETS = {"tx_hhsc_childcare": (tx_childcare_centers, "Texas HHSC Child Care Licensing (active licensed centers), "
                                                        "geocoded by the Census batch geocoder")}


@lru_cache(maxsize=None)
def facilities(key):
    """All locations for an industry across the Houston area. Returns (rows, meta, GridIndex).

    Uses the industry's authoritative dataset when it has one, otherwise OpenStreetMap.
    """
    spec = INDUSTRIES[key]
    if spec.get("dataset"):
        loader, name = DATASETS[spec["dataset"]]
        rows, meta = loader()
        meta = {**meta, "source_name": name, "source": f"public:{spec['dataset']}"}
    else:
        query = overpass_query(spec["osm"])
        payload, meta = fetch(OVERPASS, form={"data": query})
        points = []
        for e in payload.get("elements", []):
            lat, lon = element_point(e)
            if lat is None:
                continue
            tags = e.get("tags") or {}
            points.append({"lat": lat, "lon": lon, "name": tags.get("name"), "osm_id": f"{e['type']}/{e['id']}",
                           "indoor": is_indoor(tags), "commercial": is_commercial(tags)})
        rows = cluster(points, spec["cluster_mi"])
        for f in rows:
            f["ref"], f["url"], f["capacity"] = f["osm_ids"][0], osm_link(f["osm_ids"][0]), None
        meta = {**meta, "url": OVERPASS + "?data=" + query, "source_name": "OpenStreetMap via Overpass",
                "source": "public:osm"}
    # Keep locations inside the Houston area: within 2 miles of a Houston-area tract point.
    tract_idx = tract_index(2024)
    rows = [f for f in rows if tract_idx.near(f["lat"], f["lon"], 2.0)]
    return rows, meta, GridIndex(rows)


def osm_link(osm_id):
    return f"https://www.openstreetmap.org/{osm_id}"


# --- CoStar kit tenancies --------------------------------------------------------

NOT_A_DEPARTURE = {7: "Renewal", 12: "MovingIntoSameBuilding", 14: "OccupancySplit"}


def kit_path():
    for candidate in (os.environ.get("COSTAR_KIT"), os.environ.get("COSTAR_KIT_DIR"),
                      ROOT.parent / "costar-hackathon-kit", Path.home() / "Downloads" / "costar-hackathon-kit.zip"):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def day(value):
    return (value or "")[:10] or None


@lru_cache(maxsize=4)
def kit_tenancies(kit=None):
    """Houston tenancies with location, industries, arrival and departure dates. Returns (rows, GridIndex).

    An arrival is the first occupancy of a tenant in a property (renewals and
    expansions are not new arrivals). A departure is an ended occupancy whose
    reason is not a renewal, a move within the building or a split. Industries
    come from tenant names (neighborhood_industries.industries_for_tenant).
    """
    from space_fit import kit_lines  # local import: the kit is optional for the rest of the pipeline
    kit = Path(kit) if kit else kit_path()
    if not kit:
        raise ValueError("CoStar kit not found. Set COSTAR_KIT to the unzipped kit folder or the zip.")
    rules = hashlib.sha1(json.dumps({k: v["names"] for k, v in INDUSTRIES.items()}).encode()).hexdigest()[:6]
    derived = CACHE / f"kit-tenancies-{hashlib.sha1(str(kit.resolve()).encode()).hexdigest()[:8]}-{rules}.json"
    if derived.exists():
        out = json.loads(derived.read_text())
        return tuple(out), GridIndex(out)
    password = os.environ.get("COSTAR_KIT_PASSWORD", "").encode() or None
    places = {}
    for line in kit_lines(kit, "data/commercial-property.jsonl", password):
        prop = json.loads(line)
        a = prop.get("primaryAddress") or {}
        if a.get("state") == "TX" and a.get("latitude"):
            places[prop["id"]] = (a["latitude"], a["longitude"])
    companies = {}
    for line in kit_lines(kit, "data/company-detail.jsonl", password):
        c = json.loads(line)
        companies[c["id"]] = c.get("name")
    rows = [r for r in (json.loads(line) for line in kit_lines(kit, "data/location-occupancy.jsonl", password))
            if r.get("propertyId") in places]
    first = {}
    for r in rows:
        k = (r.get("tenantLocationId"), r["propertyId"])
        start = day(r.get("moveInDate") or r.get("fromDate"))
        if r.get("tenantLocationId") and start and (k not in first or start < first[k]):
            first[k] = start
    out = []
    for r in rows:
        tenant = r.get("tenantLocationId")
        name = companies.get(tenant)
        start = day(r.get("moveInDate") or r.get("fromDate"))
        end = day(r.get("toDate"))
        lat, lon = places[r["propertyId"]]
        out.append({
            "id": r["id"], "property_id": r["propertyId"], "lat": lat, "lon": lon, "name": name,
            "industries": industries_for_tenant(name),
            "arrival": start if tenant and first.get((tenant, r["propertyId"])) == start else None,
            "departure": end if end and r.get("expirationReason") not in NOT_A_DEPARTURE else None,
            "sqft": sum((sp.get("squareFeetOccupied") or 0) for sp in r.get("spaces") or []) or None,
        })
    derived.parent.mkdir(parents=True, exist_ok=True)
    derived.write_text(json.dumps(out))
    return tuple(out), GridIndex(out)


# --- Lookalike model ---------------------------------------------------------------

FEATURES = {  # name -> (label, function of a 3-mile area profile)
    "log_density": ("residents per sq mi (log)", lambda p: math.log1p(p["density_per_sqmi"] or 0)),
    "log_income": ("median household income (log, approx.)", lambda p: math.log(max(p["median_income_approx"], 1))),
    "share_renters": ("share of homes rented", lambda p: p["share_renters"] or 0.0),
    "share_25_34": ("share aged 25-34", lambda p: p["share_age_25_34"]),
    "share_35_54": ("share aged 35-54", lambda p: p["share_age_35_54"]),
    "share_55_plus": ("share aged 55+", lambda p: p["share_age_55_64"] + p["share_age_65_plus"]),
    "share_under_18": ("share under 18", lambda p: p["share_under_5"] + p["share_age_5_17"]),
    "growth": ("population growth since 2015-19", lambda p: max(-0.5, min(1.0, p["growth"] or 0.0))),
}


def features(profile):
    if not profile or profile.get("median_income_approx") is None or not profile.get("population"):
        return None
    return [fn(profile) for _, fn in FEATURES.values()]


def solve(a, b):
    """Solve a small linear system by Gauss-Jordan elimination with partial pivoting."""
    m = [row[:] + [v] for row, v in zip(a, b)]
    size = len(m)
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("singular system")
        for r in range(size):
            if r != col:
                factor = m[r][col] / m[col][col]
                m[r] = [x - factor * y for x, y in zip(m[r], m[col])]
    return [m[r][size] / m[r][r] for r in range(size)]


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))


def fit_logistic(x, y, l2=1.0, iterations=25):
    """L2-regularised logistic regression by Newton's method on standardised features."""
    k = len(x[0])
    mean = [sum(r[j] for r in x) / len(x) for j in range(k)]
    sd = [math.sqrt(sum((r[j] - mean[j]) ** 2 for r in x) / len(x)) or 1.0 for j in range(k)]
    z = [[1.0] + [(r[j] - mean[j]) / sd[j] for j in range(k)] for r in x]
    w = [0.0] * (k + 1)
    for _ in range(iterations):
        grad = [0.0] * (k + 1)
        hess = [[0.0] * (k + 1) for _ in range(k + 1)]
        for row, yi in zip(z, y):
            pi = sigmoid(sum(a * b for a, b in zip(w, row)))
            err, curv = yi - pi, pi * (1 - pi)
            for i in range(k + 1):
                grad[i] += err * row[i]
                ci = curv * row[i]
                hrow = hess[i]
                for j in range(i, k + 1):
                    hrow[j] += ci * row[j]
        for i in range(k + 1):
            for j in range(i):
                hess[i][j] = hess[j][i]
            if i:
                grad[i] -= l2 * w[i]
                hess[i][i] += l2
        step = solve(hess, grad)
        w = [a + b for a, b in zip(w, step)]
        if max(abs(v) for v in step) < 1e-6:
            break
    return {"mean": mean, "sd": sd, "weights": w, "features": list(FEATURES) if k == len(FEATURES) else None}


def model_logit(model, row):
    return model["weights"][0] + sum(w * (v - m) / s for w, v, m, s in
                                     zip(model["weights"][1:], row, model["mean"], model["sd"]))


def contributions(model, row):
    """Per-feature push on the score (standardised value x weight): what makes this place look alike."""
    return {name: round(w * (v - m) / s, 3) for name, w, v, m, s in
            zip(model["features"], model["weights"][1:], row, model["mean"], model["sd"])}


def auc(positives, negatives):
    """Probability that a random positive scores above a random negative (ties count half)."""
    if not positives or not negatives:
        return None
    ranked = sorted([(v, 1) for v in positives] + [(v, 0) for v in negatives])
    rank_sum, i = 0.0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        rank_sum += (i + j + 1) / 2 * sum(label for _, label in ranked[i:j])
        i = j
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def training_data(key, radius=RADIUS_MI, cap=MAX_TRAINING_POSITIVES, seed=7):
    """Positives: real facilities (OSM), subsampled to `cap`. Background: every Houston-area tract point."""
    rows, _, _ = facilities(key)
    sample = random.Random(seed).sample(list(rows), cap) if len(rows) > cap else list(rows)
    pos = []
    for f in sample:
        row = features(area_profile(round(f["lat"], 5), round(f["lon"], 5), radius))
        if row:
            pos.append({"lat": f["lat"], "lon": f["lon"], "x": row, "positive": True,
                        "flag": bool(f["indoor"] or f["commercial"])})
    background = [{"lat": p["lat"], "lon": p["lon"], "x": features(p), "positive": False}
                  for p in reference_profiles(radius) if features(p)]
    return pos, background, len(rows)


def spatial_fold(lat, lon, folds, cell=0.2):
    """Assign a place to a fold by its 0.2-degree grid cell, so held-out places are not next to training ones."""
    key = f"{math.floor(lat / cell)}:{math.floor(lon / cell)}"
    return int(hashlib.sha1(key.encode()).hexdigest(), 16) % folds


def cross_validate(pos, background, cols, folds, fold_of):
    """Fit on all folds but one, score the held-out fold; pooled over folds."""
    held_pos, held_neg = [], []
    for k in range(folds):
        train = [r for r in pos + background if fold_of(r) != k]
        test_pos = [r for r in pos if fold_of(r) == k]
        test_neg = [r for r in background if fold_of(r) == k]
        if not any(r["positive"] for r in train) or not test_neg:
            continue
        model = fit_logistic([[r["x"][c] for c in cols] for r in train], [1 if r["positive"] else 0 for r in train])
        neg_scores = [model_logit(model, [r["x"][c] for c in cols]) for r in test_neg]
        held_neg.extend(neg_scores)
        for r in test_pos:
            held_pos.append((model_logit(model, [r["x"][c] for c in cols]), neg_scores, r.get("flag", False)))
    return held_pos, held_neg


def evaluate(key, folds=5, seed=7, radius=RADIUS_MI):
    """Hold out real facilities and check they score above random Houston-area places.

    Positives are mapped facilities (up to 600, sampled with a fixed seed); the
    background is every Houston-area tract point (one per ~4,500 residents, roughly
    a random residential address). Two splits: random by place, and spatial by
    0.2-degree grid cell (~13 miles) so a held-out facility's neighbours are not in
    training. Controls on the spatial split: income alone, density alone, and the
    full model with facility labels shuffled (should give ~0.5; checks for leakage).
    Reports AUC with a bootstrap interval and the hit rate: the share of held-out
    facilities scoring in the top 30% of their fold's background places.
    """
    started = time.perf_counter()
    pos, background, total = training_data(key, radius, seed=seed)
    out = {"industry": key, "label": INDUSTRIES[key]["label"], "facilities_mapped": total,
           "facilities_used": len(pos), "background_places": len(background), "radius_mi": radius,
           "features": {k: v[0] for k, v in FEATURES.items()}, "splits": {}, "status": "ok", "validated": False}
    if total < MIN_FACILITIES:
        out.update(status="too_few", note=f"Only {total} mapped facilities in OpenStreetMap; at least "
                                          f"{MIN_FACILITIES} are needed to train and test a lookalike model.")
        out["evaluated_at"] = now_iso()
        return out
    rng = random.Random(seed)
    names = list(FEATURES)
    all_cols = list(range(len(names)))
    for split in ("random", "spatial"):
        folds_of = {}
        for r in pos + background:
            folds_of[id(r)] = rng.randrange(folds) if split == "random" else spatial_fold(r["lat"], r["lon"], folds)
        fold_of = lambda r: folds_of[id(r)]  # noqa: E731
        held_pos, held_neg = cross_validate(pos, background, all_cols, folds, fold_of)
        scores = [s for s, _, _ in held_pos]
        flagged = [s for s, _, flag in held_pos if flag]
        hits = [percentile(s, negs) >= 70 for s, negs, _ in held_pos if negs]
        boot = sorted(auc([rng.choice(scores) for _ in scores], [rng.choice(held_neg) for _ in held_neg])
                      for _ in range(200))
        result = {
            "auc": round(auc(scores, held_neg), 3), "auc_90ci": [round(boot[10], 3), round(boot[189], 3)],
            "hit_rate_top30": round(sum(hits) / len(hits), 3) if hits else None, "hit_rate_by_chance": 0.3,
            "indoor_or_paid_facilities": len(flagged),
            "indoor_or_paid_auc": round(auc(flagged, held_neg), 3) if len(flagged) >= 10 else None,
            "held_out_facilities": len(scores), "held_out_background": len(held_neg),
        }
        if split == "spatial":
            baselines = {}
            for label, name in (("income_only", "log_income"), ("density_only", "log_density")):
                bp, bn = cross_validate(pos, background, [names.index(name)], folds, fold_of)
                baselines[label] = round(auc([s for s, _, _ in bp], bn), 3)
            shuffled = [dict(r) for r in pos + background]
            labels = [r["positive"] for r in shuffled]
            random.Random(seed + 1).shuffle(labels)
            for r, label in zip(shuffled, labels):
                r["positive"] = label
            spatial_of = lambda r: spatial_fold(r["lat"], r["lon"], folds)  # noqa: E731
            cp, cn = cross_validate([r for r in shuffled if r["positive"]], [r for r in shuffled if not r["positive"]],
                                    all_cols, folds, spatial_of)
            baselines["shuffled_labels"] = round(auc([s for s, _, _ in cp], cn), 3)
            result["baseline_auc"] = baselines
        out["splits"][split] = result
    model = fit_logistic([r["x"] for r in pos + background], [1] * len(pos) + [0] * len(background))
    out["model"] = model
    out["coefficients"] = {k: round(w, 3) for k, w in zip(names, model["weights"][1:])}
    out["background_logits"] = sorted(round(model_logit(model, r["x"]), 4) for r in background)
    spatial = out["splits"]["spatial"]["auc"]
    out["validated"] = spatial is not None and spatial >= VALIDATED_AUC
    dataset = INDUSTRIES[key].get("dataset")
    out["source"] = DATASETS[dataset][1] if dataset else "OpenStreetMap via Overpass"
    out["caveats"] = [
        ("Locations come from official licensing data; unplaced addresses are left out." if dataset else
         "Locations come from OpenStreetMap, which is incomplete and uneven by category."),
        "Measures resemblance to where facilities already are, not paid demand or profitability.",
        "Resident features only: workers, visitors and traffic (which drive restaurants and cafes) are not measured.",
        "The bootstrap interval resamples places independently and ignores spatial correlation, so it is too narrow.",
    ]
    out["evaluated_at"] = now_iso()
    out["seconds"] = round(time.perf_counter() - started, 1)
    return out


def evaluation_path(key):
    return CACHE / f"lookalike-v4-{key}.json"


@lru_cache(maxsize=None)
def lookalike_model(key):
    """The evaluated model for an industry, from the cache (built on first use if missing)."""
    path = evaluation_path(key)
    if path.exists():
        return json.loads(path.read_text())
    if offline():
        raise Offline(f"No prepared lookalike model for {key}; run --prepare online first.")
    result = evaluate(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    return result


def _evaluate_and_save(key):
    """Worker for --prepare (separate process: the fit is CPU-bound pure Python)."""
    result = evaluate(key)
    evaluation_path(key).parent.mkdir(parents=True, exist_ok=True)
    evaluation_path(key).write_text(json.dumps(result))
    return key, summarize_eval(result)


def prepare(keys, kit=None, fetch_workers=2, eval_workers=4, log=print):
    """Build every cached artifact the requests need. Overpass allows ~2 parallel slots per client."""
    started = time.perf_counter()
    reference_profiles()
    us_population_6_plus_record()
    log(f"census tables + reference profiles: {time.perf_counter() - started:.1f}s")
    try:
        t = time.perf_counter()
        rows, _ = kit_tenancies(kit)
        log(f"kit tenancies: {len(rows)} rows, {time.perf_counter() - t:.1f}s")
    except (OSError, ValueError, RuntimeError) as error:
        log(f"kit tenancies unavailable: {error}")

    def get(key):
        t = time.perf_counter()
        try:
            rows, _, _ = facilities(key)
            return key, len(rows), round(time.perf_counter() - t, 1), None
        except (RuntimeError, OSError, ValueError) as error:
            return key, 0, round(time.perf_counter() - t, 1), str(error)
    with ThreadPoolExecutor(fetch_workers) as pool:
        fetched = list(pool.map(get, keys))
    for key, count, seconds, error in fetched:
        log(f"osm {key}: {count} facilities, {seconds}s" + (f" ERROR {error}" if error else ""))
    todo = [k for k, _, _, error in fetched if not error and not evaluation_path(k).exists()]
    summaries = {}
    with ProcessPoolExecutor(eval_workers) as pool:
        for key, summary in pool.map(_evaluate_and_save, todo):
            summaries[key] = summary
            spatial = summary.get("splits", {}).get("spatial", {})
            log(f"eval {key}: {summary['status']} AUC {spatial.get('auc')} ({summary.get('seconds')}s)")
    log(f"prepare done in {time.perf_counter() - started:.1f}s")
    return summaries


def summarize_eval(ev):
    return {k: v for k, v in ev.items() if k not in ("model", "background_logits")}


# --- Trace and signals ---------------------------------------------------------

def run_agent(trace, agent, role, fn, inputs):
    """Run one specialist and record its step: inputs, sources, findings, confidence, hand-off."""
    started = time.perf_counter()
    step = {"agent": agent, "role": role, "computed_by": "code", "inputs": inputs,
            "sources": [], "findings": [], "summary": "", "confidence": None,
            "handoff": "", "signal": None, "per_industry": {}, "status": "ok"}
    try:
        step.update(fn(**inputs) or {})
    except Offline as error:
        step.update(status="unknown", summary=f"Offline and not cached: {error}",
                    handoff="Signal stays unknown; nothing was guessed.")
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        step.update(status="error", summary=f"{type(error).__name__}: {error}",
                    handoff="Signal stays unknown; nothing was guessed.")
    step["ms"] = round(1000 * (time.perf_counter() - started))
    trace.append(step)
    return step


def stance(value, *, plus, minus):
    """+1 at or above `plus`, -1 at or below `minus`, else 0; None stays unknown."""
    if value is None:
        return None
    return 1 if value >= plus else -1 if value <= minus else 0


def per_industry(industries, fn):
    """Run fn(key) for each industry; one industry failing does not stop the others."""
    out = {}
    for key in industries:
        try:
            out[key] = fn(key)
        except Offline as error:
            out[key] = {"status": "unknown", "summary": f"Offline and not cached: {error}", "findings": [],
                        "signal": None}
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            out[key] = {"status": "error", "summary": f"{type(error).__name__}: {error}", "findings": [],
                        "signal": None}
    return out


def flatten(results):
    return [r for v in results.values() for r in v.get("findings", [])]


def one_or_many(industries, results, many):
    return results[industries[0]].get("summary", "") if len(industries) == 1 else many


# --- Agents --------------------------------------------------------------------

def residents_agent(lat, lon, industries, radius=RADIUS_MI):
    """Who lives here: size, age mix, income, tenure and growth around the site vs the Houston area."""
    profile = area_profile(round(lat, 5), round(lon, 5), radius)
    sources = census_sources(2024) + census_sources(2019)
    if not profile:
        return {"sources": sources, "status": "unknown",
                "summary": "No Census tract point lies inside the radius; residents are unknown.",
                "findings": [record("residents_population", None, "public:acs", note="No tracts in radius.")]}
    totals = area_totals()
    acs_url, age_url = sources[1]["url"], sources[2]["url"]
    note_area = (f"Sum of the {profile['tracts']} Census tracts whose internal point is within {radius:g} miles. "
                 "Tract edges make the area approximate.")
    pct = {f: houston_percentile(f, profile.get(f), radius) for f in
           ("population", "density_per_sqmi", "median_income_approx", "growth", "share_renters",
            "share_age_25_34", "share_age_35_54", "share_age_55_64", "share_age_65_plus", "share_under_5")}
    findings = [
        record("residents_population", profile["population"], "public:acs", "acs-2020-2024-tracts",
               "2020-2024", 0.9, acs_url, "agrees",
               note=f"{note_area} Margin of error about ±{profile['population_moe']:,} (90%). "
                    f"Houston-area percentile of 3-mile areas: {pct['population']}."),
        record("residents_density_per_sqmi", profile["density_per_sqmi"], "public:acs", "acs-2020-2024-tracts",
               "2020-2024", 0.85, acs_url, "agrees", note=f"Houston-area percentile: {pct['density_per_sqmi']}."),
        record("residents_median_income_approx", profile["median_income_approx"], "public:acs",
               "acs-2020-2024-B19013", "2020-2024", 0.7, acs_url, "agrees",
               note="Household-weighted mean of tract median household incomes (2024 dollars); an approximation, "
                    f"since medians do not add. Houston-area percentile: {pct['median_income_approx']}."),
        record("residents_share_renters", profile["share_renters"], "public:acs", "acs-2020-2024-B25003",
               "2020-2024", 0.85, acs_url, "agrees",
               note=f"Renter-occupied share of occupied homes. Houston-area percentile: {pct['share_renters']}."),
        record("residents_growth_2019_to_2024", profile["growth"], "public:acs", "acs-2015-2019-vs-2020-2024",
               "2015-2019 vs 2020-2024", 0.6, acs_url, "agrees" if profile["growth"] is not None else "unknown",
               note=(f"Population {profile['population_2019']:,.0f} -> {profile['population']:,.0f} on the same 2020 "
                     f"tracts (2015-19 counts moved onto 2020 tracts by land-area share). Houston area overall: "
                     f"{totals['growth']:+.1%}. Two non-overlapping 5-year periods, so this is a 5-year trend, not a yearly rate. "
                     f"90% margin of error of the local change: ±{(profile.get('growth_moe') or 0) * 100:.1f} pts."
                     if profile["population_2019"] else "No 2015-2019 tracts in radius.")),
        record("residents_age_mix", {k[6:]: profile[k] for k in profile
                                     if k.startswith("share_age") or k == "share_under_5"},
               "public:acs", "acs-2020-2024-B01001", "2020-2024", 0.85, age_url, "agrees",
               note="Shares of residents by age band (ACS sex-by-age table B01001). Houston-area percentiles: "
                    + ", ".join(f"{k.removeprefix('share_')} {v}" for k, v in pct.items()
                                if k.startswith("share_age") or k == "share_under_5")),
    ]

    def one(key):
        spec = INDUSTRIES[key]
        band = spec["demand_band"]
        growth = "growth" if band is None else f"{band}_growth"
        local, area, moe = profile.get(growth), totals[growth], profile.get(f"{growth}_moe")
        value = None if local is None else round(local - area, 4)
        # Votes only beyond 2 points AND beyond the 90% margin of error of the local change.
        clear = value is not None and moe is not None and abs(value) > moe
        out_findings = []
        if band:
            out_findings.append(record(
                f"{key}.residents_{band}", profile[band], "public:acs", f"acs-B01001-{band}", "2020-2024", 0.8,
                age_url, "agrees", note=f"{BAND_LABEL[band]} residents within {radius:g} miles; change since "
                                        f"2015-2019: {fmt_pct(local)} (Houston area {area:+.1%})."))
        part = spec["participation"]
        if part:
            us_meta, us_6_plus = us_population_6_plus_record()
            if not any(s["name"].startswith("ACS 2020-2024 national") for s in sources):
                sources.append(source("ACS 2020-2024 national population by age", us_meta))
            rate = part["value"] / us_6_plus
            out_findings.append(record(
                f"{key}.expected_participants_at_national_rate", round(profile["population"] * rate), part["source"],
                f"{part['source']}-{part['year']}", str(part["year"]), 0.4, part["url"], "agrees",
                note=(f"Residents x the national rate ({part['value'] / 1e6:.1f}M / {us_6_plus / 1e6:.0f}M Americans "
                      f"aged 6+ = {rate:.1%}). Assumes this area matches the national average; local rates are "
                      "unknown. " + part["note"])))
        return {"findings": out_findings,
                "signal": {"name": "population trend", "value": value,
                           "stance": stance(value, plus=TREND_POINTS, minus=-TREND_POINTS) if clear else (0 if value is not None else None),
                           "reason": (f"{BAND_LABEL[band]} change since 2015-19 is {fmt_pct(value, points=True)} vs the "
                                      f"Houston area (margin ±{(moe or 0) * 100:.1f} pts)"
                                      + ("" if clear else ": within the margin of error") if value is not None
                                      else "Trend unknown")},
                "summary": f"{BAND_LABEL[band]} trend {fmt_pct(value, points=True)} vs area"}
    results = per_industry(industries, one)
    summary = (f"{profile['population']:,.0f} residents within {radius:g} mi "
               f"({pct['population']}th pct of Houston-area 3-mile areas), income ≈ "
               f"${profile['median_income_approx'] or 0:,} ({pct['median_income_approx']}th pct), "
               f"{fmt_pct(profile['share_renters'])} renters, growth {fmt_pct(profile['growth'], signed=True)} "
               f"since 2015-19 vs {totals['growth']:+.1%} area-wide.")
    return {"sources": sources, "findings": findings + flatten(results), "summary": summary, "confidence": 0.8,
            "profile": profile, "percentiles": pct, "per_industry": results,
            "handoff": "Profile to the lookalike agent (features); trend per industry to the coordinator."}


def poisson_tails(observed, expected):
    """(P[X <= observed], P[X >= observed]) for X ~ Poisson(expected)."""
    if expected <= 0:
        return 1.0, 1.0 if observed == 0 else 0.0
    term, cdf = math.exp(-expected), 0.0
    for k in range(observed + 1):
        if k:
            term *= expected / k
        cdf += term
    return min(cdf, 1.0), min(1.0, 1.0 - cdf + term)


def competitors_agent(lat, lon, industries, radius=RADIUS_MI):
    """Who already serves it: locations near the site and supply per resident vs the Houston area.

    When the source has capacity (licensed childcare seats) and the industry has a demand
    age band, supply is seats per resident of that band (seats per child under 5).
    """
    profile = area_profile(round(lat, 5), round(lon, 5), radius)
    totals = area_totals()
    sources = []

    def one(key):
        rows, meta, index = facilities(key)
        spec = INDUSTRIES[key]
        word = spec["places"]
        osm = meta["source"] == "public:osm"
        note = ("Volunteer-mapped; absence on the map is not proof of absence." if osm else
                f"{meta.get('geocoded')} of {meta.get('listed')} listed locations placed by the Census geocoder.")
        sources.append(source(f"{meta['source_name']} ({word})", meta, note))
        local = sorted(index.near(lat, lon, radius), key=lambda x: x[0])
        wider = index.near(lat, lon, 5.0)
        band = spec["demand_band"]
        seats = band and rows and all(f.get("capacity") is not None for f in rows[:50])
        if seats:  # capacity per resident of the demand band (e.g. licensed seats per child under 5)
            unit = f"licensed seats per {BAND_LABEL[band].lower()} child"
            local_supply = sum(f["capacity"] or 0 for _, f in local)
            local_demand = profile[band] if profile else None
            area_supply, area_demand = sum(f["capacity"] or 0 for f in rows), totals[band]
            local_rate = local_supply / local_demand if local_demand else None
            area_rate = area_supply / area_demand
            shown = lambda v: f"{v:.2f}"  # noqa: E731
        else:
            unit = "per 10k residents"
            local_rate = len(local) / profile["population"] * 10_000 if profile and profile["population"] else None
            area_rate = len(rows) / totals["population"] * 10_000
            shown = lambda v: f"{v:.2f}"  # noqa: E731
        ratio = local_rate / area_rate if local_rate is not None and area_rate else None
        nearest = min(index.near(lat, lon, 25.0), key=lambda x: x[0], default=None)
        when = (meta.get("retrieved_at") or "")[:10]
        paid = sum(f["indoor"] or f["commercial"] for _, f in local)
        findings = [
            record(f"{key}.competitors_within_radius", len(local), meta["source"], f"{spec.get('dataset') or 'overpass'}-{key}",
                   when, 0.6 if osm else 0.85, meta["url"], "agrees",
                   note=f"{word.capitalize()} within {radius:g} miles" + (f" ({paid} indoor or paid). Features in one "
                        "place count as one location. OpenStreetMap is incomplete." if osm else
                        f"; {sum(f['capacity'] or 0 for _, f in local):,} licensed seats.")),
            record(f"{key}.competitors_within_5mi", len(wider), meta["source"], f"{spec.get('dataset') or 'overpass'}-{key}",
                   when, 0.6 if osm else 0.85, meta["url"], "agrees", note="Same count at 5 miles."),
            record(f"{key}.supply_rate", None if local_rate is None else round(local_rate, 3), "computed",
                   f"{'capacity' if seats else 'count'} / acs-population", when, 0.5 if osm else 0.75, meta["url"],
                   "agrees" if local_rate is not None else "unknown",
                   note=f"Supply here, {unit}. Houston area: {shown(area_rate)} ({len(rows):,} locations). Ratio here "
                        f"vs area: {'unknown' if ratio is None else f'{ratio:.2f}x'}."),
        ]
        if nearest:
            d, f = nearest
            findings.append(record(f"{key}.nearest_competitor", {"name": f["name"] or "(unnamed)", "miles": round(d, 2)},
                                   meta["source"], f["ref"], when, 0.6, f["url"], "agrees",
                                   note="Straight-line distance, not drive time."))
        votes = spec["supply_votes"]
        if len(rows) < MIN_FACILITIES:  # a ratio against 2 locations area-wide means nothing
            ratio = None
        # Is the difference more than chance? Expected count at the Houston-area rate for this many residents.
        demand_local = (profile[band] if seats else profile["population"]) if profile else 0
        demand_area = totals[band] if seats else totals["population"]
        expected = len(rows) * demand_local / demand_area if demand_area else 0
        p_low, p_high = poisson_tails(len(local), expected)
        mapping_gap = osm and not local and expected >= 5
        if mapping_gap:
            ratio = None
        if ratio is None:
            stance_value = None
        elif not votes:
            stance_value = 0
        else:
            stance_value = (1 if ratio <= SUPPLY_UNDER and p_low < SUPPLY_P else -1 if ratio >= SUPPLY_OVER and p_high < SUPPLY_P
                           else 0)
        tag = ("" if ratio is None or stance_value is None else " (underserved)" if stance_value == 1
               else " (crowded)" if stance_value == -1 else
               " (not different from chance)" if (ratio <= SUPPLY_UNDER or ratio >= SUPPLY_OVER) and votes else "")
        findings.append(record(
            f"{key}.supply_expected_at_area_rate", round(expected, 1), "computed", "poisson", when, 0.6, None,
            "unknown" if mapping_gap else "agrees",
            note=(f"Found {len(local)}, expected {expected:.1f} at the Houston-area rate (Poisson: P(this few or fewer)"
                  f" = {p_low:.2f}, P(this many or more) = {p_high:.2f}). Supply votes only when the gap is unlikely by "
                  "chance (p < 0.1)." + (" None mapped where 5+ are expected: probably a gap in OpenStreetMap, so "
                                         "supply stays unknown." if mapping_gap else ""))))
        what = (f"{sum(f['capacity'] or 0 for _, f in local):,} licensed seats in {len(local)} {word} within "
                f"{radius:g} mi = {ratio:.1f}x the Houston-area {unit.replace('per', 'rate per')}" if seats and ratio is not None
                else f"{len(local)} {word} mapped within {radius:g} mi" + (f" ({paid} indoor or paid)" if local and osm else "")
                + (f" = {ratio:.1f}x the Houston-area rate per resident" if ratio is not None else ""))
        return {"findings": findings,
                "signal": {"name": "local supply", "value": None if ratio is None else round(ratio, 2),
                           "stance": stance_value,
                           "reason": (f"Too few {word} in the source area-wide ({len(rows)}) to compare supply"
                                      if len(rows) < MIN_FACILITIES else
                                      f"0 {word} mapped within {radius:g} mi where {expected:.0f} would be expected: "
                                      "probably a gap in OpenStreetMap, so supply is unknown" if mapping_gap
                                      else "Supply unknown" if ratio is None
                                      else what + tag
                                      + ("" if votes else "; these businesses cluster on purpose, so supply "
                                                          "does not vote"))},
                "summary": f"{len(local)} within {radius:g} mi, {len(wider)} within 5 mi"
                           + (f"; nearest {nearest[1]['name'] or 'unnamed'} at {nearest[0]:.1f} mi" if nearest else ""),
                "nearby": [{"name": f["name"], "miles": round(d, 2), "indoor": f["indoor"], "capacity": f.get("capacity"),
                            "commercial": f["commercial"], "url": f["url"]} for d, f in local[:10]]}
    results = per_industry(industries, one)
    known = [k for k, v in results.items() if v.get("signal")]
    crowded = [INDUSTRIES[k]["label"] for k in known if results[k]["signal"]["stance"] == -1]
    under = [INDUSTRIES[k]["label"] for k in known if results[k]["signal"]["stance"] == 1]
    return {"sources": sources, "findings": flatten(results), "per_industry": results, "confidence": 0.5,
            "summary": one_or_many(industries, results,
                                   f"Checked {len(known)}/{len(industries)} industries. Underserved: "
                                   f"{', '.join(under) or 'none'}. Crowded: {', '.join(crowded) or 'none'}."),
            "handoff": "Supply ratio per industry to the coordinator; locations train the lookalike agent."}


def momentum_agent(lat, lon, industries, radius=MOMENTUM_RADIUS_MI, kit=None, today=None):
    """What is moving in: arrivals and departures of each kind of business near the site (CoStar kit)."""
    _, index = kit_tenancies(kit)
    today = today or datetime.now(timezone.utc).date().isoformat()
    since = f"{int(today[:4]) - 3}{today[4:]}"
    local = [r for _, r in index.near(lat, lon, radius)]
    buildings = len({r["property_id"] for r in local})
    years = list(range(int(today[:4]) - 7, int(today[:4]) + 1))
    all_in = sum(1 for r in local if r["arrival"] and since < r["arrival"] <= today)
    all_out = sum(1 for r in local if r["departure"] and since < r["departure"] <= today)
    source_id = "costar-kit:location-occupancy+company-detail+commercial-property"
    caveat = ("The kit covers only CoStar's Matterport-scanned properties (about 1,758 in Houston), so this is a "
              "sample of buildings, not every business. Industries come from tenant names (keyword rules).")

    def by_year(items, field):
        return {y: sum(1 for r in items if r[field] and r[field][:4] == str(y) and r[field] <= today) for y in years}

    buckets = {}  # industry -> local tenancies, built once (most tenants match no industry)
    for r in local:
        for k in r["industries"]:
            buckets.setdefault(k, []).append(r)

    def moves(keys):
        mine = list({r["id"]: r for k in keys for r in buckets.get(k, ())}.values())
        recent_in = [r for r in mine if r["arrival"] and since < r["arrival"] <= today]
        recent_out = [r for r in mine if r["departure"] and since < r["departure"] <= today]
        return mine, recent_in, recent_out

    def one(key):
        label = INDUSTRIES[key]["label"].lower()
        group = INDUSTRIES[key]["group"]
        mine, recent_in, recent_out = moves([key])
        scope = label
        if len(recent_in) + len(recent_out) < 2:  # too few of this exact business: use its group, and say so
            family = [k for k, spec in INDUSTRIES.items() if spec["group"] == group]
            mine, recent_in, recent_out = moves(family)
            scope = f"{group.lower()} businesses (too few {label} moves alone)"
        net = len(recent_in) - len(recent_out)
        enough = len(recent_in) + len(recent_out) >= 2
        findings = [
            record(f"{key}.kit_arrivals_by_year", by_year(mine, "arrival"), "costar", source_id, today, 0.5, None,
                   "agrees", note=f"Arrivals of {scope} in {buildings} kit buildings within {radius:g} mi. First "
                                  "occupancy of a tenant in a building; renewals excluded."),
            record(f"{key}.kit_departures_by_year", by_year(mine, "departure"), "costar", source_id, today, 0.5,
                   None, "agrees", note=f"Departures of {scope}, excluding renewals, moves within the building and "
                                        "splits."),
            record(f"{key}.kit_net_last_3_years", net if enough else None, "costar", source_id, today,
                   0.4 if scope == label else 0.3, None, "agrees" if enough else "unknown",
                   note=(f"{scope.capitalize()}: {len(recent_in)} arrived, {len(recent_out)} left since {since}."
                         if enough else f"Only {len(recent_in) + len(recent_out)} {scope} moves since {since} in "
                                        "the sample: too few to call a trend.")),
        ]
        return {"findings": findings,
                "signal": {"name": "business arrivals", "value": net if enough else None,
                           "stance": None if not enough else (1 if net > 0 else -1 if net < 0 else 0),
                           "reason": (f"{len(recent_in)} {scope} moved into nearby CoStar buildings since "
                                      f"{since[:4]}, {len(recent_out)} left" if enough else
                                      f"Too few {group.lower()} moves in the CoStar sample nearby to call a trend")},
                "summary": f"{len(recent_in)} in / {len(recent_out)} out since {since[:4]}"
                           + ("" if scope == label else f" ({group.lower()})"),
                "examples": [{"name": r["name"], "arrived": r["arrival"], "sqft": r["sqft"],
                              "costar_property_id": r["property_id"]}
                             for r in sorted(recent_in, key=lambda r: r["arrival"], reverse=True)[:5]]}
    results = per_industry(industries, one)
    shared = record("kit_all_business_net_last_3_years", all_in - all_out, "costar", source_id, today, 0.5, None,
                    "agrees", note=f"All tenants within {radius:g} mi: {all_in} arrived, {all_out} left since {since}. "
                                   + caveat)
    moving = [f"{INDUSTRIES[k]['label']} ({v['summary']})" for k, v in results.items()
              if (v.get("signal") or {}).get("stance") is not None]
    return {"sources": [{"name": "CoStar hackathon kit: tenancies, companies, properties", "url": None,
                         "retrieved_at": None, "cached": True, "note": caveat}],
            "findings": [shared] + flatten(results), "per_industry": results, "confidence": 0.4,
            "summary": (f"{len(local)} tenancies in {buildings} kit buildings within {radius:g} mi; all businesses: "
                        f"{all_in} arrived, {all_out} left since {since[:4]}. "
                        + (f"{INDUSTRIES[industries[0]]['label']}: {results[industries[0]].get('summary')}."
                           if len(industries) == 1 else f"Enough moves to call: {'; '.join(moving) or 'none'}.")),
            "handoff": "Per-industry momentum to the coordinator (counts only with 2+ moves). All-business moves "
                       "are context, not a vote."}


def lookalike_agent(lat, lon, industries, radius=RADIUS_MI):
    """Does this place look like places where each business already operates? Measured, not asserted."""
    row = features(area_profile(round(lat, 5), round(lon, 5), radius))
    sources = []

    def one(key):
        ev = lookalike_model(key)
        label = INDUSTRIES[key]["label"].lower()
        if ev["status"] != "ok":
            return {"findings": [record(f"{key}.lookalike_percentile", None, "computed", f"lookalike-{key}",
                                        note=ev.get("note", "No model."))],
                    "signal": {"name": "lookalike", "value": None, "stance": None, "validated": False,
                               "reason": ev.get("note", "No lookalike model")},
                    "summary": "no model (too few mapped)", "evaluation": summarize_eval(ev)}
        spatial = ev["splits"]["spatial"]
        validated = ev["validated"]
        method = (f"Logistic model on {len(FEATURES)} resident features within {radius:g} mi, trained on "
                  f"{ev['facilities_used']} of {ev['facilities_mapped']} mapped {label} vs "
                  f"{ev['background_places']} Houston-area tract points. Held out by area (13-mile cells): AUC "
                  f"{spatial['auc']} (90% CI {spatial['auc_90ci'][0]}-{spatial['auc_90ci'][1]}); income alone "
                  f"{spatial['baseline_auc']['income_only']}; shuffled labels "
                  f"{spatial['baseline_auc']['shuffled_labels']}.")
        sources.append({"name": f"Lookalike evaluation: {label}", "url": None, "retrieved_at": ev["evaluated_at"],
                        "cached": True, "note": method})
        if not row:
            return {"findings": [record(f"{key}.lookalike_percentile", None, "computed", f"lookalike-{key}",
                                        note="No resident profile at this site.")],
                    "signal": {"name": "lookalike", "value": None, "stance": None, "validated": validated,
                               "reason": "No resident profile"}, "summary": "unknown"}
        pct = percentile(model_logit(ev["model"], row), ev["background_logits"])
        push = contributions(ev["model"], row)
        top = sorted(push.items(), key=lambda kv: -abs(kv[1]))[:3]
        why = ", ".join(f"{FEATURES[k][0]} {'raises' if v > 0 else 'lowers'} it" for k, v in top)
        findings = [
            record(f"{key}.lookalike_percentile", pct, "computed", f"lookalike-{key}", ev["evaluated_at"][:10],
                   0.7 if validated else 0.3, None, "agrees" if validated else "unknown",
                   note=f"Looks more like places with {label} than {pct}% of Houston-area neighborhoods. Biggest "
                        f"drivers here: {why}. {method}"),
            record(f"{key}.lookalike_drivers", push, "computed", f"lookalike-{key}", ev["evaluated_at"][:10], 0.6,
                   None, "agrees", note="Standardised feature x model weight; positive pushes toward 'looks like "
                                        "existing facilities'. Correlation with where facilities are, not demand."),
        ]
        return {"findings": findings,
                "signal": {"name": "lookalike", "value": pct, "validated": validated,
                           "stance": stance(pct, plus=LOOKALIKE_PLUS, minus=LOOKALIKE_MINUS) if validated else None,
                           "reason": (f"Looks more like places that already have {label} than {pct}% of "
                                      "Houston-area neighborhoods" +
                                      ("" if validated else f" (model AUC {spatial['auc']} < {VALIDATED_AUC}: "
                                                            "not used in the verdict)"))},
                "summary": f"{pct}/100 ({why})", "evaluation": summarize_eval(ev)}
    results = per_industry(industries, one)
    return {"sources": sources, "findings": flatten(results), "per_industry": results, "confidence": 0.7,
            "summary": one_or_many(industries, results, "Scores: " + ", ".join(
                f"{INDUSTRIES[k]['label']} {(v.get('signal') or {}).get('value')}" for k, v in results.items())),
            "handoff": f"Percentile per industry to the coordinator (votes only if held-out AUC >= {VALIDATED_AUC})."}


# --- Site, coordinator, narrator ----------------------------------------------------

@lru_cache(maxsize=512)
def property_pin(property_id, kit=""):
    """CoStar primaryAddress for a property, cached on disk (reading the encrypted zip is slow)."""
    path = CACHE / "property-pins.json"
    pins = json.loads(path.read_text()) if path.exists() else {}
    if str(property_id) in pins:
        return pins[str(property_id)]
    from space_fit import load_property
    kit_file = Path(kit) if kit else kit_path()
    if not kit_file:
        raise ValueError("CoStar kit not found; cannot look up the property id.")
    password = os.environ.get("COSTAR_KIT_PASSWORD", "").encode() or None
    address = load_property(kit_file, property_id, password).get("primaryAddress") or {}
    pins[str(property_id)] = address
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins))
    return address


PROPERTY_TYPES = {"1": "hospitality", "2": "industrial", "3": "land", "5": "office", "6": "retail", "7": "flex",
                  "8": "sports & entertainment", "9": "mixed-use", "10": "specialty", "11": "multifamily",
                  "13": "healthcare", "15": "residential", "16": "residential"}
SUBTYPES = {"5": "auto repair shop", "7": "bar", "8": "car wash", "9": "convenience store", "10": "daycare center",
            "11": "drug store", "12": "fast-food restaurant", "18": "restaurant", "19": "service station",
            "23": "supermarket", "25": "veterinarian or kennel", "30": "rehabilitation center", "47": "self storage",
            "63": "loft or creative office", "64": "medical office", "90": "health club"}


def space_value(space):
    """Same key as the /maximo suite picker: the suite name, or listing:<id>:<size> for unnamed spaces."""
    suite = str(space.get("suite") or "").strip()
    return suite or (f"listing:{space['listing_id']}:{space['square_feet']:g}" if space.get("square_feet") else None)


@lru_cache(maxsize=512)
def building_facts(property_id, kit=""):
    """What CoStar records about the building and its listed spaces, cached on disk."""
    path = CACHE / "building-facts.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    if str(property_id) in saved:
        return saved[str(property_id)]
    from reconcile import costar_ceiling_ft
    from space_fit import building_numbers, listed_spaces, load_property
    kit_file = Path(kit) if kit else kit_path()
    if not kit_file:
        raise ValueError("CoStar kit not found; cannot check the building.")
    password = os.environ.get("COSTAR_KIT_PASSWORD", "").encode() or None
    prop = load_property(kit_file, property_id, password)
    ceiling, ceiling_source = costar_ceiling_ft(prop)
    numbers = building_numbers(prop)
    facts = {"property_id": property_id, "type": str(prop.get("propertyType") or ""),
             "subtype": str(prop.get("propertySubtype") or ""), "gba": prop.get("grossBuildingAreaSquareFeet"),
             "stories": prop.get("numStoriesAboveGrade"), "ceiling_ft": ceiling, "ceiling_source": ceiling_source,
             "drive_in_bays": numbers.get("drive_in_bays"), "docks": numbers.get("docks"),
             "listed": [{"value": space_value(sp), "suite": sp.get("suite"), "sf": sp.get("square_feet")}
                        for sp in listed_spaces(kit_file, property_id, password)]}
    saved[str(property_id)] = facts
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved))
    return facts


FIT_ORDER = {"fits": 0, "check": 1, "no": 2}
# CoStar FloorName codes (kit schemas/enums.json) that are at street level.
STREET_LEVEL_FLOORS = {21: "plaza", 22: "lobby", 23: "ground floor", 24: "first floor", 152: "ground/garden level"}
UNKNOWN_FLOORS = {None, 0, 1}


def floor_label(code):
    if code in STREET_LEVEL_FLOORS:
        return STREET_LEVEL_FLOORS[code]
    return {18: "lower level", 19: "concourse", 20: "basement", 25: "second floor", 26: "mezzanine",
            27: "third floor"}.get(code, f"floor code {code}")


def building_fit(key, facts, suite=None):
    """Can this building (or suite) host the business? Hard facts from CoStar against the registry's needs.

    Returns status fits / check / no, the reasons, what is unknown, what to verify, and a
    capacity where a sourced per-unit area exists (courts, children).
    """
    need = INDUSTRIES[key]["space"]
    status, reasons, unknowns = "fits", [], []

    def fail(reason):
        nonlocal status
        status = "no"
        reasons.append(reason)
    kind = PROPERTY_TYPES.get(facts["type"], "unknown-type")
    article = "an" if kind[:1] in "aeiou" else "a"
    if facts["type"] in RESIDENTIAL_TYPES:
        return {"status": "no", "scope": "whole building", "space_sf": None, "capacity": None,
                "reasons": [f"Residential ({kind}) building: not a commercial space"], "unknowns": [],
                "verify": [], "sources": [], "basis": "CoStar property type."}
    if facts["subtype"] in need["subtypes"]:
        sub = SUBTYPES.get(facts["subtype"], "matching")
        reasons.append(f"Built as {'an' if sub[:1] in 'aeiou' else 'a'} {sub}")
    elif facts["type"] in need["types"]:
        reasons.append(f"{kind.capitalize()} buildings commonly host this")
    else:
        fail(f"Not a typical fit for {article} {kind} building")
    # Size: the chosen suite, else the listed spaces, else the whole building.
    candidates = [sp for sp in facts["listed"] if suite and sp["value"] == suite]
    scope = (f"suite {candidates[0]['suite']}" if candidates and candidates[0].get("suite")
             else "the chosen listed space" if candidates else None)
    if not candidates and facts["listed"]:
        candidates, scope = facts["listed"], "listed spaces"
        # Executive-suite offices are not standalone premises; only consider them if nothing else is listed.
        standalone = [sp for sp in candidates if not sp.get("executive_suite")]
        if standalone:
            candidates = standalone
    if candidates and all(sp.get("executive_suite") for sp in candidates):
        fail("Executive-suite office (a furnished office inside a shared suite), not a standalone premises")
    if need.get("prefers_street_level") and candidates and status != "no":
        floors = {sp.get("floor") for sp in candidates}
        if floors <= set(STREET_LEVEL_FLOORS):
            reasons.append(f"Street level ({floor_label(next(iter(floors)))}, CoStar)")
        elif not floors & set(STREET_LEVEL_FLOORS):
            known = [f for f in floors if f not in UNKNOWN_FLOORS]
            unknowns.append((f"Listed on the {floor_label(known[0])}" if known else "Floor not recorded")
                            + ": walk-in businesses usually need street-level visibility")
    if need.get("ground_floor") and candidates and status != "no":
        floors = {sp.get("floor") for sp in candidates}
        if floors <= set(STREET_LEVEL_FLOORS):
            reasons.append(f"Street level ({floor_label(next(iter(floors)))}, CoStar)")
        elif not floors & (set(STREET_LEVEL_FLOORS) | UNKNOWN_FLOORS):
            fail(f"Listed on the {floor_label(next(iter(floors)))}; this business needs street-level space")
        else:
            candidates = [sp for sp in candidates if sp.get("floor") in set(STREET_LEVEL_FLOORS) | UNKNOWN_FLOORS]
    if not candidates:
        candidates, scope = [{"sf": facts["gba"]}], "whole building"
        unknowns.append("No space is listed for lease in CoStar: availability unknown, sizes use the whole building")
    sizes = sorted({sp["sf"] for sp in candidates if sp.get("sf")})
    lo, hi = need["min_sf"], need.get("max_sf")
    space_sf = None
    if not sizes:
        unknowns.append("Size not recorded")
    else:
        good = [v for v in sizes if v >= lo and (not hi or v <= hi)]
        big = [v for v in sizes if hi and v > hi]
        typical = f"typical {lo:,}-{hi:,} sq ft" if hi else f"{lo:,} sq ft minimum"
        if good:
            space_sf = good[-1]
            where = f"the {space_sf:,.0f} sq ft listed space" if scope == "listed spaces" else f"{space_sf:,.0f} sq ft ({scope})"
            reasons.append(f"{where[0].upper()}{where[1:]} " + (f"is within the {typical}" if hi else f"meets the {typical}"))
        elif big:
            space_sf = big[0]
            unknowns.append(f"Smallest option is {space_sf:,.0f} sq ft ({scope}), above the {typical}: only works "
                            "if part of it can be leased")
        else:
            space_sf = sizes[-1]
            fail(f"Largest option is {space_sf:,.0f} sq ft ({scope}), below the {typical}")
    if need.get("min_clear_ft"):
        if facts["ceiling_ft"] is None:
            unknowns.append(f"Clear height not recorded; needs {need['min_clear_ft']} ft")
        elif facts["ceiling_ft"] < need["min_clear_ft"]:
            fail(f"CoStar ceiling {facts['ceiling_ft']:g} ft is below the {need['min_clear_ft']} ft needed")
        else:
            reasons.append(f"CoStar ceiling {facts['ceiling_ft']:g} ft meets the {need['min_clear_ft']} ft needed")
    if (need.get("ground_floor") and status != "no" and (facts["stories"] or 1) > 1 and scope != "whole building"
            and not any(sp.get("floor") in STREET_LEVEL_FLOORS for sp in candidates)):
        unknowns.append(f"Needs ground-floor space; the building has {facts['stories']} stories, confirm the floor")
    if need.get("drive_in") and not facts["drive_in_bays"]:
        unknowns.append("No grade-level drive-in bays recorded")
    capacity = None
    if need.get("per_unit_sf") and space_sf and status != "no":
        units = int(space_sf // need["per_unit_sf"])
        noun = need["unit"].split(" (")[0]
        plural = {"child": "children"}.get(noun, noun + "s")
        capacity = f"up to {units} {noun if units == 1 else plural} in {space_sf:,.0f} sq ft, by area alone"
        reasons.append(f"Room for {capacity} ({need['per_unit_sf']:,} sq ft each"
                       + (f"; {need['unit'].split('(')[1].rstrip(')')}" if "(" in need["unit"] else "")
                       + "; columns and layout not checked)")
    if need.get("outdoor_sf_per_unit"):
        unknowns.append(f"Needs {need['outdoor_sf_per_unit']} sq ft of fenced outdoor play space per child using it "
                        "at one time; CoStar does not record outdoor space")
    if status == "fits" and unknowns:
        status = "check"
    sourced = [SPACE_SOURCES[k] for k in need.get("sources", [])]
    return {"status": status, "scope": scope, "space_sf": space_sf, "capacity": capacity, "reasons": reasons,
            "unknowns": unknowns, "verify": need["checks"], "sources": sourced,
            "basis": need.get("note") or "Size range is a planning assumption (typical small operator); CoStar "
                                         "building type, listed space sizes and ceiling height are recorded facts."}


def building_agent(property_id, industries, kit=None, suite=None):
    """Can this building host each business? The hard gate before demand (CoStar facts vs registry needs)."""
    facts = building_facts(int(property_id), str(kit or ""))
    kind = PROPERTY_TYPES.get(facts["type"], "unknown type")

    def one(key):
        fit = building_fit(key, facts, suite)
        line = "; ".join(fit["reasons"] + fit["unknowns"])
        return {"building": fit, "summary": f"{fit['status']}: {line}",
                "findings": [record(f"{key}.building_fit", {"status": fit["status"], "space_sf": fit["space_sf"],
                                                           "capacity": fit["capacity"]},
                                    "costar", f"property-{property_id}", None, 0.7 if fit["sources"] else 0.5, None,
                                    "unknown" if fit["status"] == "check" else "agrees",
                                    note=f"{line}. {fit['basis']}")]}
    results = per_industry(industries, one)
    counts = {k: sum(1 for v in results.values() if (v.get("building") or {}).get("status") == k) for k in FIT_ORDER}
    shared = record("building_facts", {k: facts[k] for k in ("type", "subtype", "gba", "stories", "ceiling_ft")}
                    | {"listed_spaces": len(facts["listed"])}, "costar", f"property-{property_id}", None, 0.8, None,
                    "agrees", note=f"CoStar: {kind} building, {facts['gba'] or 0:,.0f} sq ft, "
                                   f"{facts['stories'] or '?'} stories, ceiling "
                                   f"{facts['ceiling_ft'] or 'not recorded'} ft, {len(facts['listed'])} listed spaces.")
    if len(industries) == 1:
        summary = results[industries[0]].get("summary", "")
    else:
        summary = (f"{kind.capitalize()} building: {counts['fits']} businesses fit, {counts['check']} need a check, "
                   f"{counts['no']} can't go here.")
    return {"sources": [{"name": "CoStar hackathon kit: property and listed spaces", "url": None, "retrieved_at": None,
                         "cached": True, "note": f"Property {property_id}"}],
            "findings": [shared] + flatten(results), "per_industry": results, "confidence": 0.6,
            "facts": facts, "summary": summary,
            "handoff": "Hard gate: the coordinator ranks businesses the building can host first; the rest are "
                       "shown as demand gaps this building cannot serve."}


def site_agent(address=None, property_id=None, kit=None):
    """Resolve the request to one point: a CoStar property pin, or a Census-geocoded address."""
    if property_id:
        a = property_pin(int(property_id), str(kit or ""))
        if a.get("latitude") is None:
            raise ValueError("The CoStar record has no map pin.")
        street = " ".join(str(a.get(k) or "") for k in ("streetNumber", "streetDirection", "streetName", "streetType"))
        label = f"{' '.join(street.split())}, {a.get('city')}, {a.get('state')}"
        point = {"lat": a["latitude"], "lon": a["longitude"], "label": label, "property_id": int(property_id)}
        findings = [record("site_point", {"lat": point["lat"], "lon": point["lon"], "address": label}, "costar",
                           f"property-{property_id}", None, 0.9, None, "agrees",
                           note="CoStar property pin (primaryAddress latitude/longitude).")]
        sources = [{"name": "CoStar hackathon kit: commercial property", "url": None, "retrieved_at": None,
                    "cached": True, "note": f"Property {property_id}"}]
    else:
        if not address:
            raise ValueError("Give an address or a CoStar property id.")
        params = {"address": address, "benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"}
        result, meta = fetch(GEOCODER, params)
        matches = result["result"]["addressMatches"]
        if len(matches) != 1:
            return {"status": "unknown", "sources": [source("Census geocoder", meta)],
                    "summary": f"The Census geocoder found {len(matches)} matches; the site is unknown.",
                    "findings": [record("site_point", None, "public:census", "census-geocode", None, 0.0,
                                        meta["url"], note=f"{len(matches)} matches. Use a full street address.")],
                    "handoff": "Stop: without one verified point, every neighborhood number would be a guess."}
        m = matches[0]
        tract = (m["geographies"].get("Census Tracts") or [{}])[0].get("GEOID")
        point = {"lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"], "label": m["matchedAddress"],
                 "tract": tract}
        findings = [record("site_point", {"lat": point["lat"], "lon": point["lon"], "address": point["label"],
                                          "tract": tract}, "public:census", "census-geocode",
                           meta["retrieved_at"][:10], 0.9, meta["url"], "agrees",
                           note="Census address-range geocode (interpolated along the street).")]
        sources = [source("Census geocoder", meta)]
    inside = bool(tract_index(2024).near(point["lat"], point["lon"], RADIUS_MI))
    if not inside:
        findings.append(record("site_in_coverage", False, "computed", "coverage", None, 0.9, None, "unknown",
                               note=f"Outside the {AREA_NAME}: no comparison data."))
    return {"sources": sources, "findings": findings, "point": point, "confidence": 0.9,
            "status": "ok" if inside else "unknown",
            "summary": f"{point['label']} ({point['lat']:.5f}, {point['lon']:.5f})"
                       + ("" if inside else f": outside the {AREA_NAME}"),
            "handoff": "Point to every specialist." if inside else "Stop: outside the area we have data for."}


VERDICTS = {"supported": "The neighborhood supports this use", "mixed": "Mixed signals",
            "weak": "The neighborhood does not look like it supports this use", "unknown": "Not enough evidence"}
RULES = [
    "Lookalike votes +1 at the 70th percentile or above, -1 at the 30th or below, and only if its spatial "
    f"hold-out AUC for that industry is at least {VALIDATED_AUC}.",
    "Population trend votes +1 if growth since 2015-19 beats the Houston area by 2+ points, -1 if it trails by 2+, "
    "and only when the difference is larger than the ACS 90% margin of error (childcare uses the under-5 trend, "
    "tutoring the 5-17 trend). 2015-19 counts are moved onto 2020 tracts with the Census relationship file.",
    "Local supply votes +1 at half the Houston-area rate per resident or less (underserved), -1 at 2x or more "
    "(crowded), only when the difference is unlikely by chance (Poisson p < 0.1 against the count expected at the "
    "area rate), and not for restaurants, fast food, cafes and bars, which cluster on purpose. Zero mapped where 5+ "
    "are expected is treated as an OpenStreetMap gap (unknown).",
    "Business arrivals vote +1 if more of this kind of business moved into nearby CoStar buildings than left over 3 "
    "years, -1 if fewer; needs 2+ moves, and falls back to the business's group (e.g. sports & fitness) when the "
    "exact business has fewer.",
    "A verdict needs at least 2 known signals, one of them business-specific (a validated lookalike or a supply "
    "comparison); population trend and group-level arrivals describe the area, not the business. Supported: total +2 "
    "or more including a business-specific +1, and no -1 from the lookalike. Weak: lookalike -1, or total -2 or "
    "less including a business-specific -1. Otherwise mixed. The lookalike is not required: for need-based services "
    "such as childcare, locations follow population and the lookalike cannot tell areas apart, so supply decides.",
    "These cut-offs are our rules, set relative to the Houston-area distribution; they are not industry standards.",
]


def coordinate(key, signals):
    """Coordinator: combine one industry's signals into a verdict with the deciding reasons."""
    known = [sg for sg in signals if sg["stance"] is not None]
    look = next((sg for sg in signals if sg["name"] == "lookalike"), None)
    total = sum(sg["stance"] for sg in known)
    supply = next((sg for sg in signals if sg["name"] == "local supply"), None)
    # Business-specific evidence: a validated lookalike or a supply comparison. Population trend and
    # group-level arrivals describe the area, not this business, so they cannot decide a verdict alone.
    specific = [sg for sg in (look, supply) if sg and sg["stance"] is not None]
    if len(known) < 2 or not specific:
        verdict = "unknown"
    elif total >= VERDICT_TOTAL and any(sg["stance"] == 1 for sg in specific) and not (look and look["stance"] == -1):
        verdict = "supported"
    elif (look and look["stance"] == -1) or (total <= -VERDICT_TOTAL and any(sg["stance"] == -1 for sg in specific)):
        verdict = "weak"
    else:
        verdict = "mixed"
    basis = (f"{len(known)} signals known; business-specific: "
             + (", ".join(sg["name"] for sg in specific) or "none"))
    order = {"lookalike": 0, "local supply": 1, "population trend": 2, "business arrivals": 3}
    deciding = sorted(known, key=lambda sg: (sg["stance"] == 0, order.get(sg["name"], 9)))
    reasons = [{"signal": sg["name"], "stance": sg["stance"], "reason": sg["reason"]} for sg in deciding]
    unknown = [{"signal": sg["name"], "reason": sg["reason"]} for sg in signals if sg["stance"] is None]
    insight = None
    if look and supply and look["stance"] == 1 and supply["stance"] == 1:
        insight = "Gap: it looks like places that have this business, but has fewer of them than usual."
    elif look and supply and look["stance"] == 1 and supply["stance"] == -1:
        insight = "Proven but crowded: places like this have the business, and this one already has plenty."
    elif look and supply and look["stance"] == -1 and supply["stance"] == 1:
        insight = "Few competitors here, but the residents do not look like those near existing facilities."
    lead = reasons[0]["reason"] if reasons and verdict != "unknown" else ""
    headline = VERDICTS[verdict] + (f": {lead[0].lower()}{lead[1:]}" if lead else "")
    return {"industry": key, "use": INDUSTRIES[key]["label"], "group": INDUSTRIES[key]["group"], "verdict": verdict,
            "headline": headline, "score": total, "basis": basis, "insight": insight, "reasons": reasons, "unknown": unknown,
            "rules": RULES, "lookalike_percentile": look["value"] if look else None,
            "lookalike_validated": bool(look and look.get("validated"))}


def llm_config():
    """OpenAI-compatible chat endpoint (team contract: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY).

    OpenRouter later is just: LLM_BASE_URL=https://openrouter.ai/api/v1, LLM_MODEL=<model>,
    LLM_API_KEY=<key>. Until then it falls back to Upstage Solar Mini with UPSTAGE_API_KEY.
    """
    if os.environ.get("LLM_API_KEY"):
        return {"base_url": os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
                "model": os.environ.get("LLM_MODEL", "openai/gpt-4o-mini"), "key": os.environ["LLM_API_KEY"]}
    return {"base_url": "https://api.upstage.ai/v1", "model": os.environ.get("NEIGHBORHOOD_LLM_MODEL", "solar-mini4"),
            "key": os.environ.get("UPSTAGE_API_KEY")}


def chat(messages, max_tokens=260):
    """One chat completion against the configured OpenAI-compatible endpoint (stdlib only)."""
    cfg = llm_config()
    if not cfg["key"]:
        raise Offline("No LLM key (set LLM_API_KEY, or UPSTAGE_API_KEY)")
    headers = {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
    if "openrouter.ai" in cfg["base_url"]:
        headers["X-Title"] = "Neighborhood demand check"
    body = json.dumps({"model": cfg["model"], "temperature": 0, "max_tokens": max_tokens,
                       "messages": messages}).encode()
    with urlopen(Request(f"{cfg['base_url']}/chat/completions", data=body, headers=headers), timeout=25) as response:
        return json.loads(response.read())["choices"][0]["message"]["content"].strip()


def numbers_in(text):
    return {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", text)}


def narrate(decisions, site_label):
    """Optional plain-English summary by an LLM, from the coordinator output only.

    The text is rejected if it contains any number that is not in its input, so the
    model can explain but cannot invent figures. Returns (text, step).
    """
    cfg = llm_config()
    facts = json.dumps({"site": site_label, "decisions": [{k: d[k] for k in
                                                           ("use", "verdict", "insight", "reasons", "unknown")}
                                                          for d in decisions[:6]]})
    step = {"agent": "narrator", "role": "Writes a short plain-English summary from the coordinator's records",
            "computed_by": f"llm ({cfg['model']})", "inputs": {"records": "coordinator output (top 6 uses)"},
            "sources": [], "findings": [], "signal": None, "per_industry": {}, "confidence": None, "status": "ok"}
    cache = CACHE / "narration" / (hashlib.sha1((cfg["model"] + facts).encode()).hexdigest()[:20] + ".json")
    started = time.perf_counter()
    text = None
    try:
        if cache.exists():
            text = json.loads(cache.read_text())["text"]
        elif offline():
            raise Offline("no cached narration")
        else:
            text = chat([
                {"role": "system", "content": "You explain a site-selection verdict to a small-business owner in "
                 "3 short sentences. Use only the facts given. Do not add any number that is not in the facts. "
                 "Say plainly what is unknown. A stance of +1 supports the use, -1 counts against it, 0 is "
                 "neutral (for local supply, 0 means about the Houston-area rate, not high)."},
                {"role": "user", "content": facts}])
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"text": text, "model": cfg["model"], "created_at": now_iso()}))
        invented = sorted(numbers_in(text) - numbers_in(facts))
        if invented:
            step.update(status="rejected", summary=f"Rejected: the text used numbers not in the records {invented[:5]}.")
            text = None
        else:
            step["summary"] = "Summary written from the records; every number in it was checked against them."
    except (Offline, OSError, HTTPError, URLError, KeyError, ValueError, TimeoutError) as error:
        step.update(status="unknown", summary=f"No LLM summary ({type(error).__name__}: {error}).")
        text = None
    step["ms"] = round(1000 * (time.perf_counter() - started))
    step["handoff"] = "Shown as LLM-written text above the code-computed verdict." if text else \
        "The deterministic verdict stands on its own."
    return text, step


def plan(request):
    """Planner: which industries to check for the request, and how the request was understood (code, no LLM)."""
    if request == "suggest":
        return {"industries": list(INDUSTRIES), "how": f"suggest mode: all {len(INDUSTRIES)} industries",
                "not_modelled": dict(NOT_RESIDENT_DRIVEN), "candidates": []}
    if request in NOT_RESIDENT_DRIVEN:
        return {"industries": [], "how": f"catalog use '{request}' is not resident-driven",
                "not_modelled": {request: NOT_RESIDENT_DRIVEN[request]}, "candidates": []}
    key, how, candidates = resolve(request)
    return {"industries": [key] if key else [], "how": how, "candidates": candidates, "not_modelled": {},
            "unmatched": None if key else request}


def planner_step(request):
    p = plan(request)
    if request == "suggest":
        summary = p["how"]
    elif p["industries"]:
        summary = f"'{request}' -> {INDUSTRIES[p['industries'][0]]['label']} ({p['how']})"
    else:
        summary = f"'{request}': {p['how']}" + (f" ({next(iter(p['not_modelled'].values()))})"
                                               if p["not_modelled"] else "")
    return {"summary": summary, "plan": p, "findings": [],
            "handoff": "Site first, then the building gate (for a CoStar property), then each demand "
                       "specialist covers every planned business."}


SPECIALISTS = [
    ("residents", "Who lives here: ACS residents, age mix, income, tenure and trend vs the Houston area", residents_agent),
    ("competitors", "Who already serves it: existing locations near the site (OpenStreetMap, or official licensing "
                    "data where the registry has it)", competitors_agent),
    ("momentum", "What is moving in: CoStar kit tenant arrivals and departures by industry", momentum_agent),
    ("lookalike", "Does it look like places that already have this business? (held-out evaluated)", lookalike_agent),
]
TIERS = {"supported": 0, "mixed": 1, "weak": 2, "unknown": 3}


def investigate(request, address=None, property_id=None, kit=None, explain=False, suite=None):
    """Run the pipeline for a request: an industry key/label/free text, a catalog use, or 'suggest'.

    Order: planner -> site -> building (hard gate, CoStar properties only) -> demand specialists
    -> coordinator. Demand is computed for every business so the ones this building cannot host
    can still be shown as demand gaps nearby.
    """
    started = time.perf_counter()
    trace = []
    planned = run_agent(trace, "planner", "Maps the request to industries (registry + synonyms, code)",
                        planner_step, {"request": request})
    p = planned["plan"]
    industries = p["industries"]
    site = run_agent(trace, "site", "Resolves the address or CoStar property to one point",
                     site_agent, {"address": address, "property_id": property_id, "kit": kit})
    decisions = []
    building = None
    if site["status"] == "ok" and industries and property_id:
        building = run_agent(trace, "building", "Can this building host it? CoStar type, listed space sizes and "
                                                "ceiling vs each business's needs (hard gate before demand)",
                             building_agent, {"property_id": property_id, "industries": industries, "kit": kit,
                                              "suite": suite})
    if site["status"] == "ok" and industries:
        lat, lon = site["point"]["lat"], site["point"]["lon"]
        steps = []
        for name, role, fn in SPECIALISTS:
            inputs = {"lat": lat, "lon": lon, "industries": industries}
            if name == "momentum":
                inputs["kit"] = kit
            steps.append(run_agent(trace, name, role, fn, inputs))
        t = time.perf_counter()
        for key in industries:
            signals = [st["per_industry"][key]["signal"] for st in steps
                       if ((st.get("per_industry") or {}).get(key) or {}).get("signal")]
            decision = coordinate(key, signals)
            decision["building"] = ((building or {}).get("per_industry") or {}).get(key, {}).get("building")
            decisions.append(decision)
        # The building only separates "can host" from "can't host"; among those it can host, demand decides.
        decisions.sort(key=lambda d: ((d["building"] or {}).get("status") == "no", TIERS[d["verdict"]], -d["score"],
                                      FIT_ORDER.get((d["building"] or {}).get("status"), 1),
                                      -(d["lookalike_percentile"] or 0) if d["lookalike_validated"] else 1))
        counts = {v: sum(d["verdict"] == v for d in decisions) for v in TIERS}
        trace.append({"agent": "coordinator", "role": "Combines each industry's signals into a verdict",
                      "computed_by": "code", "inputs": {"industries": len(industries)}, "sources": [],
                      "findings": [], "signal": None, "per_industry": {}, "status": "ok", "confidence": None,
                      "ms": round(1000 * (time.perf_counter() - t)),
                      "summary": (f"{decisions[0]['use']}: {decisions[0]['verdict']} (signal total "
                                  f"{decisions[0]['score']:+d})" if len(decisions) == 1 else
                                  ", ".join(f"{n} {v}" for v, n in counts.items() if n)),
                      "handoff": "Verdicts and deciding reasons to the person."})
    narration = None
    if explain and decisions:
        narration, step = narrate(decisions, site["point"]["label"])
        trace.append(step)
    for step in trace:  # keep trace inputs small and JSON-safe
        step["inputs"] = {k: (f"{len(v)} industries" if k == "industries" and isinstance(v, list) and len(v) > 3
                              else v)
                          for k, v in step["inputs"].items() if k != "kit"}
    return {
        "mode": "suggest" if request == "suggest" else "validate", "request": request,
        "site": site.get("point"), "suite": suite, "building_checked": building is not None, "area": AREA_NAME, "radius_mi": RADIUS_MI,
        "decisions": decisions, "not_modelled": p.get("not_modelled", {}),
        "unmatched": p.get("unmatched"), "candidates": p.get("candidates", []),
        "narration": narration,
        "narration_label": f"Written by an LLM ({llm_config()['model']}) from the records below; numbers checked."
        if narration else None,
        "trace": trace, "evidence": [r for st in trace for r in st.get("findings", [])],
        "timings_ms": {st["agent"]: st.get("ms", 0) for st in trace},
        "offline": offline(), "elapsed_ms": round(1000 * (time.perf_counter() - started)),
        "computed_by": "All numbers and verdicts are computed in code. Only the optional summary is LLM-written.",
    }


def industry_catalog():
    """Industries with their group and measured lookalike accuracy (for pickers and the API)."""
    out = []
    for key, spec in INDUSTRIES.items():
        path = evaluation_path(key)
        ev = json.loads(path.read_text()) if path.exists() else {}
        spatial = ev.get("splits", {}).get("spatial", {})
        out.append({"key": key, "label": spec["label"], "group": spec["group"],
                    "facilities_mapped": ev.get("facilities_mapped"), "lookalike_auc": spatial.get("auc"),
                    "validated": ev.get("validated", False), "status": ev.get("status", "not prepared")})
    return out


def warm(kit=None):
    """Load cached tables, kit tenancies and models in the background so the first request is quick."""
    try:
        reference_profiles()
        for key in INDUSTRIES:
            if evaluation_path(key).exists():
                lookalike_model(key)
                facilities(key)
        kit_tenancies(kit)
    except (Offline, RuntimeError, OSError, ValueError, KeyError):
        pass  # the request path reports what is missing


def bench(property_ids=(13890701, 23296), kit=None):
    """Time requests after a restart: the first one runs with empty memory caches (disk caches present)."""
    for fn in (area_profile, facilities, lookalike_model, kit_tenancies, tract_table, tract_index, reference_profiles,
               reference_values, acs, tracts, property_pin, old_on_new, building_facts):
        fn.cache_clear()
    rows = []
    for round_ in ("first", "repeat"):
        for pid in property_ids:
            for request in ("indoor pickleball", "suggest"):
                t = time.perf_counter()
                report = investigate(request, property_id=pid, kit=kit)
                rows.append({"round": round_, "property_id": pid, "request": request,
                             "ms": round(1000 * (time.perf_counter() - t)), "steps_ms": report["timings_ms"],
                             "industries": len(report["decisions"])})
    return rows


def print_report():
    print(f"{'industry':20} {'mapped':>6} {'AUC':>6} {'90% CI':>14} {'top30':>6} {'income':>7} {'density':>7} "
          f"{'shuffled':>8}  status")
    for item in industry_catalog():
        path = evaluation_path(item["key"])
        if not path.exists():
            print(f"{item['key']:20} not prepared")
            continue
        ev = json.loads(path.read_text())
        s = ev.get("splits", {}).get("spatial")
        if not s:
            print(f"{item['key']:20} {ev['facilities_mapped']:>6}  {ev.get('note')}")
            continue
        b = s["baseline_auc"]
        print(f"{item['key']:20} {ev['facilities_mapped']:>6} {s['auc']:>6} {str(s['auc_90ci']):>14} "
              f"{s['hit_rate_top30']:>6} {b['income_only']:>7} {b['density_only']:>7} {b['shuffled_labels']:>8}  "
              f"{'votes' if ev['validated'] else 'shown only'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address")
    parser.add_argument("--property-id", type=int)
    parser.add_argument("--suite", help="suite value from the /maximo picker (e.g. A, or listing:<id>:<sf>)")
    parser.add_argument("--use", default="indoor pickleball",
                        help="an industry key, label or description (e.g. 'bouldering gym'), or 'suggest'")
    parser.add_argument("--kit", type=Path)
    parser.add_argument("--offline", action="store_true", help="use only cached responses")
    parser.add_argument("--explain", action="store_true", help="add the optional LLM summary")
    parser.add_argument("--prepare", metavar="KEYS", help="'all' or comma-separated industry keys: fetch + evaluate")
    parser.add_argument("--report", action="store_true", help="print the evaluation table for prepared industries")
    parser.add_argument("--bench", action="store_true", help="time cold and warm requests")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.offline:
        os.environ["NEIGHBORHOOD_OFFLINE"] = "1"
    if args.prepare:
        prepare(list(INDUSTRIES) if args.prepare == "all" else args.prepare.split(","), args.kit)
        return
    if args.report:
        print_report()
        return
    if args.bench:
        for row in bench(kit=args.kit):
            print(json.dumps(row))
        return
    report = investigate(args.use, args.address, args.property_id, args.kit, args.explain, args.suite)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print(f"Site: {(report['site'] or {}).get('label')}   ({report['elapsed_ms']} ms)")
    for step in report["trace"]:
        print(f"  [{step['agent']}] {step['status']} ({step.get('ms', 0)} ms, {step['computed_by']}): "
              f"{step.get('summary', '')}")
    if report["unmatched"]:
        print(f"\nCould not match '{report['unmatched']}' to an industry. Try one of: "
              + ", ".join(spec["label"] for spec in INDUSTRIES.values()))
    for d in report["decisions"]:
        print(f"\n{d['use']}: {d['verdict'].upper()} - {d['headline']}")
        if d.get("building"):
            b = d["building"]
            print(f"  building: {b['status'].upper()} - " + "; ".join(b["reasons"] + b["unknowns"]))
        if d["insight"]:
            print(f"  {d['insight']}")
        for r in d["reasons"]:
            print(f"  {r['stance']:+d} {r['signal']}: {r['reason']}")
        for r in d["unknown"]:
            print(f"   ? {r['signal']}: {r['reason']}")
    for use, why in report["not_modelled"].items():
        print(f"\n{use}: not modelled ({why})")
    if report["narration"]:
        print(f"\n{report['narration_label']}\n{report['narration']}")


if __name__ == "__main__":
    main()
