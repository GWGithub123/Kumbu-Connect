"""Developer-initiated SMS onboarding for CBO leaders."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import phonenumbers
from flask import current_app, has_request_context, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from phonenumbers import NumberParseException
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client
from urllib.parse import urlparse

from .cbo_web_research_service import (
    apply_verification_decision,
    apply_web_research_to_cbo,
    format_web_verification_sms,
    load_session_web_research,
    parse_web_verification_reply,
    research_cbo_online,
    store_session_web_research,
)
from .community_feedback_keywords import get_cbo_keyword, normalize_sms_keyword
from .firestore_service import sync_sms_onboarding_session
from .gemini_service import extract_sms_onboarding_answers
from .models import CBO, SmsOnboardingSession, User, db

_WEB_RESEARCH_IN_FLIGHT = set()
_WEB_RESEARCH_IN_FLIGHT_GUARD = threading.Lock()


SMS_ONBOARDING_SALT = 'sms-onboarding-claim'
SMS_ONBOARDING_TOKEN_MAX_AGE = 60 * 60 * 24 * 14
STOP_WORDS = {'STOP', 'END', 'QUIT', 'CANCEL', 'UNSUBSCRIBE'}
HELP_WORDS = {'HELP', 'INFO'}
_ONBOARDING_SESSION_LOCKS = {}
_ONBOARDING_SESSION_LOCKS_GUARD = threading.Lock()

BASIC_FIELDS = ('full_name', 'cbo_name', 'year_incorporated')
OPERATIONS_FIELDS = ('position_role', 'cbo_office_address', 'cbo_program_locations')
ACCOUNT_FIELDS = ('email_address', 'registered_bank_account', 'anecdotal_story')
REQUIRED_TO_PROVISION = ('full_name', 'cbo_name', 'email_address')
ANSWER_LABELS = {
    'full_name': 'Leader name',
    'cbo_name': 'CBO name',
    'year_incorporated': 'Year founded',
    'position_role': 'Role',
    'cbo_office_address': 'Office address',
    'cbo_program_locations': 'Program locations',
    'email_address': 'Account email',
    'registered_bank_account': 'Dedicated bank account',
    'bank_name': 'Bank name',
    'anecdotal_story': 'Impact story',
    'milestones': 'Milestones',
}
PROGRESS_STEPS = (
    {
        'key': 'awaiting_basics',
        'step': 1,
        'total': 5,
        'label': 'Invitation sent',
        'waiting': 'Waiting for name, CBO name, and year founded',
    },
    {
        'key': 'awaiting_operations',
        'step': 2,
        'total': 5,
        'label': 'Basics received',
        'waiting': 'Waiting for role, office address, and program locations',
    },
    {
        'key': 'awaiting_account',
        'step': 3,
        'total': 5,
        'label': 'Operations received',
        'waiting': 'Waiting for email, bank details, and an impact story',
    },
    {
        'key': 'awaiting_email',
        'step': 3,
        'total': 5,
        'label': 'Email still needed',
        'waiting': 'Waiting for the account email address',
    },
    {
        'key': 'awaiting_web_verification',
        'step': 4,
        'total': 5,
        'label': 'Confirming public facts',
        'waiting': 'Waiting for the leader to confirm facts found online',
    },
    {
        'key': 'awaiting_claim',
        'step': 5,
        'total': 5,
        'label': 'Profile ready',
        'waiting': 'Waiting for the leader to create a password',
    },
    {
        'key': 'completed',
        'step': 5,
        'total': 5,
        'label': 'Account claimed',
        'waiting': 'Onboarding complete',
    },
    {
        'key': 'cancelled',
        'step': 0,
        'total': 5,
        'label': 'Cancelled',
        'waiting': 'This invitation was cancelled',
    },
)

EMAIL_RE = re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)
YEAR_RE = re.compile(r'\b(?:19|20)\d{2}\b')


def twilio_is_configured() -> bool:
    return all([
        current_app.config.get('TWILIO_ACCOUNT_SID'),
        current_app.config.get('TWILIO_AUTH_TOKEN'),
        current_app.config.get('TWILIO_PHONE_NUMBER'),
    ])


def normalize_onboarding_phone(value: str) -> str:
    """Turn developer-entered phone numbers into E.164 when possible."""
    raw = (value or '').strip()
    if not raw:
        return ''

    digits = re.sub(r'\D', '', raw)
    attempts: list[tuple[str, str | None]] = []
    if raw.startswith('+'):
        attempts.append((raw, None))
    elif digits.startswith('00') and len(digits) > 8:
        attempts.append((f'+{digits[2:]}', None))

    # National 0-prefix numbers in this product are Kenyan; 10-digit numbers
    # starting 2-9 are treated as US so developer test numbers still work.
    if digits.startswith('0'):
        attempts.extend([(digits, 'KE'), (raw, 'KE')])
    elif len(digits) == 10 and digits[0] in '23456789':
        attempts.extend([(digits, 'US'), (raw, 'US'), (digits, 'KE')])
    elif len(digits) == 9 and digits.startswith('7'):
        attempts.extend([(digits, 'KE'), (raw, 'KE')])
    elif len(digits) == 11 and digits.startswith('1'):
        attempts.append((f'+{digits}', None))

    attempts.extend([(raw, 'US'), (raw, 'KE'), (digits, 'US'), (digits, 'KE')])

    seen = set()
    possible = ''
    for number, region in attempts:
        key = (number, region)
        if not number or key in seen:
            continue
        seen.add(key)
        try:
            parsed = phonenumbers.parse(number, region)
        except NumberParseException:
            continue
        formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        if not formatted:
            continue
        if phonenumbers.is_valid_number(parsed):
            return formatted
        if not possible and phonenumbers.is_possible_number(parsed):
            possible = formatted
    return possible


def active_onboarding_session(phone_number: str) -> SmsOnboardingSession | None:
    if not phone_number:
        return None
    return (
        SmsOnboardingSession.query
        .filter(
            SmsOnboardingSession.phone_number == phone_number,
            SmsOnboardingSession.status.in_([
                SmsOnboardingSession.STATUS_IN_PROGRESS,
                SmsOnboardingSession.STATUS_AWAITING_CLAIM,
            ]),
        )
        .order_by(SmsOnboardingSession.updated_at.desc(), SmsOnboardingSession.id.desc())
        .first()
    )


def initiate_sms_onboarding(
    phone_number: str,
    source_cbo: CBO,
    initiated_by_user_id: int | None = None,
) -> SmsOnboardingSession:
    normalized = normalize_onboarding_phone(phone_number)
    if not normalized:
        raise RuntimeError('Enter a valid phone number, including country code if needed.')
    if not twilio_is_configured():
        raise RuntimeError('Twilio is not configured, so SMS onboarding cannot start yet.')
    if source_cbo is None:
        raise RuntimeError('No onboarding source CBO is available.')

    existing = active_onboarding_session(normalized)
    if existing is not None:
        existing.status = SmsOnboardingSession.STATUS_CANCELLED
        existing.conversation_state = 'cancelled'
        existing.last_error = 'Restarted by developer initiate.'
        db.session.add(existing)

    session = SmsOnboardingSession(
        phone_number=normalized,
        status=SmsOnboardingSession.STATUS_IN_PROGRESS,
        conversation_state='awaiting_basics',
        answers_json=json.dumps({'whatsapp_phone_number': normalized}),
        transcript_json='[]',
        initiated_by_user_id=initiated_by_user_id,
        source_cbo_id=source_cbo.id,
    )
    db.session.add(session)
    db.session.flush()

    outbound = _welcome_message()
    sid = _send_sms(normalized, outbound)
    session.last_outbound_sid = sid or ''
    _append_transcript(session, 'outbound', outbound)
    db.session.add(session)
    db.session.commit()
    sync_sms_onboarding_session(session)
    return session


def _onboarding_lock(phone_number: str) -> threading.Lock:
    key = normalize_onboarding_phone(phone_number) or (phone_number or '').strip() or '_'
    with _ONBOARDING_SESSION_LOCKS_GUARD:
        return _ONBOARDING_SESSION_LOCKS.setdefault(key, threading.Lock())


def handle_inbound_sms_onboarding(from_number: str, body: str, inbound_sid: str = '') -> str | None:
    """Handle an inbound SMS if it belongs to an active onboarding session."""
    phone_number = normalize_onboarding_phone(from_number) or (from_number or '').strip()
    with _onboarding_lock(phone_number):
        return _handle_inbound_sms_onboarding_unlocked(phone_number, body, inbound_sid)


def _handle_inbound_sms_onboarding_unlocked(phone_number: str, body: str, inbound_sid: str = '') -> str | None:
    session = active_onboarding_session(phone_number)
    message = (body or '').strip()
    normalized = re.sub(r'\s+', ' ', message).strip().upper()
    inbound_sid = str(inbound_sid or '').strip()

    if normalized in STOP_WORDS:
        if session is not None:
            session.status = SmsOnboardingSession.STATUS_CANCELLED
            session.conversation_state = 'cancelled'
            _append_transcript(session, 'inbound', message, sid=inbound_sid)
            db.session.add(session)
            db.session.commit()
            sync_sms_onboarding_session(session)
        return None

    if session is None:
        return None

    if inbound_sid and _transcript_has_sid(session, inbound_sid):
        return _last_outbound_message(session)

    if _looks_like_community_keyword(message):
        reply = (
            "This text thread is for creating a new CBO profile on Kumbu Connect, not the community keyword. "
            "Please reply with:\n"
            "1) Your full name\n"
            "2) Your CBO's name\n"
            "3) The year it was founded"
        )
        _append_transcript(session, 'inbound', message, sid=inbound_sid)
        _append_transcript(session, 'outbound', reply)
        db.session.add(session)
        db.session.commit()
        sync_sms_onboarding_session(session)
        return reply

    if not message or normalized in HELP_WORDS:
        reply = _help_message(session)
        _append_transcript(session, 'inbound', message or 'HELP', sid=inbound_sid)
        _append_transcript(session, 'outbound', reply)
        db.session.add(session)
        db.session.commit()
        sync_sms_onboarding_session(session)
        return reply

    if session.conversation_state == 'awaiting_web_verification':
        _append_transcript(session, 'inbound', message, sid=inbound_sid)
        try:
            reply = _handle_web_verification_reply(session, message)
        except Exception:
            current_app.logger.exception('SMS onboarding web verification failed for session %s', session.id)
            session.last_error = 'Could not confirm the public facts just now.'
            reply = (
                'Thanks — we received your reply, but could not finish checking those public facts. '
                'Reply YES, NO, or SKIP to continue.'
            )
        _append_transcript(session, 'outbound', reply)
        db.session.add(session)
        db.session.commit()
        sync_sms_onboarding_session(session)
        return reply

    repair_weak_sms_onboarding_answers(session)
    _append_transcript(session, 'inbound', message, sid=inbound_sid)
    extracted = extract_sms_onboarding_answers(
        message,
        expected_fields=_expected_fields_for_state(session.conversation_state, _session_answers(session)),
        already_collected=_session_answers(session),
        transcript=_session_transcript(session),
    )
    _merge_answers(session, extracted, message)
    schedule_session_web_research(session)

    try:
        reply = _next_outbound_message(session)
    except Exception:
        current_app.logger.exception('SMS onboarding follow-up failed for session %s', session.id)
        session.last_error = 'Could not continue onboarding after the latest reply.'
        reply = (
            'Thanks — we received your message, but could not finish setting up the account just now. '
            'Please reply again in a few minutes, or ask your Kumbu Connect contact for help.'
        )

    _append_transcript(session, 'outbound', reply)
    db.session.add(session)
    db.session.commit()
    sync_sms_onboarding_session(session)
    return reply


def claim_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=SMS_ONBOARDING_SALT)


def build_claim_token(session: SmsOnboardingSession, user: User) -> str:
    return claim_serializer().dumps({
        'session_id': session.id,
        'user_id': user.id,
        'email': user.email,
    })


def load_claim_session(token: str) -> tuple[SmsOnboardingSession, User]:
    try:
        payload = claim_serializer().loads(token, max_age=SMS_ONBOARDING_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        raise RuntimeError('This account setup link has expired. Ask Kumbu Connect to send a new SMS.') from exc
    except BadSignature as exc:
        raise RuntimeError('This account setup link is not valid.') from exc

    session = db.session.get(SmsOnboardingSession, payload.get('session_id'))
    user = db.session.get(User, payload.get('user_id'))
    if session is None or user is None:
        raise RuntimeError('This account setup link is no longer available.')
    if session.provisioned_user_id and session.provisioned_user_id != user.id:
        raise RuntimeError('This account setup link does not match the prepared CBO account.')
    email = str(payload.get('email') or '').strip().lower()
    if email and user.email != email:
        raise RuntimeError('This account setup link does not match the prepared email address.')
    return session, user


def complete_sms_onboarding_claim(session: SmsOnboardingSession, user: User, password: str) -> User:
    user.set_password(password)
    user.account_status = 'active'
    session.status = SmsOnboardingSession.STATUS_COMPLETED
    session.conversation_state = 'completed'
    session.last_error = ''
    db.session.add(user)
    db.session.add(session)
    db.session.commit()
    sync_sms_onboarding_session(session)
    return user


def recent_sms_onboarding_sessions(limit: int = 25) -> list[SmsOnboardingSession]:
    return (
        SmsOnboardingSession.query
        .order_by(SmsOnboardingSession.updated_at.desc(), SmsOnboardingSession.id.desc())
        .limit(limit)
        .all()
    )


def serialize_sms_onboarding_session(session: SmsOnboardingSession) -> dict:
    answers = _session_answers(session)
    transcript = _session_transcript(session)
    progress = _progress_for_state(session.conversation_state)
    collected = [
        {'id': key, 'label': ANSWER_LABELS.get(key, key.replace('_', ' ')), 'value': value}
        for key, value in answers.items()
        if str(value or '').strip() and key not in {'whatsapp_phone_number', 'signature_and_date'}
    ]
    inbound_count = sum(1 for item in transcript if item.get('direction') == 'inbound')
    last_inbound = next((item for item in reversed(transcript) if item.get('direction') == 'inbound'), None)
    last_outbound = next((item for item in reversed(transcript) if item.get('direction') == 'outbound'), None)
    return {
        'id': session.id,
        'phone_number': session.phone_number,
        'status': session.status,
        'status_label': str(session.status or '').replace('_', ' '),
        'conversation_state': session.conversation_state,
        'progress': progress,
        'collected': collected,
        'inbound_count': inbound_count,
        'last_inbound': last_inbound,
        'last_outbound': last_outbound,
        'transcript': transcript[-8:],
        'cbo_name': (session.provisioned_cbo.name if session.provisioned_cbo else answers.get('cbo_name') or ''),
        'leader_name': answers.get('full_name') or '',
        'email': answers.get('email_address') or '',
        'last_error': session.last_error or '',
        'web_research': _serialize_session_web_research(session),
        'created_at': session.created_at,
        'updated_at': session.updated_at,
        'can_simulate': session.status in {
            SmsOnboardingSession.STATUS_IN_PROGRESS,
            SmsOnboardingSession.STATUS_AWAITING_CLAIM,
        },
    }


def catch_up_sms_onboarding_replies(sessions: list[SmsOnboardingSession] | None = None) -> dict:
    """Pull missed inbound texts from Twilio and continue those conversations."""
    tracked = sessions or recent_sms_onboarding_sessions(25)
    for session in tracked:
        if session.status == SmsOnboardingSession.STATUS_IN_PROGRESS:
            _scrub_stale_keyword_transcript(session)
            repair_weak_sms_onboarding_answers(session, send_followup=True)

    active = [
        session for session in tracked
        if session.status in {
            SmsOnboardingSession.STATUS_IN_PROGRESS,
            SmsOnboardingSession.STATUS_AWAITING_CLAIM,
        }
    ]
    processed = 0
    replies_sent = 0
    errors = []
    for session in active:
        try:
            result = _catch_up_session_from_twilio(session)
            processed += int(result.get('processed') or 0)
            replies_sent += int(result.get('replies_sent') or 0)
        except Exception as exc:
            current_app.logger.exception('Failed to catch up SMS onboarding session %s', session.id)
            errors.append(str(exc))
    return {'processed': processed, 'replies_sent': replies_sent, 'errors': errors}


def simulate_sms_onboarding_reply(session: SmsOnboardingSession, body: str, send_sms: bool = True) -> str:
    reply = handle_inbound_sms_onboarding(session.phone_number, body)
    if reply is None:
        raise RuntimeError('That phone number is no longer in an active SMS onboarding conversation.')
    if send_sms:
        sid = _send_sms(session.phone_number, reply)
        session = db.session.get(SmsOnboardingSession, session.id) or session
        session.last_outbound_sid = sid or session.last_outbound_sid
        db.session.add(session)
        db.session.commit()
    return reply


def ensure_twilio_inbound_webhook() -> dict:
    """Point the Twilio number's inbound SMS webhook at this running app when possible."""
    desired_url = current_inbound_webhook_url()
    client = _twilio_client()
    from_number = current_app.config.get('TWILIO_PHONE_NUMBER')
    if not client or not from_number:
        return {'ok': False, 'desired_url': desired_url, 'current_url': '', 'updated': False}

    numbers = client.incoming_phone_numbers.list(phone_number=from_number, limit=1)
    if not numbers:
        return {'ok': False, 'desired_url': desired_url, 'current_url': '', 'updated': False}

    number = numbers[0]
    current_url = str(number.sms_url or '').strip()
    if _is_cloud_run_webhook(current_url) and not _is_cloud_run_webhook(desired_url):
        return {'ok': True, 'desired_url': current_url, 'current_url': current_url, 'updated': False}
    if not desired_url:
        return {'ok': True, 'desired_url': desired_url, 'current_url': current_url, 'updated': False}
    if current_url.rstrip('/') == desired_url.rstrip('/'):
        return {'ok': True, 'desired_url': desired_url, 'current_url': current_url, 'updated': False}

    number.update(sms_url=desired_url, sms_method='POST')
    return {'ok': True, 'desired_url': desired_url, 'current_url': desired_url, 'updated': True}


