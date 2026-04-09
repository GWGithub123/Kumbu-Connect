"""
Creates 15 separate KoboToolbox forms (5 CBO types x 3 performance tiers),
deploys each, uploads realistic tiered data, then writes form_ids.json.

Tiers: high (large/growing), mid (patchy/flat), low (small/shrinking)
Types: tools, education, healthcare, agriculture, water
"""
import requests
import json
import random
import time
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

API_KEY = "7327ad2b882fb5d4975916811759dc339b266cd4"
KF_BASE = "https://kf.kobotoolbox.org/api/v2"
SUB_URL = "https://kc.kobotoolbox.org/submission"
HEADERS = {"Authorization": f"Token {API_KEY}"}

SURVEY_FIELDS = [
    {"type": "date",    "name": "date_loaned",           "label": "Date of activity"},
    {"type": "text",    "name": "time_loaned",            "label": "Time started"},
    {"type": "date",    "name": "date_returned",          "label": "Date returned/completed"},
    {"type": "text",    "name": "time_returned",          "label": "Time returned"},
    {"type": "text",    "name": "tool_name",              "label": "Item / service / resource"},
    {"type": "text",    "name": "borrower_name",          "label": "Beneficiary name"},
    {"type": "text",    "name": "borrower_signature",     "label": "Beneficiary signature"},
    {"type": "text",    "name": "condition_upon_return",  "label": "Condition / outcome"},
    {"type": "text",    "name": "damage_charged",         "label": "Fee / damage charged"},
    {"type": "text",    "name": "return_notes",           "label": "Notes"},
    {"type": "text",    "name": "serial_number",          "label": "Reference number"},
    {"type": "integer", "name": "quantity",               "label": "Quantity"},
]

CBO_DATA = {
    "tools": {
        "items": [
            "Hammer", "Drill", "Circular Saw", "Wheelbarrow", "Hoe", "Plough",
            "Water Pump", "Sprayer", "Ladder", "Rake", "Shovel", "Machete",
            "Wrench Set", "Pliers", "Level", "Sander", "Pickaxe", "Garden Hoe",
        ],
        "borrowers": [
            "Jesse Artache", "Isaac Simkin", "Lydia Anwor", "Danek Obriondo",
            "Derek Obriondo", "Clement Jackson", "Maria Rodriguez", "John Thompson",
            "Sarah Williams", "Michael Chen", "James Omondi", "Grace Wanjiru",
            "Peter Kamau", "Agnes Nyambura", "Daniel Kipchoge",
        ],
    },
    "education": {
        "items": [
            "Math Textbook", "Science Workbook", "Dictionary", "Calculator",
            "Tablet Device", "Tutoring Session (2hr)", "Group Study Room",
            "Exam Prep Kit", "STEM Kit", "Writing Workshop", "Online Course Access",
            "Library Card", "Laptop", "Reading Lamp", "Study Desk",
        ],
        "borrowers": [
            "Amina Hassan", "David Kiprop", "Grace Njeri", "Samuel Odhiambo",
            "Faith Wanjiku", "Kevin Mutua", "Lucy Achieng", "Peter Kamande",
            "Rebecca Mwikali", "Joseph Kimani", "Eunice Nyambura", "Brian Otieno",
            "Catherine Wairimu", "Dennis Karanja", "Mary Adhiambo",
        ],
    },
    "healthcare": {
        "items": [
            "First Aid Kit", "Blood Pressure Monitor", "Thermometer", "Wheelchair",
            "Crutches", "Glucose Monitor", "Nebulizer", "Health Screening",
            "Nutrition Consultation", "Mental Health Session", "Prenatal Care Visit",
            "Vaccination", "Eye Exam", "Dental Checkup", "Wound Dressing Kit",
        ],
        "borrowers": [
            "Jane Muthoni", "John Omondi", "Sarah Chebet", "Michael Wanjala",
            "Patricia Auma", "Daniel Kipchoge", "Agnes Wambui", "Simon Mutiso",
            "Ruth Awuor", "Francis Kiprotich", "Elizabeth Njoki", "George Onyango",
            "Nancy Wangari", "Paul Kimutai", "Helen Akinyi",
        ],
    },
    "agriculture": {
        "items": [
            "Maize Seeds 5kg", "Bean Seeds 2kg", "Fertilizer 10kg", "Pesticide Spray",
            "Hand Tiller", "Irrigation Hose", "Greenhouse Film", "Pruning Shears",
            "Soil Testing Kit", "Crop Rotation Training", "Organic Farming Training",
            "Water Pump", "Spade", "Hoe", "Wheelbarrow",
        ],
        "borrowers": [
            "James Karanja", "Rose Wanjiru", "Thomas Kiplagat", "Alice Nyokabi",
            "William Kibet", "Margaret Mumbua", "Charles Ochieng", "Susan Waithera",
            "Daniel Koech", "Florence Njambi", "Peter Rotich", "Jane Wambui",
            "David Kiptoo", "Mary Wangui", "Stephen Kiprono",
        ],
    },
    "water": {
        "items": [
            "Water Filter", "20L Jerry Can", "Water Testing Kit", "Hand Pump Repair",
            "Latrine Construction", "Hand Washing Station", "Soap Distribution",
            "Hygiene Training", "Rainwater Harvesting Tank", "Storage Tank 500L",
            "PVC Pipes 10m", "Tap Installation", "Community Water Point",
            "Borehole Maintenance", "Water Quality Test",
        ],
        "borrowers": [
            "Joseph Macharia", "Anne Njoroge", "Patrick Mwangi", "Lucy Wanjiku",
            "Robert Kimani", "Grace Nyambura", "Samuel Gitau", "Esther Wangari",
            "Francis Kamau", "Betty Njeri", "Anthony Kuria", "Martha Wambui",
            "Isaac Maina", "Purity Mwangi", "Joshua Kamande",
        ],
    },
}

