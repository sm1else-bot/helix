"""Deal facts for lease-or-buy: what this property actually asks, and what it last sold for.

Every number is an evidence record (see reconcile.record) taken from the CoStar kit:
  lease  forLeaseSummary.spaces[] on THIS building: asking rate, its period (rateBasis) and
         lease basis (serviceType), normalized to $/month and $/SF/year
  sale   forSaleSummary of an active listing on this property: asking price, sale type
         (owner/user vs investment), cap rate, time on market
  sold   commercial sale comps that include this property. Texas does not require sale prices
         to be disclosed, so many have none; bulk-portfolio prices are not this building's price.

  market rent  output/rent-estimates.json from rent_model.py (trained on the asking rents recorded on
         the kit's signed lease deals): what similar spaces that leased were asking, next to this space's
         asking rent. Skipped when the
         file has not been generated.

The capital check states what each path needs up front against the buyer's capital. It never
recommends a path: the cash-flow comparison (Nessie simulator) does that with these numbers.
"""
import json
from datetime import date
from pathlib import Path

from reconcile import record
from space_fit import kit_lines

RENT_ESTIMATES = Path(__file__).parent / "output" / "rent-estimates.json"

SERVICE = {  # who pays operating expenses on top of the asking rent
    13: ("triple net (NNN)", "plus taxes, insurance and common-area costs"),
    12: ("double net", "plus taxes and insurance"),
    10: ("net", "plus some operating costs"),
    8: ("industrial gross", "most operating costs included; utilities usually extra"),
    9: ("modified gross", "some operating costs included"),
    7: ("full service", "operating costs included"),
    14: ("to be determined", "lease basis not set"),
    11: ("negotiable", "lease basis negotiable"),
}
SALE_TYPE = {1: "investment", 2: "owner/user", 3: "investment or owner/user"}
PRICE_STATUS = {3: "confirmed by a party to the sale", 5: "computed from transfer tax, unconfirmed",
                7: "unconfirmed", 2: "approximate", 8: "allocated from a larger sale"}
PORTFOLIO_CONDITIONS = {12: "bulk portfolio sale", 43: "sale of the owning entity"}
OTHER_CONDITIONS = {1: "distress sale", 2: "1031 exchange", 3: "sale-leaseback", 9: "auction sale",
                    15: "deed restriction", 20: "deferred maintenance", 25: "high-vacancy property",
                    33: "real-estate-owned (bank) sale", 34: "short sale"}


def load_deal_data(kit_path, property_id, password=None):
    """(lease spaces with rates, active for-sale offers, sale comps) for one property."""
    needle = str(property_id)
    spaces, offers = [], []
    for line in kit_lines(kit_path, "data/commercial-listing-summary.jsonl", password):
        if needle not in line:
            continue
        listing = json.loads(line)
        if listing.get("listingStatus") != 1:  # Active only
            continue
        for space in (listing.get("forLeaseSummary") or {}).get("spaces") or []:
            if space.get("propertyId") == property_id:
                spaces.append({**space, "listing_id": listing["id"]})
        sale = listing.get("forSaleSummary")
        if sale and listing.get("propertyId") == property_id:
            offers.append({**sale, "listing_id": listing["id"]})
    comps = []
    for line in kit_lines(kit_path, "data/commercial-sale-comp.jsonl", password):
        if needle not in line:
            continue
        comp = json.loads(line)
        if any(e.get("propertyId") == property_id for e in comp.get("entities") or []):
            comps.append(comp)
    return spaces, offers, comps


def monthly_rent(space):
    """(monthly total $, $/SF/year) from a listed space's asking rate, or (None, None)."""
    rate = space.get("rateHigh") or space.get("rateLow") or 0
    sf = space.get("squareFeetMax") or 0
    if not rate or not sf or space.get("rateBasis") not in (1, 2):
        return None, None
    per_sf_year = rate * 12 if space["rateBasis"] == 1 else rate
    return round(per_sf_year * sf / 12), round(per_sf_year, 2)


