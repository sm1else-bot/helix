"""Business-specific licenses and permits in Houston, Texas, beyond the Certificate of Occupancy.

Official government pages only (texas.gov agencies, houstontx.gov, houstonhealth.org,
houstonpublicworks.org); each entry quotes the page that confirms it. Research done 2026-11-14.
"applies_when" says when a requirement is conditional. No timelines are claimed. NOT_CONFIRMED lists
what was looked for and could not be confirmed on an official page, so the review can say so.
"""


def lic(name, authority, url, quote, applies_when="Required"):
    return {"name": name, "authority": authority, "url": url, "quote": quote, "applies_when": applies_when}


SALES_TAX = lic("Texas sales and use tax permit", "Texas Comptroller of Public Accounts",
                "https://comptroller.texas.gov/taxes/sales/faq/permit.php",
                "You must obtain a Texas sales and use tax permit if you are an individual, partnership, corporation "
                "or other legal entity engaged in business in Texas and you: Sell tangible personal property in Texas",
                "If selling or renting taxable goods (merchandise, food, drinks, supplies) or taxable services")
FOOD_DEALER = lic("Food dealer's permit", "Houston Health Department",
                  "https://www.houstonhealth.org/services/permits/food-permits/opening-food-establishment",
                  "No person may operate a food establishment without a valid/current food dealer's permit.",
                  "Always for a food establishment; pre-operational inspection required")
FOOD_IF_SERVED = lic("Food dealer's permit", "Houston Health Department",
                     "https://www.houstonhealth.org/services/permits/food-permits/faq",
                     "anyone who sells or serves food (edible products) is required to obtain a food dealer's permit "
                     "from the Houston Health Department.", "Only if selling or serving food")
FOG = lic("Fats, oils and grease (FOG) special waste generator permit, with grease trap",
          "Houston Health Department (FOG - Special Waste Program)", "https://houstonpublicworks.org/protect-our-pipes",
          "Commercial kitchens and businesses that generate grease must be permitted and are required to install grease "
          "traps that must be cleaned every 90 days unless a waiver is submitted and approved by the Houston Health "
          "Department.", "Kitchens that generate grease")
SPECIAL_WASTE = lambda when: lic(  # noqa: E731
    "Special waste generator permit (interceptor or trap)", "Houston Health Department (FOG - Special Waste Program)",
    "https://www.houstonhealth.org/services/permits/special-waste-permits/special-waste-generators",
    "Permitting location for non-food establishments (apartments, car wash, lube station, laundromat, septic tank)",
    when)
TABC_ON = lic("TABC on-premise permit (Mixed Beverage MB, Wine and Malt Beverage Retailer's BG, or Retail Dealer's "
              "On-Premise BE)", "Texas Alcoholic Beverage Commission",
              "https://www.tabc.texas.gov/services/tabc-licenses-permits/tabc-license-permit-types/",
              "Authorizes the sale of distilled spirits, wine and malt beverages for on-premise consumption.",
              "Only if selling alcohol for on-premise consumption")
TABC_OFF = lic("TABC off-premise permit (Wine and Malt Beverage Retailer's Off-Premise BQ or Retail Dealer's "
               "Off-Premise BF)", "Texas Alcoholic Beverage Commission",
               "https://www.tabc.texas.gov/services/tabc-licenses-permits/tabc-license-permit-types/",
               "Authorizes the sale of malt beverages and wine for off-premise consumption.",
               "Only if selling beer or wine to go")
TABC_MEMBERS = lic("TABC retail permit", "Texas Alcoholic Beverage Commission",
                   "https://www.tabc.texas.gov/faqs/tabc-license-permit-faqs/",
                   "You will need a permit if the drinks will only be available to paying guests.",
                   "Only if serving alcohol, even only to paying members or guests")
HOUSTON_ALCOHOL = lic("City of Houston alcohol certification (Certificate of City Secretary)",
                      "City of Houston Administration & Regulatory Affairs",
                      "https://www.houstontx.gov/ara/rp/aarb-Alcohol-Sales-Guidelines.pdf",
                      "The City of Houston has updated its procedures for obtaining the Certificate of City Secretary "
                      "required by the Texas Alcoholic Beverage Commission (TABC) as part of its prequalification process.",
                      "Only if selling alcohol; minimum distances apply (e.g. 300 ft from a religious institution, "
                      "public hospital or school) unless an exception is granted")
