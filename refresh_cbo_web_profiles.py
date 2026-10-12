"""Refresh stale CBO public-web research used to keep profiles current."""
from webapp import create_app
from webapp.cbo_web_research_service import refresh_stale_cbo_web_profiles


app = create_app()


if __name__ == '__main__':
    with app.app_context():
        result = refresh_stale_cbo_web_profiles(limit=50, force=False)
        print(
            f"Refreshed {result['refreshed']} CBO public-source profiles, "
            f"skipped {result['skipped']}, evaluated {result['evaluated']}."
        )
        for error in result.get('errors') or []:
            print(f'Error: {error}')