# months, start_base transactions/month, monthly growth rate, data-gap prob, borrower pool fraction
TIERS = {
    "high": {"months": 12, "start_base": 17, "growth":  0.12, "gap": 0.04, "pool": 1.0},
    "mid":  {"months":  8, "start_base": 12, "growth":  0.08, "gap": 0.22, "pool": 0.6},
    "low":  {"months":  6, "start_base":  9, "growth": -0.18, "gap": 0.38, "pool": 0.35},
}

# (display name, slug)  — order matches high / mid / low
CBO_NAMES = {
    "tools": [
        ("Busia Community Tool Hub",     "busia-community-tool-hub"),
        ("Kakamega Farm Collective",      "kakamega-farm-collective"),
        ("Siaya Tool Share CBO",          "siaya-tool-share"),
    ],
    "education": [
        ("Bright Futures Education CBO",  "bright-futures-education"),
        ("Westlands Learning Circle",     "westlands-learning-circle"),
        ("Kibera Homework Club",          "kibera-homework-club"),
    ],
    "healthcare": [
        ("Health For All CBO",            "health-for-all"),
        ("Mombasa Wellness Network",      "mombasa-wellness-network"),
        ("Vihiga Clinic Aid CBO",         "vihiga-clinic-aid"),
    ],
    "agriculture": [
        ("Green Harvest Agriculture CBO", "green-harvest-agriculture"),
        ("Rift Valley Growers CBO",       "rift-valley-growers"),
        ("Kisii Smallholder CBO",         "kisii-smallholder"),
    ],
    "water": [
        ("Clean Water Initiative CBO",    "clean-water-initiative"),
        ("Turkana Water Access CBO",      "turkana-water-access"),
        ("Kwale Springs CBO",             "kwale-springs"),
    ],
}

