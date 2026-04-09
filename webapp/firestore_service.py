"""Optional Firestore mirroring for community feedback data."""
import json
from datetime import datetime

from flask import current_app

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None


def sync_subscriber_to_firestore(subscriber) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    cbo_ref = _get_cbo_ref(client, subscriber.cbo)
    _write_cbo_metadata(cbo_ref, subscriber.cbo)
    subscriber_ref = cbo_ref.collection('community_subscribers').document(str(subscriber.id))
    subscriber_ref.set({
        'subscriber_id': subscriber.id,
        'cbo_id': subscriber.cbo_id,
        'cbo_name': subscriber.cbo.name,
        'cbo_keyword': _get_cbo_firestore_key(subscriber.cbo),
        'cbo_slug': subscriber.cbo.slug,
        'phone_number': subscriber.phone_number,
        'status': subscriber.status,
        'signup_keyword': subscriber.signup_keyword,
        'signup_source': subscriber.signup_source,
        'conversation_state': subscriber.conversation_state,
        'added_at': _iso(subscriber.created_at),
        'consent_received_at': _iso(subscriber.consent_received_at),
        'last_response_at': _iso(subscriber.last_response_at),
        'last_checkin_sent_at': _iso(subscriber.last_checkin_sent_at),
        'created_at': _iso(subscriber.created_at),
        'updated_at': _iso(subscriber.updated_at),
    }, merge=True)
    return True


def sync_feedback_to_firestore(subscriber, feedback) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    try:
        transcript = json.loads(feedback.raw_transcript or '[]')
    except (json.JSONDecodeError, TypeError):
        transcript = []

    cbo_ref = _get_cbo_ref(client, subscriber.cbo)
    _write_cbo_metadata(cbo_ref, subscriber.cbo)
    feedback_ref = cbo_ref.collection('community_feedback').document(str(feedback.id))
    feedback_ref.set({
        'feedback_id': feedback.id,
        'subscriber_id': feedback.subscriber_id,
        'cbo_id': feedback.cbo_id,
        'cbo_name': subscriber.cbo.name,
        'cbo_keyword': _get_cbo_firestore_key(subscriber.cbo),
        'phone_number': subscriber.phone_number,
        'cycle_type': feedback.cycle_type,
        'delivery_channel': feedback.delivery_channel,
        'questionnaire_version': feedback.questionnaire_version,
        'status': feedback.status,
        'rating': feedback.rating,
        'help_count': feedback.help_count,
        'anecdote': feedback.anecdote,
        'raw_transcript': transcript,
        'added_at': _iso(feedback.created_at),
        'submitted_at': _iso(feedback.completed_at or feedback.created_at),
        'started_at': _iso(feedback.started_at),
        'completed_at': _iso(feedback.completed_at),
        'follow_up_due_at': _iso(feedback.follow_up_due_at),
        'created_at': _iso(feedback.created_at),
        'updated_at': _iso(feedback.updated_at),
    }, merge=True)
    feedback.firestore_synced_at = datetime.utcnow()
    from .models import db
    db.session.commit()
    return True


def get_feedback_document_path(cbo) -> str:
    return f"cbos/{_get_cbo_firestore_key(cbo)}"


def _get_firestore_client():
    if not firebase_admin:
        return None

    service_account_path = current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not service_account_path:
        return None

    app_name = 'kumbu-connect-firestore'
    try:
        app = firebase_admin.get_app(app_name)
    except ValueError:
        options = {}
        project_id = current_app.config.get('FIREBASE_PROJECT_ID')
        if project_id:
            options['projectId'] = project_id
        app = firebase_admin.initialize_app(
            credentials.Certificate(service_account_path),
            options=options,
            name=app_name,
        )
    return firestore.client(app=app)


def _get_cbo_ref(client, cbo):
    return client.collection('cbos').document(_get_cbo_firestore_key(cbo))


def _get_cbo_firestore_key(cbo) -> str:
    return (cbo.sms_keyword or cbo.cbo_identifier or cbo.slug or str(cbo.id)).strip().upper()


def _write_cbo_metadata(cbo_ref, cbo):
    cbo_ref.set({
        'cbo_id': cbo.id,
        'cbo_name': cbo.name,
        'cbo_keyword': _get_cbo_firestore_key(cbo),
        'cbo_slug': cbo.slug,
        'cbo_identifier': cbo.cbo_identifier,
        'community_feedback_enabled': cbo.community_feedback_enabled,
        'community_prompt': cbo.community_prompt,
        'created_at': _iso(cbo.created_at),
        'updated_at': _iso(cbo.updated_at),
    }, merge=True)


def _iso(value):
    return value.isoformat() if value else None