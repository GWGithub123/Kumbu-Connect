"""
Application configuration — reads secrets from ../.env
"""
import os
from dotenv import load_dotenv

# Load .env from the project root (one level up from webapp/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'kumbu-connect-dev-key-change-in-prod')
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(
        os.path.dirname(__file__), 'instance', 'kumbu.db'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # KoboToolbox
    KOBO_API_KEY = os.environ.get(
        'Kobo_Toobox_API_Key', ''
    ).strip()
    KOBO_BASE_URL = 'https://kf.kobotoolbox.org/api/v2'
    KOBO_ASSET_ID = 'aJ7GEDZPU3dbM4KAEqUBKW'

    # Gemini
    GEMINI_API_KEY = os.environ.get('Gemini_API_Key', '').strip()

    # Google Maps
    GOOGLE_MAPS_API_KEY = os.environ.get(
        'Google_Maps_API_Key',
        os.environ.get(
            'GOOGLE_MAPS_API_KEY',
            os.environ.get('Google_Cloud_API_Key', ''),
        ),
    ).strip()

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
