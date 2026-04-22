"""Community SMS feedback workflow built around Twilio webhooks."""
import json
import re
from datetime import datetime, timedelta

import phonenumbers
from flask import current_app
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from .community_feedback_keywords import get_cbo_keyword, normalize_sms_keyword
from .firestore_service import sync_feedback_to_firestore, sync_subscriber_to_firestore
from .models import db, CBO, CommunityFeedback, CommunitySubscriber


STOP_WORDS = {'STOP', 'END', 'QUIT', 'CANCEL', 'UNSUBSCRIBE'}
HELP_WORDS = {'HELP', 'INFO'}
SKIP_WORDS = {'SKIP', 'NONE', 'UNKNOWN', 'NA', 'N/A'}


def handle_inbound_sms(from_number: str, body: str) -> str:
    """Route an inbound SMS into the correct keyword signup or response flow."""
    phone_number = normalize_phone_number(from_number)
    message = (body or '').strip()
    normalized = normalize_keyword(message)

    if not phone_number:
        return 'We could not identify your phone number. Please try again.'

    if not normalized:
        return _keyword_help_text()

    if normalized in STOP_WORDS:
        return _handle_opt_out(phone_number)

    if normalized in HELP_WORDS:
        return _keyword_help_text()

    active_subscriber = CommunitySubscriber.query.filter_by(phone_number=phone_number, status='active').filter(
        CommunitySubscriber.conversation_state != 'idle'
    ).order_by(CommunitySubscriber.updated_at.desc()).first()
    if active_subscriber and active_subscriber.active_feedback:
        return _continue_feedback_flow(active_subscriber, active_subscriber.active_feedback, message)

    cbo = _find_cbo_for_keyword(message)
    if not cbo:
        return _keyword_help_text()

    subscriber = CommunitySubscriber.query.filter_by(cbo_id=cbo.id, phone_number=phone_number).first()
    now = datetime.utcnow()
    if not subscriber:
        subscriber = CommunitySubscriber(
            cbo_id=cbo.id,
            phone_number=phone_number,
            signup_keyword=get_cbo_keyword(cbo),
            consent_received_at=now,
            last_response_at=now,
            status='active',
        )
        db.session.add(subscriber)
        db.session.flush()
    else:
        subscriber.status = 'active'
        subscriber.signup_keyword = get_cbo_keyword(cbo)
        subscriber.consent_received_at = subscriber.consent_received_at or now
        subscriber.last_response_at = now

    feedback = _start_feedback_cycle(subscriber, cycle_type='onboarding')
    _append_transcript(feedback, 'inbound', message)
    db.session.commit()
    sync_subscriber_to_firestore(subscriber)

    return _rating_prompt(cbo, onboarding=True)


def render_sms_response(message: str):
    response = MessagingResponse()
    response.message(message)
    return str(response), 200, {'Content-Type': 'application/xml'}


def validate_twilio_request(req) -> bool:
    """Validate incoming Twilio webhook requests when enabled in config."""
    if not current_app.config.get('TWILIO_VALIDATE_SIGNATURE'):
        return True

    auth_token = current_app.config.get('TWILIO_AUTH_TOKEN')
    signature = req.headers.get('X-Twilio-Signature', '')
    if not auth_token or not signature:
        return False

    validator = RequestValidator(auth_token)
    return validator.validate(req.url, req.form.to_dict(flat=True), signature)


def send_due_checkins() -> dict:
    """Send the first SMS prompt for subscribers who are due for another check-in."""
    account_sid = current_app.config.get('TWILIO_ACCOUNT_SID')
    auth_token = current_app.config.get('TWILIO_AUTH_TOKEN')
    from_number = current_app.config.get('TWILIO_PHONE_NUMBER')
    if not account_sid or not auth_token or not from_number:
        raise RuntimeError('Twilio credentials are not configured.')

    client = Client(account_sid, auth_token)
    now = datetime.utcnow()
    months = max(int(current_app.config.get('COMMUNITY_FEEDBACK_CHECKIN_MONTHS', 6) or 6), 1)
    cutoff = now - timedelta(days=30 * months)

    due_subscribers = CommunitySubscriber.query.filter_by(status='active', conversation_state='idle').all()
    sent = 0
    skipped = 0

    for subscriber in due_subscribers:
        if subscriber.last_checkin_sent_at and subscriber.last_checkin_sent_at > cutoff:
            skipped += 1
            continue

        feedback = _start_feedback_cycle(subscriber, cycle_type='checkin')
        body = _rating_prompt(subscriber.cbo, onboarding=False)
        client.messages.create(
            body=body,
            from_=from_number,
            to=subscriber.phone_number,
        )
        subscriber.last_checkin_sent_at = now
        _append_transcript(feedback, 'outbound', body)
        db.session.commit()
        sync_subscriber_to_firestore(subscriber)
        sent += 1

    return {'sent': sent, 'skipped': skipped, 'total_due': len(due_subscribers)}


def normalize_phone_number(value: str) -> str:
    """Convert user phone numbers into E.164 when possible."""
    raw = (value or '').strip()
    if not raw:
        return ''
    try:
        parsed = phonenumbers.parse(raw, None)
        if not phonenumbers.is_possible_number(parsed):
            return raw
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        cleaned = re.sub(r'[^0-9+]', '', raw)
        return cleaned


def normalize_keyword(value: str) -> str:
    return normalize_sms_keyword(value)