def _is_cloud_run_webhook(url: str) -> bool:
    host = (urlparse(url or '').hostname or '').lower()
    return host.endswith('.run.app')


def start_local_sms_inbound_bridge(app) -> None:
    """Keep the local Twilio webhook alive and catch missed replies without a page refresh."""
    if str(app.config.get('PUBLIC_BASE_URL') or '').strip():
        return
    if app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    if getattr(app, '_sms_inbound_bridge_started', False):
        return
    app._sms_inbound_bridge_started = True

    def _loop():
        time.sleep(2)
        while True:
            with app.app_context():
                try:
                    webhook = ensure_twilio_inbound_webhook()
                    current_url = str(webhook.get('current_url') or '')
                    if not _is_cloud_run_webhook(current_url):
                        catch_up_sms_onboarding_replies()
                except Exception:
                    app.logger.exception('Local SMS inbound bridge failed')
            time.sleep(12)

    threading.Thread(target=_loop, name='sms-inbound-bridge', daemon=True).start()


def current_inbound_webhook_url() -> str:
    from .routes import (
        _build_public_route_url,
        _ensure_managed_cloudflared_tunnel,
        _is_local_request_host,
        _public_tunnel_is_live,
    )

    local_request = False
    if has_request_context():
        host = (request.host or '').split(':', 1)[0].lower()
        local_request = _is_local_request_host(host)

    app_host = (current_app.config.get('APP_HOST') or '127.0.0.1').strip().lower()
    local_app = app_host in {'127.0.0.1', 'localhost', '0.0.0.0', '::', '::1'}
    has_public_base = bool(str(current_app.config.get('PUBLIC_BASE_URL') or '').strip())
    use_local_tunnel = local_request or (not has_request_context() and local_app and not has_public_base)

    if use_local_tunnel:
        tunnel = str(current_app.config.get('PUBLIC_TUNNEL_URL') or '').strip().rstrip('/')
        if tunnel and not _public_tunnel_is_live(tunnel):
            tunnel = ''
        if not tunnel:
            tunnel = str(_ensure_managed_cloudflared_tunnel() or '').strip().rstrip('/')
        if tunnel:
            current_app.config['PUBLIC_TUNNEL_URL'] = tunnel
            return f'{tunnel}/sms/webhook'
        return ''

    if has_request_context():
        return _build_public_route_url('main.sms_webhook') or ''
    return ''


