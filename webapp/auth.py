"""Authentication blueprint for Kumbu Connect."""
import json
import os
import re
import secrets
from urllib.parse import urlparse

import requests
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from google_auth_oauthlib.flow import Flow

from .models import CBO, GoogleFormResponse, User, db


auth_bp = Blueprint('auth', __name__)
GOOGLE_SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]
GOOGLE_USERINFO_ENDPOINT = 'https://openidconnect.googleapis.com/v1/userinfo'
TEMP_BYPASS_FUNDER_EMAIL = 'temp-funder-bypass@local.kumbu.invalid'
TEMP_BYPASS_CBO_EMAIL = 'temp-cbo-bypass@local.kumbu.invalid'


def _normalize_email(value: str) -> str:
    return str(value or '').strip().lower()


def _normalize_portal_role(value: str | None) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in {'funder', 'cbo'} else ''


def _set_active_role(role: str | None) -> None:
    normalized = _normalize_portal_role(role)
    if normalized:
        session['active_role'] = normalized
        return
    session.pop('active_role', None)


def _google_profile_picture_url(profile: dict | None) -> str:
    if not isinstance(profile, dict):
        return ''

    picture_url = str(profile.get('picture') or '').strip()
    if not picture_url:
        return ''

    parsed = urlparse(picture_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return ''
    return picture_url


def _set_auth_provider(provider: str | None, profile: dict | None = None) -> None:
    normalized = str(provider or '').strip().lower()
    if normalized == 'google':
        session['auth_provider'] = 'google'
        picture_url = _google_profile_picture_url(profile)
        if picture_url:
            session['google_profile_picture_url'] = picture_url
        else:
            session.pop('google_profile_picture_url', None)
        return

    session.pop('auth_provider', None)
    session.pop('google_profile_picture_url', None)


def _active_role_for_user(user: User) -> str:
    selected_role = _normalize_portal_role(session.get('active_role'))
    if selected_role and user.has_role(selected_role):
        return selected_role
    if user.is_cbo:
        return 'cbo'
    if user.is_funder:
        return 'funder'
    return str(user.role or '').strip().lower()


def _login_redirect_for_claim(email: str) -> str:
    return url_for('auth.login', email=email, role='cbo', from_claim='1')


def _user_can_finish_claim(user: User | None) -> bool:
    return bool(user and user.has_role('cbo') and user.cbo_id and (
        user.account_status == 'pending_claim' or user.needs_password_setup
    ))


def _normalize_cbo_slug(name: str) -> str:
    normalized = re.sub(r'[^a-z0-9]+', '-', str(name or '').strip().lower()).strip('-')
    return normalized or 'cbo'


def _unique_cbo_slug(name: str) -> str:
    base_slug = _normalize_cbo_slug(name)
    candidate = base_slug
    suffix = 2
    while CBO.query.filter_by(slug=candidate).first() is not None:
        candidate = f'{base_slug}-{suffix}'
        suffix += 1
    return candidate


def _safe_next_url(raw_url: str | None, default_url: str | None = None) -> str:
    candidate = (raw_url or '').strip()
    fallback = default_url or url_for('main.marketplace')
    if not candidate:
        return fallback

    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not candidate.startswith('/'):
        return fallback
    return candidate


def _next_url_allowed_for_user(user: User, raw_url: str | None) -> bool:
    candidate = (raw_url or '').strip()
    if not candidate:
        return False

    path = urlparse(candidate).path or ''
    if not path or path == '/':
        return True

    if path == url_for('main.marketplace'):
        return user.has_role('funder')
    if path == url_for('main.cbo_dashboard'):
        return user.has_role('cbo') and bool(user.cbo_id)
    if path == url_for('main.community_feedback_admin'):
        return bool(session.get('developer_access'))
    if path == url_for('main.developer_sms_activity'):
        return bool(session.get('developer_access'))
    if path == url_for('main.bookkeeping_admin'):
        return user.has_role('funder')

    sms_activity_match = re.fullmatch(r'/admin/community-feedback/(\d+)(?:/.*)?', path)
    if sms_activity_match:
        if session.get('developer_access'):
            return True
        return user.has_role('cbo') and user.cbo_id == int(sms_activity_match.group(1))

    cbo_profile_match = re.fullmatch(r'/cbo/([^/]+)', path)
    if cbo_profile_match:
        if user.has_role('funder'):
            return True
        cbo = CBO.query.filter_by(slug=cbo_profile_match.group(1)).first()
        return cbo is not None and user.has_role('cbo') and user.cbo_id == cbo.id

    google_upload_match = re.fullmatch(r'/google-form-upload/(\d+)/file', path)
    if google_upload_match:
        from .models import GoogleFormUpload

        upload = db.session.get(GoogleFormUpload, int(google_upload_match.group(1)))
        if upload is None:
            return False
        return user.has_role('funder') or (user.has_role('cbo') and user.cbo_id == upload.cbo_id)

    bookkeeping_image_match = re.fullmatch(r'/bookkeeping/(\d+)/image', path)
    if bookkeeping_image_match:
        from .models import BookkeepingDocument

        document = db.session.get(BookkeepingDocument, int(bookkeeping_image_match.group(1)))
        if document is None:
            return False
        return user.has_role('funder') or (user.has_role('cbo') and user.cbo_id == document.cbo_id)

    funding_file_match = re.fullmatch(r'/funding-audit/(\d+)/file', path)
    if funding_file_match:
        from .models import FundingAuditDocument

        document = db.session.get(FundingAuditDocument, int(funding_file_match.group(1)))
        if document is None:
            return False
        return user.has_role('funder') or (user.has_role('cbo') and user.cbo_id == document.cbo_id)

    return True


def _safe_json_list(raw_value) -> list:
    if isinstance(raw_value, list):
        return raw_value
    if raw_value in (None, ''):
        return []
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _extract_intake_email_from_answer_items(answer_items) -> str:
    for answer in _safe_json_list(answer_items):
        if not isinstance(answer, dict) or answer.get('answer_type') != 'text':
            continue

        title = re.sub(r'\s+', ' ', str(answer.get('title') or '').strip()).lower()
        if title != 'email address':
            continue

        for value in answer.get('values') or []:
            email = _normalize_email(value)
            if email:
                return email
    return ''


def _google_form_response_email(response_record: GoogleFormResponse) -> str:
    return _normalize_email(
        response_record.respondent_email
        or _extract_intake_email_from_answer_items(response_record.answers_json)
    )


def _bundle_response_email(response_payload: dict) -> str:
    return _normalize_email(
        response_payload.get('respondent_email')
        or _extract_intake_email_from_answer_items(response_payload.get('answers') or [])
    )


def _find_google_form_response_for_email(email: str) -> GoogleFormResponse | None:
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return None

    responses = GoogleFormResponse.query.order_by(
        GoogleFormResponse.response_submitted_at.desc(),
        GoogleFormResponse.id.desc(),
    ).all()
    for response in responses:
        if _google_form_response_email(response) == normalized_email:
            return response
    return None


def _sync_claim_candidate_for_email(email: str) -> GoogleFormResponse | None:
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return None

    from .google_forms_service import get_form_response_bundle
    from .routes import _sync_google_form_responses

    existing_response = _find_google_form_response_for_email(normalized_email)
    if existing_response is not None:
        cbo = db.session.get(CBO, existing_response.cbo_id)
        if cbo is not None and cbo.intake_form_id:
            try:
                _sync_google_form_responses(cbo)
            except Exception as exc:
                current_app.logger.warning(
                    'Failed to re-sync existing intake-form response for %s: %s',
                    normalized_email,
                    exc,
                )
        return _find_google_form_response_for_email(normalized_email)

    cbos = CBO.query.filter(
        CBO.intake_form_id.isnot(None),
        CBO.intake_form_id != '',
    ).order_by(CBO.id.asc()).all()

    for cbo in cbos:
        try:
            bundle = get_form_response_bundle(cbo.intake_form_id)
        except Exception as exc:
            current_app.logger.warning(
                'Failed to inspect intake-form responses for CBO %s while looking up %s: %s',
                cbo.id,
                normalized_email,
                exc,
            )
            continue

        has_matching_response = any(
            _bundle_response_email(response_payload) == normalized_email
            for response_payload in (bundle.get('responses') or [])
        )
        if not has_matching_response:
            continue

        try:
            _sync_google_form_responses(cbo)
        except Exception as exc:
            current_app.logger.warning(
                'Failed to sync intake-form responses for CBO %s while looking up %s: %s',
                cbo.id,
                normalized_email,
                exc,
            )
        return _find_google_form_response_for_email(normalized_email)

    return None


def _claim_candidate_state(email: str, auto_sync: bool = True) -> dict:
    normalized_email = _normalize_email(email)
    state = {
        'pending_user': None,
        'active_user': None,
        'response': None,
        'error': '',
    }
    if not normalized_email:
        return state

    user = User.query.filter(User.email.ilike(normalized_email)).first()
    if user is not None and user.has_role('cbo') and user.cbo_id:
        if _user_can_finish_claim(user):
            state['pending_user'] = user
        else:
            state['active_user'] = user
        return state

    response_record = _find_google_form_response_for_email(normalized_email)
    if response_record is None and auto_sync:
        response_record = _sync_claim_candidate_for_email(normalized_email)
    state['response'] = response_record

    user = User.query.filter(User.email.ilike(normalized_email)).first()
    if user is not None and user.has_role('cbo') and user.cbo_id:
        if _user_can_finish_claim(user):
            state['pending_user'] = user
        else:
            state['active_user'] = user
        return state

    blocking_user = User.query.filter(User.email.ilike(normalized_email)).first()
    if response_record is not None and response_record.provisioning_error:
        state['error'] = response_record.provisioning_error
    elif blocking_user is not None and not blocking_user.has_role('cbo'):
        state['error'] = (
            f'That email already belongs to a {blocking_user.role} account in Kumbu Connect. '
            'Use a different email for the CBO claim flow or change that existing account first.'
        )
    else:
        state['error'] = (
            'No pre-built CBO account was found for that email yet. '
            'If you just submitted the intake form, wait a moment and try again.'
        )
    return state


def _default_redirect_target(user: User) -> str:
    if _active_role_for_user(user) == 'cbo' and user.cbo_id:
        return url_for('main.cbo_dashboard')
    return url_for('main.marketplace')


def _login_redirect_target(user: User) -> str:
    default_target = _default_redirect_target(user)
    next_target = _safe_next_url(session.pop('login_next', None), default_target)
    if _next_url_allowed_for_user(user, next_target):
        return next_target
    return default_target


def _developer_redirect_target() -> str:
    return _safe_next_url(
        session.pop('developer_login_next', None),
        url_for('main.developer_sms_activity'),
    )


def _has_developer_access() -> bool:
    return current_user.is_authenticated and bool(session.get('developer_access'))


def _clear_auth_session_flags() -> None:
    session.pop('developer_access', None)
    session.pop('active_role', None)
    _set_auth_provider(None)
    session.pop('developer_google_code_verifier', None)
    session.pop('developer_sms_cbo_id', None)
    session.pop('developer_google_state', None)
    session.pop('developer_login_next', None)
    _clear_standard_google_session()


def _clear_standard_google_session() -> None:
    session.pop('google_login_code_verifier', None)
    session.pop('google_login_state', None)
    session.pop('google_login_email_hint', None)
    session.pop('google_login_action', None)
    session.pop('google_register_role', None)
    session.pop('google_register_cbo_name', None)
    session.pop('google_register_display_name', None)


def _google_login_context(config_key: str, error_prefix: str, allowed_emails: list[str] | None = None) -> dict:
    secret_path = str(current_app.config.get(config_key) or '').strip()
    if not secret_path:
        context = {
            'enabled': False,
            'error': f'{config_key} is not configured.',
        }
    elif not os.path.exists(secret_path):
        context = {
            'enabled': False,
            'error': f'{error_prefix} client secret file was not found: {secret_path}',
        }
    else:
        context = {
            'enabled': True,
            'error': '',
        }

    if allowed_emails is not None:
        context['allowed_emails'] = allowed_emails
    return context


def _developer_login_context() -> dict:
    return _google_login_context(
        'GOOGLE_DEVELOPER_CLIENT_SECRET_JSON',
        'Google developer login',
        current_app.config.get('GOOGLE_DEVELOPER_ALLOWED_EMAILS') or [],
    )


def _user_google_login_context() -> dict:
    return _google_login_context(
        'GOOGLE_USER_CLIENT_SECRET_JSON',
        'Google sign-in',
    )


def _google_redirect_uri(endpoint: str) -> str:
    callback_url = url_for(endpoint, _external=True)
    parsed = urlparse(callback_url)
    hostname = (parsed.hostname or '').strip().lower()
    if hostname == '0.0.0.0':
        callback_url = callback_url.replace(parsed.hostname, 'localhost', 1)
    return callback_url


def _configure_google_oauth_transport(redirect_uri: str) -> None:
    parsed = urlparse(redirect_uri)
    hostname = (parsed.hostname or '').strip().lower()
    if parsed.scheme == 'http' and hostname in {'127.0.0.1', 'localhost'}:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
        return
    os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None)