LOCATION_MAP = {
    "tools":       ["Busia County, Kenya",      "Kakamega County, Kenya",    "Siaya County, Kenya"],
    "education":   ["Nairobi County, Kenya",    "Nairobi County, Kenya",     "Nairobi County, Kenya"],
    "healthcare":  ["Kisumu County, Kenya",     "Mombasa County, Kenya",     "Vihiga County, Kenya"],
    "agriculture": ["Nakuru County, Kenya",     "Uasin Gishu County, Kenya", "Kisii County, Kenya"],
    "water":       ["Machakos County, Kenya",   "Turkana County, Kenya",     "Kwale County, Kenya"],
}

CONDITIONS_GOOD = ["Good condition upon return", "Excellent condition", "Well maintained", "Returned clean"]
CONDITIONS_MID  = ["Minor wear, acceptable", "Needs cleaning", "Small scratch noted"]
CONDITIONS_BAD  = ["Needs repair", "Damaged", "Missing component", "Poor condition"]
DAMAGES         = ["", "", "", "No charge", "$5 cleaning", "$10 minor repair", "$15 damage"]

DURATION = {
    "tools":       (1, 14),
    "education":   (7, 28),
    "healthcare":  (1,  7),
    "agriculture": (7, 30),
    "water":       (1, 30),
}


def make_records(cbo_type, tier_key, prefix):
    t = TIERS[tier_key]
    d = CBO_DATA[cbo_type]
    pool_size = max(3, int(len(d["borrowers"]) * t["pool"]))
    pool = d["borrowers"][:pool_size]
    records = []
    now = datetime.now()
    counter = random.randint(2000, 8000)
    lo, hi = DURATION[cbo_type]

    for month in range(t["months"]):
        count = max(1, int(t["start_base"] * ((1 + t["growth"]) ** month)))
        month_start = now - timedelta(days=30 * (t["months"] - month))

        for _ in range(count):
            if random.random() < t["gap"] * 0.4:
                counter += 1
                continue

            def maybe(val, gap=t["gap"]):
                return val if random.random() > gap else ""

            loan_dt = month_start + timedelta(days=random.randint(0, 27))
            ret_dt  = loan_dt + timedelta(days=random.randint(lo, hi))
            borrower = random.choice(pool)

            if tier_key == "high":
                cond = random.choice(CONDITIONS_GOOD)
            elif tier_key == "mid":
                cond = random.choice(CONDITIONS_GOOD + CONDITIONS_MID)
            else:
                cond = random.choice(CONDITIONS_MID + CONDITIONS_BAD)

            minutes = ["00", "15", "30", "45"]
            records.append({
                "date_loaned":           loan_dt.strftime("%Y-%m-%d"),
                "time_loaned":           maybe(f"{random.randint(7, 17):02d}:{random.choice(minutes)}"),
                "date_returned":         ret_dt.strftime("%Y-%m-%d"),
                "time_returned":         maybe(f"{random.randint(7, 17):02d}:{random.choice(minutes)}"),
                "tool_name":             random.choice(d["items"]),
                "borrower_name":         borrower,
                "borrower_signature":    maybe(borrower.split()[0]),
                "condition_upon_return": maybe(cond),
                "damage_charged":        maybe(random.choice(DAMAGES)),
                "return_notes":          maybe(random.choice(["On time", "Late", "Good", "Repeat", "", ""])),
                "serial_number":         f"{prefix}-{counter}",
                "quantity":              str(random.randint(1, 3 if tier_key == "high" else 2)),
            })
            counter += 1
    return records


def upload_record(record, uid):
    root = ET.Element("data", id=uid)
    for k, v in record.items():
        el = ET.SubElement(root, k)
        el.text = str(v)
    meta = ET.SubElement(root, "meta")
    iid = ET.SubElement(meta, "instanceID")
    iid.text = f"uuid:{record['serial_number']}-{random.randint(10000, 99999)}"
    xml_bytes = ET.tostring(root, encoding="utf-8", method="xml")
    try:
        r = requests.post(
            SUB_URL,
            headers=HEADERS,
            files={"xml_submission_file": ("submission.xml", xml_bytes, "text/xml")},
            timeout=30,
        )
        return r.status_code in (200, 201, 202)
    except Exception:
        return False


