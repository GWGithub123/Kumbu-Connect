"""
Reads form_ids.json (produced by create_all_cbo_forms.py) and seeds the database
with all 15 CBOs plus demo user accounts.
"""
import sys
import os
import json

sys.path.insert(0, os.path.abspath("."))

from webapp import create_app
from webapp.models import db, User, CBO

DATA_FILE = "form_ids.json"

FOCUS_BY_TYPE = {
    "tools":       "Tool Lending & Community Equipment Access",
    "education":   "Education & Youth Development",
    "healthcare":  "Primary Healthcare & Community Wellness",
    "agriculture": "Smallholder Agriculture & Food Security",
    "water":       "Water, Sanitation & Hygiene (WASH)",
}

CHAIRS = {
    "tools":       ["Samuel Odhiambo", "Grace Wanjiku", "Peter Kamau"],
    "education":   ["Dr. Amina Hassan", "Kevin Mutua", "Mary Adhiambo"],
    "healthcare":  ["Dr. Jane Muthoni", "John Omondi", "Agnes Wambui"],
    "agriculture": ["James Karanja", "Rose Wanjiru", "Daniel Koech"],
    "water":       ["Joseph Macharia", "Anne Njoroge", "Isaac Maina"],
}

DIRECTORS = {
    "tools":       ["Faith Achieng", "Brian Otieno", "Lucy Siaya"],
    "education":   ["Rebecca Mwikali", "Dennis Karanja", "Catherine Wairimu"],
    "healthcare":  ["Francis Kiprotich", "Elizabeth Njoki", "Simon Mutiso"],
    "agriculture": ["Thomas Kiplagat", "William Kibet", "Florence Njambi"],
    "water":       ["Patrick Mwangi", "Grace Nyambura", "Betty Njeri"],
}

FINANCE = {
    "tools":       ["Isaac Simkin", "Maria Rodriguez", "Daniel Kipchoge"],
    "education":   ["Samuel Odhiambo", "Brian Otieno", "Joseph Kimani"],
    "healthcare":  ["George Onyango", "Nancy Wangari", "Paul Kimutai"],
    "agriculture": ["Alice Nyokabi", "Charles Ochieng", "Stephen Kiprono"],
    "water":       ["Robert Kimani", "Samuel Gitau", "Joshua Kamande"],
}

FOUNDED = {
    "high": 2016,
    "mid":  2019,
    "low":  2021,
}

app = create_app()

with app.app_context():
    if not os.path.exists(DATA_FILE):
        print(f"ERROR: {DATA_FILE} not found. Run create_all_cbo_forms.py first.")
        sys.exit(1)

    with open(DATA_FILE) as f:
        cbos_data = json.load(f)

    # Reset database
    db.drop_all()
    db.create_all()
    print("Database reset")

    # Demo funder account
    funder = User(email="funder@demo.com", role="funder", display_name="Impact Fund Manager")
    funder.set_password("password")
    db.session.add(funder)
    db.session.flush()
    print("Created funder@demo.com")

    primary_cbo_user_created = False
    tier_index = {"tools": 0, "education": 0, "healthcare": 0, "agriculture": 0, "water": 0}

    for entry in cbos_data:
        cbo_type = entry["cbo_type"]
        tier     = entry["tier"]
        idx      = tier_index[cbo_type]
        tier_index[cbo_type] += 1

        cbo = CBO(
            name             = entry["name"],
            slug             = entry["slug"],
            kobo_asset_id    = entry["kobo_asset_id"],
            cbo_identifier   = cbo_type,
            location         = entry["location"],
            founded_year     = FOUNDED[tier],
            focus_areas      = FOCUS_BY_TYPE[cbo_type],
            chairperson      = CHAIRS[cbo_type][idx],
            program_director = DIRECTORS[cbo_type][idx],
            finance_lead     = FINANCE[cbo_type][idx],
            org_type         = "Community-Based Organisation (CBO)",
        )
        db.session.add(cbo)
        db.session.flush()
        print(f"  CBO: {cbo.name:<42}  {entry['kobo_asset_id']}")

        # CBO login for each high-tier organisation
        if tier == "high":
            slug_email = f"{entry['slug']}@demo.com"
            u = User(
                email        = slug_email,
                role         = "cbo",
                display_name = CHAIRS[cbo_type][idx],
                cbo_id       = cbo.id,
            )
            u.set_password("password")
            db.session.add(u)

            if not primary_cbo_user_created:
                primary = User(
                    email        = "cbo@demo.com",
                    role         = "cbo",
                    display_name = CHAIRS[cbo_type][idx],
                    cbo_id       = cbo.id,
                )
                primary.set_password("password")
                db.session.add(primary)
                primary_cbo_user_created = True

    db.session.commit()
    print(f"\nSeeded {len(cbos_data)} CBOs successfully")
    print("  funder@demo.com / password")
    print("  cbo@demo.com    / password")
    print("\nNext step:  python3 sync_all_cbos.py")