TOBACCO = lic("Cigarette, cigar and/or tobacco products retailer permit", "Texas Comptroller of Public Accounts",
              "https://comptroller.texas.gov/taxes/tobacco/tobacco-retailer-permit.php",
              "Retailers who intend to sell, store, or make a delivery sale of cigarettes, cigars and/or tobacco "
              "products to consumers in Texas must apply for a retailer permit", "Only if selling tobacco products")
ECIG = lic("E-cigarette retailer permit", "Texas Comptroller of Public Accounts",
           "https://comptroller.texas.gov/taxes/tobacco/e-cig-retailer-permit.php",
           "Retailers who intend to sell, store, or make a delivery sale of e-cigarette products to consumers in Texas "
           "must apply for an e-cigarette retailer permit", "Only if selling e-cigarettes")
SCALES = lic("Weighing device registration (commercial scales)", "Texas Department of Agriculture - Weights and Measures",
             "https://texasagriculture.gov/Regulatory-Programs/Weights-and-Measures/Devices",
             "Businesses operating commercial scales and LPG meters must register these devices with the Department "
             "annually.", "If using commercial scales (scales used only for taxable ready-to-eat food are exempt)")
HEALTH_SPA = lic("Health spa operator's certificate of registration", "Texas Secretary of State",
                 "https://www.sos.state.tx.us/statdoc/faqs3000.shtml",
                 "[a] person may not operate a health spa or offer for sale, or sell, a membership in a health spa "
                 "unless the person holds a health spa operator's certificate of registration.",
                 "If selling memberships for instruction in, or use of facilities for, a physical exercise program "
                 "(statutory exclusions apply, e.g. businesses that only teach dance or aerobics)")
XRAY = lambda kind: lic(  # noqa: E731
    f"X-ray machine certificate of registration ({kind})", "Texas Department of State Health Services - Radiation Control",
    "https://www.dshs.texas.gov/texas-radiation-control/x-ray-machines-x-ray-services",
    "The DSHS Radiation Control Program issues certificates of registration for businesses that use X-ray machines for "
    "medical and academic, dental, veterinary, and industrial uses", "If using X-ray machines")
FIRE_LIST = "https://www.houstontx.gov/fire/formsandpermits/index.html"
FUEL_TANKS = lic("Petroleum storage tank registration", "Texas Commission on Environmental Quality",
                 "https://www.tceq.texas.gov/downloads/assistance/publications/rg-434.pdf/@@download/file/rg-434.pdf",
                 "All underground storage tanks (USTs) that contain fuel or regulated substances must be registered "
                 "with TCEQ.", "Only if storing fuel on site")
ALCOHOL_ON = [TABC_ON, HOUSTON_ALCOHOL]
FITNESS = [HEALTH_SPA, SALES_TAX]