def lease_records(spaces):
    out = []
    for s in spaces:
        monthly, per_year = monthly_rent(s)
        basis, meaning = SERVICE.get(s.get("serviceType"), ("basis not recorded", "who pays operating costs is unknown"))
        name = f"suite {s['suite']}" if s.get("suite") else "unnamed space"
        sf = s.get("squareFeetMax")
        when = (s.get("availableOnDate") or "")[:10]
        label = name[0].upper() + name[1:]
        avail = (f"Available since {when}. " if when <= date.today().isoformat() else f"Available {when}. ") if when else ""
        if monthly is None:
            out.append(record("lease_asking_rent", None, "costar", s["listing_id"], (s.get("dateOnMarket") or "")[:10] or None,
                              confidence=0.2, status="unknown", needs_human=True,
                              note=f"Listed {name} ({sf or 0:,.0f} sq ft) has no asking rent recorded; ask the broker."))
            continue
        quoted = f"${s.get('rateHigh') or s.get('rateLow'):g}/sq ft/{'month' if s['rateBasis'] == 1 else 'year'}"
        out.append(record(
            "lease_asking_rent", monthly, "costar", s["listing_id"], (s.get("dateOnMarket") or "")[:10] or None,
            confidence=0.7, status="agrees", needs_human=bool(s.get("isLeasePending")),
            note=(f"{label}, {sf:,.0f} sq ft: asking {quoted} ({basis}) = ${monthly:,}/month base rent, "
                  f"${per_year:,.2f}/sq ft/year; {meaning}. "
                  + avail
                  + ("A lease is pending on this space. " if s.get("isLeasePending") else "")
                  + "Asking rent, not a signed rate.")))
    return out


def load_rent_estimates(path=RENT_ESTIMATES):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def market_rent_records(spaces, estimates):
    """What similar spaces that leased were asking, next to each listed space's asking rent."""
    if not estimates:
        return []
    out, tested = [], (estimates.get("evaluation") or {}).get("listings") or {}
    for s in spaces:
        e = (estimates.get("spaces") or {}).get(str(s.get("id")))
        if not e:
            continue
        name = f"Suite {s['suite']}" if s.get("suite") else "The listed space"
        basis = SERVICE.get(e.get("service_type"), ("lease basis not quoted",))[0]
        span = f"${e['low']:,.2f}–${e['high']:,.2f}/sq ft/year (typical ${e['typical']:,.2f})"
        if e["method"] == "similar_deals":
            how = (f"the middle 80% of {e['similar_recent_deals']} similar {e['space_use']} deals ({basis}) signed in "
                   f"this city in the last {estimates.get('recent_years', 7)} years; the trained model did no better "
                   "than this for this kind of space")
        else:
            t = (tested.get("by_space_use") or {}).get(e["space_use"]) or tested
            how = (f"a model trained on {estimates.get('trained_on_deals', 0):,} signed CoStar lease deals "
                   f"since {estimates.get('train_since', '')}" + (
                       f"; on buildings it never saw, its typical rent was off by a median "
                       f"{t['model_median_error_pct']:g}% and its ranges held {t['range_coverage_pct']}% of asking rents"
                       if t.get("model_median_error_pct") is not None else ""))
        asking = monthly_rent(s)[1]
        status, needs_human = "agrees", False
        if e["low_confidence"]:
            status, needs_human = "unknown", True
            n = e["similar_recent_deals"]
            verdict = ((f"Only {n} recent deals share" if n else "No recent deals share")
                       + f" this use and lease basis ({basis}), so treat the range as rough."
                       + ("" if asking is not None else " No asking rent is posted either."))
        elif asking is None:
            status, needs_human = "unknown", True
            verdict = "No asking rent is posted; this is what to expect when you call."
        elif e["low"] <= asking <= e["high"]:
            verdict = f"The asking ${asking:,.2f} is within the range."
        else:
            status, needs_human = "conflict", True
            side = "above" if asking > e["high"] else "below"
            verdict = (f"The asking ${asking:,.2f} is {side} the range; ask the broker why (finish, term, "
                       "concessions)" + (" before negotiating." if side == "above" else "."))
        out.append(record(
            "market_rent_range", e["typical"], "costar", f"rent-model:{s.get('id')}", estimates.get("generated"),
            confidence=0.3 if e["low_confidence"] else 0.5 if e["method"] == "similar_deals" else 0.6,
            status=status, needs_human=needs_human,
            note=f"{name}: similar spaces that leased were asking {span}. {verdict} From {how}. An estimate, not an appraisal."))
    return out