def _build_google_flow(
    config_key: str,
    redirect_uri: str,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    _configure_google_oauth_transport(redirect_uri)
    flow = Flow.from_client_secrets_file(
        current_app.config[config_key],
        scopes=GOOGLE_SCOPES,
        state=state,
        code_verifier=code_verifier,
    )
    flow.redirect_uri = redirect_uri
    return flow


def _build_developer_google_flow(
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    context = _developer_login_context()
    if not context['enabled']:
        raise RuntimeError(context['error'])
    return _build_google_flow(
        'GOOGLE_DEVELOPER_CLIENT_SECRET_JSON',
        _google_redirect_uri('auth.developer_google_callback'),
        state=state,
        code_verifier=code_verifier,
    )


def _build_user_google_flow(
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    context = _user_google_login_context()
    if not context['enabled']:
        raise RuntimeError(context['error'])
    return _build_google_flow(
        'GOOGLE_USER_CLIENT_SECRET_JSON',
        _google_redirect_uri('auth.google_login_callback'),
        state=state,
        code_verifier=code_verifier,
    )


def _fetch_google_profile(flow: Flow, authorization_response: str) -> dict:
    flow.fetch_token(authorization_response=authorization_response)
    response = requests.get(
        GOOGLE_USERINFO_ENDPOINT,
        headers={'Authorization': f'Bearer {flow.credentials.token}'},
        timeout=15,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _validated_google_identity(profile: dict) -> tuple[str, str]:
    email = _normalize_email(profile.get('email') or '')
    name = str(profile.get('name') or '').strip() or email
    if not email:
        raise RuntimeError('Google did not return an email address for this account.')
    if not profile.get('email_verified'):
        raise RuntimeError('Your Google account email is not verified.')
    return email, name


def _developer_user_from_google_profile(profile: dict) -> User:
    email, name = _validated_google_identity(profile)

    allowed_emails = current_app.config.get('GOOGLE_DEVELOPER_ALLOWED_EMAILS') or []
    if allowed_emails and email not in allowed_emails:
        raise RuntimeError('This Google account is not allowed to use the developer login.')

    bypass_emails = current_app.config.get('GOOGLE_DEVELOPER_EXISTING_USER_BYPASS_EMAILS') or []
    user = User.query.filter_by(email=email).first()
    if user and not user.has_role('funder') and email not in bypass_emails:
        raise RuntimeError('This Google account is already linked to a non-developer user.')

    if not user:
        user = User(
            email=email,
            role='funder',
            account_status='active',
            display_name=name,
        )
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
        db.session.commit()
        return user

    if name and user.display_name != name:
        user.display_name = name
        db.session.commit()
    return user


def _standard_user_from_google_profile(profile: dict, expected_email: str = '') -> User:
    email, name = _validated_google_identity(profile)
    if expected_email and email != expected_email:
        raise RuntimeError(
            'This Google account does not match the email you entered for your pre-built account.'
        )

    if expected_email:
        claim_state = _claim_candidate_state(email, auto_sync=True)
        user = claim_state.get('pending_user') or claim_state.get('active_user')
        if not user:
            raise RuntimeError(
                claim_state.get('error')
                or 'No Kumbu Connect account was found for this Google email yet. Submit the intake form or claim your account first.'
            )
    else:
        user = User.query.filter_by(email=email).first()
        if not user:
            raise RuntimeError(
                'No Kumbu Connect account was found for this Google email yet. Submit the intake form or claim your account first.'
            )

    if user.account_status == 'pending_claim':
        user.account_status = 'active'
    if name and user.display_name != name:
        user.display_name = name
    db.session.commit()
    return user


def _standard_user_from_google_registration(
    profile: dict,
    requested_role: str = 'funder',
    requested_cbo_name: str = '',
    requested_display_name: str = '',
) -> tuple[User, str]:
    email, google_name = _validated_google_identity(profile)
    role = _normalize_portal_role(requested_role) or 'funder'
    display_name = str(requested_display_name or '').strip() or google_name or email
    cbo_name = str(requested_cbo_name or '').strip()

    existing_user = User.query.filter_by(email=email).first()
    if existing_user is not None:
        account_activated = False
        updates_required = False
        if existing_user.has_role('cbo') and existing_user.account_status == 'pending_claim':
            existing_user.account_status = 'active'
            account_activated = True
            updates_required = True
        if display_name and existing_user.display_name != display_name:
            existing_user.display_name = display_name
            updates_required = True
        if updates_required:
            db.session.add(existing_user)
            db.session.commit()
        return existing_user, 'activated' if account_activated else 'existing'

    cbo = None
    if role == 'cbo':
        if not cbo_name:
            raise RuntimeError('Organisation name is required for CBO registration with Google.')
        cbo = CBO(name=cbo_name, slug=_unique_cbo_slug(cbo_name))
        db.session.add(cbo)
        db.session.flush()

    user = User(
        email=email,
        role=role,
        account_status='active',
        display_name=display_name,
        cbo_id=cbo.id if cbo else None,
    )
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(user)
    db.session.commit()
    return user, 'created'


def _preferred_temp_bypass_cbo() -> CBO | None:
    configured_cbo_id = current_app.config.get('TEMP_LOGIN_BYPASS_CBO_ID')
    if configured_cbo_id is not None:
        configured_cbo = db.session.get(CBO, configured_cbo_id)
        if configured_cbo is not None:
            return configured_cbo

    configured_slug = str(current_app.config.get('TEMP_LOGIN_BYPASS_CBO_SLUG') or '').strip().lower()
    if configured_slug:
        configured_cbo = CBO.query.filter_by(slug=configured_slug).first()
        if configured_cbo is not None:
            return configured_cbo

    demo_cbo_user = User.query.filter_by(email='cbo@demo.com').first()
    if demo_cbo_user and demo_cbo_user.has_role('cbo') and demo_cbo_user.cbo_id:
        demo_cbo = db.session.get(CBO, demo_cbo_user.cbo_id)
        if demo_cbo is not None:
            return demo_cbo

    cbo_user_counts: dict[int, int] = {}
    for user in User.query.all():
        if not user.has_role('cbo') or user.cbo_id is None:
            continue
        cbo_user_counts[user.cbo_id] = cbo_user_counts.get(user.cbo_id, 0) + 1

    if cbo_user_counts:
        candidate_cbos = {
            cbo.id: cbo
            for cbo in CBO.query.filter(CBO.id.in_(tuple(cbo_user_counts.keys()))).all()
        }
        ranked_cbo_ids = sorted(
            candidate_cbos,
            key=lambda cbo_id: (-cbo_user_counts[cbo_id], candidate_cbos[cbo_id].name.lower()),
        )
        for cbo_id in ranked_cbo_ids:
            candidate = candidate_cbos.get(cbo_id)
            if candidate is not None:
                return candidate

    return CBO.query.order_by(CBO.name.asc()).first()


def _ensure_temp_bypass_user(role: str) -> User:
    if role == 'funder':
        email = TEMP_BYPASS_FUNDER_EMAIL
        display_name = 'Temporary Funder Access'
        cbo_id = None
    elif role == 'cbo':
        cbo = _preferred_temp_bypass_cbo()
        if cbo is None:
            raise RuntimeError('No CBO profile is available for temporary CBO bypass access.')
        email = TEMP_BYPASS_CBO_EMAIL
        display_name = f'Temporary {cbo.name} Access'
        cbo_id = cbo.id
    else:
        raise RuntimeError('Unsupported temporary bypass role.')

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(
            email=email,
            role=role,
            account_status='active',
            display_name=display_name,
            cbo_id=cbo_id,
        )
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
        db.session.commit()
        return user

    if user.role != role:
        raise RuntimeError(f'Temporary bypass user {email} already exists with the wrong role.')

    updates_required = False
    if user.account_status != 'active':
        user.account_status = 'active'
        updates_required = True
    if user.display_name != display_name:
        user.display_name = display_name
        updates_required = True
    if user.cbo_id != cbo_id:
        user.cbo_id = cbo_id
        updates_required = True

    if updates_required:
        db.session.add(user)
        db.session.commit()
    return user


def _login_standard_user(user: User, email: str) -> str | None:
    if user.account_status != 'active' or user.needs_password_setup:
        flash(
            'Your account has already been prepared from your intake form. Claim it with the same email to create your password, or continue with Google to access it.',
            'info',
        )
        return url_for('auth.claim_account', email=email)

    session.pop('developer_access', None)
    _set_auth_provider(None)
    login_user(user, remember=True)
    return None


def _render_register_page(
    *,
    google_user: dict | None = None,
    prefill_email: str = '',
    prefill_display_name: str = '',
    prefill_cbo_name: str = '',
    selected_role: str = 'funder',
    next_url: str = '',
):
    return render_template(
        'auth/register.html',
        google_user=google_user or _user_google_login_context(),
        prefill_email=prefill_email,
        prefill_display_name=prefill_display_name,
        prefill_cbo_name=prefill_cbo_name,
        selected_role=_normalize_portal_role(selected_role) or 'funder',
        next_url=next_url,
    )


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(_default_redirect_target(current_user))

    next_url = request.args.get('next') or request.form.get('next') or session.get('login_next')
    if next_url:
        session['login_next'] = _safe_next_url(next_url, url_for('main.marketplace'))

    google_user = _user_google_login_context()
    developer_google = _developer_login_context()
    prefill_email = _normalize_email(request.args.get('email') or request.form.get('email') or '')
    selected_role = _normalize_portal_role(request.args.get('role') or request.form.get('role')) or 'funder'

    if request.args.get('from_claim') == '1' and request.method == 'GET':
        flash('Use your CBO password here. If you just created one during claim, that password is already saved.', 'info')

    if request.method == 'POST':
        role = _normalize_portal_role(request.form.get('role')) or 'funder'
        selected_role = role

        login_action = str(request.form.get('action') or 'password').strip().lower()
        email = _normalize_email(request.form.get('email') or '')
        password = request.form.get('password') or ''
        user = User.query.filter_by(email=email).first()

        if login_action == 'bypass':
            if not current_app.config.get('TEMP_LOGIN_BYPASS_ENABLED'):
                flash('Temporary password bypass is not enabled.', 'danger')
            else:
                try:
                    bypass_user = _ensure_temp_bypass_user(role)
                except RuntimeError as exc:
                    flash(str(exc), 'danger')
                else:
                    _set_active_role(role)
                    redirect_target = _login_standard_user(bypass_user, bypass_user.email)
                    if redirect_target:
                        return redirect(redirect_target)
                    session.pop('login_next', None)
                    flash('Temporary password bypass used.', 'info')
                    return redirect(_default_redirect_target(bypass_user))
        elif user and user.has_role(role) and user.needs_password_setup:
            flash('This CBO account still needs a password. Finish the claim flow to create one.', 'info')
            return redirect(url_for('auth.claim_account', email=email))
        elif not user or not user.has_role(role) or not user.check_password(password):
            flash('Invalid email, role, or password.', 'danger')
        else:
            _set_active_role(role)
            redirect_target = _login_standard_user(user, email)
            if redirect_target:
                return redirect(redirect_target)
            return redirect(_login_redirect_target(user))

        prefill_email = email

    elif selected_role == 'funder' and prefill_email:
        matched_user = User.query.filter_by(email=prefill_email).first()
        if matched_user and matched_user.has_role('cbo') and not matched_user.has_role('funder'):
            selected_role = 'cbo'

    return render_template(
        'auth/login.html',
        login_variant='standard',
        developer_google=developer_google,
        google_user=google_user,
        prefill_email=prefill_email,
        selected_role=selected_role,
        temp_login_bypass_enabled=bool(current_app.config.get('TEMP_LOGIN_BYPASS_ENABLED')),
    )


@auth_bp.route('/developer-login', methods=['GET'])
def developer_login():
    if _has_developer_access():
        return redirect(_safe_next_url(request.args.get('next'), url_for('main.developer_sms_activity')))

    context = _developer_login_context()
    if request.args.get('next'):
        session['developer_login_next'] = _safe_next_url(
            request.args.get('next'),
            url_for('main.developer_sms_activity'),
        )

    return render_template(
        'auth/login.html',
        login_variant='developer',
        developer_google=context,
        google_user=_user_google_login_context(),
        prefill_email='',
        temp_login_bypass_enabled=False,
    )


@auth_bp.route('/developer-login/google')
def developer_google_start():
    if _has_developer_access():
        return redirect(_developer_redirect_target())

    next_url = request.args.get('next') or session.get('developer_login_next')
    session['developer_login_next'] = _safe_next_url(
        next_url,
        url_for('main.developer_sms_activity'),
    )

    try:
        flow = _build_developer_google_flow()
    except RuntimeError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('auth.developer_login'))

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='false',
        prompt='select_account',
    )
    session['developer_google_code_verifier'] = flow.code_verifier
    session['developer_google_state'] = state
    return redirect(authorization_url)


@auth_bp.route('/developer-login/google/callback')
def developer_google_callback():
    expected_state = session.get('developer_google_state')
    code_verifier = session.get('developer_google_code_verifier')
    if not expected_state or not code_verifier:
        session.pop('developer_google_code_verifier', None)
        session.pop('developer_google_state', None)
        flash('The Google login session expired. Start the developer login again.', 'danger')
        return redirect(url_for('auth.developer_login'))

    try:
        flow = _build_developer_google_flow(
            state=expected_state,
            code_verifier=code_verifier,
        )
        profile = _fetch_google_profile(flow, request.url)
        user = _developer_user_from_google_profile(profile)
    except Exception as exc:
        current_app.logger.warning('Google developer login failed: %s', exc)
        flash(str(exc) or 'Google login failed. Please try again.', 'danger')
        session.pop('developer_google_code_verifier', None)
        session.pop('developer_google_state', None)
        return redirect(url_for('auth.developer_login'))

    session.pop('developer_google_code_verifier', None)
    session.pop('developer_google_state', None)
    _set_auth_provider('google', profile)
    login_user(user, remember=True)
    session['developer_access'] = True
    return redirect(_developer_redirect_target())


@auth_bp.route('/login/google')
def google_login_start():
    _clear_standard_google_session()

    next_url = request.args.get('next') or session.get('login_next')
    if next_url:
        session['login_next'] = _safe_next_url(next_url, url_for('main.marketplace'))

    email_hint = _normalize_email(request.args.get('email') or '')
    session['google_login_action'] = 'login'
    if email_hint:
        _set_active_role('cbo')
        session['google_login_email_hint'] = email_hint
    else:
        session.pop('google_login_email_hint', None)

    try:
        flow = _build_user_google_flow()
    except RuntimeError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('auth.login'))

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='false',
        prompt='select_account',
    )
    session['google_login_code_verifier'] = flow.code_verifier
    session['google_login_state'] = state
    return redirect(authorization_url)


