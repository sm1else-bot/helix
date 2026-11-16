"""CoStar Hackathon Kit Service for Project Helix.

Provides fast, indexed, grounded access to all CoStar datasets:
- Commercial properties (RBA, GFA, asset class, building class, zoning, coords)
- Active commercial listings & advertised spaces (suites, asking rents, $/SF/yr, lease basis)
- Market-wide space screening (filter by size, max rent, property type, lease basis)
- Tenancies and occupants (company names, suites, move-in dates)
- Transaction history (commercial sale comps, prices, dates)
- Multifamily & Apartment communities (Apartments.com listings, unit counts, rent ranges)
- Residential MLS listings (Homes.com single-family listings, asking prices)
- Matterport 3D twin cross-references
"""

import csv
import io
import json
import os
from pathlib import Path
import re
from zipfile import ZipFile

PROPERTY_TYPES = {
    0: "Unknown", 1: "Hospitality", 2: "Industrial", 3: "Land", 5: "Office",
    6: "GeneralRetail", 7: "Flex", 8: "SportsAndEntertainment", 9: "MixedUse",
    10: "Specialty", 11: "Multifamily", 13: "Healthcare", 14: "Unknown",
    15: "Residential", 16: "Homes"
}

BUILDING_CLASSES = {0: "Unknown", 1: "Class A", 2: "Class B", 3: "Class C", 4: "Class F"}

SERVICE_TYPES = {
    7: "Full Service Gross",
    8: "Industrial Gross",
    9: "Modified Gross",
    10: "Net",
    11: "Negotiable",
    12: "Double Net",
    13: "Triple Net (NNN)",
    14: "To Be Determined"
}

RATE_BASIS = {
    1: "$/SF/month",
    2: "$/SF/year",
    3: "$/month",
    4: "$/year"
}

ABBREVS = {
    'pkwy': 'pky', 'parkway': 'pky',
    'road': 'rd',
    'street': 'st',
    'boulevard': 'blvd',
    'drive': 'dr',
    'highway': 'hwy',
    'freeway': 'fwy',
    'avenue': 'ave',
    'suite': 'ste'
}

def normalize_tokens(text: str):
    words = re.findall(r'[a-z0-9]+', (text or "").lower())
    return [ABBREVS.get(w, w) for w in words]