def _progress_for_state(state: str) -> dict:
    for item in PROGRESS_STEPS:
        if item['key'] == state:
            return dict(item)
    return dict(PROGRESS_STEPS[0])


def _last_outbound_message(session: SmsOnboardingSession) -> str:
    for item in reversed(_session_transcript(session)):
        if item.get('direction') == 'outbound' and item.get('message'):
            return str(item['message'])
    return _help_message(session)


def _transcript_has_sid(session: SmsOnboardingSession, sid: str) -> bool:
    if not sid:
        return False
    return any(str(item.get('sid') or '') == sid for item in _session_transcript(session))


def _catch_up_session_from_twilio(session: SmsOnboardingSession) -> dict:
    client = _twilio_client()
    from_number = current_app.config.get('TWILIO_PHONE_NUMBER')
    if not client or not from_number:
        return {'processed': 0, 'replies_sent': 0}

    inbound_messages = []
    started = _session_start_utc(session)
    for message in client.messages.list(
        from_=session.phone_number,
        to=from_number,
        limit=20,
    ):
        if str(getattr(message, 'direction', '') or '') != 'inbound':
            continue
        if not _twilio_message_belongs_to_session(message, session):
            continue
        inbound_messages.append(message)
    inbound_messages.reverse()
    processed = 0
    replies_sent = 0
    for message in inbound_messages:
        sid = str(getattr(message, 'sid', '') or '')
        body = str(getattr(message, 'body', '') or '')
        if not body:
            continue
        if _looks_like_community_keyword(body):
            continue
        if sid and _transcript_has_sid(session, sid):
            continue
        if any(
            item.get('direction') == 'inbound' and str(item.get('message') or '') == body
            for item in _session_transcript(session)
        ):
            continue
        session = db.session.get(SmsOnboardingSession, session.id) or session
        if session.status not in {
            SmsOnboardingSession.STATUS_IN_PROGRESS,
            SmsOnboardingSession.STATUS_AWAITING_CLAIM,
        }:
            break
        reply = handle_inbound_sms_onboarding(session.phone_number, body, inbound_sid=sid)
        processed += 1
        if reply:
            outbound_sid = _send_sms(session.phone_number, reply)
            session = db.session.get(SmsOnboardingSession, session.id) or session
            session.last_outbound_sid = outbound_sid or session.last_outbound_sid
            db.session.add(session)
            db.session.commit()
            replies_sent += 1
    return {'processed': processed, 'replies_sent': replies_sent}