def create_and_deploy_form(name):
    payload = {
        "name": name,
        "asset_type": "survey",
        "content": json.dumps({"survey": SURVEY_FIELDS, "settings": [{}]}),
    }
    r = requests.post(f"{KF_BASE}/assets/", headers=HEADERS, data=payload, timeout=30)
    r.raise_for_status()
    uid = r.json()["uid"]
    dep = requests.post(
        f"{KF_BASE}/assets/{uid}/deployment/",
        headers=HEADERS,
        json={"backend": "kobocat", "active": True},
        timeout=30,
    )
    dep.raise_for_status()
    return uid


def main():
    # Forms already created with data uploaded — skip creation AND upload for all of these
    EXISTING = {
        "busia-community-tool-hub":    "anbM2Z9ECM85NdMiURVBQE",  # 285 subs (tools/high)
        "kakamega-farm-collective":    "a7UnyaSzP9dbwuSkVHqWkY",  # 110 subs (tools/mid)
        "siaya-tool-share":            "a6eL2HKxpdMdPNJ52BemuV",  #  29 subs (tools/low)
        "bright-futures-education":    "aYk5Ft2KgzKfYQ9paBwohJ",  # 420 subs (education/high)
        "westlands-learning-circle":   "ahSga2Cf8UbTa7xD2ErAet",  # 107 subs (education/mid)
        "kibera-homework-club":        "aqU2oEQ8r64tUkh6MC6mBg",  #  27 subs (education/low)
        "health-for-all":              "anvxtsE6RZHyWidkARQsJ6",  # 395 subs (healthcare/high)
        "mombasa-wellness-network":    "aoUDmsxXR6eWngfayDnzaR",  # 111 subs (healthcare/mid)
        "vihiga-clinic-aid":           "aJmDgWnUkoedcGL4JVMTtt",  #  27 subs (healthcare/low)
        "green-harvest-agriculture":   "aUq4yxrjyuD29swHNBvYbW",  # 492 subs (agriculture/high)
    }
    results = []
    tier_order = ["high", "mid", "low"]

    for cbo_type, name_pairs in CBO_NAMES.items():
        for i, (name, slug) in enumerate(name_pairs):
            tier_key = tier_order[i]

            print(f"\n{'=' * 65}")
            print(f"  {name}")
            print(f"  Type: {cbo_type}  |  Tier: {tier_key}  |  Slug: {slug}")
            print(f"{'=' * 65}")

            if slug in EXISTING:
                uid = EXISTING[slug]
                print(f"  Reusing existing form  uid={uid}")
                skip_upload = True
            else:
                print(f"  Creating new form...")
                uid = create_and_deploy_form(name)
                print(f"  Created & deployed  uid={uid}")
                skip_upload = False
                time.sleep(1.5)

            if not skip_upload:
                prefix = f"{cbo_type[:3].upper()}{tier_key[0].upper()}"
                records = make_records(cbo_type, tier_key, prefix)
                print(f"  Uploading {len(records)} records...")
                ok = fail = consec_fail = 0
                for j, rec in enumerate(records, 1):
                    if upload_record(rec, uid):
                        ok += 1
                        consec_fail = 0
                    else:
                        fail += 1
                        consec_fail += 1
                    if consec_fail >= 5:
                        time.sleep(3)
                        consec_fail = 0
                    if j % 50 == 0:
                        print(f"     {j}/{len(records)}  ok={ok}  fail={fail}")
                print(f"  Done: {ok} uploaded, {fail} failed")

            location = LOCATION_MAP[cbo_type][i]
            results.append({
                "name":          name,
                "slug":          slug,
                "kobo_asset_id": uid,
                "cbo_type":      cbo_type,
                "tier":          tier_key,
                "location":      location,
            })

    with open("form_ids.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n\n" + "=" * 65)
    print("ALL FORMS CREATED — form_ids.json saved")
    print("=" * 65)
    for r in results:
        print(f"  {r['name']:<42}  {r['kobo_asset_id']}")
    print("\nNext step:  python3 auto_seed.py")


if __name__ == "__main__":
    main()
