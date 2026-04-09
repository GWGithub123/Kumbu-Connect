"""
Kumbu Connect — Flask application factory.
"""
import json
import threading
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import inspect, text
from .config import Config
from .models import db, User


def _background_sync(app):
    """Sync any unsynced CBOs in a background thread after startup."""
    import time
    time.sleep(3)  # let the server fully start first
    with app.app_context():
        from .models import CBO
        from .kobo_service import fetch_kobo_submissions
        from .gemini_service import analyse_kobo_data, compute_growth_metrics, compute_data_quality_badge
        from .maps_service import ensure_cbo_geocoded
        from datetime import datetime
        import json

        unsynced = CBO.query.filter(CBO.last_synced == None).all()
        syncable_cbos = [cbo for cbo in unsynced if cbo.has_kobo_connection]
        if not syncable_cbos:
            print("[startup] All CBOs already synced — skipping background sync.")
            return

        print(f"[startup] Auto-syncing {len(syncable_cbos)} unsynced CBO(s) in background...")
        for cbo in syncable_cbos:
            for attempt in range(3):
                try:
                    submissions = fetch_kobo_submissions(cbo.kobo_asset_id)
                    cbo.raw_kobo_json = json.dumps(submissions, default=str)
                    profile = analyse_kobo_data(submissions, cbo_name=cbo.name)
                    cbo.ai_profile_json = json.dumps(profile, default=str)
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
                    growth_data = compute_growth_metrics(submissions)
                    cbo.growth_metrics_json = json.dumps(growth_data, default=str)
                    # Badge, classifications, social impact score
                    cbo.data_quality_badge = compute_data_quality_badge(submissions)
                    import json as _json
                    cbo.classifications_json = _json.dumps(profile.get('classifications', [cbo.cbo_identifier or 'community']))
                    cbo.social_impact_score = int(profile.get('social_impact_score', 0) or 0)
                    ensure_cbo_geocoded(cbo, profile=profile)
                    cbo.last_synced = datetime.utcnow()
                    db.session.commit()
                    print(f"[startup] ✓ Synced: {cbo.name}")
                    break
                except Exception as e:
                    db.session.rollback()
                    if attempt < 2:
                        print(f"[startup] ↻ Retrying {cbo.name} (attempt {attempt+2}/3)...")
                        time.sleep(5)
                    else:
                        print(f"[startup] ✗ Failed: {cbo.name} — {e}")


# ── Jinja helpers ─────────────────────────────────────────────────
ICON_MAP = {
    'clock':    'clock',
    'farm':     'tractor',
    'chart-up': 'chart-line',
    'people':   'users',
    'tools':    'tools',
    'money':    'coins',
    'check':    'check-circle',
    'leaf':     'leaf',
    'hands':    'hands-helping',
    'globe':    'globe-africa',
}


def _icon(hint: str) -> str:
    """Map an icon_hint from Gemini to a Font-Awesome icon name."""
    return ICON_MAP.get(hint, hint)


def _safe_json_inline(text: str) -> dict:
    """Parse a JSON string, returning {} on failure."""
    try:
        return json.loads(text or '{}')
    except (json.JSONDecodeError, TypeError):
        return {}


def _from_json_list(text: str) -> list:
    """Jinja filter: parse a JSON array string, returning [] on failure."""
    try:
        val = json.loads(text or '[]')
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _apply_sqlite_compat_migrations():
    """Patch older SQLite databases that predate newer SQLAlchemy models."""
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if 'cbos' not in table_names:
        return

    cbo_columns = {column['name'] for column in inspector.get_columns('cbos')}
    required_columns = {
        'growth_metrics_json': "ALTER TABLE cbos ADD COLUMN growth_metrics_json TEXT DEFAULT '[]'",
        'classifications_json': "ALTER TABLE cbos ADD COLUMN classifications_json TEXT DEFAULT '[]'",
        'data_quality_badge': "ALTER TABLE cbos ADD COLUMN data_quality_badge VARCHAR(10) DEFAULT ''",
        'social_impact_score': "ALTER TABLE cbos ADD COLUMN social_impact_score INTEGER DEFAULT 0",
        'kobo_connection_active': 'ALTER TABLE cbos ADD COLUMN kobo_connection_active BOOLEAN DEFAULT 1',
        'kobo_disconnected_at': 'ALTER TABLE cbos ADD COLUMN kobo_disconnected_at DATETIME',
        'street_address': "ALTER TABLE cbos ADD COLUMN street_address VARCHAR(255) DEFAULT ''",
        'formatted_address': "ALTER TABLE cbos ADD COLUMN formatted_address VARCHAR(255) DEFAULT ''",
        'latitude': 'ALTER TABLE cbos ADD COLUMN latitude REAL',
        'longitude': 'ALTER TABLE cbos ADD COLUMN longitude REAL',
        'geocode_query': "ALTER TABLE cbos ADD COLUMN geocode_query VARCHAR(255) DEFAULT ''",
        'geocoded_at': 'ALTER TABLE cbos ADD COLUMN geocoded_at DATETIME',
        'place_id': "ALTER TABLE cbos ADD COLUMN place_id VARCHAR(255) DEFAULT ''",
        'sms_keyword': 'ALTER TABLE cbos ADD COLUMN sms_keyword VARCHAR(50)',
        'community_prompt': "ALTER TABLE cbos ADD COLUMN community_prompt TEXT DEFAULT ''",
        'community_feedback_enabled': 'ALTER TABLE cbos ADD COLUMN community_feedback_enabled BOOLEAN DEFAULT 1',
    }

    with db.engine.begin() as conn:
        for column_name, statement in required_columns.items():
            if column_name not in cbo_columns:
                conn.execute(text(statement))

        conn.execute(text(
            'UPDATE cbos SET kobo_connection_active = 1 WHERE kobo_connection_active IS NULL'
        ))

        conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_cbos_sms_keyword ON cbos (sms_keyword) WHERE sms_keyword IS NOT NULL'
        ))


def create_app():
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static',
    )
    app.config.from_object(Config)

    # ── Register Jinja globals ────────────────────────────────────
    app.jinja_env.globals['_icon'] = _icon
    app.jinja_env.globals['_safe_json_inline'] = _safe_json_inline
    app.jinja_env.filters['from_json_list'] = _from_json_list

    # ── Extensions ────────────────────────────────────────────────
    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'info'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # ── Blueprints ────────────────────────────────────────────────
    from .auth import auth_bp
    from .routes import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    # ── Create tables on first run ────────────────────────────────
    with app.app_context():
        import os
        os.makedirs(
            os.path.join(os.path.dirname(__file__), 'instance'),
            exist_ok=True,
        )
        db.create_all()
        _apply_sqlite_compat_migrations()

    # ── Auto-sync unsynced CBOs in background on startup ──────────
    # Guard against Flask debug reloader spawning the thread twice
    import os
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'false':
        t = threading.Thread(target=_background_sync, args=(app,), daemon=True)
        t.start()

    return app