def _welcome_message() -> str:
    return (
        "Welcome to Kumbu Connect! We're excited to set up your CBO profile.\n\n"
        "Please reply with:\n"
        "1) Your full name\n"
        "2) Your CBO's name\n"
        "3) The year it was founded\n\n"
        "One short message in your own words is perfect. Reply STOP to opt out."
    )


def _operations_message(answers: dict) -> str:
    first_name = _first_name(answers.get('full_name'))
    greeting = f'Thanks, {first_name}.' if first_name else 'Thanks.'
    return (
        f"{greeting} Next, please reply with:\n"
        "1) Your role in the CBO (chairperson, director, coordinator, etc.)\n"
        "2) Your CBO office address\n"
        "3) Where your programs run\n\n"
        "You can send this as one message."
    )


def _account_message() -> str:
    return (
        "Almost done. Please reply with:\n"
        "1) The email you want for your Kumbu Connect account\n"
        "2) Whether the CBO has a dedicated bank account (yes/no), and the bank name if yes\n"
        "3) One short story or recent milestone that shows your CBO's impact"
    )


def _email_followup_message() -> str:
    return (
        "We still need the email address that should own this CBO account. "
        "Please reply with that email, for example name@example.com."
    )


def _help_message(session: SmsOnboardingSession) -> str:
    if session.status == SmsOnboardingSession.STATUS_AWAITING_CLAIM:
        return (
            "Your Kumbu Connect profile is ready. Use the link in our last message to create a password. "
            "Reply STOP to cancel messages."
        )
    state = session.conversation_state
    if state == 'awaiting_web_verification':
        return _web_verification_message(session) or _account_message()
    if state == 'awaiting_operations':
        return _operations_message(_session_answers(session))
    if state in {'awaiting_account', 'awaiting_email'}:
        return _email_followup_message() if state == 'awaiting_email' else _account_message()
    return _welcome_message()


