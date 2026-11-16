"""Industry registry for the neighborhood demand check.

One entry per industry. Adding an industry means adding an entry here; the
pipeline, the evaluation, the API and the UI pick it up from this table.

Fields:
  label        shown to people
  group        for grouping in the UI
  osm          Overpass selectors (no bbox; the pipeline adds it). Map-feature tags from
               https://wiki.openstreetmap.org/wiki/Map_features
  names        regex on CoStar kit tenant names (word-bounded, case-insensitive)
  cluster_mi   features closer than this with compatible names are one facility
               (courts in one park); small for shops so neighbours stay separate
  demand_band  ACS age band whose trend matters by definition (childcare -> under 5);
               None = total population
  synonyms     words people use, for resolving free text to an industry
  participation  optional sourced national participation figure
  places       what the mapped facilities are, for sentences ("pickleball courts or clubs, indoor or
               outdoor"); defaults to the lowercased label
  dataset      optional authoritative source used instead of OpenStreetMap for the locations
               (e.g. Texas licensed childcare centers, with licensed capacity)
  supply_votes False for businesses that cluster on purpose (restaurants, bars, cafes): many
               nearby is not a negative on its own, so local supply is shown but does not vote
"""
import re

INDUSTRIES = {
    # --- Sports, fitness, recreation ---------------------------------------------
    "indoor_pickleball": {
        "label": "Indoor pickleball", "group": "Sports & fitness",
        "places": "pickleball courts or clubs (indoor or outdoor)",
        "osm": ['nwr["sport"~"pickleball"]'],
        "names": r"pickleball|paddle (club|sports)",
        "cluster_mi": 0.15,
        "synonyms": ["pickleball", "pickle ball", "paddle sports", "indoor pickleball"],
        "participation": {
            "value": 24_300_000, "core": 7_500_000, "year": 2025, "source": "public:sfia",
            "url": "https://www.sfia.org/u-s-pickleball-participation",
            "note": ("SFIA Topline Participation: 24.3M players in 2025 (+479% since 2020), 7.5M core (8+ times a "
                     "year), 42.9% female; ages 13-24 have the highest participation rate. SFIA does not publish "
                     "age-specific rates on this page, so no age weighting is applied."),
        },
    },
    "fitness_gym": {
        "label": "Gym / fitness studio", "group": "Sports & fitness",
        "osm": ['nwr["leisure"="fitness_centre"]'],
        "names": r"gym|gyms|fitness|crossfit|orangetheory|f45|health club|bootcamp|personal training|barre|"
                 r"cyclebar|soulcycle|anytime fitness|planet fitness|la fitness|24 hour fitness|curves",
        "cluster_mi": 0.03,
        "synonyms": ["gym", "fitness", "fitness studio", "crossfit", "health club", "personal training",
                     "boutique fitness", "spin studio", "cycling studio"],
    },
    "yoga_pilates": {
        "label": "Yoga / pilates studio", "group": "Sports & fitness",
        "osm": ['nwr["sport"~"yoga|pilates"]', 'nwr["leisure"="fitness_centre"]["name"~"yoga|pilates",i]'],
        "names": r"yoga|pilates",
        "cluster_mi": 0.03,
        "synonyms": ["yoga", "pilates", "hot yoga", "barre studio", "mind body studio"],
    },
    "martial_arts": {
        "label": "Martial arts / boxing", "group": "Sports & fitness",
        "osm": ['nwr["sport"~"martial_arts|karate|taekwondo|judo|jiu-jitsu|boxing|kickboxing|mma"]',
                'nwr["leisure"~"sports_centre|fitness_centre"]["name"~"karate|martial|taekwondo|jiu|boxing|mma",i]'],
        "names": r"martial arts?|karate|taekwondo|tae kwon do|jiu[- ]?jitsu|judo|boxing|kickboxing|mma|kung fu",
        "cluster_mi": 0.03,
        "synonyms": ["martial arts", "karate", "taekwondo", "jiu jitsu", "bjj", "boxing gym", "mma", "judo"],
    },
    "dance_studio": {
        "label": "Dance studio", "group": "Sports & fitness",
        "osm": ['nwr["leisure"="dance"]', 'nwr["amenity"="dancing_school"]'],
        "names": r"dance (studio|academy|center|centre|school|company)|ballet",
        "cluster_mi": 0.03,
        "synonyms": ["dance", "dance studio", "ballet", "dance school"],
    },
    "climbing_gym": {
        "label": "Climbing gym", "group": "Sports & fitness",
        "osm": ['nwr["sport"="climbing"]["leisure"~"sports_centre|fitness_centre|sports_hall"]'],
        "names": r"climbing|bouldering",
        "cluster_mi": 0.05,
        "synonyms": ["climbing", "bouldering", "rock climbing gym", "climbing gym"],
    },
    # --- Children and education ----------------------------------------------------
    "childcare": {
        "label": "Childcare / daycare", "group": "Children & education",
        "osm": ['nwr["amenity"~"^(childcare|kindergarten)$"]'],
        "dataset": "tx_hhsc_childcare",
        "places": "licensed childcare centers",
        "names": r"day ?care|child ?care|childcare|preschool|pre-school|montessori|kindercare|kiddie academy|"
                 r"primrose school|goddard school|la petite|tutor time|early (learning|childhood)|nursery school|"
                 r"children'?s (center|academy|learning)|child development",
        "cluster_mi": 0.03, "demand_band": "under_5",
        "synonyms": ["daycare", "day care", "childcare", "child care", "preschool", "nursery", "montessori",
                     "early learning"],
    },
    "tutoring": {
        "label": "Tutoring / learning center", "group": "Children & education",
        "osm": ['nwr["amenity"~"^(prep_school|tutoring)$"]', 'nwr["office"="educational_institution"]',
                'nwr["name"~"kumon|mathnasium|sylvan|huntington learning|tutoring",i]["name"]'],
        "names": r"tutor(ing)?|kumon|mathnasium|sylvan|huntington learning|learning center|test prep",
        "cluster_mi": 0.03, "demand_band": "age_5_17",
        "synonyms": ["tutoring", "tutor", "learning center", "test prep", "kumon", "mathnasium", "after school"],
    },
    # --- Food and drink ------------------------------------------------------------------
    "restaurant": {
        "supply_votes": False,
        "label": "Restaurant", "group": "Food & drink",
        "osm": ['nwr["amenity"="restaurant"]'],
        "names": r"restaurant|grill|kitchen|bistro|cantina|eatery|taqueria|sushi|bbq|bar-b-q|steakhouse|"
                 r"pizzeria|trattoria|diner",
        "cluster_mi": 0.0,
        "synonyms": ["restaurant", "dining", "eatery", "bistro", "diner", "steakhouse", "sushi"],
    },
    "fast_food": {
        "supply_votes": False,
        "label": "Fast food / quick service", "group": "Food & drink",
        "osm": ['nwr["amenity"="fast_food"]'],
        "names": r"pizza|taco|tacos|burger|subway|chick-fil-a|mcdonald'?s|wingstop|wings|sandwich|"
                 r"chicken|kolache|donut|doughnut",
        "cluster_mi": 0.0,
        "synonyms": ["fast food", "quick service", "qsr", "burger", "pizza", "taco", "sandwich shop", "wings"],
    },
    "cafe": {
        "supply_votes": False,
        "label": "Coffee shop / cafe", "group": "Food & drink",
        "osm": ['nwr["amenity"="cafe"]'],
        "names": r"cafe|café|coffee|espresso|starbucks|tea house|boba|bakery|smoothie",
        "cluster_mi": 0.0,
        "synonyms": ["coffee", "cafe", "coffee shop", "espresso bar", "bakery cafe", "boba", "tea shop"],
    },
    "bar": {
        "supply_votes": False,
        "label": "Bar / brewery", "group": "Food & drink",
        "osm": ['nwr["amenity"~"^(bar|pub|biergarten)$"]', 'nwr["craft"="brewery"]'],
        "names": r"\bbar\b|pub|tavern|brewery|brewing|taproom|saloon|lounge|wine bar",
        "cluster_mi": 0.0,
        "synonyms": ["bar", "pub", "brewery", "taproom", "tavern", "wine bar", "lounge"],
    },
    # --- Health ----------------------------------------------------------------------------
    "medical_clinic": {
        "label": "Medical clinic / urgent care", "group": "Health",
        "osm": ['nwr["amenity"~"^(clinic|doctors)$"]', 'nwr["healthcare"~"^(clinic|doctor)$"]'],
        "names": r"clinic|urgent care|medical (center|group|associates)|physicians?|family (medicine|practice)|"
                 r"pediatrics?|internal medicine|\bm\.?d\.?\b",
        "cluster_mi": 0.0,
        "synonyms": ["clinic", "urgent care", "doctor", "medical office", "primary care", "pediatrician",
                     "walk-in clinic"],
    },
    "dental": {
        "label": "Dental office", "group": "Health",
        "osm": ['nwr["amenity"="dentist"]', 'nwr["healthcare"="dentist"]'],
        "names": r"dental|dentist|dentistry|orthodont|\bdds\b|\bdmd\b",
        "cluster_mi": 0.0,
        "synonyms": ["dentist", "dental", "orthodontist", "dental office"],
    },
    "pharmacy": {
        "label": "Pharmacy", "group": "Health",
        "osm": ['nwr["amenity"="pharmacy"]', 'nwr["healthcare"="pharmacy"]'],
        "names": r"pharmacy|drug store|cvs|walgreens",
        "cluster_mi": 0.0,
        "synonyms": ["pharmacy", "drugstore", "drug store", "chemist"],
    },
    "physical_therapy": {
        "label": "Physical therapy / chiropractic", "group": "Health",
        "osm": ['nwr["healthcare"~"physiotherapist|chiropractor"]', 'nwr["healthcare:speciality"~"chiropractic|physiotherapy"]'],
        "names": r"physical therapy|physiotherapy|chiropract|rehab(ilitation)?|sports medicine",
        "cluster_mi": 0.0,
        "synonyms": ["physical therapy", "pt clinic", "chiropractor", "chiropractic", "physiotherapy", "rehab"],
    },
    "veterinary": {
        "label": "Veterinary clinic", "group": "Health",
        "osm": ['nwr["amenity"="veterinary"]'],
        "names": r"veterinary|animal hospital|\bvet\b|pet clinic",
        "cluster_mi": 0.0,
        "synonyms": ["vet", "veterinary", "veterinarian", "animal hospital", "pet clinic"],
    },
    # --- Personal services --------------------------------------------------------------
    "salon_barber": {
        "label": "Hair salon / barber", "group": "Personal services",
        "osm": ['nwr["shop"~"^(hairdresser|beauty|barber)$"]'],
        "names": r"salon|barber|hair|nails?|beauty|spa\b|lash|brow",
        "cluster_mi": 0.0,
        "synonyms": ["salon", "hair salon", "barber", "barbershop", "nail salon", "beauty salon", "spa"],
    },
    "laundromat": {
        "label": "Laundromat / dry cleaner", "group": "Personal services",
        "osm": ['nwr["shop"~"^(laundry|dry_cleaning)$"]', 'nwr["amenity"="laundry"]'],
        "names": r"laundr|cleaners|dry clean|wash ?(and|&|n) ?fold",
        "cluster_mi": 0.0,
        "synonyms": ["laundromat", "laundry", "dry cleaner", "dry cleaning", "wash and fold"],
    },
    "pet_services": {
        "label": "Pet grooming / boarding", "group": "Personal services",
        "osm": ['nwr["shop"~"^(pet_grooming|pet)$"]', 'nwr["amenity"="animal_boarding"]'],
        "names": r"pets?\b|groom|kennel|doggy|dog (daycare|boarding|spa)|boarding",
        "cluster_mi": 0.0,
        "synonyms": ["pet grooming", "dog grooming", "pet boarding", "dog daycare", "kennel", "pet store"],
    },
    "coworking": {
        "label": "Coworking space", "group": "Personal services",
        "osm": ['nwr["amenity"="coworking_space"]', 'nwr["office"="coworking"]'],
        "names": r"cowork|co-work|regus|wework|executive suites|shared office",
        "cluster_mi": 0.0,
        "synonyms": ["coworking", "co-working", "shared office", "flex office", "executive suites"],
    },
    # --- Retail ----------------------------------------------------------------------------
    "grocery": {
        "label": "Grocery store", "group": "Retail",
        "osm": ['nwr["shop"~"^(supermarket|grocery|greengrocer)$"]'],
        "names": r"grocery|supermarket|market\b|foods?\b|h-e-b\b|kroger|aldi\b|fiesta mart",
        "cluster_mi": 0.0,
        "synonyms": ["grocery", "grocery store", "supermarket", "market", "food store"],
    },
    "convenience": {
        "label": "Convenience store", "group": "Retail",
        "osm": ['nwr["shop"="convenience"]'],
        "names": r"convenience|food mart|mini mart|7-eleven|corner store",
        "cluster_mi": 0.0,
        "synonyms": ["convenience store", "corner store", "mini mart", "gas station store"],
    },
    # --- Auto and storage --------------------------------------------------------------
    "auto_repair": {
        "label": "Auto repair", "group": "Auto & storage",
        "osm": ['nwr["shop"~"^(car_repair|tyres)$"]'],
        "names": r"auto (repair|service|body|electric|care)|automotive|collision|tire|transmission|muffler|mechanic",
        "cluster_mi": 0.0,
        "synonyms": ["auto repair", "mechanic", "car repair", "auto shop", "tire shop", "body shop", "collision"],
    },
    "car_wash": {
        "label": "Car wash", "group": "Auto & storage",
        "osm": ['nwr["amenity"="car_wash"]'],
        "names": r"car ?wash|auto spa|detail(ing)?",
        "cluster_mi": 0.0,
        "synonyms": ["car wash", "auto detailing", "detailing"],
    },
    "self_storage": {
        "label": "Self storage", "group": "Auto & storage",
        "osm": ['nwr["shop"="storage_rental"]', 'nwr["amenity"="storage_rental"]'],
        "names": r"self[- ]?storage|storage (center|units?|solutions)|public storage|extra space|cubesmart|mini storage",
        "cluster_mi": 0.05,
        "synonyms": ["self storage", "storage units", "mini storage", "storage facility"],
    },
}