def sale_records(offers, building_sf=None):
    out = []
    for o in offers:
        low, high = o.get("askingPriceLow"), o.get("askingPriceHigh")
        price = high or low
        on_market = (o.get("minDateOnMarket") or "")[:10] or None
        kind = SALE_TYPE.get(o.get("saleType"), "sale type not recorded")
        if not price:
            out.append(record("sale_asking_price", None, "costar", o["listing_id"], on_market, confidence=0.3,
                              status="unknown", needs_human=True,
                              note=f"The property is listed for sale ({kind}) but no asking price is published; "
                                   "ask the listing broker."))
            continue
        sf = o.get("totalAvailableSquareFeet") or building_sf
        per_sf = f", ${price / sf:,.0f}/sq ft over {sf:,.0f} sq ft" if sf else ""
        days = (date.today() - date.fromisoformat(on_market)).days if on_market else None
        extra = []
        if o.get("capitalizationRate"):
            extra.append(f"cap rate {o['capitalizationRate']:g}%")
        if o.get("netOperatingIncome"):
            extra.append(f"NOI ${o['netOperatingIncome']:,.0f}")
        if (o.get("assetCount") or 1) > 1:
            extra.append(f"offered with {o['assetCount'] - 1} other propert{'y' if o['assetCount'] == 2 else 'ies'}")
        out.append(record(
            "sale_asking_price", price, "costar", o["listing_id"], on_market, confidence=0.7, status="agrees",
            needs_human=o.get("saleStatus") not in (None, 1),
            note=(f"Listed for sale at ${price:,.0f}" + (f" to ${high:,.0f}" if low and high and low != high else "")
                  + f"{per_sf}; marketed to an {kind} buyer"
                  + (f", on the market {days} days" if days is not None else "")
                  + (f" ({'; '.join(extra)})" if extra else "")
                  + (". Under contract or pending, not just active" if o.get("saleStatus") not in (None, 1) else "")
                  + ". Asking price, not a sale price.")))
    return out


def sold_records(comps, property_id):
    out = []
    for c in sorted(comps, key=lambda c: c.get("soldDate") or "", reverse=True)[:3]:
        when = (c.get("soldDate") or "")[:10] or None
        conditions = c.get("saleConditions") or []
        portfolio = [PORTFOLIO_CONDITIONS[x] for x in conditions if x in PORTFOLIO_CONDITIONS]
        others = [OTHER_CONDITIONS[x] for x in conditions if x in OTHER_CONDITIONS]
        price = c.get("soldForPrice")
        if portfolio or (c.get("numberOfEntities") or 0) > 1:
            out.append(record("last_sale_price", None, "costar", c["id"], when, confidence=0.3, status="unknown",
                              note=(f"Sold {when} as part of a larger transaction ({', '.join(portfolio) or 'multiple properties'}"
                                    + (f", ${price:,.0f} for everything" if price else "")
                                    + "); that price is not this building's value.")))
            continue
        if not price:
            out.append(record("last_sale_price", None, "costar", c["id"], when, confidence=0.3, status="unknown",
                              note=f"Sold {when}; price not disclosed (Texas does not require sale prices to be "
                                   "disclosed)." + (f" Conditions: {', '.join(others)}." if others else "")))
            continue
        out.append(record(
            "last_sale_price", price, "costar", c["id"], when, confidence=0.7 if c.get("salePriceStatus") == 3 else 0.5,
            status="agrees",
            note=(f"Sold {when} for ${price:,.0f} ({PRICE_STATUS.get(c.get('salePriceStatus'), 'price basis unknown')}"
                  f"; {SALE_TYPE.get(c.get('saleType'), 'sale type unknown')})"
                  + (f". Conditions: {', '.join(others)}" if others else "") + ".")))
    return out