LICENSES = {
    "indoor_pickleball": [HEALTH_SPA, TABC_MEMBERS, HOUSTON_ALCOHOL, FOOD_IF_SERVED, SALES_TAX],
    "fitness_gym": FITNESS,
    "yoga_pilates": FITNESS,
    "martial_arts": FITNESS,
    "dance_studio": [SALES_TAX],
    "climbing_gym": FITNESS,
    "childcare": [
        lic("Child-care operation license (child-care center)", "Texas Health and Human Services Commission - Child Care "
            "Regulation", "https://www.hhs.texas.gov/providers/child-care-regulation/become-a-provider/ccr-licensing-exemptions",
            "Chapter 42 of the Texas Human Resources Code requires persons who operate a child care facility to be licensed",
            "Any child-care center not exempt under Human Resources Code Chapter 42"),
        lic("Food dealer's permit (day-care center)", "Houston Health Department",
            "https://www.houstontx.gov/business/start/food-service.html",
            "Food service businesses including caterers, restaurants, mobile food units, roadside, farmers markets, day "
            "care centers, manufacturing, packaging etc., require permits and periodic inspection.",
            "Day-care centers inside Houston city limits"),
        lic("Fire permit for day-care facilities", "Houston Fire Department (Fire Marshal)",
            "https://www.houstontx.gov/business/start/food-service.html",
            "The HFD issues permits for storage, handling, and manufacture of flammable liquids and combustibles; Public "
            "Assembly; Maximum Occupancy; Tents; Open burning; Day Care facilities", "Day-care facilities in Houston"),
    ],
    "tutoring": [SALES_TAX],
    "restaurant": [FOOD_DEALER, FOG, *ALCOHOL_ON,
                   lic("TABC Food and Beverage Certificate (FB)", "Texas Alcoholic Beverage Commission",
                       "https://www.tabc.texas.gov/services/tabc-licenses-permits/tabc-license-permit-types/",
                       "In certain circumstances, Mixed Beverage Permit holder must have a Food and Beverage Certificate.",
                       "With a mixed beverage permit, where TABC requires it"), SALES_TAX],
    "fast_food": [FOOD_DEALER, FOG, TABC_OFF | {"name": "TABC retail permit (e.g. BG or BE)",
                                               "applies_when": "Only if selling alcohol"}, HOUSTON_ALCOHOL, SALES_TAX],
    "cafe": [FOOD_DEALER, FOG | {"applies_when": "If the kitchen generates grease or the site has an interceptor"},
             TABC_OFF | {"name": "TABC retail permit (e.g. BG or BE)", "applies_when": "Only if selling alcohol"},
             HOUSTON_ALCOHOL, SALES_TAX],
    "bar": [TABC_ON | {"applies_when": "Always: selling alcohol for on-premise consumption"},
            lic("TABC Late Hours Certificate (LH)", "Texas Alcoholic Beverage Commission",
                "https://www.tabc.texas.gov/services/tabc-licenses-permits/tabc-license-permit-types/",
                "It authorizes the holder to sell and/or serve between the hours of midnight and 2 a.m.",
                "Only if serving between midnight and 2 a.m."),
            HOUSTON_ALCOHOL | {"applies_when": "Always for alcohol sales; minimum distances apply"},
            FOOD_IF_SERVED, FOG | {"applies_when": "If the kitchen generates grease"},
            lic("Dance hall permit", "City of Houston Administration & Regulatory Affairs",
                "https://www.houstontx.gov/business/start/food-service.html",
                "Permits are issued yearly for ... Dance Halls", "Only if operating as a dance hall"),
            TOBACCO, SALES_TAX],
    "medical_clinic": [XRAY("medical") | {"url": "https://www.dshs.texas.gov/texas-radiation-control/x-ray-machines-x-ray-"
                                                  "services/frequently-asked-questions",
                                           "quote": "A facility must apply for a certificate of registration within 30 days "
                                                    "of beginning use of radiation machines."}, SALES_TAX],
    "dental": [lic("Dentist license", "Texas State Board of Dental Examiners",
                   "https://tsbde.texas.gov/78i8ljhbj/2019/02/Dental-Practice.pdf",
                   "A person may not practice or offer to practice dentistry or dental surgery or represent that the person "
                   "practices dentistry unless the person holds a license issued by the board.",
                   "Every dentist practicing at the location"), XRAY("dental"), SALES_TAX],
    "pharmacy": [lic("Pharmacy license (e.g. Class A community pharmacy)", "Texas State Board of Pharmacy",
                     "https://www.pharmacy.texas.gov/files_pdf/BN/Feb22/F.8.pdf",
                     "A pharmacy must be licensed before purchasing prescription medications.",
                     "Always; application, background check and pre-inspection"), SALES_TAX, TOBACCO],
    "physical_therapy": [lic("Physical therapist license (practitioners)", "Texas Board of Physical Therapy Examiners",
                             "https://ptot.texas.gov/wp-content/uploads/2022/06/PT-Practice-Act-2021.pdf",
                             "A person may not practice physical therapy or practice as a physical therapist assistant, "
                             "unless the person is an individual who holds a license issued by the board.",
                             "Physical therapy must be provided by licensed PTs or PTAs"), SALES_TAX],
    "veterinary": [lic("Veterinary license (practitioners)", "Texas Board of Veterinary Medical Examiners",
                       "https://www.veterinary.texas.gov/documents/vetact/Veterinary%20Licensing%20Act%202021.pdf",
                       "Except as provided by Section 801.004, a person may not practice, or offer or attempt to practice, "
                       "veterinary medicine unless the person holds a license to practice veterinary medicine issued "
                       "under this chapter.", "Every veterinarian practicing at the location"),
                   XRAY("veterinary"), SALES_TAX],
    "salon_barber": [lic("Barbering or cosmetology establishment license", "Texas Department of Licensing and Regulation",
                         "https://www.tdlr.texas.gov/barbering-and-cosmetology/inspections-guide/",
                         "A person may not own, operate, or manage a barbering or cosmetology establishment unless the "
                         "person holds a license.", "Always"), SALES_TAX],
    "laundromat": [SPECIAL_WASTE("Laundromats inside Houston city limits; interceptors pumped every 90 days"),
                   lic("Fire permit - dry cleaning plants", "Houston Fire Department", FIRE_LIST, "Dry Cleaning Plants",
                       "Only if dry cleaning on site"), SALES_TAX],
    "pet_services": [lic("Commercial pet service facility license", "City of Houston - BARC",
                         "https://www.houstontx.gov/barc/pdfs/Commercial_Pet_Service_Facility_License_Application.pdf",
                         "REQUIRES ANY PERSON IN THE CITY CONDUCTING, MANAGING, OR MAINTAINING A COMMERCIAL PET FACILITY "
                         "SHALL OBTAIN A LICENSE.",
                         "If dogs or cats over four months old are kept on site for a commercial purpose (boarding, "
                         "daycare); renewed each calendar year"), SALES_TAX],
    "coworking": [FOOD_IF_SERVED | {"applies_when": "Only if selling or serving food, even free food for members"},
                  TABC_MEMBERS, SALES_TAX],
    "grocery": [FOOD_DEALER, SCALES, TABC_OFF, HOUSTON_ALCOHOL, TOBACCO, ECIG,
                FOG | {"applies_when": "If there is a deli or prepared-food kitchen"}, SALES_TAX],
    "convenience": [FOOD_IF_SERVED | {"applies_when": "Selling food, including packaged food"}, TABC_OFF, HOUSTON_ALCOHOL,
                    TOBACCO, ECIG, SCALES, FUEL_TANKS,
                    lic("Fire permit - motor vehicle fuel-dispensing stations", "Houston Fire Department", FIRE_LIST,
                        "Motor Vehicle Fuel-Dispensing Stations", "Only if dispensing fuel"), SALES_TAX],
    "auto_repair": [lic("Automotive repair facility (ARF) license", "City of Houston - Houston Police Department Auto "
                        "Dealers Detail", "https://www.houstontx.gov/police/auto_dealers_detail/automotive_repair_facility_faq.htm",
                        "you must be licensed in accordance with Chapter 8 of the City of Houston Code of Ordinances",
                        "Always inside Houston city limits"),
                    SPECIAL_WASTE("If a lube station or with an oil/water separator or grit trap"),
                    lic("Used-oil collection center registration", "Texas Commission on Environmental Quality",
                        "https://www.tceq.texas.gov/downloads/assistance/publications/rg-308.pdf",
                        "Your shop must register as a “used-oil-collection center” if the shop collects used oil "
                        "from other shops or from the public.", "Only if collecting used oil from others"),
                    lic("Scrap tire generator registration", "Texas Commission on Environmental Quality",
                        "https://www.tceq.texas.gov/downloads/assistance/publications/rg-308.pdf",
                        "You must register with the TCEQ as a scrap-tire generator if your shop routinely generates, "
                        "transports, or retails used or scrap tires", "Above the TCEQ storage thresholds"),
                    SALES_TAX],
    "car_wash": [SPECIAL_WASTE("Car washes inside Houston city limits; interceptors pumped every 90 days"),
                 lic("Sewer discharge approval", "Local wastewater utility (per TCEQ guidance)",
                     "https://www.tceq.texas.gov/downloads/assistance/publications/rg-434.pdf/@@download/file/rg-434.pdf",
                     "Call your local wastewater treatment facility for approval to discharge car wash water into their "
                     "sewer system.", "If discharging wash water to the sanitary sewer"), FUEL_TANKS, SALES_TAX],
    "self_storage": [lic("Mini-warehouse (own or operate) license", "City of Houston Administration & Regulatory Affairs",
                         "https://www.houstontx.gov/ara/rp/mini_warehouse.pdf",
                         "The Undersigned hereby makes application for a license to own or operate a mini-warehouse "
                         "within the City of Houston", "Always inside Houston city limits; expires December 31"),
                     SALES_TAX],
}

NOT_CONFIRMED = {
    "indoor_pickleball": "Whether court or membership fees fall under the Health Spa Act was not confirmed.",
    "yoga_pilates": "Whether a class-pack studio counts as a health spa was not confirmed.",
    "medical_clinic": "Texas Medical Board pages could not be opened; physician licensing is not listed here.",
    "physical_therapy": "Facility registration programs appear discontinued (2019); not confirmed on an official page.",
    "childcare": "The licensing threshold (number of children) was not confirmed on the page read.",
    "restaurant": "The certified food manager staffing rule was not found stated on an official page.",
}