# Uses from alternatives.CATALOG (the property review) mapped onto industries.
CATALOG_TO_INDUSTRY = {"indoor pickleball": "indoor_pickleball", "fitness studio": "fitness_gym"}

# Catalog uses whose demand does not come from nearby residents: no neighborhood model.
NOT_RESIDENT_DRIVEN = {
    "warehouse / storage": "Business-to-business demand (logistics, contractors); residents nearby do not drive it. "
                           "For consumer self storage, check 'Self storage'.",
    "flex workspace": "Business-to-business demand; residents nearby do not drive it.",
    "office / service": "Demand depends on firms and workers, which ACS residence data does not measure.",
    "retail / showroom": "Too broad to model as one use. Pick a specific store type (grocery, convenience, ...).",
}

for key, spec in INDUSTRIES.items():
    spec["key"] = key
    spec["names_re"] = re.compile(rf"\b(?:{spec['names']})", re.I)
    spec.setdefault("demand_band", None)
    spec.setdefault("participation", None)
    spec.setdefault("supply_votes", True)
    spec.setdefault("places", spec["label"].lower() + " locations")

# Suppliers and consultants to an industry are not the industry ("Golf Design", "Fitness Wholesalers").
NOT_A_VENUE = re.compile(r"\b(design|contractors?|wholesal\w*|consult\w*|equipment|suppl(y|ies)|network|"
                         r"services group|council|apartments|properties|realty|holdings?|investments?|"
                         r"insurance|law|attorneys?|software|staffing|logistics)\b", re.I)