def _claim_message(session: SmsOnboardingSession, user: User, claim_url: str) -> str:
    answers = _session_answers(session)
    cbo_name = answers.get('cbo_name') or (session.provisioned_cbo.name if session.provisioned_cbo else 'your CBO')
    first_name = _first_name(answers.get('full_name') or user.display_name)
    greeting = f'Thank you, {first_name}.' if first_name else 'Thank you.'
    return (
        f"{greeting} We've created a starter profile for {cbo_name}.\n\n"
        f"Create your password here (your email is already filled in):\n{claim_url}\n\n"
        "After you set a password you'll land on your pre-populated profile."
    )


def _next_outbound_message(session: SmsOnboardingSession) -> str:
    answers = _session_answers(session)

    if session.status == SmsOnboardingSession.STATUS_AWAITING_CLAIM:
        return _resend_claim_message(session)

    if not _has_value(answers, 'full_name') or not _has_value(answers, 'cbo_name'):
        session.conversation_state = 'awaiting_basics'
        missing = []
        if not _has_value(answers, 'full_name'):
            missing.append('your full name')
        if not _has_value(answers, 'cbo_name'):
            missing.append("your CBO's name")
        return (
            "Thanks — I still need "
            + ' and '.join(missing)
            + ". Please reply with those details so we can continue."
        )

    if not _has_any(answers, OPERATIONS_FIELDS):
        session.conversation_state = 'awaiting_operations'
        return _operations_message(answers)

    if not _has_value(answers, 'email_address'):
        if session.conversation_state == 'awaiting_email':
            return _email_followup_message()
        if session.conversation_state != 'awaiting_account':
            session.conversation_state = 'awaiting_account'
            return _account_message()
        session.conversation_state = 'awaiting_email'
        return _email_followup_message()

    if session.conversation_state in {'awaiting_basics', 'awaiting_operations'} and not _has_value(answers, 'anecdotal_story'):
        session.conversation_state = 'awaiting_account'
        return _account_message()

    if session.conversation_state != 'awaiting_web_verification':
        verification = _maybe_web_verification_message(session)
        if verification:
            return verification

    return _provision_and_claim_message(session)


