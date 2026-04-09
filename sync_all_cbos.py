"""
Sync all 6 CBOs with their KoboToolbox data.
Pulls live data, generates AI profiles, and computes growth metrics.
"""
import sys
import os
sys.path.insert(0, os.path.abspath('.'))

from webapp import create_app
from webapp.models import db, CBO
from webapp.kobo_service import fetch_kobo_submissions
from webapp.gemini_service import analyse_kobo_data, compute_growth_metrics, compute_data_quality_badge
from datetime import datetime
import json

def sync_cbo(cbo):
    """Sync a single CBO with its KoboToolbox data."""
    print(f"\n{'=' * 70}")
    print(f"📊 Syncing: {cbo.name}")
    print(f"{'=' * 70}")

    if not cbo.has_kobo_connection:
        print("  ↷ Skipping: KoboToolbox API connection is disconnected or incomplete")
        return True
    
    try:
        # 1. Fetch live data from KoboToolbox
        print("  ⏳ Fetching data from KoboToolbox...")
        asset_id = cbo.kobo_asset_id
        submissions = fetch_kobo_submissions(asset_id)
        print(f"  ✓ Retrieved {len(submissions)} submissions")
        
        # 2. Cache raw data
        cbo.raw_kobo_json = json.dumps(submissions, default=str)
        
        # 3. Generate AI profile
        print("  ⏳ Analyzing with Gemini AI...")
        profile = analyse_kobo_data(submissions, cbo_name=cbo.name)
        cbo.ai_profile_json = json.dumps(profile, default=str)
        
        # Apply profile fields
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
        
        print(f"  ✓ AI profile generated")
        
        # 4. Compute growth metrics
        print("  ⏳ Computing growth metrics...")
        growth_data = compute_growth_metrics(submissions)
        cbo.growth_metrics_json = json.dumps(growth_data, default=str)
        print(f"  ✓ Generated {len(growth_data)} months of growth data")
        
        # 5. Badge, classifications, social impact score
        cbo.data_quality_badge   = compute_data_quality_badge(submissions)
        cbo.classifications_json = json.dumps(profile.get('classifications', [cbo.cbo_identifier or 'community']))
        cbo.social_impact_score  = int(profile.get('social_impact_score', 0) or 0)
        print(f"  ✓ Badge: {cbo.data_quality_badge} | Score: {cbo.social_impact_score} | Classes: {cbo.classifications_json}")
        
        # 6. Save
        cbo.last_synced = datetime.utcnow()
        db.session.commit()
        
        print(f"  ✅ {cbo.name} synced successfully!")
        return True
        
    except Exception as e:
        print(f"  ❌ Error syncing {cbo.name}: {e}")
        db.session.rollback()
        return False


def main():
    app = create_app()
    
    with app.app_context():
        print("=" * 70)
        print("SYNCING ALL CBOs WITH KOBO TOOLBOX DATA")
        print("=" * 70)
        
        cbos = CBO.query.all()
        print(f"\nFound {len(cbos)} CBOs to sync:")
        for cbo in cbos:
            print(f"  • {cbo.name} ({cbo.slug})")
        
        successful = 0
        failed = 0
        
        for cbo in cbos:
            if sync_cbo(cbo):
                successful += 1
            else:
                failed += 1
        
        print("\n" + "=" * 70)
        print("SYNC COMPLETE")
        print("=" * 70)
        print(f"✅ Successfully synced: {successful}/{len(cbos)}")
        if failed > 0:
            print(f"❌ Failed: {failed}")
        print("\n🎉 Your marketplace is now populated with diverse CBO profiles!")
        print("   Visit http://127.0.0.1:5000 to view the marketplace")
        print("=" * 70)


if __name__ == '__main__':
    main()