def industries_for_tenant(name):
    """Industries a CoStar tenant name belongs to (can be several, e.g. 'Kids Dance & Gymnastics')."""
    if not name or NOT_A_VENUE.search(name):
        return []
    return [key for key, spec in INDUSTRIES.items() if spec["names_re"].search(name)]


def normalize(text):
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").casefold()).split()
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def _rarity():
    """How specific each synonym word is: words used by fewer industries count more."""
    seen = {}
    for key, spec in INDUSTRIES.items():
        for word in {w for phrase in spec["synonyms"] + [spec["label"]] for w in normalize(phrase)}:
            seen.setdefault(word, set()).add(key)
    return {word: 1 / len(keys) for word, keys in seen.items()}


RARITY = _rarity()


def _matches(target, words):
    """A target word matches a typed word exactly, or as a prefix of similar length ('climb' ~ 'climbing')."""
    return target in words or (len(target) > 4 and any(
        x.startswith(target) and len(x) - len(target) <= 3 for x in words))


def resolve(text):
    """Map what a person typed to an industry, in code (no LLM).

    Exact key, label or catalog use first; then the synonym with the most matching
    words, ties going to the more specific words. Returns (key or None, how, ranked
    candidates). Unmatched text stays None: an LLM resolver can be layered on top.
    """
    raw = (text or "").strip()
    if raw in INDUSTRIES:
        return raw, "industry key", [raw]
    if raw.casefold() in CATALOG_TO_INDUSTRY:
        key = CATALOG_TO_INDUSTRY[raw.casefold()]
        return key, f"catalog use '{raw}'", [key]
    for key, spec in INDUSTRIES.items():
        if raw.casefold() == spec["label"].casefold():
            return key, "label", [key]
    words = set(normalize(raw))
    if not words:
        return None, "empty", []
    scores = {}
    for key, spec in INDUSTRIES.items():
        best = 0.0
        for phrase in spec["synonyms"] + [spec["label"]]:
            target = normalize(phrase)
            hits = [w for w in target if _matches(w, words)]
            if hits:
                specific = sum(RARITY.get(w, 1) for w in hits) / len(hits)
                best = max(best, len(hits) / len(target) + 0.1 * specific + 0.01 * len(hits))
        if best > 0.55:
            scores[key] = best
    ranked = sorted(scores, key=lambda k: -scores[k])
    if not ranked:
        return None, "no synonym matched", []
    return ranked[0], f"synonym match ({scores[ranked[0]]:.2f})", ranked[:3]


