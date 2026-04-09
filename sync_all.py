"""
sync_all.py — Pull live KoboToolbox data for every CBO and
run it through Gemini to generate their profiles.

    python3 sync_all.py
"""
import json
from datetime import datetime
from webapp import create_app
from webapp.models import db, CBO
from webapp.kobo_service import fetch_kobo_submissions
from webapp.gemini_service import analyse_kobo_data

app = create_app()

with app.app_context():
    cbos = CBO.query.all()
    if not cbos:
        print("No CBOs in database. Run seed.py first.")
        exit(1)

    for cbo in cbos:
        print(f"\n{'='*60}")
        print(f"Syncing: {cbo.name}")
        print(f"{'='*60}")

        if not cbo.has_kobo_connection:
            print("  ↷ Skipping: KoboToolbox API connection is disconnected or incomplete")
            continue

        try:
            # 1. Pull live data from KoboToolbox
            asset_id = cbo.kobo_asset_id
            print(f"  → Fetching submissions from asset {asset_id}…")
            submissions = fetch_kobo_submissions(asset_id)
            print(f"  → Got {len(submissions)} submissions")

            # 2. Cache raw data
            cbo.raw_kobo_json = json.dumps(submissions, default=str)

            # 3. Send to Gemini for AI analysis
            print(f"  → Sending to Gemini for analysis…")
            profile = analyse_kobo_data(submissions, cbo_name=cbo.name)
            print(f"  → AI profile generated!")

            # 4. Save
            cbo.ai_profile_json = json.dumps(profile, default=str)

            # Apply fields
            cbo.location = profile.get('location', cbo.location)
            cbo.founded_year = profile.get('founded_year', cbo.founded_year)
            cbo.focus_areas = profile.get('focus_areas', cbo.focus_areas)
            cbo.org_type = profile.get('org_type', cbo.org_type)

            leadership = profile.get('leadership', {})
            cbo.chairperson = leadership.get('chairperson', cbo.chairperson)
            cbo.program_director = leadership.get('program_director', cbo.program_director)
            cbo.finance_lead = leadership.get('finance_lead', cbo.finance_lead)

            cbo.impact_json = json.dumps(profile.get('quantified_impact', []))
            fp = profile.get('flagship_project', {})
            cbo.flagship_summary = json.dumps(fp) if fp else cbo.flagship_summary
            ss = profile.get('success_story', {})
            cbo.success_story = json.dumps(ss) if ss else cbo.success_story
            cbo.join_us_text = profile.get('join_us', cbo.join_us_text)

            cbo.last_synced = datetime.utcnow()
            db.session.commit()

            print(f"  ✅ {cbo.name} synced successfully!")
            print(f"     Impact items: {len(profile.get('quantified_impact', []))}")
            print(f"     Flagship: {profile.get('flagship_project', {}).get('title', '—')}")

        except Exception as e:
            db.session.rollback()
            print(f"  ❌ Failed: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print("Done! Refresh the marketplace to see live data.")