@auth_bp.route('/register/google', methods=['POST'])
def google_register_start():
    if current_user.is_authenticated:
        return redirect(_default_redirect_target(current_user))

    _clear_standard_google_session()

    next_url = request.form.get('next') or session.get('login_next')
    if next_url:
        session['login_next'] = _safe_next_url(next_url, url_for('main.marketplace'))

    role = _normalize_portal_role(request.form.get('role')) or 'funder'
    prefill_email = _normalize_email(request.form.get('email') or '')
    prefill_display_name = str(request.form.get('display_name') or '').strip()
    prefill_cbo_name = str(request.form.get('cbo_name') or '').strip()
    google_user = _user_google_login_context()

    if role == 'cbo' and not prefill_cbo_name:
        flash('Organisation name is required for CBO registration with Google.', 'danger')
        return _render_register_page(
            google_user=google_user,
            prefill_email=prefill_email,
            prefill_display_name=prefill_display_name,
            prefill_cbo_name=prefill_cbo_name,
            selected_role=role,
            next_url=session.get('login_next', ''),
        )

    try:
        flow = _build_user_google_flow()
    except RuntimeError as exc:
        flash(str(exc), 'danger')
        return _render_register_page(
            google_user=google_user,
            prefill_email=prefill_email,
            prefill_display_name=prefill_display_name,
            prefill_cbo_name=prefill_cbo_name,
            selected_role=role,
            next_url=session.get('login_next', ''),
        )

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='false',
        prompt='select_account',
    )
    session['google_login_action'] = 'register'
    session['google_register_role'] = role
    session['google_register_cbo_name'] = prefill_cbo_name
    session['google_register_display_name'] = prefill_display_name
    session['google_login_code_verifier'] = flow.code_verifier
    session['google_login_state'] = state
    return redirect(authorization_url)