def _provision_and_claim_message(session: SmsOnboardingSession) -> str:
    from .routes import (
        _build_offline_intake_answer_items,
        _ingest_intake_response_payload,
    )

    answers = _finalize_answers(session)
    email = str(answers.get('email_address') or '').strip().lower()
    source_cbo = session.source_cbo or db.session.get(CBO, session.source_cbo_id)
    if source_cbo is None:
        raise RuntimeError('SMS onboarding is missing its source CBO.')

    now = datetime.utcnow().isoformat()
    response_payload = {
        'form_id': f'sms-onboarding-source-{source_cbo.id}',
        'response_id': f'sms-onboarding-{session.id}',
        'respondent_email': email,
        'create_time': now,
        'submitted_at': now,
        'answers': _build_offline_intake_answer_items(answers),
        'file_uploads': [],
        'raw_response': {
            'source': 'sms_onboarding',
            'session_id': session.id,
            'phone_number': session.phone_number,
            'submitted_at': now,
            'form_values': answers,
        },
    }
    result = _ingest_intake_response_payload(source_cbo, response_payload, allow_claude=False)
    session = db.session.get(SmsOnboardingSession, session.id)
    if session is None:
        raise RuntimeError('The SMS onboarding session disappeared during provisioning.')

    if result.get('failures'):
        session.last_error = '; '.join(str(item) for item in result['failures'][:3])
        db.session.add(session)
        db.session.commit()
        return (
            "We saved your answers, but could not finish creating the account yet. "
            "Please reply again in a few minutes or ask your Kumbu Connect contact for help."
        )

    user = User.query.filter_by(email=email).first()
    if user is None:
        session.last_error = 'Provisioning finished without a user account.'
        db.session.add(session)
        db.session.commit()
        return (
            "We saved your CBO details, but still need a moment to finish the account. "
            "Please reply again shortly."
        )

    token = build_claim_token(session, user)
    session.claim_token = token
    session.provisioned_user_id = user.id
    session.provisioned_cbo_id = user.cbo_id
    session.google_form_response_id = _latest_sms_response_id(source_cbo, session)
    session.status = SmsOnboardingSession.STATUS_AWAITING_CLAIM
    session.conversation_state = 'awaiting_claim'
    session.last_error = ''
    session.answers_json = json.dumps(answers)
    db.session.add(session)
    db.session.commit()

    _apply_session_web_research_to_provisioned_cbo(session)
    db.session.commit()

    claim_url = _claim_url(token)
    return _claim_message(session, user, claim_url)


def _resend_claim_message(session: SmsOnboardingSession) -> str:
    user = session.provisioned_user or db.session.get(User, session.provisioned_user_id)
    if user is None:
        session.status = SmsOnboardingSession.STATUS_IN_PROGRESS
        session.conversation_state = 'awaiting_account'
        return _account_message()
    token = session.claim_token or build_claim_token(session, user)
    session.claim_token = token
    return _claim_message(session, user, _claim_url(token))


def _claim_url(token: str) -> str:
    from .routes import _build_public_route_url

    public_url = _build_public_route_url('auth.sms_onboarding_setup', token=token)
    if public_url:
        return public_url
    if has_request_context():
        return url_for('auth.sms_onboarding_setup', token=token, _external=True)
    with current_app.test_request_context('/'):
        return url_for('auth.sms_onboarding_setup', token=token, _external=True)


def schedule_session_web_research(session: SmsOnboardingSession) -> None:
    """Kick off public-web research as soon as we know the CBO name."""
    answers = _session_answers(session)
    cbo_name = str(answers.get('cbo_name') or '').strip()
    if not cbo_name or _is_weak_onboarding_value(cbo_name, 'cbo_name'):
        return
    existing = load_session_web_research(session)
    if (
        existing.get('cbo_name') == cbo_name
        and existing.get('status') in {'pending', 'ready', 'verified', 'rejected', 'skipped', 'pending_review'}
    ):
        return
    store_session_web_research(session, {
        'status': 'pending',
        'cbo_name': cbo_name,
        'started_at': datetime.utcnow().isoformat(),
        'claims': [],
    })
    db.session.add(session)
    db.session.commit()

    with _WEB_RESEARCH_IN_FLIGHT_GUARD:
        if session.id in _WEB_RESEARCH_IN_FLIGHT:
            return
        _WEB_RESEARCH_IN_FLIGHT.add(session.id)

    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                _complete_session_web_research(session.id)
        finally:
            with _WEB_RESEARCH_IN_FLIGHT_GUARD:
                _WEB_RESEARCH_IN_FLIGHT.discard(session.id)

    threading.Thread(target=_run, name=f'sms-web-research-{session.id}', daemon=True).start()


def _complete_session_web_research(session_id: int) -> dict:
    session = db.session.get(SmsOnboardingSession, session_id)
    if session is None:
        return {}
    answers = _session_answers(session)
    location = str(answers.get('cbo_program_locations') or answers.get('cbo_office_address') or '').strip()
    extra = str(answers.get('anecdotal_story') or answers.get('milestones') or '').strip()
    research = research_cbo_online(answers.get('cbo_name') or '', location=location, extra_context=extra)
    store_session_web_research(session, research)
    db.session.add(session)
    db.session.commit()
    if session.provisioned_cbo_id and research.get('claims'):
        _apply_session_web_research_to_provisioned_cbo(session, only_verified=True)
        db.session.commit()
    sync_sms_onboarding_session(session)
    return research


def _maybe_web_verification_message(session: SmsOnboardingSession) -> str:
    research = load_session_web_research(session)
    if research.get('status') == 'pending':
        research = _wait_briefly_for_web_research(session)
    claims = [item for item in (research.get('claims') or []) if isinstance(item, dict)]
    if research.get('status') in {'verified', 'rejected', 'skipped'}:
        return ''
    if not claims:
        return ''
    session.conversation_state = 'awaiting_web_verification'
    return _web_verification_message(session, research)


def _wait_briefly_for_web_research(session: SmsOnboardingSession, timeout_seconds: float = 2.5) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        db.session.expire(session)
        session = db.session.get(SmsOnboardingSession, session.id) or session
        research = load_session_web_research(session)
        if research.get('status') != 'pending':
            return research
        time.sleep(0.35)
    return load_session_web_research(session)


def _web_verification_message(session: SmsOnboardingSession, research: dict | None = None) -> str:
    payload = research or load_session_web_research(session)
    answers = _session_answers(session)
    cbo_name = answers.get('cbo_name') or payload.get('cbo_name') or 'your CBO'
    return format_web_verification_sms(cbo_name, payload)


