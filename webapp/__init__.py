"""Flask application factory for Kumbu Connect."""
import json
import os

from flask import Flask, flash, redirect, request, url_for
from flask_login import LoginManager
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .models import User, db


login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return User.query.get(int(user_id))
    except (TypeError, ValueError):
        return None


@login_manager.unauthorized_handler
def _handle_unauthorized():
    if login_manager.login_message:
        flash(login_manager.login_message, category=login_manager.login_message_category)

    next_url = request.path
    if request.method == 'GET' and request.query_string:
        next_url = request.full_path.rstrip('?')

    if request.path.startswith('/admin/community-feedback'):
        return redirect(url_for('auth.developer_login', next=next_url))
    return redirect(url_for('auth.login', next=next_url))


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    if app.config.get('TRUST_PROXY_HEADERS'):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    app.jinja_env.filters['from_json_list'] = _from_json_list
    app.jinja_env.globals['_safe_json_inline'] = _safe_json_inline
    app.jinja_env.globals['_icon'] = _icon

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config['BOOKKEEPING_UPLOAD_DIR'], exist_ok=True)
    os.makedirs(app.config['GOOGLE_FORM_UPLOAD_DIR'], exist_ok=True)
    os.makedirs(app.config['FUNDING_AUDIT_UPLOAD_DIR'], exist_ok=True)
    os.makedirs(app.config['CONTACT_UPLOAD_DIR'], exist_ok=True)
    os.makedirs(app.config['PROGRAM_PHOTO_UPLOAD_DIR'], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    from .auth import auth_bp
    from .routes import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    with app.app_context():
        db.create_all()
        _ensure_runtime_schema()

    return app


def _ensure_runtime_schema() -> None:
    inspector = inspect(db.engine)
    dialect_name = db.engine.dialect.name
    table_columns = {
        'users': {
            'account_status': "ALTER TABLE users ADD COLUMN account_status VARCHAR(20) NOT NULL DEFAULT 'active'",
            'password_is_temporary': 'ALTER TABLE users ADD COLUMN password_is_temporary BOOLEAN NOT NULL DEFAULT 0',
        },
        'cbos': {
            'bookkeeping_template_json': "ALTER TABLE cbos ADD COLUMN bookkeeping_template_json TEXT DEFAULT '{}'",
            'bookkeeping_workspace_entries_json': "ALTER TABLE cbos ADD COLUMN bookkeeping_workspace_entries_json TEXT DEFAULT '[]'",
        },
        'bookkeeping_documents': {
            'client_submission_id': "ALTER TABLE bookkeeping_documents ADD COLUMN client_submission_id VARCHAR(64) DEFAULT ''",
            'include_in_workspace': 'ALTER TABLE bookkeeping_documents ADD COLUMN include_in_workspace BOOLEAN NOT NULL DEFAULT TRUE',
            'workspace_period_key': "ALTER TABLE bookkeeping_documents ADD COLUMN workspace_period_key VARCHAR(7) DEFAULT ''",
        },
        'funding_audit_documents': {
            'storage_backend': "ALTER TABLE funding_audit_documents ADD COLUMN storage_backend VARCHAR(20) NOT NULL DEFAULT 'local'",
        },
        'google_form_responses': {
            'provisioning_status': "ALTER TABLE google_form_responses ADD COLUMN provisioning_status VARCHAR(20) DEFAULT 'pending'",
            'provisioning_error': "ALTER TABLE google_form_responses ADD COLUMN provisioning_error TEXT DEFAULT ''",
            'provisioned_user_id': 'ALTER TABLE google_form_responses ADD COLUMN provisioned_user_id INTEGER',
            'provisioned_cbo_id': 'ALTER TABLE google_form_responses ADD COLUMN provisioned_cbo_id INTEGER',
            'provisioned_at': 'ALTER TABLE google_form_responses ADD COLUMN provisioned_at DATETIME',
        },
        'google_form_uploads': {
            'storage_backend': "ALTER TABLE google_form_uploads ADD COLUMN storage_backend VARCHAR(20) DEFAULT 'local'",
        },
    }

    with db.engine.begin() as connection:
        for table_name, column_statements in table_columns.items():
            if table_name not in inspector.get_table_names():
                continue
            existing_columns = {column['name'] for column in inspector.get_columns(table_name)}
            for column_name, statement in column_statements.items():
                if column_name in existing_columns:
                    continue
                connection.execute(text(statement))

        if dialect_name.startswith('postgresql') and 'bookkeeping_documents' in inspector.get_table_names():
            connection.execute(text(
                'ALTER TABLE bookkeeping_documents ALTER COLUMN upload_batch_id TYPE VARCHAR(255)'
            ))
            connection.execute(text(
                'ALTER TABLE bookkeeping_documents ALTER COLUMN client_submission_id TYPE VARCHAR(255)'
            ))

        if 'users' in inspector.get_table_names():
            connection.execute(text(
                "UPDATE users SET password_is_temporary = TRUE WHERE account_status = 'pending_claim'"
            ))
        if 'google_form_responses' in inspector.get_table_names() and 'users' in inspector.get_table_names():
            connection.execute(text(
                """
                UPDATE google_form_responses
                SET provisioned_cbo_id = (
                    SELECT users.cbo_id
                    FROM users
                    WHERE users.id = google_form_responses.provisioned_user_id
                )
                WHERE provisioned_cbo_id IS NULL
                  AND provisioned_user_id IS NOT NULL
                """
            ))


def _from_json_list(raw_value) -> list:
    parsed = _safe_json_inline(raw_value)
    return parsed if isinstance(parsed, list) else []


def _safe_json_inline(raw_value):
    if isinstance(raw_value, (dict, list)):
        return raw_value
    if raw_value in (None, ''):
        return []
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_text = str(raw_value).strip()
        if raw_text.startswith('{'):
            return {}
        if raw_text.startswith('['):
            return []
        return {}


def _icon(icon_hint: str) -> str:
    normalized = str(icon_hint or '').strip().lower()
    return {
        'clock': 'clock',
        'farm': 'tractor',
        'chart-up': 'chart-line',
        'chart-line': 'chart-line',
        'people': 'users',
        'tools': 'screwdriver-wrench',
        'money': 'coins',
        'water': 'droplet',
        'health': 'heart-pulse',
        'education': 'book-open',
    }.get(normalized, 'chart-line')