@auth_bp.route('/login/google/callback')
def google_login_callback():
    expected_state = session.get('google_login_state')
    code_verifier = session.get('google_login_code_verifier')
    google_action = str(session.get('google_login_action') or 'login').strip().lower()
    expected_email = str(session.get('google_login_email_hint') or '').strip()
    requested_role = _normalize_portal_role(session.get('google_register_role'))
    requested_cbo_name = str(session.get('google_register_cbo_name') or '').strip()
    requested_display_name = str(session.get('google_register_display_name') or '').strip()
    failure_redirect = url_for('auth.register') if google_action == 'register' else url_for(
        'auth.claim_account' if expected_email else 'auth.login',
        **({'email': expected_email} if expected_email else {}),
    )

    if not expected_state or not code_verifier:
        _clear_standard_google_session()
        flash('The Google login session expired. Start again.', 'danger')
        return redirect(failure_redirect)

    try:
        flow = _build_user_google_flow(
            state=expected_state,
            code_verifier=code_verifier,
        )
        profile = _fetch_google_profile(flow, request.url)
        registration_outcome = ''
        if google_action == 'register':
            user, registration_outcome = _standard_user_from_google_registration(
                profile,
                requested_role=requested_role or 'funder',
                requested_cbo_name=requested_cbo_name,
                requested_display_name=requested_display_name,
            )
        else:
            user = _standard_user_from_google_profile(
                profile,
                expected_email=expected_email,
            )
    except Exception as exc:
        current_app.logger.warning('Google user %s failed: %s', google_action or 'login', exc)
        flash(str(exc) or 'Google login failed. Please try again.', 'danger')
        _clear_standard_google_session()
        return redirect(failure_redirect)

    _clear_standard_google_session()
    session.pop('developer_access', None)

    if google_action == 'register' and requested_role and user.has_role(requested_role):
        _set_active_role(requested_role)
    elif expected_email:
        _set_active_role('cbo')
    elif user.is_cbo and not user.is_funder:
        _set_active_role('cbo')
    elif user.is_funder:
        _set_active_role('funder')

    _set_auth_provider('google', profile)
    login_user(user, remember=True)

    if google_action == 'register':
        if registration_outcome == 'created':
            flash('Account created successfully with Google.', 'success')
        elif registration_outcome == 'activated':
            flash('Your pre-built CBO account is now active with Google sign-in.', 'success')
        else:
            flash('That Google account already has a Kumbu Connect profile. You were signed in instead.', 'info')

    return redirect(_login_redirect_target(user))


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(_default_redirect_target(current_user))

    next_url = request.args.get('next') or request.form.get('next') or session.get('login_next')
    if next_url:
        session['login_next'] = _safe_next_url(next_url, url_for('main.marketplace'))

    google_user = _user_google_login_context()
    prefill_email = _normalize_email(request.args.get('email') or request.form.get('email') or '')
    prefill_display_name = str(request.args.get('display_name') or request.form.get('display_name') or '').strip()
    prefill_cbo_name = str(request.args.get('cbo_name') or request.form.get('cbo_name') or '').strip()
    selected_role = _normalize_portal_role(request.args.get('role') or request.form.get('role')) or 'funder'

    if request.method == 'POST':
        role = _normalize_portal_role(request.form.get('role')) or 'funder'
        selected_role = role

        display_name = str(request.form.get('display_name') or '').strip()
        email = _normalize_email(request.form.get('email') or '')
        password = request.form.get('password') or ''
        cbo_name = str(request.form.get('cbo_name') or '').strip()
        prefill_display_name = display_name
        prefill_cbo_name = cbo_name

        if not display_name or not email or len(password) < 6:
            flash('Name, email, and a password with at least 6 characters are required.', 'danger')
        else:
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                if existing_user.has_role('cbo') and existing_user.account_status == 'pending_claim':
                    existing_user.display_name = display_name or existing_user.display_name
                    existing_user.set_password(password)
                    existing_user.account_status = 'active'
                    db.session.add(existing_user)
                    db.session.commit()
                    session.pop('developer_access', None)
                    _set_active_role('cbo')
                    _set_auth_provider(None)
                    login_user(existing_user, remember=True)
                    flash('Your pre-built CBO account is now active.', 'success')
                    return redirect(_login_redirect_target(existing_user))

                flash('An account with that email already exists.', 'danger')
            else:
                cbo = None
                if role == 'cbo':
                    if not cbo_name:
                        flash('Organisation name is required for CBO registration.', 'danger')
                        return _render_register_page(
                            google_user=google_user,
                            prefill_email=email,
                            prefill_display_name=display_name,
                            prefill_cbo_name=cbo_name,
                            selected_role=role,
                            next_url=session.get('login_next', ''),
                        )
                    cbo = CBO(name=cbo_name, slug=_unique_cbo_slug(cbo_name))
                    db.session.add(cbo)
                    db.session.flush()

                user = User(
                    email=email,
                    role=role,
                    account_status='active',
                    display_name=display_name,
                    cbo_id=cbo.id if cbo else None,
                )
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                session.pop('developer_access', None)
                _set_active_role(role)
                _set_auth_provider(None)
                login_user(user, remember=True)
                flash('Account created successfully.', 'success')
                return redirect(_login_redirect_target(user))

        prefill_email = email

    return _render_register_page(
        google_user=google_user,
        prefill_email=prefill_email,
        prefill_display_name=prefill_display_name,
        prefill_cbo_name=prefill_cbo_name,
        selected_role=selected_role,
        next_url=session.get('login_next', ''),
    )