def _handle_web_verification_reply(session: SmsOnboardingSession, message: str) -> str:
    research = load_session_web_research(session)
    decision = parse_web_verification_reply(message, research.get('claims') or [])
    if decision.get('decision') == 'unknown':
        return (
            'Please reply YES if those public facts look right, NO if they are about the wrong group, '
            'SKIP to ignore them, or tell us which number is wrong — for example "2 is wrong".'
        )
    updated = apply_verification_decision(research, decision)
    store_session_web_research(session, updated)
    db.session.add(session)
    db.session.commit()
    return _provision_and_claim_message(session)


def _apply_session_web_research_to_provisioned_cbo(
    session: SmsOnboardingSession,
    only_verified: bool = True,
) -> None:
    cbo = session.provisioned_cbo or db.session.get(CBO, session.provisioned_cbo_id)
    research = load_session_web_research(session)
    if cbo is None or not research:
        return
    apply_web_research_to_cbo(cbo, research, only_verified=only_verified)
    db.session.add(cbo)


def _serialize_session_web_research(session: SmsOnboardingSession) -> dict:
    research = load_session_web_research(session)
    claims = [item for item in (research.get('claims') or []) if isinstance(item, dict)]
    return {
        'status': str(research.get('status') or ''),
        'summary': str(research.get('summary') or ''),
        'claim_count': len(claims),
        'verified_count': sum(1 for item in claims if item.get('verified')),
        'error': str(research.get('error') or ''),
    }


def _latest_sms_response_id(source_cbo: CBO, session: SmsOnboardingSession) -> int | None:
    from .models import GoogleFormResponse

    record = GoogleFormResponse.query.filter_by(
        form_id=f'sms-onboarding-source-{source_cbo.id}',
        response_id=f'sms-onboarding-{session.id}',
    ).first()
    return record.id if record else None


def repair_weak_sms_onboarding_answers(session: SmsOnboardingSession, send_followup: bool = False) -> bool:
    """Replace keyword placeholders like KIWA with later real name/CBO answers."""
    if session.status != SmsOnboardingSession.STATUS_IN_PROGRESS:
        return False

    answers = _session_answers(session)
    original = dict(answers)
    original_state = session.conversation_state
    tainted_basics = any(_is_weak_onboarding_value(answers.get(field), field) for field in ('full_name', 'cbo_name'))
    if not tainted_basics:
        return False

    for field in BASIC_FIELDS:
        if _is_weak_onboarding_value(answers.get(field), field):
            answers.pop(field, None)
    for field in OPERATIONS_FIELDS:
        answers.pop(field, None)
    session.answers_json = json.dumps(answers)

    for item in _session_transcript(session):
        if item.get('direction') != 'inbound':
            continue
        message = str(item.get('message') or '').strip()
        if not message or _looks_like_community_keyword(message):
            continue
        expected = list(BASIC_FIELDS) if _looks_like_basics_reply(message) else _expected_fields_for_state(
            session.conversation_state,
            _session_answers(session),
        )
        extracted = extract_sms_onboarding_answers(
            message,
            expected_fields=expected,
            already_collected=_session_answers(session),
            transcript=_session_transcript(session),
        )
        _merge_answers(session, extracted, message)

    _sync_conversation_state(session)
    changed = _session_answers(session) != original or session.conversation_state != original_state
    if not changed:
        return False

    db.session.add(session)
    db.session.commit()
    if send_followup and session.conversation_state != original_state:
        reply = _help_message(session)
        _append_transcript(session, 'outbound', reply)
        try:
            session.last_outbound_sid = _send_sms(session.phone_number, reply) or session.last_outbound_sid
        except Exception:
            current_app.logger.exception('Failed to send SMS onboarding name-correction follow-up')
        db.session.add(session)
        db.session.commit()
    sync_sms_onboarding_session(session)
    return True


def _sync_conversation_state(session: SmsOnboardingSession) -> None:
    if session.status != SmsOnboardingSession.STATUS_IN_PROGRESS:
        return
    if session.conversation_state == 'awaiting_web_verification':
        return
    answers = _session_answers(session)
    if _is_weak_onboarding_value(answers.get('full_name'), 'full_name') or _is_weak_onboarding_value(answers.get('cbo_name'), 'cbo_name'):
        session.conversation_state = 'awaiting_basics'
        return
    if not _has_any(answers, OPERATIONS_FIELDS):
        session.conversation_state = 'awaiting_operations'
        return
    if not _has_value(answers, 'email_address'):
        session.conversation_state = 'awaiting_account'


def _reserved_sms_tokens() -> set[str]:
    tokens = set()
    for cbo in CBO.query.all():
        keyword = get_cbo_keyword(cbo)
        if keyword:
            tokens.add(keyword)
        name_token = normalize_sms_keyword(cbo.name)
        if name_token:
            tokens.add(name_token)
    return tokens


def _looks_like_community_keyword(message: str) -> bool:
    token = normalize_sms_keyword(message)
    if not token or ' ' in (message or '').strip():
        return False
    if len((message or '').strip().split()) != 1:
        return False
    return token in _reserved_sms_tokens()


def _looks_like_basics_reply(message: str) -> bool:
    raw = str(message or '').strip()
    if not raw:
        return False
    has_year = bool(YEAR_RE.search(raw))
    has_separators = (',' in raw) or (';' in raw) or bool(re.search(r'\d+[\).\:-]', raw))
    return has_year and (has_separators or len(raw.split()) >= 3)


def _is_weak_onboarding_value(value: str, field: str) -> bool:
    cleaned = str(value or '').strip()
    if not cleaned:
        return True
    if field not in {'full_name', 'cbo_name'}:
        return False
    token = normalize_sms_keyword(cleaned)
    return bool(token and token in _reserved_sms_tokens() and len(cleaned.split()) <= 2)