class CoStarService:
    def __init__(self, kit_path: str = None, password: str = None):
        if not kit_path:
            kit_path = os.environ.get("COSTAR_KIT_PATH") or str(Path.home() / "Downloads" / "costar-hackathon-kit.zip")
        if not password:
            password = os.environ.get("COSTAR_KIT_ZIP_PASSWORD") or os.environ.get("COSTAR_KIT_PASSWORD")
        if not password:
            raise RuntimeError("Set COSTAR_KIT_PASSWORD (or COSTAR_KIT_ZIP_PASSWORD) in .env to open the CoStar kit.")
        
        self.kit_path = Path(kit_path)
        self.password = password.encode("utf-8") if isinstance(password, str) else password
        self._properties_csv = None
        self._property_index = {}
        self._active_spaces_cache = None
        self._apartments_cache = None
        self._residential_csv = None

    def _ensure_csv_loaded(self):
        if self._properties_csv is not None:
            return
        if not self.kit_path.exists():
            raise FileNotFoundError(f"CoStar kit not found at {self.kit_path}")

        with ZipFile(self.kit_path) as z:
            with z.open("commercial-properties.csv", pwd=self.password) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                self._properties_csv = list(reader)
        
        for r in self._properties_csv:
            pid = int(r["PropertyId"])
            if pid not in self._property_index:
                self._property_index[pid] = {
                    "property_id": pid,
                    "address": r.get("Address", ""),
                    "city": r.get("City", ""),
                    "state": r.get("State", ""),
                    "matterport_model_ids": []
                }
            if r.get("MatterportModelId"):
                model_id = r["MatterportModelId"]
                if model_id not in self._property_index[pid]["matterport_model_ids"]:
                    self._property_index[pid]["matterport_model_ids"].append(model_id)

    def search_properties(self, query: str = "", city: str = "", limit: int = 6):
        """Search commercial properties by address query, street name, or PropertyId."""
        self._ensure_csv_loaded()
        q_tokens = normalize_tokens(query)
        city_lower = city.strip().lower()

        results = []
        scored = []
        for pid, prop in self._property_index.items():
            if city_lower and city_lower not in prop["city"].lower():
                continue
            addr_tokens = normalize_tokens(prop["address"])
            if any(str(pid) == tok for tok in q_tokens):
                scored.append((100, prop))
                continue
            matches = sum(1 for tok in q_tokens if tok in addr_tokens)
            if matches > 0 and (matches == len(q_tokens) or (len(q_tokens) > 2 and matches >= len(q_tokens) - 1)):
                scored.append((matches, prop))

        scored.sort(key=lambda x: x[0], reverse=True)
        for _, prop in scored[:limit]:
            results.append({
                "property_id": prop["property_id"],
                "address": prop["address"],
                "city": prop["city"],
                "state": prop["state"],
                "matterport_tours_count": len(prop["matterport_model_ids"]),
                "matterport_model_ids": prop["matterport_model_ids"][:2]
            })
        return {"query": query, "count": len(results), "properties": results}

    def get_property(self, property_id: int):
        """Fetch detailed facts for a single property by PropertyId."""
        self._ensure_csv_loaded()
        pid_int = int(property_id)
        
        found = None
        needle_1 = f'"id": {pid_int},'.encode("utf-8")
        needle_2 = f'"id":{pid_int},'.encode("utf-8")

        with ZipFile(self.kit_path) as z:
            with z.open("data/commercial-property.jsonl", pwd=self.password) as f:
                for line in f:
                    if needle_1 in line or needle_2 in line:
                        item = json.loads(line)
                        if item.get("id") == pid_int:
                            found = item
                            break
        
        if not found:
            return {"error": f"Property {property_id} not found in commercial properties."}

        p_type_code = found.get("propertyType", 0)
        p_class_code = found.get("buildingClass", 0)
        prim_addr = found.get("primaryAddress") or {}
        
        tours = list(self._property_index.get(pid_int, {}).get("matterport_model_ids", []))
        for att in found.get("attachments") or []:
            if att.get("attachmentUri") and "matterport.com" in att["attachmentUri"]:
                tours.append(att["attachmentUri"])

        return {
            "property_id": pid_int,
            "address": f"{prim_addr.get('streetNumber', '')} {prim_addr.get('streetName', '')} {prim_addr.get('streetType', '')}".strip() or self._property_index.get(pid_int, {}).get("address", ""),
            "city": prim_addr.get("city") or self._property_index.get(pid_int, {}).get("city", "Houston"),
            "state": prim_addr.get("state") or "TX",
            "postal_code": prim_addr.get("postcode", ""),
            "latitude": prim_addr.get("latitude"),
            "longitude": prim_addr.get("longitude"),
            "property_type": PROPERTY_TYPES.get(p_type_code, f"Code {p_type_code}"),
            "building_class": BUILDING_CLASSES.get(p_class_code, "Unknown"),
            "gross_building_area_sqft": found.get("grossBuildingAreaSquareFeet"),
            "rentable_building_area_sqft": found.get("rentableBuildingAreaSquareFeet"),
            "stories": found.get("numStoriesAboveGrade"),
            "has_sprinklers": found.get("hasSprinklers"),
            "zoning_description": found.get("zoningDescription") or "Unzoned / Harris County jurisdiction",
            "parking_ratio": found.get("parkingRatio"),
            "matterport_model_ids": tours[:4],
            "amenities": [a.get("name") for a in (found.get("amenities") or []) if isinstance(a, dict) and a.get("name")]
        }

    def get_stage2_raw_record(self, property_id: int):
        """Read one original CoStar property row for Stage 2 evidence reconciliation.

        This additive method does not change Stage 1 responses. Callers must select
        only provenance fields they need; do not serialize the full row to clients.
        """
        pid = int(property_id)
        with ZipFile(self.kit_path) as archive:
            with archive.open("data/commercial-property.jsonl", pwd=self.password) as stream:
                for line in stream:
                    if f'"id": {pid},'.encode() not in line and f'"id":{pid},'.encode() not in line:
                        continue
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if item.get("id") == pid:
                        return item
        return None

    def get_listings_and_spaces(self, property_id: int):
        """Fetch active listings, spaces/suites, and asking rents for a property."""
        pid_int = int(property_id)
        needle = str(pid_int).encode("utf-8")

        spaces_found = []
        sale_offers = []

        with ZipFile(self.kit_path) as z:
            with z.open("data/commercial-listing-summary.jsonl", pwd=self.password) as f:
                for line in f:
                    if needle in line:
                        listing = json.loads(line)
                        if listing.get("listingStatus") != 1:  # 1 = Active
                            continue
                        
                        forlease = listing.get("forLeaseSummary") or {}
                        for s in forlease.get("spaces") or []:
                            if s.get("propertyId") == pid_int:
                                s_type = s.get("serviceType", forlease.get("serviceType", 0))
                                r_basis = s.get("rateBasis", forlease.get("askingRateBasis", 2))
                                rate_low = s.get("rateLow") or forlease.get("askingRateLow")
                                rate_high = s.get("rateHigh") or forlease.get("askingRateHigh")

                                spaces_found.append({
                                    "listing_id": listing.get("id"),
                                    "space_id": s.get("id"),
                                    "suite": s.get("suite", "Entire Building / Unspecified"),
                                    "square_feet_min": s.get("squareFeetMin"),
                                    "square_feet_max": s.get("squareFeetMax"),
                                    "asking_rate_low": rate_low,
                                    "asking_rate_high": rate_high,
                                    "rate_basis": RATE_BASIS.get(r_basis, "Unknown basis"),
                                    "service_type": SERVICE_TYPES.get(s_type, f"Code {s_type}"),
                                    "is_full_floor": s.get("isFullFloor", False),
                                    "available_date": s.get("availableOnDate"),
                                    "date_on_market": s.get("dateOnMarket")
                                })

                        forsale = listing.get("forSaleSummary")
                        if forsale and listing.get("propertyId") == pid_int:
                            sale_offers.append({
                                "listing_id": listing.get("id"),
                                "asking_price": forsale.get("askingPrice"),
                                "cap_rate": forsale.get("capRate"),
                                "days_on_market": forsale.get("daysOnMarket")
                            })

        return {
            "property_id": pid_int,
            "active_spaces_count": len(spaces_found),
            "spaces": spaces_found,
            "sale_offers": sale_offers
        }

    def discover_active_listings(self, city: str, transaction_types: list[str], limit: int = 1000):
        """Read the actual active commercial listing feed once for bounded city discovery.

        `transaction_types` is supplied by the deterministic policy engine. Sale records
        are never traversed for lease-only requests.
        """
        if city not in ("Houston", "Los Angeles"):
            raise ValueError("Unsupported CoStar market")
        if not set(transaction_types).issubset({"sale", "lease"}):
            raise ValueError("Unsupported transaction type")
        if not 1 <= int(limit) <= 2000:
            raise ValueError("Discovery limit must be between 1 and 2000")
        self._ensure_csv_loaded()
        properties = {pid: row for pid, row in self._property_index.items()
                      if row.get("city", "").casefold() == city.casefold()}
        candidates = {"sale": [], "lease": []}
        scan_truncated = False
        scanned_lines = 0
        sale_records_scanned = 0
        skipped_malformed = 0
        with ZipFile(self.kit_path) as z:
            with z.open("data/commercial-listing-summary.jsonl", pwd=self.password) as stream:
                for line in stream:
                    scanned_lines += 1
                    if scanned_lines > 100_000:
                        scan_truncated = True
                        break
                    try:
                        listing = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        skipped_malformed += 1
                        continue
                    if listing.get("listingStatus") != 1:
                        continue
                    pid = listing.get("propertyId")
                    prop = properties.get(pid)
                    if not prop:
                        continue
                    # The selected feed is commercial, but exclude explicitly
                    # residential/Homes property-type rows if they are present.
                    if listing.get("propertyType") in (15, 16):
                        continue
                    if "sale" in transaction_types:
                        sale = listing.get("forSaleSummary")
                        if sale:
                            sale_records_scanned += 1
                            candidates["sale"].append({
                                "transaction_type": "sale", "property_id": pid,
                                "listing_id": listing.get("id"), "space_id": None,
                                "stable_id": f"sale:{listing.get('id')}", "property": prop,
                                "address": listing.get("address"), "city": prop.get("city"), "state": prop.get("state"),
                                "property_type_code": listing.get("propertyType"),
                                "listing_status": listing.get("listingStatus"),
                                "availability_status": sale.get("saleStatus"),
                                "area_min_sqft": sale.get("totalAvailableSquareFeet") or listing.get("buildingSizeSquareFeet"),
                                "area_max_sqft": sale.get("totalAvailableSquareFeet") or listing.get("buildingSizeSquareFeet"),
                                "asking_price_low": sale.get("askingPriceLow"),
                                "asking_price_high": sale.get("askingPriceHigh"),
                                "asking_price_raw": {"low": sale.get("askingPriceLow"), "high": sale.get("askingPriceHigh")},
                                "monetary_unit": listing.get("monetaryUnit"), "rate_low": None, "rate_high": None,
                                "rate_basis_code": None, "asking_rate_has_value": None,
                                "date_on_market": sale.get("minDateOnMarket"), "available_at": None,
                                "certified_fresh_date": listing.get("certifiedFreshDate"),
                                "created_date": listing.get("createdDate"),
                                "listing_status_updated_at": listing.get("listingStatusUpdatedDate"),
                                "source_reference": f"data/commercial-listing-summary.jsonl#{listing.get('id')}",
                                "original_evidence": {"listing_id": listing.get("id"), "sale_summary": sale,
                                                      "property_type_code": listing.get("propertyType"),
                                                      "listing_status": listing.get("listingStatus")},
                            })
                    if "lease" in transaction_types:
                        lease = listing.get("forLeaseSummary") or {}
                        for space in lease.get("spaces") or []:
                            space_pid = space.get("propertyId") or pid
                            space_prop = properties.get(space_pid)
                            if not space_prop:
                                continue
                            sid = space.get("id")
                            candidates["lease"].append({
                                "transaction_type": "lease", "property_id": space_pid,
                                "listing_id": listing.get("id"), "space_id": sid,
                                "stable_id": f"lease:{listing.get('id')}:{sid if sid is not None else space.get('suite', 'unknown')}",
                                "property": space_prop, "address": listing.get("address"),
                                "city": space_prop.get("city"), "state": space_prop.get("state"),
                                "property_type_code": listing.get("propertyType"),
                                "listing_status": listing.get("listingStatus"),
                                "availability_status": space.get("availabilityType"),
                                "area_min_sqft": space.get("squareFeetMin"),
                                "area_max_sqft": space.get("squareFeetMax"),
                                "asking_price_low": None, "asking_price_high": None,
                                "asking_price_raw": None,
                                "monetary_unit": space.get("monetaryUnit", lease.get("monetaryUnit", listing.get("monetaryUnit"))),
                                "rate_low": space.get("rateLow") if space.get("rateLow") is not None else lease.get("askingRateLow"),
                                "rate_high": space.get("rateHigh") if space.get("rateHigh") is not None else lease.get("askingRateHigh"),
                                "rate_basis_code": space.get("rateBasis") if space.get("rateBasis") is not None else lease.get("askingRateBasis"),
                                "asking_rate_has_value": space.get("askingRateHasValue"),
                                "date_on_market": space.get("dateOnMarket") or lease.get("minDateOnMarket"),
                                "available_at": space.get("availableOnDate"),
                                "certified_fresh_date": listing.get("certifiedFreshDate"),
                                "created_date": listing.get("createdDate"),
                                "listing_status_updated_at": listing.get("listingStatusUpdatedDate"),
                                "known_additional_charges_raw": {k: space.get(k) for k in (
                                    "commonAreaMaintenanceState", "propertyTaxState", "leaseTermType", "serviceType")},
                                "service_type": space.get("serviceType", lease.get("serviceType")),
                                "suite": space.get("suite"),
                                "source_reference": f"data/commercial-listing-summary.jsonl#{listing.get('id')}:{sid}",
                                "original_evidence": {"listing_id": listing.get("id"), "space": space,
                                                      "lease_summary": {k: lease.get(k) for k in (
                                                          "askingRateBasis", "askingRateLow", "askingRateHigh", "serviceType")},
                                                      "listing_status": listing.get("listingStatus")},
                            })
        requested = [kind for kind in ("sale", "lease") if kind in transaction_types]
        if requested == ["sale", "lease"]:
            sale_cap, lease_cap = int(limit) // 2, int(limit) - int(limit) // 2
            if len(candidates["sale"]) < sale_cap:
                lease_cap += sale_cap - len(candidates["sale"])
            if len(candidates["lease"]) < lease_cap:
                sale_cap += lease_cap - len(candidates["lease"])
        else:
            sale_cap = int(limit) if requested == ["sale"] else 0
            lease_cap = int(limit) if requested == ["lease"] else 0

        def diverse_take(rows, cap):
            groups = {}
            for row in rows:
                groups.setdefault(row.get("property_type_code", 0), []).append(row)
            chosen = []
            while len(chosen) < cap and any(groups.values()):
                for code in sorted(groups):
                    if groups[code] and len(chosen) < cap:
                        chosen.append(groups[code].pop(0))
            return chosen

        results = diverse_take(candidates["sale"], sale_cap) + diverse_take(candidates["lease"], lease_cap)
        all_requested = sum(len(candidates[kind]) for kind in requested)
        truncated = scan_truncated or len(results) < all_requested
        return {"listings": results[:limit], "truncated": truncated, "records_discovered": all_requested,
                "records_scanned": scanned_lines, "scan_truncated": scan_truncated,
                "skipped_malformed_records": skipped_malformed,
                "sale_listing_records_seen": sale_records_scanned if "sale" in transaction_types else 0,
                "transaction_types_searched": list(transaction_types), "city": city}

    def _ensure_active_spaces_loaded(self):
        """Index all active commercial spaces across Houston in memory for fast screening."""
        if self._active_spaces_cache is not None:
            return
        self._ensure_csv_loaded()
        self._active_spaces_cache = []

        with ZipFile(self.kit_path) as z:
            with z.open("data/commercial-listing-summary.jsonl", pwd=self.password) as f:
                for line in f:
                    listing = json.loads(line)
                    if listing.get("listingStatus") != 1:
                        continue
                    forlease = listing.get("forLeaseSummary") or {}
                    for s in forlease.get("spaces") or []:
                        pid = s.get("propertyId") or listing.get("propertyId")
                        if not pid: continue
                        rate_low = s.get("rateLow") or forlease.get("askingRateLow")
                        rate_high = s.get("rateHigh") or forlease.get("askingRateHigh")
                        rate = rate_low if rate_low is not None else rate_high
                        sf = s.get("squareFeetMax") or s.get("squareFeetMin")
                        s_type = s.get("serviceType", forlease.get("serviceType", 0))

                        prop_info = self._property_index.get(pid, {})
                        self._active_spaces_cache.append({
                            "property_id": pid,
                            "address": prop_info.get("address", f"Property #{pid}"),
                            "city": prop_info.get("city", "Houston"),
                            "suite": s.get("suite", "Main Space"),
                            "square_feet": sf,
                            "asking_rate": rate,
                            "service_type": SERVICE_TYPES.get(s_type, f"Code {s_type}"),
                            "is_full_floor": s.get("isFullFloor", False),
                            "matterport_available": len(prop_info.get("matterport_model_ids", [])) > 0
                        })

    def filter_spaces(self, min_square_feet: float = None, max_square_feet: float = None,
                      min_asking_rate: float = None, max_asking_rate: float = None,
                      city: str = None, sort_by: str = "lowest_rent", limit: int = 8):
        """Screen active available commercial spaces across the market by budget, size, and city.
        
        sort_by options: 'lowest_rent', 'highest_rent', 'largest_sf', 'smallest_sf'.
        city options: 'Houston', 'Los Angeles', or None for all cities.
        """
        self._ensure_active_spaces_loaded()
        city_lower = (city or "").lower().strip()

        matched = []
        for s in self._active_spaces_cache:
            if city_lower and city_lower not in s["city"].lower():
                continue
            sf = s.get("square_feet")
            rate = s.get("asking_rate")

            if min_square_feet is not None and (sf is None or sf < min_square_feet):
                continue
            if max_square_feet is not None and (sf is not None and sf > max_square_feet):
                continue
            if min_asking_rate is not None and (rate is None or rate < min_asking_rate):
                continue
            if max_asking_rate is not None and (rate is None or rate > max_asking_rate):
                continue

            matched.append(s)

        if sort_by == "highest_rent":
            matched.sort(key=lambda x: (x["asking_rate"] if x["asking_rate"] is not None else -1, x["square_feet"] or 0), reverse=True)
        elif sort_by == "largest_sf":
            matched.sort(key=lambda x: (x["square_feet"] or 0), reverse=True)
        elif sort_by == "smallest_sf":
            matched.sort(key=lambda x: (x["square_feet"] if x["square_feet"] is not None else 999999))
        else: # lowest_rent
            matched.sort(key=lambda x: (x["asking_rate"] if x["asking_rate"] is not None else 9999, -(x["square_feet"] or 0)))

        rates_in_match = [s["asking_rate"] for s in matched if s["asking_rate"] is not None]
        return {
            "criteria": {
                "min_square_feet": min_square_feet,
                "max_square_feet": max_square_feet,
                "min_asking_rate": min_asking_rate,
                "max_asking_rate": max_asking_rate,
                "city": city or "All (Houston & Los Angeles)",
                "sort_by": sort_by
            },
            "total_matches": len(matched),
            "highest_asking_rate_found": max(rates_in_match) if rates_in_match else None,
            "lowest_asking_rate_found": min(rates_in_match) if rates_in_match else None,
            "spaces": matched[:limit]
        }

    def get_tenants(self, property_id: int, limit: int = 10):
        """Fetch recorded tenants and occupants for a property."""
        pid_int = int(property_id)
        needle = f'"propertyId": {pid_int},'.encode("utf-8")
        needle_alt = f'"propertyId":{pid_int},'.encode("utf-8")

        location_ids = []
        occupancies = []

        with ZipFile(self.kit_path) as z:
            with z.open("data/location-occupancy.jsonl", pwd=self.password) as f:
                for line in f:
                    if needle in line or needle_alt in line:
                        occ = json.loads(line)
                        if occ.get("propertyId") == pid_int:
                            loc_id = occ.get("tenantLocationId")
                            if loc_id:
                                location_ids.append(loc_id)
                                occupancies.append({
                                    "tenant_location_id": loc_id,
                                    "square_feet_occupied": occ.get("squareFeetOccupied"),
                                    "move_in_date": occ.get("moveInDate")
                                })
                            if len(location_ids) >= limit:
                                break

        names = {}
        if location_ids:
            with ZipFile(self.kit_path) as z:
                with z.open("data/company-detail.jsonl", pwd=self.password) as f:
                    for line in f:
                        for loc_id in location_ids:
                            tag_1 = f'"id": {loc_id},'.encode("utf-8")
                            tag_2 = f'"id":{loc_id},'.encode("utf-8")
                            if tag_1 in line or tag_2 in line:
                                comp = json.loads(line)
                                if comp.get("id") == loc_id:
                                    names[loc_id] = comp.get("name") or "Confidential Tenant"
                                    break
                        if len(names) == len(location_ids):
                            break

        tenants = []
        for occ in occupancies:
            loc_id = occ["tenant_location_id"]
            tenants.append({
                "company_name": names.get(loc_id, f"Occupant Location #{loc_id}"),
                "square_feet": occ.get("square_feet_occupied"),
                "move_in_date": occ.get("move_in_date")
            })

        return {
            "property_id": pid_int,
            "tenant_count": len(tenants),
            "tenants": tenants
        }

    def get_sale_comps(self, property_id: int, limit: int = 5):
        """Fetch historical recorded commercial transactions."""
        pid_int = int(property_id)
        needle = str(pid_int).encode("utf-8")

        comps = []
        with ZipFile(self.kit_path) as z:
            with z.open("data/commercial-sale-comp.jsonl", pwd=self.password) as f:
                for line in f:
                    if needle in line:
                        comp = json.loads(line)
                        for entity in comp.get("entities") or []:
                            if entity.get("propertyId") == pid_int:
                                comps.append({
                                    "comp_id": comp.get("id"),
                                    "sale_date": comp.get("saleDate"),
                                    "sale_price": comp.get("salePrice"),
                                    "cap_rate": comp.get("capRate"),
                                    "buyer": (comp.get("buyer") or {}).get("name"),
                                    "seller": (comp.get("seller") or {}).get("name")
                                })
                                break
                        if len(comps) >= limit:
                            break

        return {
            "property_id": pid_int,
            "comps_count": len(comps),
            "sale_comps": comps
        }

    def search_apartments(self, query: str = "", limit: int = 5):
        """Search multifamily/apartment communities (Apartments.com dataset)."""
        q_tokens = normalize_tokens(query)
        matches = []

        with ZipFile(self.kit_path) as z:
            with z.open("data/apartment-listing.jsonl", pwd=self.password) as f:
                for line in f:
                    apt = json.loads(line)
                    name = apt.get("buildingName") or ""
                    addr = apt.get("address") or {}
                    street = f"{addr.get('streetNumber', '')} {addr.get('streetName', '')} {addr.get('streetType', '')}"
                    text = f"{name} {street} {addr.get('city', '')}".lower()

                    if not q_tokens or any(tok in text for tok in q_tokens):
                        matches.append({
                            "apartment_id": apt.get("id"),
                            "property_id": apt.get("propertyId"),
                            "building_name": name or "Apartment Community",
                            "address": street.strip(),
                            "city": addr.get("city", "Houston"),
                            "total_units": apt.get("totalNumUnits"),
                            "min_rent": apt.get("minRent"),
                            "max_rent": apt.get("maxRent"),
                            "year_built": apt.get("yearBuilt"),
                            "management_company": apt.get("propertyManagementCompanyName")
                        })
                        if len(matches) >= limit:
                            break

        return {"query": query, "count": len(matches), "apartments": matches}

    def search_residential(self, query: str = "", max_price: float = None, limit: int = 5):
        """Search single-family home MLS listings (Homes.com dataset)."""
        if self._residential_csv is None:
            with ZipFile(self.kit_path) as z:
                with z.open("residential-listings.csv", pwd=self.password) as f:
                    self._residential_csv = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

        q_tokens = normalize_tokens(query)
        matches = []
        for r in self._residential_csv:
            addr = r.get("Address", "")
            if q_tokens and not any(tok in addr.lower() for tok in q_tokens):
                continue
            matches.append({
                "listing_id": r.get("ListingId"),
                "residential_id": r.get("ResidentialListingId"),
                "address": addr,
                "city": r.get("City"),
                "matterport_model_id": r.get("MatterportModelId")
            })
            if len(matches) >= limit:
                break

        return {"query": query, "count": len(matches), "homes": matches}