# --- What a building must offer ---------------------------------------------------------
# CoStar propertyType codes (kit schemas/enums.json): 1 Hospitality, 2 Industrial, 3 Land,
# 5 Office, 6 GeneralRetail, 7 Flex, 8 SportsAndEntertainment, 9 MixedUse, 10 Specialty,
# 11 Multifamily, 13 Healthcare, 15 Residential, 16 Homes. Subtypes name a building built for
# one business (10 DayCareCenter, 18 Restaurant, 90 HealthClub, ...).
#
# Sizes are planning assumptions (typical small-operator ranges) unless `sources` says
# otherwise; they screen out obvious misfits, they do not replace a measured plan.
SPACE_SOURCES = {
    "usa-pickleball-rule-3a3": "https://www.mypickleballconnect.com/guides/pickleball-court-dimensions/",
    "pickleball-ceiling-guidance": "https://millersports.com/pickleball-court-size/",
    "tx-746-indoor": "https://regulations.justia.com/states/texas/title-26/part-1/chapter-746/subchapter-t/division-1",
    "tx-746-4301-outdoor": "https://regulations.justia.com/states/texas/title-26/part-1/chapter-746/subchapter-t/"
                           "division-2/section-746-4301",
}
RESIDENTIAL_TYPES = {"11", "15", "16"}
STOREFRONT = {"6", "9"}  # general retail and mixed use