def _expected_fields_for_state(state: str, answers: dict | None = None) -> list[str]:
    answers = answers or {}
    if state == 'awaiting_operations':
        fields = list(OPERATIONS_FIELDS)
    elif state in {'awaiting_account', 'awaiting_email'}:
        fields = ['email_address', 'registered_bank_account', 'bank_name', 'anecdotal_story', 'milestones']
    elif state == 'awaiting_web_verification':
        fields = []
    else:
        fields = list(BASIC_FIELDS)

    ordered = []
    for field in [item for item in BASIC_FIELDS if _is_weak_onboarding_value(answers.get(item), item)] + fields:
        if field not in ordered:
            ordered.append(field)
    return ordered


def _session_start_utc(session: SmsOnboardingSession) -> datetime | None:
    started = session.created_at
    if started is None:
        return None
    if started.tzinfo is None:
        return started.replace(tzinfo=timezone.utc)
    return started.astimezone(timezone.utc)


def _twilio_message_belongs_to_session(message, session: SmsOnboardingSession) -> bool:
    started = _session_start_utc(session)
    sent = getattr(message, 'date_sent', None)
    if started is None or sent is None:
        return True
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent >= (started - timedelta(seconds=30))


def _scrub_stale_keyword_transcript(session: SmsOnboardingSession) -> bool:
    transcript = _session_transcript(session)
    cleaned = []
    skip_next_outbound = False
    for item in transcript:
        message = str(item.get('message') or '').strip()
        if item.get('direction') == 'inbound' and _looks_like_community_keyword(message):
            skip_next_outbound = True
            continue
        if skip_next_outbound and item.get('direction') == 'outbound':
            skip_next_outbound = False
            continue
        skip_next_outbound = False
        cleaned.append(item)
    if cleaned == transcript:
        return False
    session.transcript_json = json.dumps(cleaned)
    db.session.add(session)
    db.session.commit()
    return True


def _session_answers(session: SmsOnboardingSession) -> dict:
    try:
        value = json.loads(session.answers_json or '{}')
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _session_transcript(session: SmsOnboardingSession) -> list:
    try:
        value = json.loads(session.transcript_json or '[]')
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _append_transcript(session: SmsOnboardingSession, direction: str, message: str, sid: str = '') -> None:
    transcript = _session_transcript(session)
    entry = {
        'direction': direction,
        'message': message,
        'at': datetime.utcnow().isoformat(),
    }
    if sid:
        entry['sid'] = sid
    transcript.append(entry)
    session.transcript_json = json.dumps(transcript)
    session.updated_at = datetime.utcnow()


def _merge_answers(session: SmsOnboardingSession, extracted: dict, raw_message: str) -> None:
    answers = _session_answers(session)
    for key, value in (extracted or {}).items():
        cleaned = str(value or '').strip()
        if cleaned and (not _has_value(answers, key) or _is_weak_onboarding_value(answers.get(key), key)):
            answers[key] = cleaned
    if not _has_value(answers, 'email_address'):
        match = EMAIL_RE.search(raw_message or '')
        if match:
            answers['email_address'] = match.group(0).lower()
    if not _has_value(answers, 'year_incorporated'):
        match = YEAR_RE.search(raw_message or '')
        if match:
            answers['year_incorporated'] = match.group(0)
    if not _has_value(answers, 'whatsapp_phone_number'):
        answers['whatsapp_phone_number'] = session.phone_number
    session.answers_json = json.dumps(answers)


def _finalize_answers(session: SmsOnboardingSession) -> dict:
    answers = dict(_session_answers(session))
    answers['whatsapp_phone_number'] = session.phone_number
    answers['email_address'] = str(answers.get('email_address') or '').strip().lower()
    if not _has_value(answers, 'signature_and_date'):
        signer = answers.get('full_name') or 'CBO leader'
        answers['signature_and_date'] = f'{signer} via SMS on {datetime.utcnow().date().isoformat()}'
    if not _has_value(answers, 'anecdotal_story') and _has_value(answers, 'milestones'):
        answers['anecdotal_story'] = answers['milestones']
    if _has_value(answers, 'registered_bank_account'):
        answers['registered_bank_account'] = _normalize_yes_no(answers['registered_bank_account'])
    return answers


def _normalize_yes_no(value: str) -> str:
    lowered = str(value or '').strip().lower()
    if lowered in {'y', 'yes', 'true', '1'}:
        return 'Yes'
    if lowered in {'n', 'no', 'false', '0'}:
        return 'No'
    if 'yes' in lowered:
        return 'Yes'
    if 'no' in lowered:
        return 'No'
    return str(value or '').strip()


def _has_value(answers: dict, field: str) -> bool:
    return bool(str((answers or {}).get(field) or '').strip())


def _has_any(answers: dict, fields: tuple[str, ...]) -> bool:
    return any(_has_value(answers, field) for field in fields)


def _first_name(value: str) -> str:
    return str(value or '').strip().split()[0] if str(value or '').strip() else ''


def _twilio_client() -> Client | None:
    account_sid = current_app.config.get('TWILIO_ACCOUNT_SID')
    auth_token = current_app.config.get('TWILIO_AUTH_TOKEN')
    if not account_sid or not auth_token:
        return None
    return Client(account_sid, auth_token)


def _send_sms(to_number: str, body: str) -> str:
    from_number = current_app.config.get('TWILIO_PHONE_NUMBER')
    client = _twilio_client()
    if not client or not from_number:
        raise RuntimeError('Twilio credentials are not configured.')
    try:
        message = client.messages.create(body=body, from_=from_number, to=to_number)
        return message.sid or ''
    except TwilioRestException as exc:
        raise RuntimeError(exc.msg or 'Twilio could not send the onboarding SMS.') from exc
