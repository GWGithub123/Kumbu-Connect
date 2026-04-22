"""
seed.py — Populate the database with demo accounts so you can
immediately log in as either a Funder or a CBO.

Run once:
    python seed.py
"""
import json
from pathlib import Path
from webapp import create_app
from webapp.models import db, User, CBO

app = create_app()


def _load_kobo_asset_ids() -> dict[str, str]:
    form_ids_path = Path(__file__).with_name('form_ids.json')
    if not form_ids_path.exists():
        return {}

    try:
        payload = json.loads(form_ids_path.read_text())
    except Exception:
        return {}

    asset_ids: dict[str, str] = {}
    for item in payload:
        slug = str(item.get('slug') or '').strip().lower()
        asset_id = str(item.get('kobo_asset_id') or '').strip()
        if slug and asset_id:
            asset_ids[slug] = asset_id
    return asset_ids


KOBO_ASSET_IDS = _load_kobo_asset_ids()


def _seed_asset_id(slug: str, fallback: str) -> str:
    return KOBO_ASSET_IDS.get(slug, fallback)

with app.app_context():
    # Avoid duplicates
    if User.query.first():
        print("⚠  Database already seeded. Delete webapp/instance/kumbu.db to reset.")
        exit(0)

    # ── 1. Create a demo CBO ─────────────────────────────────────
    cbo = CBO(
        name="Busia Community Tool Hub",
        slug="busia-community-tool-hub",
        kobo_asset_id=_seed_asset_id("busia-community-tool-hub", "aJ7GEDZPU3dbM4KAEqUBKW"),
        cbo_identifier="tools",
        location="Busia County, Kenya",
        county_region="Busia County",
        founded_year="2023",
        focus_areas="Rural livelihood, subsistence agriculture, youth empowerment",
        chairperson="Jesse Artache",
        program_director="Isaac Simkin",
        finance_lead="Lydia Anwor",
    )
    db.session.add(cbo)
    db.session.flush()

    # ── 2. Create a second demo CBO ──────────────────────────────
    cbo2 = CBO(
        name="Kakamega Farm Collective",
        slug="kakamega-farm-collective",
        kobo_asset_id=_seed_asset_id("kakamega-farm-collective", "aJ7GEDZPU3dbM4KAEqUBKW"),
        cbo_identifier="tools",
        location="Kakamega County, Kenya",
        county_region="Kakamega County",
        founded_year="2024",
        focus_areas="Smallholder agriculture, women's empowerment, sustainable farming",
        chairperson="Maria Rodriguez",
        program_director="Clement Jackson",
        finance_lead="Patricia Davis",
    )
    db.session.add(cbo2)
    db.session.flush()

    # ── 3. Education CBO ─────────────────────────────────────────
    cbo3 = CBO(
        name="Bright Futures Education CBO",
        slug="bright-futures-education",
        kobo_asset_id=_seed_asset_id("bright-futures-education", "aJ7GEDZPU3dbM4KAEqUBKW"),
        cbo_identifier="education",
        location="Nairobi County, Kenya",
        county_region="Nairobi County",
        founded_year="2023",
        focus_areas="Education access, tutoring, learning materials, youth development",
        chairperson="Amina Hassan",
        program_director="David Kiprop",
        finance_lead="Grace Njeri",
    )
    db.session.add(cbo3)
    db.session.flush()

    # ── 4. Healthcare CBO ────────────────────────────────────────
    cbo4 = CBO(
        name="Health For All CBO",
        slug="health-for-all",
        kobo_asset_id=_seed_asset_id("health-for-all", "aJ7GEDZPU3dbM4KAEqUBKW"),
        cbo_identifier="healthcare",
        location="Kisumu County, Kenya",
        county_region="Kisumu County",
        founded_year="2022",
        focus_areas="Primary healthcare, medical supplies, wellness programs, maternal health",
        chairperson="Jane Muthoni",
        program_director="Dr. John Omondi",
        finance_lead="Sarah Chebet",
    )
    db.session.add(cbo4)
    db.session.flush()

    # ── 5. Agriculture CBO ───────────────────────────────────────
    cbo5 = CBO(
        name="Green Harvest Agriculture CBO",
        slug="green-harvest-agriculture",
        kobo_asset_id=_seed_asset_id("green-harvest-agriculture", "aJ7GEDZPU3dbM4KAEqUBKW"),
        cbo_identifier="agriculture",
        location="Nakuru County, Kenya",
        county_region="Nakuru County",
        founded_year="2023",
        focus_areas="Sustainable farming, seeds distribution, agricultural training, food security",
        chairperson="James Karanja",
        program_director="Rose Wanjiru",
        finance_lead="Thomas Kiplagat",
    )
    db.session.add(cbo5)
    db.session.flush()

    # ── 6. Water & Sanitation CBO ────────────────────────────────
    cbo6 = CBO(
        name="Clean Water Initiative CBO",
        slug="clean-water-initiative",
        kobo_asset_id=_seed_asset_id("clean-water-initiative", "aJ7GEDZPU3dbM4KAEqUBKW"),
        cbo_identifier="water",
        location="Machakos County, Kenya",
        county_region="Machakos County",
        founded_year="2024",
        focus_areas="Clean water access, sanitation facilities, hygiene education, community health",
        chairperson="Joseph Macharia",
        program_director="Anne Njoroge",
        finance_lead="Patrick Mwangi",
    )
    db.session.add(cbo6)
    db.session.flush()

    # ── 7. CBO user accounts ────────────────────────────────────
    cbo_user = User(
        email="cbo@demo.com",
        role="cbo",
        display_name="Jesse Artache",
        cbo_id=cbo.id,
    )
    cbo_user.set_password("password")
    db.session.add(cbo_user)

    cbo_user2 = User(
        email="cbo2@demo.com",
        role="cbo",
        display_name="Maria Rodriguez",
        cbo_id=cbo2.id,
    )
    cbo_user2.set_password("password")
    db.session.add(cbo_user2)

    # ── 4. Funder user account ──────────────────────────────────
    funder = User(
        email="funder@demo.com",
        role="funder",
        display_name="Impact Fund Manager",
    )
    funder.set_password("password")
    db.session.add(funder)

    db.session.commit()
    print("✅ Database seeded successfully!")
    print()
    print("   Funder login:  funder@demo.com / password")
    print("   CBO login:     cbo@demo.com    / password")
    print("   CBO 2 login:   cbo2@demo.com   / password")