def capital_records(capital, lease, offers):
    """What each path needs up front vs the buyer's capital. States the gap; never picks a path.
    lease: (monthly rent, description of the space) or None; offers: for-sale offers with prices."""
    out = []
    if capital is None:
        return out
    if lease:
        monthly, which = lease
        year_one = monthly * 12
        out.append(record(
            "capital_vs_lease", year_one, "costar", None, confidence=0.5,
            status="agrees" if capital >= year_one else "conflict", needs_human=True,
            note=(f"Leasing {which}: base rent ${monthly:,}/month, ${year_one:,} for the first year before "
                  f"operating costs, deposit and build-out. Capital ${capital:,.0f} "
                  + ("covers it." if capital >= year_one else "does not cover a year of base rent."))))
    priced = [o for o in offers if o.get("askingPriceHigh") or o.get("askingPriceLow")]
    if priced:
        offer = min(priced, key=lambda o: o.get("askingPriceHigh") or o.get("askingPriceLow"))
        price = offer.get("askingPriceHigh") or offer.get("askingPriceLow")
        bundle = (offer.get("assetCount") or 1) > 1
        out.append(record(
            "capital_vs_purchase", price, "costar", offer["listing_id"], confidence=0.3 if bundle else 0.5,
            status="unknown" if bundle else ("agrees" if capital >= price else "conflict"), needs_human=True,
            note=(f"Buying: asking price ${price:,.0f} covers {offer['assetCount']} properties offered together, so "
                  "this building alone has no price; ask the broker whether it can be bought separately before "
                  "comparing it with leasing." if bundle else
                  f"Buying: asking price ${price:,.0f}. Capital ${capital:,.0f} "
                  + ("covers an all-cash purchase before closing costs and build-out."
                     if capital >= price else
                     f"leaves ${price - capital:,.0f} to finance (about {capital / price:.0%} of the price as a down "
                     "payment); a lender sets the actual terms."))))
    return out


def chosen_space(spaces, suite=None, square_feet=None):
    """(monthly rent, space) being considered: the named suite, else the space of that size, else the lowest
    listed rent."""
    priced = [(monthly_rent(s)[0], s) for s in spaces if monthly_rent(s)[0]]
    if not priced:
        return None
    return (next(((m, s) for m, s in priced if suite and str(s.get("suite")).lower() == str(suite).lower()), None)
            or next(((m, s) for m, s in priced if square_feet and s.get("squareFeetMax") == square_feet), None)
            or min(priced, key=lambda ms: ms[0]))


def space_name(space):
    return f"suite {space['suite']}" if space.get("suite") else f"the {space.get('squareFeetMax') or 0:,.0f} sq ft space"


def chosen_rent(spaces, suite=None, square_feet=None):
    """(monthly rent, description) for the space being considered."""
    pick = chosen_space(spaces, suite, square_feet)
    return (pick[0], space_name(pick[1])) if pick else None


def rent_for_simulation(spaces, suite=None, square_feet=None):
    """{'monthly', 'source'} for the cash simulator, or None when no listed space has an asking rent."""
    pick = chosen_space(spaces, suite, square_feet)
    if not pick:
        return None
    monthly, space = pick
    basis = SERVICE.get(space.get("serviceType"), ("",))[0]
    return {"monthly": monthly, "source": f"CoStar asking rent, {space_name(space)}" + (f", {basis}" if basis else "")}


def deal_facts(property_id, spaces, offers, comps, building_sf=None, capital=None, suite=None, square_feet=None,
               rent_estimates=None):
    lease = lease_records(spaces) + market_rent_records(spaces, rent_estimates)
    sale = sale_records(offers, building_sf)
    sold = sold_records(comps, property_id)
    paths = (["lease"] if spaces else []) + (["buy"] if offers else [])
    return {
        "property_id": property_id, "paths": paths, "capital": capital,
        "records": lease + sale + sold + capital_records(capital, chosen_rent(spaces, suite, square_feet), offers),
    }


def run_deal(kit_path, property_id, capital=None, password=None, suite=None, square_feet=None):
    from space_fit import load_property  # local import keeps module load light for the web server
    prop = load_property(kit_path, property_id, password)
    spaces, offers, comps = load_deal_data(kit_path, property_id, password)
    result = deal_facts(property_id, spaces, offers, comps, prop.get("grossBuildingAreaSquareFeet"), capital, suite,
                        square_feet, load_rent_estimates())
    a = prop.get("primaryAddress") or {}
    result["address"] = f"{a.get('streetNumber', '')} {a.get('streetName', '')} {a.get('streetType', '')}".strip()
    return result
