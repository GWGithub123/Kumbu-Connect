"""
Application configuration — reads secrets from ../.env
"""
import os
from dotenv import load_dotenv

# Load .env from the project root (one level up from webapp/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


_DEFAULT_SQLITE_DATABASE_URI = 'sqlite:///' + os.path.join(
    os.path.dirname(__file__), 'instance', 'kumbu.db'
)


def _normalize_database_uri(raw_value: str) -> str:
    database_uri = str(raw_value or '').strip()
    if not database_uri:
        return _DEFAULT_SQLITE_DATABASE_URI
    if database_uri.startswith('postgres://'):
        return 'postgresql+psycopg://' + database_uri[len('postgres://'):]
    if database_uri.startswith('postgresql://') and '+psycopg' not in database_uri.split('://', 1)[0]:
        return 'postgresql+psycopg://' + database_uri[len('postgresql://'):]
    return database_uri


def _normalize_same_site(raw_value: str, default: str = 'Lax') -> str:
    normalized = str(raw_value or '').strip().lower()
    if normalized == 'strict':
        return 'Strict'
    if normalized == 'none':
        return 'None'
    return 'Lax' if default.lower() == 'lax' else default

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'kumbu-connect-dev-key-change-in-prod')
    SQLALCHEMY_DATABASE_URI = _normalize_database_uri(
        os.environ.get('DATABASE_URL', os.environ.get('SQLALCHEMY_DATABASE_URI', ''))
    )
    SQLALCHEMY_ENGINE_OPTIONS = (
        {'pool_pre_ping': True}
        if SQLALCHEMY_DATABASE_URI.startswith('postgresql')
        else {}
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', str(20 * 1024 * 1024)))
    APP_HOST = os.environ.get('APP_HOST', '127.0.0.1').strip() or '127.0.0.1'
    APP_PORT = int(os.environ.get('APP_PORT', os.environ.get('PORT', '8000')) or 8000)
    TRUST_PROXY_HEADERS = os.environ.get('TRUST_PROXY_HEADERS', 'false').strip().lower() == 'true'
    PREFERRED_URL_SCHEME = os.environ.get('PREFERRED_URL_SCHEME', 'http').strip() or 'http'
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'false').strip().lower() == 'true'
    SESSION_COOKIE_SAMESITE = _normalize_same_site(os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax'))
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE
    ALLOW_LOCAL_FILE_STORAGE_FALLBACK = (
        os.environ.get('ALLOW_LOCAL_FILE_STORAGE_FALLBACK', 'true').strip().lower() == 'true'
    )
    ALLOW_GOOGLE_ADC_FALLBACK = os.environ.get('ALLOW_GOOGLE_ADC_FALLBACK', 'false').strip().lower() == 'true'

    # KoboToolbox
    KOBO_API_KEY = os.environ.get(
        'Kobo_Toobox_API_Key', ''
    ).strip()
    KOBO_BASE_URL = 'https://kf.kobotoolbox.org/api/v2'
    KOBO_ASSET_ID = 'aJ7GEDZPU3dbM4KAEqUBKW'

    # Gemini
    GEMINI_API_KEY = os.environ.get('Gemini_API_Key', '').strip()

    # Anthropic Claude bookkeeping extraction
    CLAUDE_API_KEY = os.environ.get(
        'CLAUDE_API_KEY',
        os.environ.get(
            'ANTHROPIC_API_KEY',
            os.environ.get(
                'Claude_API_Key',
                os.environ.get('Anthropic_API_Key', ''),
            ),
        ),
    ).strip()
    BOOKKEEPING_VISION_MODEL = os.environ.get('BOOKKEEPING_VISION_MODEL', 'claude-opus-4-6').strip() or 'claude-opus-4-6'
    BOOKKEEPING_REQUEST_TIMEOUT = int(os.environ.get('BOOKKEEPING_REQUEST_TIMEOUT', '180') or 180)
    BOOKKEEPING_MAX_IMAGE_EDGE = int(os.environ.get('BOOKKEEPING_MAX_IMAGE_EDGE', '1568') or 1568)
    BOOKKEEPING_IMAGE_QUALITY = int(os.environ.get('BOOKKEEPING_IMAGE_QUALITY', '85') or 85)
    BOOKKEEPING_MAX_OUTPUT_TOKENS = int(os.environ.get('BOOKKEEPING_MAX_OUTPUT_TOKENS', '12000') or 12000)
    BOOKKEEPING_RETRY_MAX_OUTPUT_TOKENS = int(
        os.environ.get('BOOKKEEPING_RETRY_MAX_OUTPUT_TOKENS', '24000') or 24000
    )
    BOOKKEEPING_BROWSER_TIMEOUT_MS = int(os.environ.get('BOOKKEEPING_BROWSER_TIMEOUT_MS', '300000') or 300000)
    BOOKKEEPING_CLAUDE_REVIEW_ROW_LIMIT = int(os.environ.get('BOOKKEEPING_CLAUDE_REVIEW_ROW_LIMIT', '8') or 8)
    BOOKKEEPING_CLAUDE_REVIEW_CELL_LIMIT = int(os.environ.get('BOOKKEEPING_CLAUDE_REVIEW_CELL_LIMIT', '4') or 4)
    BOOKKEEPING_CLAUDE_ROW_REPAIR_CONFIDENCE_THRESHOLD = float(
        os.environ.get('BOOKKEEPING_CLAUDE_ROW_REPAIR_CONFIDENCE_THRESHOLD', '0.68') or 0.68
    )
    BOOKKEEPING_CLAUDE_CELL_REPAIR_CONFIDENCE_THRESHOLD = float(
        os.environ.get('BOOKKEEPING_CLAUDE_CELL_REPAIR_CONFIDENCE_THRESHOLD', '0.72') or 0.72
    )

    # Azure Document Intelligence hybrid bookkeeping transcription
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT',
        os.environ.get(
            'AZURE_DOC_INTELLIGENCE_ENDPOINT',
            os.environ.get('Azure_Document_Intelligence_Endpoint', ''),
        ),
    ).strip()
    AZURE_DOCUMENT_INTELLIGENCE_KEY = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_KEY',
        os.environ.get(
            'AZURE_DOC_INTELLIGENCE_KEY',
            os.environ.get(
                'Azure_Document_Intelligence_Key_1',
                os.environ.get('Azure_Document_Intelligence_Key', ''),
            ),
        ),
    ).strip()
    AZURE_DOCUMENT_INTELLIGENCE_API_VERSION = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_API_VERSION',
        '2024-11-30',
    ).strip() or '2024-11-30'
    AZURE_DOCUMENT_INTELLIGENCE_LOCALE = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_LOCALE',
        'en-KE',
    ).strip() or 'en-KE'
    AZURE_DOCUMENT_INTELLIGENCE_LAYOUT_MODEL = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_LAYOUT_MODEL',
        'prebuilt-layout',
    ).strip() or 'prebuilt-layout'
    AZURE_DOCUMENT_INTELLIGENCE_READ_MODEL = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_READ_MODEL',
        'prebuilt-read',
    ).strip() or 'prebuilt-read'
    AZURE_DOCUMENT_INTELLIGENCE_FEATURES = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_FEATURES',
        '',
    ).strip()
    AZURE_DOCUMENT_INTELLIGENCE_HIGH_RESOLUTION = os.environ.get(
        'AZURE_DOCUMENT_INTELLIGENCE_HIGH_RESOLUTION',
        'true',
    ).strip().lower() == 'true'
    AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT = float(
        os.environ.get('AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT', '60') or 60
    )
    AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT = float(
        os.environ.get('AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT', '180') or 180
    )
    AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL = float(
        os.environ.get('AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL', '1.5') or 1.5
    )

    # OpenAI funding audit
    OPENAI_API_KEY = os.environ.get(
        'OPENAI_API_KEY',
        os.environ.get(
            'OpenAI_API_Key',
            os.environ.get('OPEN_AI_API_KEY', ''),
        ),
    ).strip()
    OPENAI_VISION_MODEL = os.environ.get('OPENAI_VISION_MODEL', 'gpt-4.1').strip() or 'gpt-4.1'
    OPENAI_REQUEST_TIMEOUT = int(os.environ.get('OPENAI_REQUEST_TIMEOUT', '60') or 60)
    BOOKKEEPING_UPLOAD_DIR = os.environ.get(
        'BOOKKEEPING_UPLOAD_DIR',
        os.path.join(os.path.dirname(__file__), 'instance', 'bookkeeping_uploads'),
    ).strip()
    GOOGLE_FORM_UPLOAD_DIR = os.environ.get(
        'GOOGLE_FORM_UPLOAD_DIR',
        os.path.join(os.path.dirname(__file__), 'instance', 'google_form_uploads'),
    ).strip()
    FUNDING_AUDIT_UPLOAD_DIR = os.environ.get(
        'FUNDING_AUDIT_UPLOAD_DIR',
        os.path.join(os.path.dirname(__file__), 'instance', 'funding_audit_uploads'),
    ).strip()
    BOOKKEEPING_MAX_FILES = int(os.environ.get('BOOKKEEPING_MAX_FILES', '5') or 5)
    BOOKKEEPING_MAX_PDF_PAGES = int(os.environ.get('BOOKKEEPING_MAX_PDF_PAGES', '10') or 10)
    PUBLIC_BASE_URL = os.environ.get(
        'PUBLIC_BASE_URL',
        os.environ.get('PUBLIC_APP_URL', ''),
    ).strip()
    PUBLIC_TUNNEL_URL = os.environ.get(
        'PUBLIC_TUNNEL_URL',
        os.environ.get('NGROK_PUBLIC_URL', ''),
    ).strip()
    NGROK_PUBLIC_URL = os.environ.get('NGROK_PUBLIC_URL', '').strip()
    CLOUDFLARED_METRICS_URL = os.environ.get('CLOUDFLARED_METRICS_URL', '').strip()
    FIREBASE_STORAGE_BUCKET = os.environ.get('FIREBASE_STORAGE_BUCKET', '').strip()

    # Google Maps
    GOOGLE_MAPS_API_KEY = os.environ.get(
        'Google_Maps_API_Key',
        os.environ.get(
            'GOOGLE_MAPS_API_KEY',
                os.environ.get(
                    'GOOGLE_CLOUD_API_KEY',
                    os.environ.get('Google_Cloud_API_Key', ''),
                ),
        ),
    ).strip()
    GOOGLE_SEARCH_API_KEY = os.environ.get(
        'Google_Search_API_Key',
        os.environ.get('GOOGLE_SEARCH_API_KEY', ''),
    ).strip()
    GOOGLE_SEARCH_ENGINE_ID = os.environ.get(
        'Google_Search_Engine_ID',
        os.environ.get('GOOGLE_SEARCH_ENGINE_ID', ''),
    ).strip()
    GOOGLE_AUTHORIZED_USER_JSON = os.environ.get(
        'GOOGLE_AUTHORIZED_USER_JSON',
        os.environ.get('GOOGLE_FORMS_AUTHORIZED_USER_JSON', ''),
    ).strip()
    GOOGLE_OAUTH_CLIENT_SECRET_JSON = os.environ.get(
        'GOOGLE_OAUTH_CLIENT_SECRET_JSON',
        os.environ.get('GOOGLE_FORMS_OAUTH_CLIENT_SECRET_JSON', ''),
    ).strip()
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get(
        'GOOGLE_OAUTH_CLIENT_ID',
        os.environ.get('GOOGLE_FORMS_OAUTH_CLIENT_ID', ''),
    ).strip()
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get(
        'GOOGLE_OAUTH_CLIENT_SECRET',
        os.environ.get('GOOGLE_FORMS_OAUTH_CLIENT_SECRET', ''),
    ).strip()
    GOOGLE_OAUTH_TOKEN_JSON = os.environ.get(
        'GOOGLE_OAUTH_TOKEN_JSON',
        os.environ.get(
            'GOOGLE_FORMS_OAUTH_TOKEN_JSON',
            os.path.join(os.path.dirname(__file__), 'instance', 'google_forms_user_token.json'),
        ),
    ).strip()
    GOOGLE_DEVELOPER_CLIENT_SECRET_JSON = os.environ.get(
        'GOOGLE_DEVELOPER_CLIENT_SECRET_JSON',
        GOOGLE_OAUTH_CLIENT_SECRET_JSON,
    ).strip()
    GOOGLE_USER_CLIENT_SECRET_JSON = os.environ.get(
        'GOOGLE_USER_CLIENT_SECRET_JSON',
        GOOGLE_DEVELOPER_CLIENT_SECRET_JSON,
    ).strip()
    DEVELOPER_SMS_ACTIVITY_CBO_ID = (
        int(os.environ.get('DEVELOPER_SMS_ACTIVITY_CBO_ID', '').strip())
        if os.environ.get('DEVELOPER_SMS_ACTIVITY_CBO_ID', '').strip().isdigit()
        else None
    )
    DEVELOPER_SMS_ACTIVITY_CBO_SLUG = os.environ.get(
        'DEVELOPER_SMS_ACTIVITY_CBO_SLUG',
        '',
    ).strip().lower()
    TEMP_LOGIN_BYPASS_ENABLED = os.environ.get(
        'TEMP_LOGIN_BYPASS_ENABLED',
        'false',
    ).strip().lower() == 'true'
    TEMP_LOGIN_BYPASS_CBO_ID = (
        int(os.environ.get('TEMP_LOGIN_BYPASS_CBO_ID', '').strip())
        if os.environ.get('TEMP_LOGIN_BYPASS_CBO_ID', '').strip().isdigit()
        else None
    )
    TEMP_LOGIN_BYPASS_CBO_SLUG = os.environ.get(
        'TEMP_LOGIN_BYPASS_CBO_SLUG',
        '',
    ).strip().lower()
    GOOGLE_DEVELOPER_ALLOWED_EMAILS = [
        email.strip().lower()
        for email in os.environ.get('GOOGLE_DEVELOPER_ALLOWED_EMAILS', '').split(',')
        if email.strip()
    ]

    # Community SMS feedback
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
    TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '').strip()
    TWILIO_VALIDATE_SIGNATURE = os.environ.get('TWILIO_VALIDATE_SIGNATURE', 'false').strip().lower() == 'true'
    COMMUNITY_FEEDBACK_CHECKIN_MONTHS = int(
        os.environ.get('COMMUNITY_FEEDBACK_CHECKIN_MONTHS', '6').strip() or 6
    )

    # Firebase / Firestore
    FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', '').strip()
    FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()