@auth_bp.route('/claim-account', methods=['GET', 'POST'])
def claim_account():
    if current_user.is_authenticated:
        return redirect(_default_redirect_target(current_user))

    email = _normalize_email(request.args.get('email') or request.form.get('email') or '')
    pending_user = None
    google_user = _user_google_login_context()
    claim_state = _claim_candidate_state(email, auto_sync=bool(email)) if email else {
        'pending_user': None,
        'active_user': None,
        'response': None,
        'error': '',
    }

    if request.method == 'POST':
        action = str(request.form.get('action') or 'lookup').strip().lower()
        user = claim_state.get('pending_user') or claim_state.get('active_user')

        if action == 'activate':
            password = request.form.get('password') or ''
            password_confirm = request.form.get('password_confirm') or ''
            display_name = str(request.form.get('display_name') or '').strip()

            if claim_state.get('active_user') is not None:
                flash('That account is already active. Log in instead.', 'info')
                return redirect(_login_redirect_for_claim(email))
            if not _user_can_finish_claim(user):
                flash(claim_state.get('error') or 'No pending pre-built CBO account was found for that email.', 'danger')
            elif len(password) < 6:
                flash('Choose a password with at least 6 characters.', 'danger')
                pending_user = user
            elif password != password_confirm:
                flash('The password confirmation does not match.', 'danger')
                pending_user = user
            else:
                if display_name:
                    user.display_name = display_name
                user.set_password(password)
                user.account_status = 'active'
                db.session.add(user)
                db.session.commit()
                session.pop('developer_access', None)
                _set_active_role('cbo')
                _set_auth_provider(None)
                login_user(user, remember=True)
                flash('Your CBO account is now active.', 'success')
                return redirect(_login_redirect_target(user))
        else:
            if claim_state.get('pending_user'):
                pending_user = claim_state['pending_user']
            elif claim_state.get('active_user'):
                flash('That account is already active. Log in instead.', 'info')
                return redirect(_login_redirect_for_claim(email))
            else:
                flash(claim_state.get('error') or 'No pre-built CBO account was found for that email.', 'danger')
    elif email:
        if claim_state.get('active_user'):
            flash('That account is already active. Log in instead.', 'info')
            return redirect(_login_redirect_for_claim(email))
        pending_user = claim_state.get('pending_user')

    return render_template(
        'auth/claim_account.html',
        email=email,
        pending_user=pending_user,
        google_user=google_user,
    )


@auth_bp.route('/logout')
@login_required
def logout():
    _clear_auth_session_flags()
    session.pop('login_next', None)
    logout_user()
    return redirect(url_for('main.marketplace'))