def _handle_opt_out(phone_number: str) -> str:
    subscribers = CommunitySubscriber.query.filter_by(phone_number=phone_number).all()
    for subscriber in subscribers:
        subscriber.status = 'opted_out'
        subscriber.conversation_state = 'idle'
        subscriber.active_feedback_id = None
    db.session.commit()
    return 'You have been unsubscribed from Kumbu Connect community feedback messages. Reply HELP for instructions.'


def _find_cbo_for_keyword(message: str) -> CBO | None:
    normalized = normalize_keyword(message)
    if not normalized:
        return None

    for cbo in CBO.query.filter_by(community_feedback_enabled=True).all():
        if normalized == get_cbo_keyword(cbo):
            return cbo
    return None


def _start_feedback_cycle(subscriber: CommunitySubscriber, cycle_type: str) -> CommunityFeedback:
    if subscriber.active_feedback and subscriber.active_feedback.status == 'in_progress':
        return subscriber.active_feedback

    feedback = CommunityFeedback(
        subscriber_id=subscriber.id,
        cbo_id=subscriber.cbo_id,
        cycle_type=cycle_type,
        status='in_progress',
        follow_up_due_at=datetime.utcnow() + timedelta(days=30 * max(current_app.config.get('COMMUNITY_FEEDBACK_CHECKIN_MONTHS', 6), 1)),
    )
    db.session.add(feedback)
    db.session.flush()
    subscriber.active_feedback_id = feedback.id
    subscriber.conversation_state = 'awaiting_rating'
    return feedback


def _continue_feedback_flow(subscriber: CommunitySubscriber, feedback: CommunityFeedback, message: str) -> str:
    state = subscriber.conversation_state
    cleaned = (message or '').strip()
    now = datetime.utcnow()
    _append_transcript(feedback, 'inbound', cleaned)

    if state == 'awaiting_rating':
        rating = _parse_rating(cleaned)
        if rating is None:
            db.session.commit()
            return 'Please reply with a whole number from 1 to 10 to rate this CBO.'

        feedback.rating = rating
        subscriber.conversation_state = 'awaiting_help_count'
        subscriber.last_response_at = now
        db.session.commit()
        sync_subscriber_to_firestore(subscriber)
        return 'About how many times has this CBO helped you or your household? Reply with a number, or reply SKIP if you are not sure.'

    if state == 'awaiting_help_count':
        help_count = _parse_optional_count(cleaned)
        if help_count is False:
            db.session.commit()
            return 'Reply with a number for how many times they helped you, or reply SKIP if you are not sure.'

        feedback.help_count = help_count if isinstance(help_count, int) else None
        subscriber.conversation_state = 'awaiting_story'
        subscriber.last_response_at = now
        db.session.commit()
        sync_subscriber_to_firestore(subscriber)
        return 'In 1 to 3 sentences, what impact has this CBO had on you or your community? You may stay anonymous.'

    if state == 'awaiting_story':
        if normalize_keyword(cleaned) not in SKIP_WORDS and len(cleaned) < 8:
            db.session.commit()
            return 'Please share a little more detail, or reply SKIP if you do not want to add a story.'

        feedback.anecdote = '' if normalize_keyword(cleaned) in SKIP_WORDS else cleaned
        feedback.status = 'completed'
        feedback.completed_at = now
        subscriber.conversation_state = 'idle'
        subscriber.active_feedback_id = None
        subscriber.last_response_at = now
        subscriber.last_checkin_sent_at = now
        db.session.commit()
        sync_subscriber_to_firestore(subscriber)
        sync_feedback_to_firestore(subscriber, feedback)
        return f'Thank you. Your feedback for {subscriber.cbo.name} has been saved anonymously.'

    subscriber.conversation_state = 'idle'
    subscriber.active_feedback_id = None
    db.session.commit()
    return _keyword_help_text()


def _append_transcript(feedback: CommunityFeedback, direction: str, message: str):
    transcript = []
    try:
        transcript = json.loads(feedback.raw_transcript or '[]')
    except (json.JSONDecodeError, TypeError):
        transcript = []
    transcript.append({
        'direction': direction,
        'message': message,
        'timestamp': datetime.utcnow().isoformat(),
    })
    feedback.raw_transcript = json.dumps(transcript, default=str)


def _parse_rating(message: str) -> int | None:
    try:
        rating = int(message.strip())
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 10 else None


def _parse_optional_count(message: str):
    if normalize_keyword(message) in SKIP_WORDS:
        return None
    try:
        count = int(message.strip())
    except (TypeError, ValueError):
        return False
    return count if count >= 0 else False


def _rating_prompt(cbo: CBO, onboarding: bool) -> str:
    keyword = get_cbo_keyword(cbo)
    custom_prompt = (cbo.community_prompt or '').strip()
    if custom_prompt:
        return custom_prompt
    if onboarding:
        return f'Thank you for joining {cbo.name} feedback. Reply with a number from 1 to 10 to rate how {keyword} has helped your community.'
    return f'It is time for your next {cbo.name} check-in. Reply with a number from 1 to 10 to rate their recent impact.'


def _keyword_help_text() -> str:
    active_keywords = []
    for cbo in CBO.query.filter_by(community_feedback_enabled=True).limit(5).all():
        keyword = get_cbo_keyword(cbo)
        if keyword:
            active_keywords.append(keyword)
    if active_keywords:
        return 'Reply with your CBO keyword to begin feedback. Example keywords: ' + ', '.join(active_keywords)
    return 'Reply with your CBO keyword to begin feedback.'