SPACE = {
    "indoor_pickleball": {"types": {"2", "7", "8", "6", "9"}, "subtypes": set(),
                          "min_sf": 5400, "per_unit_sf": 1800, "unit": "court", "min_clear_ft": 18,
                          "sources": ["usa-pickleball-rule-3a3", "pickleball-ceiling-guidance"],
                          "note": "1,800 sq ft playing area per court (USA Pickleball 30x60 ft minimum); 3 courts "
                                  "minimum is our business assumption; 18 ft clear is common guidance, not an official rule.",
                          "checks": ["Columns inside the courts and run-off", "Floor flatness and surface"]},
    "fitness_gym": {"types": {"2", "7", "6", "5", "9", "8"}, "subtypes": {"90"}, "min_sf": 2500, "max_sf": 40000,
                    "checks": ["Floor load for weights", "HVAC, showers and peak parking"]},
    "yoga_pilates": {"types": {"6", "9", "5", "7"}, "subtypes": {"90"}, "min_sf": 1000, "max_sf": 4000,
                     "checks": ["Quiet, column-free studio room", "Showers or changing rooms"]},
    "martial_arts": {"types": {"6", "9", "7", "5", "2"}, "subtypes": {"90"}, "min_sf": 1200, "max_sf": 6000,
                     "checks": ["Mat area free of columns", "Changing rooms"]},
    "dance_studio": {"types": {"6", "9", "7", "5"}, "subtypes": set(), "min_sf": 1200, "max_sf": 5000,
                     "checks": ["Sprung floor and mirrors", "Sound isolation from neighbours"]},
    "climbing_gym": {"types": {"2", "7", "8"}, "subtypes": set(), "min_sf": 8000, "min_clear_ft": 15,
                     "checks": ["Wall anchoring into the structure", "Fall-zone flooring"]},
    "childcare": {"types": {"6", "9", "5", "13", "7"}, "subtypes": {"10"}, "min_sf": 2500, "max_sf": 15000,
                  "per_unit_sf": 30, "unit": "child (indoor minimum, before deducting halls, restrooms, offices)",
                  "outdoor_sf_per_unit": 80,
                  "ground_floor": True, "sources": ["tx-746-indoor", "tx-746-4301-outdoor"],
                  "note": "Texas requires 30 sq ft of indoor activity space per child and 80 sq ft of outdoor "
                          "activity space per child using it at one time (26 TAC ch. 746). Center size range is "
                          "our assumption.",
                  "checks": ["Fenced outdoor play area on or next to the site", "Licensing inspection and landlord consent"]},
    "tutoring": {"types": {"5", "6", "9"}, "subtypes": set(), "min_sf": 800, "max_sf": 3500,
                 "checks": ["Classrooms with daylight", "After-school pickup parking"]},
    "restaurant": {"types": STOREFRONT, "subtypes": {"18"}, "min_sf": 1500, "max_sf": 8000, "ground_floor": True,
                   "checks": ["Kitchen hood and exhaust route", "Grease interceptor, gas and 3-phase power"]},
    "fast_food": {"types": STOREFRONT, "subtypes": {"12", "18"}, "min_sf": 1000, "max_sf": 4500, "ground_floor": True,
                  "checks": ["Drive-through lane if needed", "Hood, grease interceptor"]},
    "cafe": {"types": STOREFRONT | {"5"}, "subtypes": {"18"}, "min_sf": 600, "max_sf": 3000, "ground_floor": True,
             "checks": ["Water and drain for espresso and dish", "Street visibility"]},
    "bar": {"types": STOREFRONT | {"8"}, "subtypes": {"7", "18"}, "min_sf": 1500, "max_sf": 6000, "ground_floor": True,
            "checks": ["TABC licensing and distance rules", "Occupant load, restrooms, sound"]},
    "medical_clinic": {"types": {"13", "5", "6", "9"}, "subtypes": {"64"}, "min_sf": 1500, "max_sf": 12000,
                       "checks": ["Exam-room plumbing", "Accessible entry and parking"]},
    "dental": {"types": {"13", "5", "6", "9"}, "subtypes": {"64"}, "min_sf": 1200, "max_sf": 4500,
               "checks": ["Plumbing, compressed air and vacuum to each chair", "X-ray shielding"]},
    "pharmacy": {"types": {"6", "13", "9"}, "subtypes": {"11"}, "min_sf": 1000, "max_sf": 15000, "ground_floor": True,
                 "checks": ["Texas State Board of Pharmacy licensing", "Drive-through window if needed"]},
    "physical_therapy": {"types": {"13", "5", "6", "9", "7"}, "subtypes": {"64", "30"}, "min_sf": 1500, "max_sf": 6000,
                         "checks": ["Open gym area plus treatment rooms", "Accessible entry"]},
    "veterinary": {"types": {"6", "7", "13", "9"}, "subtypes": {"25"}, "min_sf": 2000, "max_sf": 8000,
                   "checks": ["Kennel ventilation and noise", "X-ray shielding"]},
    "salon_barber": {"types": STOREFRONT | {"5"}, "subtypes": set(), "min_sf": 500, "max_sf": 3000,
                     "prefers_street_level": True,
                     "checks": ["Shampoo-bowl plumbing", "Ventilation for chemicals"]},
    "laundromat": {"types": STOREFRONT, "subtypes": set(), "min_sf": 1500, "max_sf": 5000, "ground_floor": True,
                   "checks": ["Water supply, large drains and gas or 3-phase power for dryers", "Venting"]},
    "pet_services": {"types": {"6", "9", "7", "2"}, "subtypes": {"25"}, "min_sf": 1500, "max_sf": 10000,
                     "checks": ["Wash-station drainage", "Noise and outdoor relief area"]},
    "coworking": {"types": {"5", "9", "7"}, "subtypes": {"63"}, "min_sf": 3000, "max_sf": 30000,
                  "checks": ["Daylight, meeting rooms and restrooms", "Network and after-hours access"]},
    "grocery": {"types": STOREFRONT, "subtypes": {"23"}, "min_sf": 5000, "max_sf": 60000, "ground_floor": True,
                "checks": ["Loading for deliveries", "Refrigeration power"]},
    "convenience": {"types": STOREFRONT, "subtypes": {"9", "19"}, "min_sf": 1500, "max_sf": 5000, "ground_floor": True,
                    "checks": ["Corner or high-traffic frontage", "Alcohol/tobacco permits"]},
    "auto_repair": {"types": {"2", "7", "6"}, "subtypes": {"5"}, "min_sf": 2000, "max_sf": 15000, "ground_floor": True,
                    "drive_in": True, "checks": ["Grade-level bays and lifts", "Oil separator and hazardous-waste storage"]},
    "car_wash": {"types": {"6", "10", "3"}, "subtypes": {"8"}, "min_sf": 2000, "ground_floor": True,
                 "checks": ["Drive-through lot and queue length", "Water reclaim and drainage"]},
    "self_storage": {"types": {"2", "7", "10"}, "subtypes": {"47"}, "min_sf": 20000,
                     "checks": ["Floor load for mezzanine units", "Drive-up access and security"]},
}

for key, spec in INDUSTRIES.items():
    spec["space"] = SPACE[key]
