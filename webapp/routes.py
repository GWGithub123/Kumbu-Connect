""" 
Main application routes — marketplace, profile, sync.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import csv
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from io import BytesIO
from io import StringIO
from urllib.request import urlopen
from urllib.parse import parse_qs, urlencode, urlparse
from anthropic import APIConnectionError, APIStatusError, APITimeoutError, Anthropic, BadRequestError, RateLimitError
from flask import Blueprint, render_template, redirect, url_for, flash, abort, jsonify, request, current_app, send_file, Response, session
from flask_login import login_required, current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
import qrcode
from qrcode.image.svg import SvgPathImage
from werkzeug.utils import secure_filename
from .community_feedback import handle_inbound_sms, render_sms_response, send_due_checkins, validate_twilio_request
from .community_feedback_keywords import get_cbo_keyword, normalize_sms_keyword
from .firestore_service import (
    delete_bookkeeping_document_from_firestore,
    get_feedback_document_path,
    sync_bookkeeping_document_to_firestore,
    sync_bookkeeping_summary_to_firestore,
)
from .firebase_storage_service import (
    delete_bookkeeping_image,
    delete_stored_file,
    get_bookkeeping_image_bytes,
    get_stored_file_bytes,
    is_stored_file_available,
    store_supporting_file,
)
from .document_ingestion_service import DocumentIngestionError, prepare_document_bytes, prepare_uploaded_document
from .maps_service import ensure_cbo_geocoded, get_google_maps_api_key
from .models import db, BookkeepingDocument, CBO, CommunitySubscriber, CommunityFeedback, FundingAuditDocument, GoogleFormResponse, GoogleFormUpload, User
from .kobo_service import fetch_kobo_submissions
from .gemini_service import analyse_kobo_data, interpret_marketplace_query, rank_marketplace_candidates
from .bookkeeping_audit_service import audit_bookkeeping_document, audit_bookkeeping_group
from .funding_audit_service import FundingAuditError, build_funding_audit_payload, observed_charitable_giving
from .openai_bookkeeping_service import BookkeepingExtractionError, extract_bookkeeping_document, refine_extracted_bookkeeping_payload
from .google_forms_service import (
    ADDITIONAL_TRACKING_FIELDS_TITLE,
    ANECDOTAL_STORY_TITLE,
    BOOKKEEPING_UPLOAD_TITLE,
    GENERAL_IMAGE_UPLOAD_TITLE,
    create_cbo_intake_form,
    download_drive_file,
    ensure_intake_form_upload_guidance,
    expected_upload_question_titles,
    get_form_response_bundle,
    get_form_responses,
    get_intake_form_schema,
    google_forms_enabled,
)

main_bp = Blueprint('main', __name__)

BOOKKEEPING_MOBILE_SCAN_MAX_AGE = 60 * 60 * 24 * 30
BOOKKEEPING_MOBILE_SCAN_SALT = 'bookkeeping-mobile-scan'
BOOKKEEPING_OFFLINE_MAX_AGE = 60 * 60 * 24 * 30
BOOKKEEPING_OFFLINE_SALT = 'bookkeeping-offline'
INTAKE_OFFLINE_MAX_AGE = 60 * 60 * 24 * 30
INTAKE_OFFLINE_SALT = 'intake-offline'
MANAGED_CLOUDFLARED_LOCK = threading.Lock()
MANAGED_CLOUDFLARED_PROCESS = None
MANAGED_CLOUDFLARED_URL = ''
MANAGED_CLOUDFLARED_STARTED_AT = 0.0
DEVELOPER_SMS_ACTIVITY_RECENT_INTAKE_DAYS = 60


# ── Landing ───────────────────────────────────────────────────────
@main_bp.route('/')
def index():
    if _has_developer_access():
        return redirect(url_for('main.developer_sms_activity'))
    if current_user.is_authenticated:
        if _active_portal_role() == 'cbo' and getattr(current_user, 'cbo_id', None):
            return redirect(url_for('main.cbo_dashboard'))
        if _user_has_funder_role(current_user):
            return redirect(url_for('main.marketplace'))
        if _user_has_cbo_role(current_user):
            return redirect(url_for('main.cbo_dashboard'))
    return redirect(url_for('auth.login'))


# ── Funder marketplace ───────────────────────────────────────────
@main_bp.route('/marketplace')
@login_required
def marketplace():
    _require_funder()
    # ── Read filter params ───────────────────────────────────────
    q = request.args.get('q', '').strip()
    classification_arg = request.args.get('classification', '').strip().lower()
    badge_arg = request.args.get('badge', '').strip().lower()
    min_score_arg = request.args.get('min_score', '').strip()
    max_score_arg = request.args.get('max_score', '').strip()
    min_revenue_arg = request.args.get('min_revenue', '').strip()
    min_rating_arg = request.args.get('min_rating', '').strip()
    sort_arg = request.args.get('sort', '').strip()

    ai_search = interpret_marketplace_query(q) if q else None
    ai_filters = ai_search.get('structured_filters', {}) if ai_search else {}
    ai_preferences = ai_search.get('qualitative_preferences', {}) if ai_search else {}

    f_class = classification_arg or (ai_filters.get('classification', '').strip().lower() if ai_filters else '')
    f_badge = badge_arg or (ai_filters.get('badge', '').strip().lower() if ai_filters else '')
    f_min_score = _parse_int_arg(min_score_arg, ai_filters.get('min_score') if ai_filters else None, 0)
    f_max_score = _parse_int_arg(max_score_arg, ai_filters.get('max_score') if ai_filters else None, 100)
    f_min_rev = _parse_float_arg(min_revenue_arg, ai_filters.get('min_revenue') if ai_filters else None, 0.0)
    f_min_rating = _parse_float_arg(min_rating_arg, ai_filters.get('min_rating') if ai_filters else None, 0.0)
    f_sort = sort_arg or (ai_search.get('recommended_sort') if ai_search else '') or 'name'
    ai_search_active = bool(q)

    cbos = CBO.query.all()
    cbo_profiles = []
    map_data_changed = False
    for cbo in cbos:
        profile = _safe_json(cbo.ai_profile_json)
        classifications = _safe_json_list(cbo.classifications_json)
        community_feedback = _community_feedback_summary(cbo)
        score = cbo.social_impact_score or 0
        badge = cbo.data_quality_badge or 'bronze'

        # ── Apply filters ──────────────────────────────────────
        if f_class and f_class not in [c.lower() for c in classifications]:
            continue
        if f_badge and badge != f_badge:
            continue
        if not (f_min_score <= score <= f_max_score):
            continue
        avg_rating = community_feedback.get('avg_rating')
        if f_min_rating and (avg_rating is None or avg_rating < f_min_rating):
            continue

        # ── Compute growth rate from growth_metrics_json ──────────────
        growth_data = _safe_json_list(cbo.growth_metrics_json)
        revenue_growth = _compute_growth_rate(growth_data, 'revenue')
        rentals_growth = _compute_growth_rate(growth_data, 'rentals')
        total_revenue  = sum(m.get('revenue', 0) for m in growth_data)

        if f_min_rev and total_revenue < f_min_rev:
            continue

        if q and not ai_search_active:
            q_lower = q.lower()
            if q_lower not in cbo.name.lower() and q_lower not in (cbo.location or '').lower() \
                    and q_lower not in (cbo.focus_areas or '').lower():
                continue

        map_data_changed = ensure_cbo_geocoded(cbo, profile=profile) or map_data_changed

        cbo_profiles.append({
            'cbo': cbo,
            'profile': profile,
            'classifications': classifications,
            'community_feedback': community_feedback,
            'badge': badge,
            'score': score,
            'revenue_growth': revenue_growth,
            'rentals_growth': rentals_growth,
            'total_revenue': total_revenue,
            'ai_match': None,
        })

    ai_ranking_summary = ''
    ai_match_groups = []
    if ai_search_active and cbo_profiles:
        ranking_input = []
        for item in cbo_profiles:
            ranking_input.append({
                'slug': item['cbo'].slug,
                'name': item['profile'].get('name', item['cbo'].name),
                'location': item['profile'].get('location', item['cbo'].location) or '',
                'tagline': item['profile'].get('tagline', ''),
                'focus_areas': item['profile'].get('focus_areas', item['cbo'].focus_areas) or '',
                'classifications': item['classifications'],
                'badge': item['badge'],
                'score': item['score'],
                'total_revenue': item['total_revenue'],
                'revenue_growth': item['revenue_growth'],
                'avg_rating': item['community_feedback'].get('avg_rating'),
                'responses': item['community_feedback'].get('responses', 0),
                'recent_quotes': [quote.get('quote', '') for quote in item['community_feedback'].get('recent_quotes', [])],
            })
        ranking = rank_marketplace_candidates(q, ranking_input, ai_search)
        ai_ranking_summary = ranking.get('summary', '')
        match_lookup = {match.get('slug'): match for match in ranking.get('matches', [])}
        for item in cbo_profiles:
            item['ai_match'] = match_lookup.get(item['cbo'].slug)
        ai_match_groups = _build_ai_match_groups(cbo_profiles)

    for item in cbo_profiles:
        overview_bullets = _build_detailed_ai_overview_bullets(item)
        item['detailed_ai_overview_bullets'] = overview_bullets
        item['detailed_ai_overview'] = _flatten_ai_overview_bullets(overview_bullets)

    if map_data_changed:
        db.session.commit()

    # ── Sort ───────────────────────────────────────────────
    if f_sort == 'ai_match' and ai_search_active:
        cbo_profiles.sort(key=lambda item: (
            -(item['ai_match'] or {}).get('score', 0),
            -item['score'],
            -item['total_revenue'],
            item['cbo'].name.lower(),
        ))
    else:
        sort_key = {
            'name':           lambda x: x['cbo'].name,
            'score_desc':     lambda x: -x['score'],
            'revenue_desc':   lambda x: -x['total_revenue'],
            'growth_desc':    lambda x: -x['revenue_growth'],
            'badge':          lambda x: {'gold': 0, 'silver': 1, 'bronze': 2}.get(x['badge'], 3),
        }.get(f_sort, lambda x: x['cbo'].name)
        cbo_profiles.sort(key=sort_key)

    map_pins = [
        _build_map_pin(item, ai_search_active)
        for item in cbo_profiles
        if item['cbo'].latitude is not None and item['cbo'].longitude is not None
    ]

    return render_template('marketplace.html',
                           cbo_profiles=cbo_profiles,
                           q=q, f_class=f_class, f_badge=f_badge,
                           f_min_score=f_min_score, f_max_score=f_max_score,
                           f_min_rev=f_min_rev, f_min_rating=f_min_rating,
                           f_sort=f_sort, ai_search=ai_search, ai_search_active=ai_search_active,
                           ai_ranking_summary=ai_ranking_summary, ai_preferences=ai_preferences,
                           ai_match_groups=ai_match_groups,
                           google_maps_api_key=get_google_maps_api_key(),
                           map_pins=map_pins,
                           min_score_display=min_score_arg if min_score_arg else (f_min_score if f_min_score else ''),
                           max_score_display=max_score_arg if max_score_arg else (f_max_score if (max_score_arg or (ai_filters and ai_filters.get('max_score') is not None)) else ''),
                           min_revenue_display=min_revenue_arg if min_revenue_arg else (int(f_min_rev) if f_min_rev else ''),
                           min_rating_display=min_rating_arg if min_rating_arg else (f_min_rating if f_min_rating else ''))


# ── CBO's own dashboard ──────────────────────────────────────────
@main_bp.route('/dashboard')
@login_required
def cbo_dashboard():
    if not _user_has_cbo_role(current_user) or not current_user.cbo:
        return redirect(url_for('main.marketplace'))
    cbo = current_user.cbo
    profile = _safe_json(cbo.ai_profile_json)
    community_feedback = _community_feedback_summary(cbo)
    bookkeeping_summary = _bookkeeping_summary(cbo)
    return render_template(
        'cbo_profile.html',
        cbo=cbo,
        profile=profile,
        own=True,
        viewer_is_funder=False,
        can_manage_bookkeeping=_can_manage_bookkeeping(cbo),
        community_feedback=community_feedback,
        bookkeeping_summary=bookkeeping_summary,
        bookkeeping_offline=_bookkeeping_offline_context(cbo),
        funding_audit_summary=_funding_audit_summary(cbo),
        mobile_scan=_bookkeeping_mobile_scan_context(cbo),
    )


# ── Public / funder view of a single CBO profile ─────────────────
@main_bp.route('/cbo/<slug>')
@login_required
def cbo_profile(slug):
    cbo = CBO.query.filter_by(slug=slug).first_or_404()
    _require_feedback_access(cbo)
    profile = _safe_json(cbo.ai_profile_json)
    own = _user_owns_cbo(current_user, cbo)
    viewer_is_funder = _user_has_funder_role(current_user) and not own
    community_feedback = _community_feedback_summary(cbo)
    bookkeeping_summary = _bookkeeping_summary(cbo)
    isolated_view = str(request.args.get('isolated') or '').strip().lower()
    embedded_layout = str(request.args.get('embedded') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if isolated_view not in {'profile', 'bookkeeping-live', 'bookkeeping-digitized'}:
        isolated_view = ''
    return render_template(
        'cbo_profile.html',
        cbo=cbo,
        profile=profile,
        own=own,
        viewer_is_funder=viewer_is_funder,
        can_manage_bookkeeping=_can_manage_bookkeeping(cbo),
        isolated_view=isolated_view,
        embedded_layout=embedded_layout,
        community_feedback=community_feedback,
        bookkeeping_summary=bookkeeping_summary,
        bookkeeping_offline=_bookkeeping_offline_context(cbo),
        funding_audit_summary=_funding_audit_summary(cbo),
        mobile_scan=_bookkeeping_mobile_scan_context(cbo),
    )


@main_bp.route('/bookkeeping/mobile-scan/<token>', methods=['GET', 'POST'])
def bookkeeping_mobile_scan(token):
    cbo = _load_bookkeeping_mobile_scan_cbo(token)
    mobile_scan = _bookkeeping_mobile_scan_context(cbo, token=token)
    summary = _bookkeeping_summary(cbo)

    if request.method == 'POST':
        upload_options = _parse_bookkeeping_upload_options(request.form)
        if upload_options['errors']:
            for failure in upload_options['errors']:
                flash(failure, 'danger')
            return render_template(
                'bookkeeping_mobile_scan.html',
                cbo=cbo,
                bookkeeping_summary=summary,
                mobile_scan=mobile_scan,
                offline_payload=_bookkeeping_mobile_scan_payload(cbo, token, summary),
                embedded_layout=True,
            )

        uploads = request.files.getlist('bookkeeping_images')
        if not uploads:
            single_upload = request.files.get('bookkeeping_image')
            if single_upload:
                uploads = [single_upload]
        successes, failures, summary = _process_bookkeeping_uploads(
            cbo,
            uploads,
            uploaded_by_user_id=None,
            combine_related_pages=(request.form.get('combine_related_pages') == 'on'),
            include_in_workspace=upload_options['include_in_workspace'],
            document_date_override=upload_options['document_date'],
            workspace_period_key=upload_options['workspace_period_key'],
        )
        if successes:
            flash(f'Processed {successes} bookkeeping document(s) for {cbo.name}.', 'success')
        for failure in failures:
            flash(failure, 'danger' if not successes else 'info')

    return render_template(
        'bookkeeping_mobile_scan.html',
        cbo=cbo,
        bookkeeping_summary=summary,
        mobile_scan=mobile_scan,
        offline_payload=_bookkeeping_mobile_scan_payload(cbo, token, summary),
        embedded_layout=True,
    )


@main_bp.route('/bookkeeping/mobile-scan/<token>/sw.js')
def bookkeeping_mobile_scan_service_worker(token):
    cbo = _load_bookkeeping_mobile_scan_cbo(token)
    service_worker_path = os.path.join(current_app.root_path, 'static', 'js', 'bookkeeping_mobile_scan_offline_sw.js')
    response = send_file(service_worker_path, mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Service-Worker-Allowed'] = url_for('main.bookkeeping_mobile_scan', token=token)
    response.headers['X-Kumbu-CBO'] = str(cbo.id)
    return response


@main_bp.route('/bookkeeping/mobile-scan/<token>/submit', methods=['POST'])
def bookkeeping_mobile_scan_submit(token):
    cbo = _load_bookkeeping_mobile_scan_cbo(token)

    upload_options = _parse_bookkeeping_upload_options(request.form)
    if upload_options['errors']:
        return jsonify({
            'ok': False,
            'message': ' '.join(upload_options['errors']),
            'failure_messages': upload_options['errors'],
        }), 400

    submission_id = str(request.form.get('submission_id') or '').strip()
    if not submission_id:
        return jsonify({
            'ok': False,
            'message': 'Missing offline submission id.',
        }), 400

    existing_document = BookkeepingDocument.query.filter_by(
        cbo_id=cbo.id,
        client_submission_id=submission_id,
    ).first()
    if existing_document:
        return jsonify({
            'ok': True,
            'duplicate': True,
            'message': 'Upload was already synced.',
            'bootstrap': _bookkeeping_mobile_scan_payload(cbo, token),
        })

    uploads = request.files.getlist('bookkeeping_images')
    if not uploads:
        single_upload = request.files.get('bookkeeping_image')
        if single_upload:
            uploads = [single_upload]
    if len(uploads) != 1:
        return jsonify({
            'ok': False,
            'message': 'Sync each queued upload as a single file.',
        }), 400

    group_id = str(request.form.get('group_id') or submission_id).strip() or submission_id
    combine_related_pages = str(request.form.get('combine_related_pages') or '').strip().lower() in {'1', 'true', 'on', 'yes'}
    successes, failures, summary = _process_bookkeeping_uploads(
        cbo,
        uploads,
        uploaded_by_user_id=None,
        combine_related_pages=combine_related_pages,
        upload_batch_id=group_id,
        source_channel_override='mobile_offline_sync',
        client_submission_id=submission_id,
        include_in_workspace=upload_options['include_in_workspace'],
        document_date_override=upload_options['document_date'],
        workspace_period_key=upload_options['workspace_period_key'],
    )

    if failures:
        status_code = 422 if not successes else 200
        return jsonify({
            'ok': False,
            'message': ' '.join(failures),
            'failure_messages': failures,
            'processed_count': successes,
            'bootstrap': _bookkeeping_mobile_scan_payload(cbo, token, summary),
        }), status_code

    return jsonify({
        'ok': True,
        'duplicate': False,
        'message': f'Uploaded {successes} bookkeeping document(s).',
        'processed_count': successes,
        'bootstrap': _bookkeeping_mobile_scan_payload(cbo, token, summary),
    })


@main_bp.route('/intake/offline/<token>')
def intake_offline_app(token):
    cbo = _load_intake_offline_cbo(token)
    return render_template(
        'intake_offline.html',
        cbo=cbo,
        intake_offline=_intake_offline_context(cbo, token=token),
        offline_payload=_intake_offline_payload(cbo, token),
    )


@main_bp.route('/intake/offline/<token>/sw.js')
def intake_offline_service_worker(token):
    cbo = _load_intake_offline_cbo(token)
    service_worker_path = os.path.join(current_app.root_path, 'static', 'js', 'intake_offline_sw.js')
    response = send_file(service_worker_path, mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Service-Worker-Allowed'] = url_for('main.intake_offline_app', token=token)
    response.headers['X-Kumbu-CBO'] = str(cbo.id)
    return response


@main_bp.route('/intake/offline/<token>/submit', methods=['POST'])
def intake_offline_submit(token):
    cbo = _load_intake_offline_cbo(token)

    if request.is_json:
        metadata = request.get_json(silent=True) or {}
    else:
        metadata_raw = request.form.get('metadata', '').strip()
        if not metadata_raw:
            return jsonify({
                'ok': False,
                'message': 'Missing intake submission metadata.',
            }), 400
        try:
            metadata = json.loads(metadata_raw)
        except (TypeError, ValueError):
            return jsonify({
                'ok': False,
                'message': 'Intake submission metadata is not valid JSON.',
            }), 400

    schema = get_intake_form_schema()
    field_lookup = {
        str(field.get('id') or ''): field
        for field in schema.get('fields', [])
        if str(field.get('id') or '')
    }
    upload_lookup = {
        str(field.get('id') or ''): field
        for field in schema.get('upload_fields', [])
        if str(field.get('id') or '')
    }

    submitted_values = metadata.get('form_values') if isinstance(metadata.get('form_values'), dict) else {}
    normalized_values = {}
    errors = []
    for field_id, field in field_lookup.items():
        value = _normalize_offline_intake_value(submitted_values.get(field_id))
        normalized_values[field_id] = value
        if field.get('required') and not value:
            errors.append(f"{field.get('title') or field_id} is required.")

    created_at = str(metadata.get('created_at') or datetime.utcnow().isoformat())
    submitted_at = datetime.utcnow().isoformat()
    submission_id = str(metadata.get('submission_id') or uuid.uuid4().hex).strip() or uuid.uuid4().hex

    inline_uploads = {}
    normalized_uploads = []
    for upload_item in metadata.get('uploads') or []:
        if not isinstance(upload_item, dict):
            continue
        field_id = str(upload_item.get('field_id') or '').strip()
        if not field_id:
            continue
        upload_field = upload_lookup.get(field_id)
        if upload_field is None:
            errors.append(f'Unknown upload field: {field_id}.')
            continue

        upload_id = str(upload_item.get('upload_id') or upload_item.get('file_id') or uuid.uuid4().hex).strip()
        file_id = str(upload_item.get('file_id') or upload_id or uuid.uuid4().hex).strip()
        uploaded_file = None
        if not request.is_json:
            uploaded_file = request.files.get(f'upload::{upload_id}') or request.files.get(f'upload::{file_id}')
        if uploaded_file is None:
            errors.append(f"Missing file payload for {upload_field.get('title') or field_id}.")
            continue

        file_bytes = uploaded_file.read()
        if not file_bytes:
            continue

        safe_name = secure_filename(uploaded_file.filename or f'{field_id}-upload') or f'{field_id}-upload'
        mime_type = uploaded_file.mimetype or _guess_mime_type(safe_name)
        inline_uploads[file_id] = {
            'file_name': safe_name,
            'mime_type': mime_type,
            'bytes': file_bytes,
            'web_view_link': '',
        }
        normalized_uploads.append({
            'field_id': field_id,
            'file_id': file_id,
            'file_name': safe_name,
            'mime_type': mime_type,
            'size': len(file_bytes),
        })

    if errors:
        return jsonify({
            'ok': False,
            'message': errors[0],
            'errors': errors,
        }), 400

    response_payload = _build_offline_intake_submission_payload(
        cbo,
        submission_id=submission_id,
        form_values=normalized_values,
        upload_items=normalized_uploads,
        created_at=created_at,
        submitted_at=submitted_at,
    )
    result = _ingest_intake_response_payload(cbo, response_payload, inline_uploads=inline_uploads)
    message = 'Intake submission synced.'
    if result.get('duplicate_response'):
        message = 'This intake submission was already synced.'
    elif result.get('failures'):
        message = 'Intake submission saved with follow-up items.'

    return jsonify({
        'ok': True,
        'submission_id': submission_id,
        'message': message,
        'warnings': result.get('failures') or [],
        'result': result,
    })


@main_bp.route('/cbo/<int:cbo_id>/bookkeeping/upload', methods=['POST'])
@login_required
def upload_bookkeeping_documents(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)
    combine_related_pages = request.form.get('combine_related_pages') == 'on'
    upload_options = _parse_bookkeeping_upload_options(request.form)
    if upload_options['errors']:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=upload_options['errors'],
            redirect_slug=cbo.slug,
        )
    successes, failures, summary = _process_bookkeeping_uploads(
        cbo,
        request.files.getlist('bookkeeping_images'),
        uploaded_by_user_id=current_user.id,
        combine_related_pages=combine_related_pages,
        include_in_workspace=upload_options['include_in_workspace'],
        document_date_override=upload_options['document_date'],
        workspace_period_key=upload_options['workspace_period_key'],
    )

    return _bookkeeping_response(
        cbo,
        own=own,
        summary=summary,
        success_message=f'Processed {successes} bookkeeping document(s).' if successes else '',
        failure_messages=failures,
        redirect_slug=cbo.slug,
    )


@main_bp.route('/cbo/<int:cbo_id>/bookkeeping/inventory', methods=['POST'])
@login_required
def update_bookkeeping_inventory(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)

    raw_value = (request.form.get('tool_inventory_total') or '').strip()
    if not raw_value:
        cbo.tool_inventory_total = None
    else:
        try:
            parsed = int(raw_value)
        except ValueError:
            return _bookkeeping_response(
                cbo,
                own=own,
                summary=_bookkeeping_summary(cbo),
                success_message='',
                failure_messages=['Inventory total must be a whole number.'],
                redirect_slug=cbo.slug,
            )
        if parsed < 0:
            return _bookkeeping_response(
                cbo,
                own=own,
                summary=_bookkeeping_summary(cbo),
                success_message='',
                failure_messages=['Inventory total cannot be negative.'],
                redirect_slug=cbo.slug,
            )
        cbo.tool_inventory_total = parsed

    db.session.add(cbo)
    db.session.commit()
    _refresh_bookkeeping_audits(cbo)
    summary = _bookkeeping_summary(cbo)
    sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))
    return _bookkeeping_response(
        cbo,
        own=own,
        summary=summary,
        success_message='Audit inventory baseline updated.' if cbo.tool_inventory_total is not None else 'Audit inventory baseline cleared.',
        failure_messages=[],
        redirect_slug=cbo.slug,
    )


@main_bp.route('/cbo/<int:cbo_id>/bookkeeping/workspace', methods=['POST'])
@login_required
def add_bookkeeping_workspace_entry(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_bookkeeping_owner(cbo)
    own = True
    autosave_requested = str(request.form.get('autosave') or '').strip().lower() in {'1', 'true', 'on', 'yes'}

    summary, template_columns, template_column_lookup = _bookkeeping_workspace_template_context(cbo)
    if not template_columns:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=['No bookkeeping workspace template exists for this CBO yet. Sync an intake-form response first.'],
            redirect_slug=cbo.slug,
        )

    grid_rows = _normalize_bookkeeping_workspace_grid_rows(template_columns, request.form)
    if grid_rows:
        _replace_bookkeeping_workspace_entries(cbo, grid_rows)
        updated_summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
        db.session.commit()
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(updated_summary))
        visible_rows = len(updated_summary.get('workspace', {}).get('entries') or [])
        if autosave_requested and _is_ajax_request():
            return jsonify({
                'ok': True,
                'autosave': True,
                'saved_at': datetime.utcnow().isoformat(),
                'row_count': visible_rows,
                'workspace': _serialize_bookkeeping_workspace(updated_summary),
                'success_message': 'Workbook saved to the cloud.',
                'failure_messages': [],
            })
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=updated_summary,
            success_message=f"Workbook database updated. {visible_rows} row{'' if visible_rows == 1 else 's'} currently stored in the living table.",
            failure_messages=[],
            redirect_slug=cbo.slug,
        )

    sheet_rows = _normalize_bookkeeping_workspace_sheet_rows(template_columns, request.form)
    inserted_rows = 0

    if sheet_rows:
        for row_values in sheet_rows:
            if _append_bookkeeping_workspace_entry(
                cbo,
                template_columns=template_columns,
                row_values=row_values,
                row_id=uuid.uuid4().hex,
                created_at=datetime.utcnow().isoformat(),
            ):
                inserted_rows += 1
    else:
        column_names = request.form.getlist('column_name')
        column_values = request.form.getlist('column_value')
        row_values = _normalize_bookkeeping_workspace_values(
            template_columns,
            template_column_lookup,
            [
                (column_name, column_values[index] if index < len(column_values) else '')
                for index, column_name in enumerate(column_names)
            ],
        )

        if not any(row_values.get(column, '').strip() for column in template_columns):
            return _bookkeeping_response(
                cbo,
                own=own,
                summary=summary,
                success_message='',
                failure_messages=['Enter at least one value in the workbook grid before saving bookkeeping workspace rows.'],
                redirect_slug=cbo.slug,
            )

        if _append_bookkeeping_workspace_entry(
            cbo,
            template_columns=template_columns,
            row_values=row_values,
            row_id=uuid.uuid4().hex,
            created_at=datetime.utcnow().isoformat(),
        ):
            inserted_rows = 1

    if inserted_rows == 0:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=['Enter at least one value in the workbook grid before saving bookkeeping workspace rows.'],
            redirect_slug=cbo.slug,
        )

    updated_summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
    db.session.commit()
    sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(updated_summary))
    return _bookkeeping_response(
        cbo,
        own=own,
        summary=updated_summary,
        success_message=f"Saved {inserted_rows} bookkeeping workspace row{'s' if inserted_rows != 1 else ''}.",
        failure_messages=[],
        redirect_slug=cbo.slug,
    )


@main_bp.route('/cbo/<int:cbo_id>/bookkeeping/workspace/columns', methods=['POST'])
@login_required
def update_bookkeeping_workspace_columns(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_bookkeeping_owner(cbo)
    own = True

    summary, template_columns, _template_column_lookup = _bookkeeping_workspace_template_context(cbo)
    if not template_columns:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=['No bookkeeping workspace template exists for this CBO yet.'],
            redirect_slug=cbo.slug,
        )

    workspace_template = _safe_json(cbo.bookkeeping_template_json)
    existing_custom_fields = [
        _titleize_workspace_label(value)
        for value in (workspace_template.get('custom_fields') or [])
        if _titleize_workspace_label(value)
    ]
    next_columns, next_custom_fields, column_pairs, column_errors = _normalize_bookkeeping_workspace_column_update(
        existing_custom_fields,
        request.form.getlist('existing_column_key'),
        request.form.getlist('column_label'),
    )
    if column_errors:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=column_errors,
            redirect_slug=cbo.slug,
        )

    raw_entries = _safe_json_list(cbo.bookkeeping_workspace_entries_json)
    cbo.bookkeeping_workspace_entries_json = json.dumps(
        _remap_bookkeeping_workspace_entries_to_columns(raw_entries, column_pairs, next_columns),
        default=str,
    )

    next_template = dict(workspace_template)
    next_template['columns'] = next_columns
    next_template['custom_fields'] = next_custom_fields
    next_template['worksheets'] = _normalize_workspace_template_worksheets(next_template.get('worksheets') or [])
    next_template['generated_at'] = datetime.utcnow().isoformat()
    next_template['source'] = 'workspace_editor'
    cbo.bookkeeping_template_json = json.dumps(next_template, default=str)
    db.session.add(cbo)

    updated_summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
    db.session.commit()
    sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(updated_summary))
    return _bookkeeping_response(
        cbo,
        own=own,
        summary=updated_summary,
        success_message='Workbook columns updated.',
        failure_messages=[],
        redirect_slug=cbo.slug,
    )


@main_bp.route('/cbo/<int:cbo_id>/bookkeeping/workspace/worksheets', methods=['POST'])
@login_required
def create_bookkeeping_workspace_worksheet(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_bookkeeping_owner(cbo)
    own = True

    summary, template_columns, _template_column_lookup = _bookkeeping_workspace_template_context(cbo)
    if not template_columns:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=['No bookkeeping workspace template exists for this CBO yet.'],
            redirect_slug=cbo.slug,
        )

    worksheet_label = _titleize_workspace_label(request.form.get('worksheet_label') or request.form.get('worksheet_category'))
    if not worksheet_label:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=['Enter a worksheet category.'],
            redirect_slug=cbo.slug,
        )

    worksheet_periods = []
    for raw_period in request.form.getlist('worksheet_periods'):
        normalized_period = _normalize_workspace_period_key(raw_period)
        if normalized_period and normalized_period not in worksheet_periods:
            worksheet_periods.append(normalized_period)

    if not worksheet_periods:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=['Choose at least one month for the worksheet.'],
            redirect_slug=cbo.slug,
        )

    worksheet_key = _normalize_workspace_document_type_key(worksheet_label)
    default_period_key = _normalize_workspace_period_key(request.form.get('default_period_key'))
    if default_period_key not in worksheet_periods:
        default_period_key = worksheet_periods[0]

    workspace_template = _safe_json(cbo.bookkeeping_template_json)
    worksheet_definitions = _normalize_workspace_template_worksheets(workspace_template.get('worksheets') or [])
    existing_definition = next((item for item in worksheet_definitions if item['key'] == worksheet_key), None)
    if existing_definition:
        existing_definition['label'] = worksheet_label
        existing_definition['periods'] = sorted(
            {str(value).strip() for value in (existing_definition.get('periods') or []) + worksheet_periods if str(value).strip()},
            reverse=True,
        )
        if default_period_key in existing_definition['periods']:
            existing_definition['default_period_key'] = default_period_key
    else:
        worksheet_definitions.append({
            'key': worksheet_key,
            'label': worksheet_label,
            'periods': sorted(worksheet_periods, reverse=True),
            'default_period_key': default_period_key,
            'source': 'manual',
            'created_at': datetime.utcnow().isoformat(),
        })

    next_template = dict(workspace_template)
    next_template['worksheets'] = _normalize_workspace_template_worksheets(worksheet_definitions)
    next_template['generated_at'] = datetime.utcnow().isoformat()
    next_template['source'] = 'workspace_editor'
    cbo.bookkeeping_template_json = json.dumps(next_template, default=str)
    db.session.add(cbo)
    db.session.commit()

    updated_summary = _bookkeeping_summary(cbo)
    return _bookkeeping_response(
        cbo,
        own=own,
        summary=updated_summary,
        success_message=f'{worksheet_label} worksheet ready.',
        failure_messages=[],
        redirect_slug=cbo.slug,
        extra_payload={
            'workspace_focus': {
                'type_key': worksheet_key,
                'period_key': default_period_key,
            },
        },
    )


@main_bp.route('/cbo/<int:cbo_id>/bookkeeping/offline/')
def bookkeeping_offline_app_redirect(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    bookkeeping_offline = _bookkeeping_offline_context(cbo)
    return redirect(bookkeeping_offline['preview_url'])


@main_bp.route('/bookkeeping/offline/<token>')
def bookkeeping_offline_app(token):
    cbo = _load_bookkeeping_offline_cbo(token)
    summary = _bookkeeping_summary(cbo)
    return render_template(
        'bookkeeping_offline.html',
        cbo=cbo,
        offline_payload=_bookkeeping_offline_payload(cbo, token, summary),
    )


@main_bp.route('/bookkeeping/offline/<token>/bootstrap')
def bookkeeping_offline_bootstrap(token):
    cbo = _load_bookkeeping_offline_cbo(token)
    return jsonify(_bookkeeping_offline_payload(cbo, token))


@main_bp.route('/bookkeeping/offline/<token>/sw.js')
def bookkeeping_offline_service_worker(token):
    _load_bookkeeping_offline_cbo(token)
    service_worker_path = os.path.join(current_app.root_path, 'static', 'js', 'bookkeeping_offline_sw.js')
    response = send_file(service_worker_path, mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Service-Worker-Allowed'] = url_for('main.bookkeeping_offline_app', token=token)
    return response


@main_bp.route('/bookkeeping/offline/<token>/sync/workspace', methods=['POST'])
def bookkeeping_offline_sync_workspace(token):
    cbo = _load_bookkeeping_offline_cbo(token)

    payload = request.get_json(silent=True) or {}
    summary, template_columns, template_column_lookup = _bookkeeping_workspace_template_context(cbo)
    if not template_columns:
        return jsonify({
            'ok': False,
            'message': 'No bookkeeping workspace template exists for this CBO yet.',
        }), 400

    grid_rows = _normalize_bookkeeping_workspace_json_rows(template_columns, payload.get('rows'))
    if grid_rows:
        _replace_bookkeeping_workspace_entries(cbo, grid_rows)
        updated_summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
        db.session.commit()
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(updated_summary))
        return jsonify({
            'ok': True,
            'duplicate': False,
            'message': 'Workbook database synced.',
            'bootstrap': _bookkeeping_offline_payload(cbo, token, updated_summary),
        })

    raw_values = payload.get('values') or {}
    if not isinstance(raw_values, dict):
        raw_values = {}

    row_values = _normalize_bookkeeping_workspace_values(
        template_columns,
        template_column_lookup,
        list(raw_values.items()),
    )
    if not any(row_values.get(column, '').strip() for column in template_columns):
        return jsonify({
            'ok': False,
            'message': 'Enter at least one value before saving a bookkeeping workspace row.',
        }), 400

    row_id = str(payload.get('row_id') or payload.get('submission_id') or uuid.uuid4().hex).strip() or uuid.uuid4().hex
    created_at = str(payload.get('created_at') or datetime.utcnow().isoformat()).strip() or datetime.utcnow().isoformat()
    inserted = _append_bookkeeping_workspace_entry(
        cbo,
        template_columns=template_columns,
        row_values=row_values,
        row_id=row_id,
        created_at=created_at,
        workspace_period_key=payload.get('workspace_period_key'),
        workspace_document_type_key=payload.get('workspace_document_type_key'),
    )
    updated_summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
    db.session.commit()
    if inserted:
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(updated_summary))

    return jsonify({
        'ok': True,
        'duplicate': not inserted,
        'message': 'Workspace row synced.' if inserted else 'Workspace row was already synced.',
        'bootstrap': _bookkeeping_offline_payload(cbo, token, updated_summary),
    })


@main_bp.route('/bookkeeping/offline/<token>/sync/uploads', methods=['POST'])
def bookkeeping_offline_sync_upload(token):
    cbo = _load_bookkeeping_offline_cbo(token)

    upload_options = _parse_bookkeeping_upload_options(request.form)
    if upload_options['errors']:
        return jsonify({
            'ok': False,
            'message': ' '.join(upload_options['errors']),
            'failure_messages': upload_options['errors'],
        }), 400

    submission_id = str(request.form.get('submission_id') or '').strip()
    if not submission_id:
        return jsonify({
            'ok': False,
            'message': 'Missing offline submission id.',
        }), 400

    existing_document = BookkeepingDocument.query.filter_by(
        cbo_id=cbo.id,
        client_submission_id=submission_id,
    ).first()
    if existing_document:
        return jsonify({
            'ok': True,
            'duplicate': True,
            'message': 'Upload was already synced.',
            'bootstrap': _bookkeeping_offline_payload(cbo, token),
        })

    uploads = request.files.getlist('bookkeeping_images')
    if not uploads:
        single_upload = request.files.get('bookkeeping_image')
        if single_upload:
            uploads = [single_upload]
    if len(uploads) != 1:
        return jsonify({
            'ok': False,
            'message': 'Sync each queued upload as a single file.',
        }), 400

    group_id = str(request.form.get('group_id') or submission_id).strip() or submission_id
    combine_related_pages = str(request.form.get('combine_related_pages') or '').strip().lower() in {'1', 'true', 'on', 'yes'}
    successes, failures, summary = _process_bookkeeping_uploads(
        cbo,
        uploads,
        uploaded_by_user_id=current_user.id if getattr(current_user, 'is_authenticated', False) else None,
        combine_related_pages=combine_related_pages,
        upload_batch_id=group_id,
        source_channel_override='offline_sync',
        client_submission_id=submission_id,
        include_in_workspace=upload_options['include_in_workspace'],
        document_date_override=upload_options['document_date'],
        workspace_period_key=upload_options['workspace_period_key'],
    )

    if failures:
        status_code = 422 if not successes else 200
        return jsonify({
            'ok': False,
            'message': ' '.join(failures),
            'failure_messages': failures,
            'processed_count': successes,
            'bootstrap': _bookkeeping_offline_payload(cbo, token, summary),
        }), status_code

    return jsonify({
        'ok': True,
        'duplicate': False,
        'message': f'Uploaded {successes} bookkeeping document(s).',
        'processed_count': successes,
        'bootstrap': _bookkeeping_offline_payload(cbo, token, summary),
    })


@main_bp.route('/cbo/<int:cbo_id>/funding-audit/upload', methods=['POST'])
@login_required
def upload_funding_audit_document(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)

    uploaded = request.files.get('funding_document')
    if not uploaded or not uploaded.filename:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=['Choose a grant, donation, or award document to upload.'],
            redirect_slug=cbo.slug,
        )

    declared_funding_amount = _parse_nonnegative_money_arg((request.form.get('declared_funding_amount') or '').strip())
    declared_working_capital = _parse_nonnegative_money_arg((request.form.get('declared_working_capital') or '').strip())
    if declared_funding_amount is None:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=['Declared funding amount must be a valid non-negative number.'],
            redirect_slug=cbo.slug,
        )
    if declared_working_capital is None:
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=['Declared working capital must be a valid non-negative number.'],
            redirect_slug=cbo.slug,
        )

    declared = {
        'funder_name': (request.form.get('declared_funder_name') or '').strip(),
        'funding_amount': declared_funding_amount,
        'working_capital': declared_working_capital,
        'period_start': (request.form.get('declared_period_start') or '').strip(),
        'period_end': (request.form.get('declared_period_end') or '').strip(),
        'currency': 'KES',
    }

    try:
        prepared = prepare_uploaded_document(
            uploaded,
            max_pdf_pages=current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10),
        )
        _process_funding_audit_document(
            cbo=cbo,
            filename=prepared['source_filename'],
            mime_type=prepared['source_mime_type'],
            source_bytes=prepared['source_bytes'],
            page_images=prepared['pages'],
            source_channel=prepared.get('source_channel', 'web_upload'),
            uploaded_by_user_id=current_user.id,
            declared=declared,
        )
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            funding_summary=_funding_audit_summary(cbo),
            success_message='Funding document uploaded and verification audit completed.',
            failure_messages=[],
            redirect_slug=cbo.slug,
        )
    except (DocumentIngestionError, FundingAuditError) as exc:
        db.session.rollback()
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=[str(exc)],
            redirect_slug=cbo.slug,
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception('Failed to process funding audit upload for CBO %s', cbo.id)
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=[str(exc)],
            redirect_slug=cbo.slug,
        )


@main_bp.route('/funding-audit/<int:document_id>/file')
@login_required
def funding_audit_document_file(document_id):
    document = FundingAuditDocument.query.get_or_404(document_id)
    _require_feedback_access(document.cbo)

    try:
        file_bytes, mime_type = get_stored_file_bytes(
            storage_backend=getattr(document, 'storage_backend', 'local'),
            stored_path=document.stored_path,
            mime_type=document.mime_type,
        )
    except FileNotFoundError:
        abort(404)

    return send_file(BytesIO(file_bytes), mimetype=mime_type, download_name=document.original_filename)


@main_bp.route('/funding-audit/<int:document_id>/delete', methods=['POST'])
@login_required
def delete_funding_audit_document(document_id):
    document = FundingAuditDocument.query.get_or_404(document_id)
    cbo = document.cbo
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)

    try:
        delete_stored_file(
            storage_backend=getattr(document, 'storage_backend', 'local'),
            stored_path=document.stored_path,
        )
        db.session.delete(document)
        db.session.commit()
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            funding_summary=_funding_audit_summary(cbo),
            success_message=f'Deleted {document.original_filename}.',
            failure_messages=[],
            redirect_slug=cbo.slug,
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception('Failed to delete funding audit document %s', document_id)
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=_bookkeeping_summary(cbo),
            success_message='',
            failure_messages=[str(exc).strip() or 'Could not delete the funding audit document.'],
            redirect_slug=cbo.slug,
        )


@main_bp.route('/bookkeeping/<int:document_id>/image')
@login_required
def bookkeeping_document_image(document_id):
    document = BookkeepingDocument.query.get_or_404(document_id)
    _require_feedback_access(document.cbo)

    image_bytes, mime_type = get_bookkeeping_image_bytes(document)

    return send_file(BytesIO(image_bytes), mimetype=mime_type, download_name=document.original_filename)


@main_bp.route('/google-form-upload/<int:upload_id>/file')
@login_required
def google_form_upload_file(upload_id):
    upload = GoogleFormUpload.query.get_or_404(upload_id)
    _require_feedback_access(upload.cbo)

    if upload.bookkeeping_document_id and not upload.stored_path:
        document = upload.bookkeeping_document or db.session.get(BookkeepingDocument, upload.bookkeeping_document_id)
        if document is None:
            abort(404)
        image_bytes, mime_type = get_bookkeeping_image_bytes(document)
        return send_file(BytesIO(image_bytes), mimetype=mime_type, download_name=document.original_filename)

    try:
        file_bytes, mime_type = get_stored_file_bytes(
            storage_backend=upload.storage_backend or 'local',
            stored_path=upload.stored_path,
            mime_type=upload.mime_type or _guess_mime_type(upload.original_filename),
        )
    except FileNotFoundError:
        abort(404)

    return send_file(
        BytesIO(file_bytes),
        mimetype=mime_type,
        download_name=upload.original_filename or f'google-form-upload-{upload.id}',
    )


@main_bp.route('/bookkeeping/<int:document_id>/rescan', methods=['POST'])
@login_required
def rescan_bookkeeping_document(document_id):
    document = BookkeepingDocument.query.get_or_404(document_id)
    cbo = document.cbo
    _require_feedback_access(cbo)

    try:
        source_bytes, mime_type = get_bookkeeping_image_bytes(document)
        prepared = prepare_document_bytes(
            document.original_filename,
            mime_type,
            source_bytes,
            max_pdf_pages=current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10),
        )
        _process_bookkeeping_document(
            cbo=cbo,
            filename=prepared['source_filename'],
            mime_type=prepared['source_mime_type'],
            source_bytes=prepared['source_bytes'],
            page_images=prepared['pages'],
            source_channel=document.source_channel,
            uploaded_by_user_id=current_user.id,
            existing_document=document,
        )
        summary = _refresh_cbo_operational_profile(cbo, allow_claude=True)
        db.session.commit()
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))
        return _bookkeeping_response(
            cbo,
            own=_user_owns_cbo(current_user, cbo),
            summary=summary,
            success_message=f'Re-scanned {document.original_filename}.',
            failure_messages=[],
            redirect_slug=cbo.slug,
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception('Failed to re-scan bookkeeping document %s', document.id)
        summary = _bookkeeping_summary(cbo)
        return _bookkeeping_response(
            cbo,
            own=_user_owns_cbo(current_user, cbo),
            summary=summary,
            success_message='',
            failure_messages=[str(exc)],
            redirect_slug=cbo.slug,
        )


@main_bp.route('/bookkeeping/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_bookkeeping_documents():
    document_ids = request.form.getlist('document_ids[]', type=int)
    if not document_ids:
        return jsonify({'ok': False, 'failure_messages': ['No documents selected.']}), 400

    # All documents must belong to the same CBO the user has access to
    documents = BookkeepingDocument.query.filter(BookkeepingDocument.id.in_(document_ids)).all()
    if not documents:
        return jsonify({'ok': False, 'failure_messages': ['Documents not found.']}), 404

    cbo = documents[0].cbo
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)

    deleted, failures = [], []
    for document in documents:
        if document.cbo_id != cbo.id:
            failures.append(f'Document {document.id} does not belong to this CBO.')
            continue
        try:
            if current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON'):
                delete_bookkeeping_document_from_firestore(cbo, document.id)
            try:
                delete_bookkeeping_image(document)
            except Exception:
                current_app.logger.exception('Failed to delete source file for document %s', document.id)
            db.session.delete(document)
            deleted.append(document.original_filename)
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to delete bookkeeping document %s', document.id)
            failures.append(f'Could not delete {document.original_filename}.')

    if deleted:
        db.session.commit()
        _refresh_bookkeeping_audits(cbo)

    summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
    db.session.commit()
    sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))

    msg = f'Deleted {len(deleted)} document{"s" if len(deleted) != 1 else ""}.' if deleted else ''
    return _bookkeeping_response(
        cbo,
        own=own,
        summary=summary,
        success_message=msg,
        failure_messages=failures,
        redirect_slug=cbo.slug,
    )


@main_bp.route('/bookkeeping/bulk-import-live', methods=['POST'])
@login_required
def bulk_import_bookkeeping_documents_to_live():
    document_ids = request.form.getlist('document_ids[]', type=int)
    workspace_period_key = _normalize_workspace_period_key(request.form.get('workspace_period_key'))

    if not document_ids:
        return jsonify({'ok': False, 'failure_messages': ['No documents selected.']}), 400
    if not workspace_period_key:
        return jsonify({'ok': False, 'failure_messages': ['Choose the month and year for the live bookkeeping import.']}), 400

    documents = BookkeepingDocument.query.filter(BookkeepingDocument.id.in_(document_ids)).all()
    if not documents:
        return jsonify({'ok': False, 'failure_messages': ['Documents not found.']}), 404

    cbo = documents[0].cbo
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)

    updated_count = 0
    failures = []
    for document in documents:
        if document.cbo_id != cbo.id:
            failures.append(f'Document {document.id} does not belong to this CBO.')
            continue
        try:
            document.include_in_workspace = True
            document.workspace_period_key = workspace_period_key
            db.session.add(document)
            updated_count += 1
        except Exception:
            current_app.logger.exception('Failed to update bookkeeping document %s for live import', document.id)
            failures.append(f'Could not prepare {document.original_filename} for the live workbook.')

    if updated_count:
        db.session.commit()
        for document in documents:
            if document.cbo_id != cbo.id:
                continue
            try:
                sync_bookkeeping_document_to_firestore(document)
            except Exception:
                current_app.logger.exception('Failed to sync bookkeeping document %s after live import update', document.id)
        summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
        db.session.commit()
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))
    else:
        summary = _bookkeeping_summary(cbo)

    period_label = _workspace_period_label(workspace_period_key)
    if updated_count:
        success_message = (
            f'Added {updated_count} document'
            f'{"s" if updated_count != 1 else ""} to the live workbook for {period_label}. '
            'Imported rows keep their original document-type grouping.'
        )
    else:
        success_message = ''

    return _bookkeeping_response(
        cbo,
        own=own,
        summary=summary,
        success_message=success_message,
        failure_messages=failures,
        redirect_slug=cbo.slug,
    )


@main_bp.route('/bookkeeping/<int:document_id>/delete', methods=['POST'])
@login_required
def delete_bookkeeping_document(document_id):
    document = BookkeepingDocument.query.get_or_404(document_id)
    cbo = document.cbo
    _require_feedback_access(cbo)
    own = _user_owns_cbo(current_user, cbo)
    filename = document.original_filename

    try:
        firestore_deleted = False
        if current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON'):
            try:
                delete_bookkeeping_document_from_firestore(cbo, document_id)
                firestore_deleted = True
            except Exception:
                current_app.logger.exception(
                    'Failed to delete bookkeeping document %s from Firestore', document_id
                )
                raise

        try:
            delete_bookkeeping_image(document)
        except Exception:
            current_app.logger.exception('Failed to delete bookkeeping source file for document %s', document.id)

        db.session.delete(document)
        db.session.commit()

        _refresh_bookkeeping_audits(cbo)
        summary = _refresh_cbo_operational_profile(cbo, allow_claude=False)
        db.session.commit()
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message=f'Deleted {filename}.',
            failure_messages=[],
            redirect_slug=cbo.slug,
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception('Failed to delete bookkeeping document %s', document_id)
        summary = _bookkeeping_summary(cbo)
        message = str(exc).strip() or 'Could not delete the bookkeeping document.'
        if current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON') and not firestore_deleted:
            message = f'Could not delete {filename} from Firestore, so the document was left in place. {message}'
        return _bookkeeping_response(
            cbo,
            own=own,
            summary=summary,
            success_message='',
            failure_messages=[message],
            redirect_slug=cbo.slug,
        )


# ── Sync: pull Kobo data → Gemini analysis → save profile ────────
@main_bp.route('/cbo/<int:cbo_id>/sync', methods=['POST'])
@login_required
def sync_cbo(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)

    # Only the CBO's own users or funders can trigger a sync
    if _user_has_cbo_role(current_user) and not _user_has_funder_role(current_user) and current_user.cbo_id != cbo.id:
        abort(403)

    if not cbo.has_kobo_connection:
        flash('This CBO no longer has an active KoboToolbox API connection. Reconfigure the connection before syncing again.', 'info')
        return redirect(url_for('main.cbo_profile', slug=cbo.slug))

    try:
        # 1. Pull live data from KoboToolbox
        asset_id = cbo.kobo_asset_id
        submissions = fetch_kobo_submissions(asset_id)

        # 2. Cache raw data
        cbo.raw_kobo_json = json.dumps(submissions, default=str)

        # 3. Send to Gemini for analysis
        profile = analyse_kobo_data(submissions, cbo_name=cbo.name)

        # 4. Persist structured profile
        cbo.ai_profile_json = json.dumps(profile, default=str)
        _apply_profile_fields(cbo, profile)
        ensure_cbo_geocoded(cbo, profile=profile)
        
        # 5. Compute and save growth metrics
        from .gemini_service import compute_growth_metrics, compute_data_quality_badge
        growth_data = compute_growth_metrics(submissions)
        cbo.growth_metrics_json = json.dumps(growth_data, default=str)

        # 6. Badge, classifications, social impact score
        cbo.data_quality_badge  = compute_data_quality_badge(submissions)
        cbo.classifications_json = json.dumps(profile.get('classifications', [cbo.cbo_identifier or 'community']))
        cbo.social_impact_score  = int(profile.get('social_impact_score', 0) or 0)

        cbo.last_synced = datetime.utcnow()
        db.session.commit()

        flash('Profile synced with live KoboToolbox data & AI analysis!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Sync failed: {e}', 'danger')

    return redirect(url_for('main.cbo_profile', slug=cbo.slug))


@main_bp.route('/cbo/<int:cbo_id>/disconnect-kobo', methods=['POST'])
@login_required
def disconnect_kobo(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)

    if not _user_owns_cbo(current_user, cbo):
        abort(403)

    if not cbo.has_kobo_connection:
        flash('Your KoboToolbox API connection is already disconnected.', 'info')
        return redirect(url_for('main.cbo_profile', slug=cbo.slug))

    try:
        cbo.disconnect_kobo()
        db.session.commit()
        flash('KoboToolbox API connection terminated. Existing profile content remains visible, but no future Kobo syncs will run until the connection is set up again.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Could not terminate the KoboToolbox API connection: {exc}', 'danger')

    return redirect(url_for('main.cbo_profile', slug=cbo.slug))


# ── API: return profile JSON (for AJAX) ──────────────────────────
@main_bp.route('/api/cbo/<slug>')
@login_required
def api_cbo_profile(slug):
    cbo = CBO.query.filter_by(slug=slug).first_or_404()
    _require_feedback_access(cbo)
    return jsonify(_safe_json(cbo.ai_profile_json))


@main_bp.route('/api/cbo/<slug>/community-feedback')
@login_required
def api_community_feedback(slug):
    cbo = CBO.query.filter_by(slug=slug).first_or_404()
    _require_feedback_access(cbo)
    return jsonify(_community_feedback_summary(cbo))


@main_bp.route('/admin/community-feedback')
@login_required
def community_feedback_admin():
    _require_developer_access()

    cbos = CBO.query.order_by(CBO.name.asc()).all()
    cbo_cards = []
    total_subscribers = 0
    total_responses = 0

    for cbo in cbos:
        summary = _community_feedback_summary(cbo)
        total_subscribers += summary['subscribers']
        total_responses += summary['responses']
        cbo_cards.append({
            'cbo': cbo,
            'summary': summary,
        })

    recent_feedback = CommunityFeedback.query.filter_by(status='completed').order_by(
        CommunityFeedback.completed_at.desc()
    ).limit(12).all()

    readiness = {
        'twilio_configured': all([
            current_app.config.get('TWILIO_ACCOUNT_SID'),
            current_app.config.get('TWILIO_AUTH_TOKEN'),
            current_app.config.get('TWILIO_PHONE_NUMBER'),
        ]),
        'firestore_configured': bool(current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON')),
        'signature_validation': bool(current_app.config.get('TWILIO_VALIDATE_SIGNATURE')),
        'checkin_months': current_app.config.get('COMMUNITY_FEEDBACK_CHECKIN_MONTHS', 6),
        'webhook_url': url_for('main.sms_webhook', _external=True),
    }

    return render_template(
        'community_feedback_admin.html',
        cbo_cards=cbo_cards,
        recent_feedback=recent_feedback,
        readiness=readiness,
        developer_access=_has_developer_access(),
        totals={
            'cbos': len(cbos),
            'subscribers': total_subscribers,
            'responses': total_responses,
        },
    )


@main_bp.route('/admin/community-feedback/activity')
@login_required
def developer_sms_activity():
    _require_developer_access()

    activity_context = _developer_sms_activity_context()
    return render_template(
        'developer_sms_activity.html',
        cbo_cards=activity_context['cbo_cards'],
        totals=activity_context['totals'],
        featured_cbo=activity_context['featured_cbo'],
        recent_intake_days=activity_context['recent_intake_days'],
        add_cbo_source=activity_context['add_cbo_source'],
        add_cbo_intake=activity_context['add_cbo_intake'],
    )


@main_bp.route('/admin/bookkeeping')
@login_required
def bookkeeping_admin():
    _require_funder()

    q = request.args.get('q', '').strip().lower()
    document_type = request.args.get('document_type', '').strip().lower()
    cbo_id_arg = request.args.get('cbo_id', '').strip()
    selected_cbo_id = int(cbo_id_arg) if cbo_id_arg.isdigit() else None

    cbos = CBO.query.order_by(CBO.name.asc()).all()
    documents = BookkeepingDocument.query.order_by(BookkeepingDocument.created_at.desc()).all()
    filtered_documents = []
    document_types = set()
    totals = {'documents': 0, 'entries': 0, 'income': 0.0, 'expenses': 0.0}

    for document in documents:
        extracted = _safe_json(document.extracted_data_json)
        search_haystack = ' '.join([
            document.original_filename or '',
            document.summary_text or '',
            extracted.get('raw_text', '') if isinstance(extracted, dict) else '',
            extracted.get('document_title', '') if isinstance(extracted, dict) else '',
            extracted.get('organization_name', '') if isinstance(extracted, dict) else '',
        ]).lower()
        document_types.add(document.document_type)

        if selected_cbo_id and document.cbo_id != selected_cbo_id:
            continue
        if document_type and document.document_type != document_type:
            continue
        if q and q not in search_haystack:
            continue

        entries = extracted.get('bookkeeping_entries', []) if isinstance(extracted, dict) else []
        filtered_documents.append({
            'document': document,
            'cbo': document.cbo,
            'entry_count': len(entries),
        })
        totals['documents'] += 1
        totals['entries'] += len(entries)
        totals['income'] += float(document.total_income or 0.0)
        totals['expenses'] += float(document.total_expenses or 0.0)

    return render_template(
        'bookkeeping_admin.html',
        cbos=cbos,
        document_types=sorted(value for value in document_types if value),
        document_rows=filtered_documents,
        totals=totals,
        q=request.args.get('q', '').strip(),
        selected_cbo_id=selected_cbo_id,
        selected_document_type=document_type,
    )


@main_bp.route('/admin/bookkeeping/export')
@login_required
def export_bookkeeping_csv():
    _require_funder()

    q = request.args.get('q', '').strip().lower()
    document_type = request.args.get('document_type', '').strip().lower()
    cbo_id_arg = request.args.get('cbo_id', '').strip()
    selected_cbo_id = int(cbo_id_arg) if cbo_id_arg.isdigit() else None

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'cbo_name', 'document_id', 'original_filename', 'document_type', 'document_date',
        'entry_date', 'description', 'entry_type', 'category', 'amount', 'quantity', 'unit',
        'confidence', 'summary', 'source_channel'
    ])

    documents = BookkeepingDocument.query.order_by(BookkeepingDocument.created_at.desc()).all()
    for document in documents:
        extracted = _safe_json(document.extracted_data_json)
        search_haystack = ' '.join([
            document.original_filename or '',
            document.summary_text or '',
            extracted.get('raw_text', '') if isinstance(extracted, dict) else '',
            extracted.get('document_title', '') if isinstance(extracted, dict) else '',
            extracted.get('organization_name', '') if isinstance(extracted, dict) else '',
        ]).lower()

        if selected_cbo_id and document.cbo_id != selected_cbo_id:
            continue
        if document_type and document.document_type != document_type:
            continue
        if q and q not in search_haystack:
            continue

        entries = extracted.get('bookkeeping_entries', []) if isinstance(extracted, dict) else []
        if not entries:
            writer.writerow([
                document.cbo.name, document.id, document.original_filename, document.document_type,
                document.document_date, '', '', '', '', '', '', '', '', document.summary_text,
                document.source_channel,
            ])
            continue

        for entry in entries:
            writer.writerow([
                document.cbo.name,
                document.id,
                document.original_filename,
                document.document_type,
                document.document_date,
                entry.get('entry_date', ''),
                entry.get('description', ''),
                entry.get('entry_type', ''),
                entry.get('category', ''),
                entry.get('amount', ''),
                entry.get('quantity', ''),
                entry.get('unit', ''),
                entry.get('confidence', ''),
                document.summary_text,
                document.source_channel,
            ])

    filename = f'bookkeeping-export-{datetime.utcnow().strftime("%Y%m%d-%H%M%S")}.csv'
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@main_bp.route('/admin/community-feedback/run-checkins', methods=['POST'])
@login_required
def run_community_feedback_checkins():
    _require_developer_access()

    try:
        result = send_due_checkins()
        flash(
            f"Check-in run completed: sent {result['sent']}, skipped {result['skipped']}, evaluated {result['total_due']} subscribers.",
            'success',
        )
    except Exception as exc:
        flash(f'Check-in run failed: {exc}', 'danger')

    return redirect(url_for('main.community_feedback_admin'))


@main_bp.route('/admin/community-feedback/<int:cbo_id>')
@login_required
def community_feedback_cbo_detail(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_sms_activity_access(cbo)
    if _has_developer_access():
        session['developer_sms_cbo_id'] = cbo.id

    subscribers = CommunitySubscriber.query.filter_by(cbo_id=cbo.id).order_by(
        CommunitySubscriber.updated_at.desc()
    ).all()
    feedback_entries = CommunityFeedback.query.filter_by(cbo_id=cbo.id).order_by(
        CommunityFeedback.created_at.desc()
    ).limit(50).all()

    latest_simulation = request.args.get('latest_reply', '').strip()
    latest_message = request.args.get('latest_message', '').strip()
    summary = _community_feedback_summary(cbo)
    profile = _safe_json(cbo.ai_profile_json)
    bookkeeping_summary = _bookkeeping_summary(cbo)
    google_form_summary = _google_form_response_summary(cbo)
    intake_offline = _intake_offline_context(cbo)
    mobile_scan = _bookkeeping_mobile_scan_context(cbo)
    bookkeeping_offline = _bookkeeping_offline_context(cbo)

    twilio_phone = current_app.config.get('TWILIO_PHONE_NUMBER', '')
    sms_qr_svg = ''
    sms_qr_url = ''
    wa_qr_svg = ''
    wa_qr_url = ''
    signup_message = ''
    if twilio_phone:
        from urllib.parse import quote as _url_quote
        signup_message = summary.get('keyword') or get_cbo_keyword(cbo)
        sms_qr_url = f'sms:{twilio_phone}?body={_url_quote(signup_message)}'
        sms_qr_svg = _render_qr_code_svg(sms_qr_url)
        # WhatsApp deep link — strip leading '+' for wa.me format
        wa_phone = twilio_phone.lstrip('+')
        wa_qr_url = f'https://wa.me/{wa_phone}?text={_url_quote(signup_message)}'
        wa_qr_svg = _render_qr_code_svg(wa_qr_url)

    intake_form_qr_svg = ''
    if cbo.intake_form_responder_url:
        intake_form_qr_svg = _render_qr_code_svg(cbo.intake_form_responder_url)

    return render_template(
        'community_feedback_cbo_detail.html',
        cbo=cbo,
        summary=summary,
        profile=profile,
        bookkeeping_summary=bookkeeping_summary,
        developer_access=_has_developer_access(),
        subscribers=subscribers,
        feedback_entries=feedback_entries,
        latest_simulation=latest_simulation,
        latest_message=latest_message,
        twilio_ready=all([
            current_app.config.get('TWILIO_ACCOUNT_SID'),
            current_app.config.get('TWILIO_AUTH_TOKEN'),
            twilio_phone,
        ]),
        firestore_ready=bool(current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON')),
        firestore_document_path=get_feedback_document_path(cbo),
        sms_qr_svg=sms_qr_svg,
        sms_qr_url=sms_qr_url,
        wa_qr_svg=wa_qr_svg,
        wa_qr_url=wa_qr_url,
        twilio_phone=twilio_phone,
        signup_message=signup_message,
        intake_form_qr_svg=intake_form_qr_svg,
        intake_form_responder_url=cbo.intake_form_responder_url or '',
        intake_form_edit_url=cbo.intake_form_edit_url or '',
        intake_offline=intake_offline,
        mobile_scan=mobile_scan,
        bookkeeping_offline=bookkeeping_offline,
        google_forms_enabled=google_forms_enabled(),
        google_form_summary=google_form_summary,
        google_form_upload_titles=expected_upload_question_titles(),
    )


@main_bp.route('/admin/community-feedback/<int:cbo_id>/settings', methods=['POST'])
@login_required
def update_community_feedback_settings(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_sms_activity_access(cbo)

    sms_keyword = normalize_sms_keyword(request.form.get('sms_keyword', ''))
    community_prompt = request.form.get('community_prompt', '').strip()
    enabled = request.form.get('community_feedback_enabled') == 'on'

    if sms_keyword:
        for other in CBO.query.filter(CBO.id != cbo.id).all():
            if get_cbo_keyword(other) == sms_keyword:
                flash(f'The SMS keyword {sms_keyword} is already in use by {other.name}.', 'danger')
                return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))

    cbo.sms_keyword = sms_keyword or None
    cbo.community_prompt = community_prompt
    cbo.community_feedback_enabled = enabled
    db.session.commit()

    flash('Community feedback settings updated.', 'success')
    return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))


@main_bp.route('/admin/community-feedback/<int:cbo_id>/simulate', methods=['POST'])
@login_required
def simulate_community_feedback_sms(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_sms_activity_access(cbo)

    from_number = request.form.get('from_number', '').strip()
    body = request.form.get('body', '').strip()
    if not from_number or not body:
        flash('Both phone number and message body are required for simulation.', 'danger')
        return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))

    try:
        reply = handle_inbound_sms(from_number, body)
        flash(f'Simulation processed successfully. Submitted message: {body}', 'success')
    except Exception as exc:
        current_app.logger.exception('Failed to simulate inbound SMS')
        flash(f'Simulation failed: {exc}', 'danger')
        return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))

    return redirect(
        url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id, latest_reply=reply, latest_message=body)
    )


@main_bp.route('/admin/community-feedback/<int:cbo_id>/simulate-demo', methods=['POST'])
@login_required
def simulate_community_feedback_demo(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_sms_activity_access(cbo)

    from_number = request.form.get('from_number', '').strip()
    custom_message = request.form.get('body', '').strip()
    rating = request.form.get('rating', '8').strip()
    help_count = request.form.get('help_count', '3').strip()
    if not from_number:
        flash('A phone number is required to run the full demo flow.', 'danger')
        return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))
    if not custom_message:
        flash('A custom feedback message is required for the full submission flow.', 'danger')
        return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))

    keyword = get_cbo_keyword(cbo)
    demo_messages = [
        keyword,
        rating or '8',
        help_count or '3',
        custom_message,
    ]

    try:
        replies = [handle_inbound_sms(from_number, message) for message in demo_messages]
        flash('Full submission processed successfully.', 'success')
    except Exception as exc:
        current_app.logger.exception('Failed to run full demo simulation')
        flash(f'Full demo simulation failed: {exc}', 'danger')
        return redirect(url_for('main.community_feedback_cbo_detail', cbo_id=cbo.id))

    return redirect(
        url_for(
            'main.community_feedback_cbo_detail',
            cbo_id=cbo.id,
            latest_reply=replies[-1],
            latest_message=' | '.join(demo_messages),
        )
    )


@main_bp.route('/sms/webhook', methods=['POST'])
def sms_webhook():
    if not validate_twilio_request(request):
        return ('Invalid Twilio signature', 403)

    from_number = request.form.get('From', '')
    body = request.form.get('Body', '')

    try:
        reply = handle_inbound_sms(from_number, body)
    except Exception:
        current_app.logger.exception('Failed to process inbound SMS')
        reply = 'We could not process your message right now. Please try again in a few minutes.'

    return render_sms_response(reply)


# ── Helpers ───────────────────────────────────────────────────────
def _safe_json(text: str | None) -> dict:
    try:
        value = json.loads(text or '{}')
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _safe_json_list(text: str | None) -> list:
    try:
        val = json.loads(text or '[]')
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_int_arg(raw_value: str, fallback_value, default: int) -> int:
    if raw_value:
        try:
            return int(raw_value)
        except ValueError:
            return default
    if fallback_value is None:
        return default
    try:
        return int(fallback_value)
    except (TypeError, ValueError):
        return default


def _parse_float_arg(raw_value: str, fallback_value, default: float) -> float:
    if raw_value:
        try:
            return float(raw_value)
        except ValueError:
            return default
    if fallback_value is None:
        return default
    try:
        return float(fallback_value)
    except (TypeError, ValueError):
        return default


def _build_ai_match_groups(cbo_profiles: list[dict]) -> list[dict]:
    groups = [
        {
            'key': 'top',
            'title': 'Top Matches',
            'description': 'Strongest alignment across the prompt, community feedback, and CBO profile signals.',
            'entries': [],
        },
        {
            'key': 'close',
            'title': 'Close Matches',
            'description': 'Solid candidates that satisfy core criteria but have thinner supporting evidence.',
            'entries': [],
        },
        {
            'key': 'partial',
            'title': 'Partial Matches',
            'description': 'Related organisations that may fit part of the request, but not the full brief.',
            'entries': [],
        },
    ]

    for item in cbo_profiles:
        match_score = (item.get('ai_match') or {}).get('score', 0)
        if match_score >= 70:
            groups[0]['entries'].append(item)
        elif match_score >= 40:
            groups[1]['entries'].append(item)
        else:
            groups[2]['entries'].append(item)

    return [group for group in groups if group['entries']]


def _compute_growth_rate(monthly_data: list, field: str) -> float:
    """Return % growth rate comparing last 3 months vs previous 3 months."""
    if len(monthly_data) < 4:
        return 0.0
    recent = sum(m.get(field, 0) for m in monthly_data[-3:])
    prior  = sum(m.get(field, 0) for m in monthly_data[-6:-3])
    if prior == 0:
        return 0.0
    return round((recent - prior) / prior * 100, 1)


def _apply_profile_fields(cbo: CBO, profile: dict):
    """Copy top-level Gemini fields into the relational columns."""
    cbo.street_address = profile.get('address', cbo.street_address)
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


PROFILE_CHART_COLOR_PALETTE = [
    '40,167,69',
    '0,123,255',
    '255,193,7',
    '111,66,193',
    '13,148,136',
    '225,29,72',
]

PROFILE_DATE_COLUMN_KEYWORDS = ('date', 'day', 'period', 'month', 'year')
PROFILE_PARTICIPANT_COLUMN_KEYWORDS = (
    'name', 'beneficiary', 'borrower', 'member', 'customer', 'client', 'participant',
    'student', 'patient', 'farmer', 'household', 'vendor', 'supplier', 'group',
)
PROFILE_CATEGORY_COLUMN_KEYWORDS = ('category', 'type', 'purpose', 'activity', 'service', 'item', 'project')
PROFILE_DESCRIPTION_COLUMN_KEYWORDS = ('description', 'details', 'notes', 'narrative', 'summary', 'remark')
PROFILE_INCOME_COLUMN_KEYWORDS = (
    'income', 'revenue', 'sale', 'sales', 'grant', 'donation', 'fee', 'fees',
    'received', 'deposit', 'credit', 'collection', 'contribution', 'payment in', 'cash in',
)
PROFILE_EXPENSE_COLUMN_KEYWORDS = (
    'expense', 'expenses', 'cost', 'costs', 'paid', 'payment out', 'cash out', 'debit',
    'withdraw', 'purchase', 'transport', 'salary', 'wage', 'utility', 'supplies', 'liability', 'debt',
)
PROFILE_NET_COLUMN_KEYWORDS = ('net', 'balance', 'surplus', 'deficit')

PROFILE_SYNTHESIS_SCHEMA = {
    'type': 'object',
    'properties': {
        'tagline': {'type': 'string'},
        'focus_areas': {'type': 'string'},
        'governance_note': {'type': 'string'},
        'flagship_project': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'summary': {'type': 'string'},
                'stats': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'label': {'type': 'string'},
                            'value': {'type': 'string'},
                        },
                        'required': ['label', 'value'],
                        'additionalProperties': False,
                    },
                },
            },
            'required': ['title', 'summary', 'stats'],
            'additionalProperties': False,
        },
        'success_story': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'summary': {'type': 'string'},
                'quote': {'type': 'string'},
                'attribution': {'type': 'string'},
            },
            'required': ['title', 'summary', 'quote', 'attribution'],
            'additionalProperties': False,
        },
        'join_us': {'type': 'string'},
        'quantified_impact': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'icon_hint': {'type': 'string'},
                    'metric_value': {'type': 'string'},
                    'metric_unit': {'type': 'string'},
                    'description': {'type': 'string'},
                },
                'required': ['icon_hint', 'metric_value', 'metric_unit', 'description'],
                'additionalProperties': False,
            },
        },
        'social_impact_score': {'type': 'integer'},
        'social_impact_score_rationale': {'type': 'string'},
        'operational_metric_cards': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'field': {'type': 'string'},
                    'label': {'type': 'string'},
                    'accent': {'type': 'boolean'},
                },
                'required': ['field', 'label', 'accent'],
                'additionalProperties': False,
            },
        },
        'financial_overview_cards': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'field': {'type': 'string'},
                    'label': {'type': 'string'},
                    'accent': {'type': 'boolean'},
                },
                'required': ['field', 'label', 'accent'],
                'additionalProperties': False,
            },
        },
        'growth_charts': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'field': {'type': 'string'},
                    'label': {'type': 'string'},
                    'value_format': {'type': 'string'},
                },
                'required': ['field', 'label', 'value_format'],
                'additionalProperties': False,
            },
        },
        'classifications': {
            'type': 'array',
            'items': {'type': 'string'},
        },
    },
    'required': [
        'tagline',
        'focus_areas',
        'governance_note',
        'flagship_project',
        'success_story',
        'join_us',
        'quantified_impact',
        'social_impact_score',
        'social_impact_score_rationale',
        'operational_metric_cards',
        'financial_overview_cards',
        'growth_charts',
        'classifications',
    ],
    'additionalProperties': False,
}

PROFILE_SYNTHESIS_SYSTEM_PROMPT = """You customize a public-facing CBO profile using intake answers and digitized operational records.

Rules:
- Use only the provided context. Do not invent numeric values, beneficiaries, programs, dates, or money amounts.
- The candidate metric fields are the only fields you may choose for operational cards, financial cards, and growth charts.
- Prefer labels that match the organisation's actual operations instead of generic tool-rental language.
- Keep the tagline under 18 words.
- Keep the governance note to 1-2 sentences.
- Keep the join_us text to 2-3 sentences.
- Quantified impact items must stay grounded in the provided data. If evidence is thin, be conservative.
- Return only valid JSON matching the required schema.
"""


def _dedupe_strings(values) -> list[str]:
    deduped = []
    for value in values:
        text = str(value or '').strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _success_story_text(value) -> str:
    if isinstance(value, dict):
        return str(value.get('summary') or value.get('quote') or '').strip()
    return str(value or '').strip()


def _latest_provisioned_intake_response(cbo: CBO) -> GoogleFormResponse | None:
    return GoogleFormResponse.query.filter_by(provisioned_cbo_id=cbo.id).order_by(
        GoogleFormResponse.response_submitted_at.desc(),
        GoogleFormResponse.created_at.desc(),
    ).first()


def _profile_parse_date(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or '').strip()
    if not text:
        return None
    parsed = _parse_google_timestamp(text)
    if parsed:
        return parsed
    if re.fullmatch(r'\d{4}-\d{2}', text):
        try:
            return datetime.strptime(text + '-01', '%Y-%m-%d')
        except ValueError:
            return None
    for fmt in (
        '%Y-%m-%d',
        '%d/%m/%Y',
        '%m/%d/%Y',
        '%d-%m-%Y',
        '%Y/%m/%d',
        '%b %d %Y',
        '%B %d %Y',
        '%d %b %Y',
        '%d %B %Y',
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _profile_month_key(value) -> str:
    parsed = _profile_parse_date(value)
    return parsed.strftime('%Y-%m') if parsed else ''


def _format_profile_month_label(month_key: str) -> str:
    text = str(month_key or '').strip()
    if not text:
        return ''
    try:
        return datetime.strptime(text + '-01', '%Y-%m-%d').strftime('%b %Y')
    except ValueError:
        return text


def _metric_candidate(label: str, value: str, rank_value: float = 0.0, detail: str = '', accent: bool = False) -> dict:
    return {
        'label': str(label or '').strip(),
        'value': str(value or '').strip(),
        'rank_value': float(rank_value or 0.0),
        'detail': str(detail or '').strip(),
        'accent': bool(accent),
    }


def _build_metric_cards_from_selection(selection, candidate_map: dict[str, dict], default_fields: list[str], limit: int) -> list[dict]:
    cards = []
    seen = set()

    def append_card(field_name: str, label: str = '', accent: bool | None = None):
        field = str(field_name or '').strip()
        candidate = candidate_map.get(field)
        if not field or not candidate or field in seen:
            return
        if not str(candidate.get('value') or '').strip():
            return
        cards.append({
            'field': field,
            'label': str(label or candidate.get('label') or field).strip(),
            'value': str(candidate.get('value') or '').strip(),
            'detail': str(candidate.get('detail') or '').strip(),
            'accent': candidate.get('accent', False) if accent is None else bool(accent),
        })
        seen.add(field)

    for item in selection or []:
        if isinstance(item, dict):
            append_card(item.get('field'), item.get('label', ''), item.get('accent'))

    if not cards:
        for field in default_fields:
            append_card(field)

    if not cards:
        for field, candidate in sorted(candidate_map.items(), key=lambda item: item[1].get('rank_value', 0.0), reverse=True):
            if candidate.get('value'):
                append_card(field)
            if len(cards) >= limit:
                break

    return cards[:limit]


def _build_growth_charts_from_selection(selection, candidate_map: dict[str, dict], default_fields: list[str], limit: int = 4) -> list[dict]:
    charts = []
    seen = set()

    def append_chart(field_name: str, label: str = '', value_format: str = '', color: str = ''):
        field = str(field_name or '').strip()
        candidate = candidate_map.get(field)
        if not field or not candidate or field in seen:
            return
        charts.append({
            'field': field,
            'label': str(label or candidate.get('label') or field).strip(),
            'value_format': str(value_format or candidate.get('value_format') or 'number').strip(),
            'color': str(color or candidate.get('color') or '').strip(),
        })
        seen.add(field)

    for item in selection or []:
        if isinstance(item, dict):
            append_chart(item.get('field'), item.get('label', ''), item.get('value_format', ''), item.get('color', ''))

    if not charts:
        for field in default_fields:
            append_chart(field)

    if not charts:
        for field, candidate in sorted(candidate_map.items(), key=lambda item: item[1].get('rank_value', 0.0), reverse=True):
            append_chart(field)
            if len(charts) >= limit:
                break

    return charts[:limit]


def _classify_workspace_amount_column(column_name: str) -> str:
    normalized = str(column_name or '').strip().lower()
    if not normalized:
        return ''
    if any(keyword in normalized for keyword in PROFILE_INCOME_COLUMN_KEYWORDS):
        return 'income'
    if any(keyword in normalized for keyword in PROFILE_EXPENSE_COLUMN_KEYWORDS):
        return 'expense'
    if any(keyword in normalized for keyword in PROFILE_NET_COLUMN_KEYWORDS):
        return 'net'
    if any(keyword in normalized for keyword in ('amount', 'total', 'value', 'paid', 'received')):
        return 'amount'
    return ''


def _build_workspace_row_signal(entry: dict) -> dict | None:
    values = entry.get('values') or {}
    if not isinstance(values, dict):
        return None

    created_at = entry.get('created_at') if isinstance(entry.get('created_at'), datetime) else _profile_parse_date(entry.get('created_at'))
    updated_at = entry.get('updated_at') if isinstance(entry.get('updated_at'), datetime) else _profile_parse_date(entry.get('updated_at'))
    entry_date = None
    participant = ''
    category = ''
    description = ''
    ambiguous_amounts = []
    income_total = 0.0
    expense_total = 0.0
    amount_samples = []

    for column_name, raw_value in values.items():
        text = str(raw_value or '').strip()
        normalized = str(column_name or '').strip().lower()
        if not text:
            continue

        if entry_date is None and any(keyword in normalized for keyword in PROFILE_DATE_COLUMN_KEYWORDS):
            entry_date = _profile_parse_date(text)
        if not participant and any(keyword in normalized for keyword in PROFILE_PARTICIPANT_COLUMN_KEYWORDS):
            participant = text
        if not category and any(keyword in normalized for keyword in PROFILE_CATEGORY_COLUMN_KEYWORDS):
            category = text
        if not description and any(keyword in normalized for keyword in PROFILE_DESCRIPTION_COLUMN_KEYWORDS):
            description = text

        classification = _classify_workspace_amount_column(normalized)
        if not classification:
            continue

        amount = _parse_money_value(text)
        if amount == 0.0 and text not in {'0', '0.0', '0.00', 'KSh 0', 'KES 0'}:
            continue

        if classification == 'income':
            income_total += abs(amount)
            amount_samples.append(abs(amount))
        elif classification == 'expense':
            expense_total += abs(amount)
            amount_samples.append(abs(amount))
        elif classification == 'net':
            if amount < 0:
                expense_total += abs(amount)
            else:
                income_total += amount
            amount_samples.append(abs(amount))
        else:
            ambiguous_amounts.append(amount)

    if ambiguous_amounts:
        combined = sum(ambiguous_amounts)
        context_text = ' '.join(part for part in [category, description] if part).lower()
        if any(keyword in context_text for keyword in PROFILE_EXPENSE_COLUMN_KEYWORDS) or combined < 0:
            expense_total += abs(combined)
        else:
            income_total += abs(combined)
        amount_samples.append(abs(combined))

    if entry_date is None:
        entry_date = created_at or updated_at
    if not description:
        description = category or participant or 'Ledger entry'

    tracked_amount = round(income_total + expense_total, 2)
    if not tracked_amount and not participant and not category and not description:
        return None

    return {
        'date': entry_date,
        'month': _profile_month_key(entry_date),
        'participant': participant,
        'category': category,
        'description': description,
        'income_total': round(income_total, 2),
        'expense_total': round(expense_total, 2),
        'tracked_amount': tracked_amount,
        'avg_amount': round(sum(amount_samples) / len(amount_samples), 2) if amount_samples else 0.0,
        'entry_source': str(entry.get('entry_source') or 'manual').strip() or 'manual',
    }


def _build_bookkeeping_profile_context(cbo: CBO, summary: dict | None = None) -> dict:
    summary = summary or _bookkeeping_summary(cbo)
    workspace = summary.get('workspace') or {}
    workspace_entries = [entry for entry in (workspace.get('entries') or []) if isinstance(entry, dict)]
    row_signals = []
    manual_row_count = 0
    imported_row_count = 0

    for entry in workspace_entries:
        if str(entry.get('entry_source') or '').strip() == 'document_import':
            imported_row_count += 1
        else:
            manual_row_count += 1
        signal = _build_workspace_row_signal(entry)
        if signal is not None:
            row_signals.append(signal)

    monthly = defaultdict(lambda: {
        'activity_count': 0,
        'income_total': 0.0,
        'expense_total': 0.0,
        'participants': set(),
        'amount_samples': [],
    })

    for signal in row_signals:
        month_key = signal.get('month') or ''
        if not month_key:
            continue
        bucket = monthly[month_key]
        bucket['activity_count'] += 1
        bucket['income_total'] += float(signal.get('income_total') or 0.0)
        bucket['expense_total'] += float(signal.get('expense_total') or 0.0)
        if signal.get('participant'):
            bucket['participants'].add(signal['participant'])
        if signal.get('avg_amount'):
            bucket['amount_samples'].append(float(signal['avg_amount'] or 0.0))

    if not monthly:
        for item in summary.get('documents') or []:
            document = item.get('document') if isinstance(item, dict) else None
            if document is None:
                continue
            month_key = (
                _profile_month_key(document.document_date)
                or _profile_month_key(document.period_end)
                or _profile_month_key(document.processed_at)
                or _profile_month_key(document.created_at)
            )
            if not month_key:
                continue
            bucket = monthly[month_key]
            bucket['activity_count'] += int(item.get('entry_count') or 1)
            bucket['income_total'] += float(document.total_income or 0.0)
            bucket['expense_total'] += float(document.total_expenses or 0.0)
            if document.vendor_or_counterparty:
                bucket['participants'].add(str(document.vendor_or_counterparty).strip())
            if document.total_income or document.total_expenses:
                bucket['amount_samples'].append(max(float(document.total_income or 0.0), float(document.total_expenses or 0.0)))

    growth_data = []
    for month_key in sorted(monthly.keys()):
        bucket = monthly[month_key]
        income_total = round(bucket['income_total'], 2)
        expense_total = round(bucket['expense_total'], 2)
        avg_amount = round(sum(bucket['amount_samples']) / len(bucket['amount_samples']), 2) if bucket['amount_samples'] else 0.0
        participant_count = len(bucket['participants'])
        growth_data.append({
            'month': month_key,
            'rentals': int(bucket['activity_count']),
            'borrowers': participant_count,
            'revenue': income_total,
            'avg_duration': avg_amount,
            'activity_count': int(bucket['activity_count']),
            'unique_participants': participant_count,
            'income_total': income_total,
            'expense_total': expense_total,
            'net_total': round(income_total - expense_total, 2),
            'avg_transaction_amount': avg_amount,
        })

    unique_participants = _dedupe_strings(signal.get('participant') for signal in row_signals if signal.get('participant'))
    top_categories = summary.get('top_categories') or []
    top_category = top_categories[0] if top_categories else {}
    document_types = _dedupe_strings((workspace.get('source_document_types') or []) + list((summary.get('documents_by_type') or {}).keys()))
    latest_month = growth_data[-1]['month'] if growth_data else ''
    row_count = int(workspace.get('row_count') or len(workspace_entries) or summary.get('entry_count') or 0)

    operational_candidates = {
        'living_row_count': _metric_candidate(
            'Living Ledger Rows',
            f'{row_count:,}',
            row_count,
            'Rows currently stored in the living bookkeeping worksheet.',
        ),
        'document_count': _metric_candidate(
            'Digitized Documents',
            f"{int(summary.get('document_count') or 0):,}",
            float(summary.get('document_count') or 0),
            'Bookkeeping documents digitized through uploads and scans.',
        ),
        'digitized_entry_count': _metric_candidate(
            'Digitized Rows',
            f"{int(summary.get('entry_count') or 0):,}",
            float(summary.get('entry_count') or 0),
            'Structured bookkeeping rows extracted from digitized documents.',
        ),
        'unique_participants': _metric_candidate(
            'People / Orgs Logged',
            f'{len(unique_participants):,}',
            len(unique_participants),
            'Distinct people, groups, or counterparties referenced in ledger rows.',
        ),
        'months_covered': _metric_candidate(
            'Months Covered',
            f'{len(growth_data):,}',
            len(growth_data),
            'Distinct months represented in the operational record.',
        ),
        'document_type_count': _metric_candidate(
            'Document Types',
            f'{len(document_types):,}',
            len(document_types),
            'Different bookkeeping layouts or document families currently tracked.',
        ),
        'manual_row_count': _metric_candidate(
            'Manual Rows',
            f'{manual_row_count:,}',
            manual_row_count,
            'Rows added or corrected directly in the living worksheet.',
        ),
        'top_category': _metric_candidate(
            'Top Category',
            str(top_category.get('label') or '—'),
            float(top_category.get('amount') or 0.0),
            'Largest observed category in the digitized bookkeeping totals.',
            True,
        ),
        'latest_activity_month': _metric_candidate(
            'Latest Activity Window',
            _format_profile_month_label(latest_month) or '—',
            float(len(growth_data) or 0),
            'Most recent month represented in the bookkeeping record.',
        ),
    }

    growth_candidates = {}
    for index, (field, label, value_format) in enumerate([
        ('activity_count', 'Tracked Activity', 'number'),
        ('income_total', 'Tracked Income', 'currency'),
        ('expense_total', 'Tracked Expenses', 'currency'),
        ('net_total', 'Net Position', 'currency'),
        ('unique_participants', 'People / Orgs Logged', 'number'),
        ('avg_transaction_amount', 'Average Entry Amount', 'currency'),
    ]):
        max_value = max((float(item.get(field) or 0.0) for item in growth_data), default=0.0)
        growth_candidates[field] = {
            'label': label,
            'value_format': value_format,
            'rank_value': max_value,
            'color': PROFILE_CHART_COLOR_PALETTE[index % len(PROFILE_CHART_COLOR_PALETTE)],
        }

    return {
        'summary': summary,
        'growth_data': growth_data,
        'row_count': row_count,
        'document_count': int(summary.get('document_count') or 0),
        'digitized_entry_count': int(summary.get('entry_count') or 0),
        'manual_row_count': manual_row_count,
        'imported_row_count': imported_row_count,
        'unique_participants': unique_participants,
        'document_types': document_types,
        'months_covered': len(growth_data),
        'latest_month': latest_month,
        'top_category': top_category,
        'operational_candidates': operational_candidates,
        'growth_candidates': growth_candidates,
        'default_operational_fields': [
            'living_row_count',
            'unique_participants',
            'document_count',
            'months_covered',
            'top_category',
            'manual_row_count',
        ],
        'default_growth_fields': ['activity_count', 'income_total', 'expense_total', 'unique_participants'],
        'material_data_available': bool(row_count or summary.get('document_count') or summary.get('income_total') or summary.get('expense_total')),
    }


def _build_intake_profile_context(cbo: CBO, profile: dict, response_record: GoogleFormResponse | None = None, answer_lookup: dict | None = None) -> dict:
    resolved_response = response_record or _latest_provisioned_intake_response(cbo)
    if answer_lookup is None and resolved_response is not None:
        answer_lookup = _google_form_answer_lookup(_safe_json_list(resolved_response.answers_json))

    intake_summary = dict(profile.get('intake_summary') or {})
    if answer_lookup is not None:
        intake_summary.update({
            'submitter_name': _google_form_answer_value(answer_lookup, 'Your full name') or intake_summary.get('submitter_name', ''),
            'submitter_role': _google_form_answer_value(answer_lookup, 'Your position / role in the CBO') or intake_summary.get('submitter_role', ''),
            'whatsapp_number': _google_form_answer_value(answer_lookup, 'WhatsApp phone number') or intake_summary.get('whatsapp_number', ''),
            'email_address': _google_form_answer_value(answer_lookup, 'Email address') or intake_summary.get('email_address', ''),
            'program_locations': _google_form_answer_value(answer_lookup, 'CBO Program Locations') or intake_summary.get('program_locations', ''),
            'bank_name': _google_form_answer_value(answer_lookup, 'Financial Institution (Bank) Name') or intake_summary.get('bank_name', ''),
            'bank_contact': _google_form_answer_value(answer_lookup, 'Financial Institution (Bank) Contact Information') or intake_summary.get('bank_contact', ''),
            'dedicated_bank_account': _google_form_answer_value(answer_lookup, 'Do you have a registered bank account dedicated to CBO programs and activity?') or intake_summary.get('dedicated_bank_account', ''),
            'full_budget': _format_kes_amount(_google_form_answer_value(answer_lookup, 'Full CBO budget (past year)')) or intake_summary.get('full_budget', ''),
            'total_expenses': _format_kes_amount(_google_form_answer_value(answer_lookup, 'Total CBO expenses (past year)')) or intake_summary.get('total_expenses', ''),
            'expense_distribution': _google_form_answer_value(answer_lookup, 'Describe how expenses were distributed') or intake_summary.get('expense_distribution', ''),
            'debt_liabilities': _format_kes_amount(_google_form_answer_value(answer_lookup, 'CBO debt / liabilities')) or intake_summary.get('debt_liabilities', ''),
            'cash_reserves': _format_kes_amount(_google_form_answer_value(answer_lookup, 'Full CBO cash reserves')) or intake_summary.get('cash_reserves', ''),
            'grants': _google_form_answer_value(answer_lookup, 'Describe past and present grants obtained (donor, amount, dates, purpose)') or intake_summary.get('grants', ''),
            'milestones': _google_form_answer_value(answer_lookup, 'Describe any milestones achieved in the past three years') or intake_summary.get('milestones', ''),
            'references': _google_form_answer_value(answer_lookup, 'Please list three references: name, contact information (WhatsApp / email), and relationship with the CBO') or intake_summary.get('references', ''),
            'additional_tracking_fields': _split_intake_list_value(_google_form_answer_value(answer_lookup, ADDITIONAL_TRACKING_FIELDS_TITLE)) or intake_summary.get('additional_tracking_fields', []),
        })

    anecdotal_story = _google_form_answer_value(answer_lookup, ANECDOTAL_STORY_TITLE) if answer_lookup is not None else ''
    success_story = profile.get('success_story') or {}
    story_text = anecdotal_story or intake_summary.get('anecdotal_story') or _success_story_text(success_story)
    email_address = intake_summary.get('email_address') or (resolved_response.respondent_email if resolved_response else '')

    return {
        'cbo_name': str(profile.get('name') or cbo.name or '').strip(),
        'location': str(profile.get('location') or cbo.location or '').strip(),
        'address': str(profile.get('address') or cbo.street_address or '').strip(),
        'founded_year': str(profile.get('founded_year') or cbo.founded_year or '').strip(),
        'submitter_name': str(intake_summary.get('submitter_name') or '').strip(),
        'submitter_role': str(intake_summary.get('submitter_role') or '').strip(),
        'whatsapp_number': str(intake_summary.get('whatsapp_number') or '').strip(),
        'email_address': str(email_address or '').strip(),
        'program_locations': str(intake_summary.get('program_locations') or '').strip(),
        'bank_name': str(intake_summary.get('bank_name') or '').strip(),
        'bank_contact': str(intake_summary.get('bank_contact') or '').strip(),
        'dedicated_bank_account': str(intake_summary.get('dedicated_bank_account') or '').strip(),
        'full_budget': str(intake_summary.get('full_budget') or '').strip(),
        'total_expenses': str(intake_summary.get('total_expenses') or '').strip(),
        'expense_distribution': str(intake_summary.get('expense_distribution') or '').strip(),
        'debt_liabilities': str(intake_summary.get('debt_liabilities') or '').strip(),
        'cash_reserves': str(intake_summary.get('cash_reserves') or '').strip(),
        'grants': str(intake_summary.get('grants') or '').strip(),
        'milestones': str(intake_summary.get('milestones') or '').strip(),
        'references': str(intake_summary.get('references') or '').strip(),
        'additional_tracking_fields': [
            _titleize_workspace_label(value)
            for value in (intake_summary.get('additional_tracking_fields') or [])
            if _titleize_workspace_label(value)
        ],
        'anecdotal_story': str(story_text or '').strip(),
        'intake_summary': intake_summary,
        'has_substantive_input': bool(
            story_text
            or intake_summary.get('milestones')
            or intake_summary.get('grants')
            or intake_summary.get('full_budget')
            or intake_summary.get('total_expenses')
            or intake_summary.get('program_locations')
            or intake_summary.get('references')
            or (intake_summary.get('additional_tracking_fields') or [])
        ),
    }


def _build_local_quantified_impact(bookkeeping_context: dict, intake_context: dict, financial_candidates: dict[str, dict]) -> list[dict]:
    impacts = []
    row_count = int(bookkeeping_context.get('row_count') or 0)
    document_count = int(bookkeeping_context.get('document_count') or 0)
    months_covered = int(bookkeeping_context.get('months_covered') or 0)
    participant_count = len(bookkeeping_context.get('unique_participants') or [])
    additional_fields = intake_context.get('additional_tracking_fields') or []

    if row_count:
        impacts.append({
            'icon_hint': 'chart-up',
            'metric_value': f'{row_count:,}',
            'metric_unit': 'ledger rows',
            'description': 'Operational rows are now tracked in the living bookkeeping worksheet.',
            'details': {
                'raw_data': f'{row_count:,} visible rows currently stored in the live worksheet.',
                'methodology': 'Counted from the living worksheet after imported and manual rows were merged.',
                'breakdown': [
                    f"Manual rows: {int(bookkeeping_context.get('manual_row_count') or 0):,}",
                    f"Imported rows: {int(bookkeeping_context.get('imported_row_count') or 0):,}",
                    f"Document-backed rows: {int(bookkeeping_context.get('digitized_entry_count') or 0):,}",
                ],
            },
        })

    income_candidate = financial_candidates.get('income_total')
    if income_candidate and income_candidate.get('value'):
        impacts.append({
            'icon_hint': 'money',
            'metric_value': income_candidate['value'],
            'metric_unit': 'tracked income',
            'description': 'Revenue or incoming funds documented through the current operational record.',
            'details': {
                'raw_data': f"Summary total: {income_candidate['value']}.",
                'methodology': 'Summed from digitized bookkeeping documents and any classified worksheet income rows.',
                'breakdown': [
                    f"Months covered: {months_covered}",
                    f"Digitized documents: {document_count}",
                    f"Top category: {str((bookkeeping_context.get('top_category') or {}).get('label') or 'Not yet categorized')}",
                ],
            },
        })

    expense_candidate = financial_candidates.get('expense_total')
    if expense_candidate and expense_candidate.get('value'):
        impacts.append({
            'icon_hint': 'money',
            'metric_value': expense_candidate['value'],
            'metric_unit': 'tracked expenses',
            'description': 'Outgoing costs already reflected in the digitized and live-ledger record.',
            'details': {
                'raw_data': f"Summary total: {expense_candidate['value']}.",
                'methodology': 'Summed from digitized bookkeeping documents and any classified worksheet expense rows.',
                'breakdown': [
                    f"Largest category: {str((bookkeeping_context.get('top_category') or {}).get('label') or 'Not yet categorized')}",
                    f"Distinct document types: {len(bookkeeping_context.get('document_types') or [])}",
                    f"Latest reporting month: {_format_profile_month_label(bookkeeping_context.get('latest_month', '')) or 'Not yet dated'}",
                ],
            },
        })

    if participant_count:
        impacts.append({
            'icon_hint': 'people',
            'metric_value': f'{participant_count:,}',
            'metric_unit': 'people or organizations logged',
            'description': 'Named stakeholders or counterparties are now visible inside the operational ledger.',
            'details': {
                'raw_data': f'{participant_count:,} unique names appeared in worksheet rows with a participant-like field.',
                'methodology': 'De-duplicated names were counted across participant, member, customer, and vendor-style columns.',
                'breakdown': [
                    f"Latest activity month: {_format_profile_month_label(bookkeeping_context.get('latest_month', '')) or 'Not yet dated'}",
                    f"Months covered: {months_covered}",
                    f"Living worksheet rows: {row_count}",
                ],
            },
        })

    if months_covered:
        impacts.append({
            'icon_hint': 'clock',
            'metric_value': f'{months_covered:,}',
            'metric_unit': 'months of operational evidence',
            'description': 'The profile now reflects longitudinal data rather than a single static snapshot.',
            'details': {
                'raw_data': f"Growth series currently spans {months_covered:,} distinct month(s).",
                'methodology': 'Month buckets are derived from dated worksheet rows, document dates, or processing timestamps.',
                'breakdown': [
                    f"First visible month: {_format_profile_month_label((bookkeeping_context.get('growth_data') or [{}])[0].get('month', '')) if bookkeeping_context.get('growth_data') else 'Not yet dated'}",
                    f"Latest visible month: {_format_profile_month_label(bookkeeping_context.get('latest_month', '')) or 'Not yet dated'}",
                    f"Documents represented: {document_count}",
                ],
            },
        })

    if additional_fields:
        impacts.append({
            'icon_hint': 'chart-up',
            'metric_value': f'{len(additional_fields):,}',
            'metric_unit': 'custom indicators requested',
            'description': 'The intake form identified custom data points this CBO wants tracked over time.',
            'details': {
                'raw_data': ', '.join(additional_fields[:6]),
                'methodology': 'Derived from the intake form field asking which extra metrics should be captured in bookkeeping and reporting.',
                'breakdown': additional_fields[:4],
            },
        })

    return impacts[:6]


def _default_social_impact_score(bookkeeping_context: dict, intake_context: dict, financial_candidates: dict[str, dict]) -> tuple[int, str]:
    row_count = int(bookkeeping_context.get('row_count') or 0)
    document_count = int(bookkeeping_context.get('document_count') or 0)
    months_covered = int(bookkeeping_context.get('months_covered') or 0)
    participant_count = len(bookkeeping_context.get('unique_participants') or [])
    intake_signals = sum(1 for value in [
        intake_context.get('anecdotal_story'),
        intake_context.get('milestones'),
        intake_context.get('grants'),
        intake_context.get('references'),
        intake_context.get('program_locations'),
        intake_context.get('bank_name'),
    ] if str(value or '').strip())
    if intake_context.get('additional_tracking_fields'):
        intake_signals += 1

    breadth_score = min(25, participant_count * 3 + min(months_covered, 4) * 2)
    transparency_score = min(25, document_count * 4 + min(row_count, 40) // 2)
    finance_score = min(
        25,
        (10 if financial_candidates.get('income_total', {}).get('rank_value') else 0)
        + (10 if financial_candidates.get('expense_total', {}).get('rank_value') else 0)
        + min(months_covered, 5),
    )
    intake_score = min(25, intake_signals * 4)
    total_score = max(12, min(100, breadth_score + transparency_score + finance_score + intake_score))
    rationale = (
        f"Score combines {row_count:,} living-ledger rows, {document_count:,} digitized document(s), "
        f"{months_covered:,} month(s) of operational history, and {intake_signals:,} substantive intake signals."
    )
    return total_score, rationale


def _extract_anthropic_json_payload(response) -> dict:
    for block in getattr(response, 'content', []) or []:
        parsed_output = getattr(block, 'parsed_output', None)
        if isinstance(parsed_output, dict):
            return parsed_output
        if parsed_output is not None and hasattr(parsed_output, 'model_dump'):
            return parsed_output.model_dump()

    message = ''.join(
        getattr(block, 'text', '')
        for block in getattr(response, 'content', []) or []
        if getattr(block, 'type', '') == 'text'
    ).strip()
    if not message:
        return {}
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        start = message.find('{')
        end = message.rfind('}')
        if start != -1 and end > start:
            return json.loads(message[start:end + 1])
        raise


def _request_claude_profile_customization(cbo: CBO, profile: dict, intake_context: dict, bookkeeping_context: dict, selection_context: dict) -> dict:
    api_key = str(current_app.config.get('CLAUDE_API_KEY') or '').strip()
    if not api_key:
        return {}
    if not bookkeeping_context.get('material_data_available') and not intake_context.get('has_substantive_input'):
        return {}

    payload = {
        'cbo': {
            'name': cbo.name,
            'slug': cbo.slug,
            'location': intake_context.get('location') or cbo.location,
            'org_type': profile.get('org_type') or cbo.org_type,
        },
        'intake': {
            'submitter_name': intake_context.get('submitter_name'),
            'submitter_role': intake_context.get('submitter_role'),
            'program_locations': intake_context.get('program_locations'),
            'bank_name': intake_context.get('bank_name'),
            'full_budget': intake_context.get('full_budget'),
            'total_expenses': intake_context.get('total_expenses'),
            'cash_reserves': intake_context.get('cash_reserves'),
            'debt_liabilities': intake_context.get('debt_liabilities'),
            'grants': intake_context.get('grants'),
            'milestones': intake_context.get('milestones'),
            'references': intake_context.get('references'),
            'additional_tracking_fields': intake_context.get('additional_tracking_fields') or [],
            'anecdotal_story': intake_context.get('anecdotal_story'),
        },
        'bookkeeping': {
            'document_count': bookkeeping_context.get('document_count'),
            'digitized_entry_count': bookkeeping_context.get('digitized_entry_count'),
            'living_row_count': bookkeeping_context.get('row_count'),
            'manual_row_count': bookkeeping_context.get('manual_row_count'),
            'months_covered': bookkeeping_context.get('months_covered'),
            'unique_participants': len(bookkeeping_context.get('unique_participants') or []),
            'document_types': bookkeeping_context.get('document_types') or [],
            'top_category': bookkeeping_context.get('top_category') or {},
            'growth_preview': (bookkeeping_context.get('growth_data') or [])[-6:],
        },
        'candidate_operational_cards': [
            {
                'field': field,
                'label': candidate.get('label', field),
                'value': candidate.get('value', ''),
                'detail': candidate.get('detail', ''),
            }
            for field, candidate in selection_context.get('operational_candidates', {}).items()
        ],
        'candidate_financial_cards': [
            {
                'field': field,
                'label': candidate.get('label', field),
                'value': candidate.get('value', ''),
                'detail': candidate.get('detail', ''),
            }
            for field, candidate in selection_context.get('financial_candidates', {}).items()
        ],
        'candidate_growth_charts': [
            {
                'field': field,
                'label': candidate.get('label', field),
                'value_format': candidate.get('value_format', 'number'),
            }
            for field, candidate in selection_context.get('growth_candidates', {}).items()
        ],
        'profile_seed': {
            'tagline': profile.get('tagline', ''),
            'focus_areas': profile.get('focus_areas', ''),
            'governance_note': profile.get('governance_note', ''),
            'flagship_project': profile.get('flagship_project', {}),
            'success_story': profile.get('success_story', {}),
            'join_us': profile.get('join_us', ''),
            'quantified_impact': profile.get('quantified_impact', []),
            'social_impact_score': profile.get('social_impact_score', 0),
            'social_impact_score_rationale': profile.get('social_impact_score_rationale', ''),
        },
    }

    client = Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=current_app.config.get('PROFILE_SYNTHESIS_MODEL', 'claude-3-5-sonnet-latest'),
            temperature=0.2,
            max_tokens=6000,
            timeout=current_app.config.get('BOOKKEEPING_REQUEST_TIMEOUT', 180),
            system=PROFILE_SYNTHESIS_SYSTEM_PROMPT,
            output_config={
                'format': {
                    'type': 'json_schema',
                    'schema': PROFILE_SYNTHESIS_SCHEMA,
                },
            },
            messages=[
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'text',
                            'text': (
                                'Customize the CBO profile using this intake and bookkeeping context. '\
                                'Choose only from the provided candidate metric fields. '\
                                f'Context JSON:\n{json.dumps(payload, indent=2, default=str)}'
                            ),
                        }
                    ],
                }
            ],
        )
        return _extract_anthropic_json_payload(response)
    except RateLimitError:
        current_app.logger.warning('Claude profile synthesis quota exceeded for CBO %s', cbo.id)
    except APITimeoutError:
        current_app.logger.warning('Claude profile synthesis timed out for CBO %s', cbo.id)
    except APIConnectionError:
        current_app.logger.warning('Claude profile synthesis connection failed for CBO %s', cbo.id)
    except BadRequestError as exc:
        current_app.logger.warning('Claude profile synthesis rejected payload for CBO %s: %s', cbo.id, exc)
    except APIStatusError as exc:
        current_app.logger.warning('Claude profile synthesis API status error for CBO %s: %s', cbo.id, exc)
    except json.JSONDecodeError:
        current_app.logger.warning('Claude profile synthesis returned invalid JSON for CBO %s', cbo.id)
    except Exception:
        current_app.logger.exception('Claude profile synthesis failed for CBO %s', cbo.id)
    return {}


def _refresh_cbo_operational_profile(
    cbo: CBO,
    seed_profile: dict | None = None,
    response_record: GoogleFormResponse | None = None,
    answer_lookup: dict | None = None,
    bookkeeping_summary: dict | None = None,
    allow_claude: bool = False,
) -> dict:
    profile = dict(seed_profile or _safe_json(cbo.ai_profile_json) or {})
    bookkeeping_context = _build_bookkeeping_profile_context(cbo, bookkeeping_summary)
    intake_context = _build_intake_profile_context(cbo, profile, response_record=response_record, answer_lookup=answer_lookup)

    focus_areas = str(profile.get('focus_areas') or cbo.focus_areas or 'Community development, local service delivery').strip()
    classifications = _safe_json_list(cbo.classifications_json) or profile.get('classifications') or [cbo.cbo_identifier or 'community']
    classifications = _dedupe_strings(classifications)
    if not classifications:
        classifications = [cbo.cbo_identifier or 'community']

    financial_candidates = {
        'income_total': _metric_candidate(
            'Tracked Income',
            _format_kes_amount(bookkeeping_context['summary'].get('income_total')),
            float(bookkeeping_context['summary'].get('income_total') or 0.0),
            'Income total currently visible in digitized and live-bookkeeping records.',
        ),
        'expense_total': _metric_candidate(
            'Tracked Expenses',
            _format_kes_amount(bookkeeping_context['summary'].get('expense_total')),
            float(bookkeeping_context['summary'].get('expense_total') or 0.0),
            'Expense total currently visible in digitized and live-bookkeeping records.',
        ),
        'net_total': _metric_candidate(
            'Net Position',
            _format_kes_amount(bookkeeping_context['summary'].get('net_total')),
            float(abs(bookkeeping_context['summary'].get('net_total') or 0.0)),
            'Difference between tracked income and tracked expenses.',
            True,
        ),
        'annual_budget': _metric_candidate(
            'Annual Budget',
            str(intake_context.get('full_budget') or '').strip(),
            _parse_money_value(intake_context.get('full_budget')),
            'Budget reported in the intake form.',
        ),
        'cash_reserves': _metric_candidate(
            'Cash Reserves',
            str(intake_context.get('cash_reserves') or '').strip(),
            _parse_money_value(intake_context.get('cash_reserves')),
            'Reserves reported in the intake form.',
        ),
        'debt_liabilities': _metric_candidate(
            'Debt / Liabilities',
            str(intake_context.get('debt_liabilities') or '').strip(),
            _parse_money_value(intake_context.get('debt_liabilities')),
            'Debt or liabilities reported in the intake form.',
        ),
    }

    default_financial_fields = [
        'income_total',
        'expense_total',
        'net_total',
        'cash_reserves' if financial_candidates.get('cash_reserves', {}).get('rank_value') else 'annual_budget',
    ]

    operational_cards = _build_metric_cards_from_selection(
        profile.get('operational_metric_cards'),
        bookkeeping_context['operational_candidates'],
        bookkeeping_context['default_operational_fields'],
        limit=6,
    )
    financial_cards = _build_metric_cards_from_selection(
        profile.get('financial_overview_cards'),
        financial_candidates,
        default_financial_fields,
        limit=4,
    )
    growth_charts = _build_growth_charts_from_selection(
        profile.get('growth_charts'),
        bookkeeping_context['growth_candidates'],
        bookkeeping_context['default_growth_fields'],
        limit=4,
    )

    total_score, score_rationale = _default_social_impact_score(bookkeeping_context, intake_context, financial_candidates)
    success_story = dict(profile.get('success_story') or {})
    if intake_context.get('anecdotal_story'):
        success_story = {
            'title': str(success_story.get('title') or 'Community Story').strip(),
            'summary': str(success_story.get('summary') or intake_context.get('anecdotal_story') or '').strip(),
            'quote': str(success_story.get('quote') or intake_context.get('anecdotal_story') or '').strip(),
            'attribution': str(success_story.get('attribution') or intake_context.get('submitter_name') or profile.get('name') or cbo.name).strip(),
        }

    operational_model_bits = _dedupe_strings([
        intake_context.get('dedicated_bank_account'),
        intake_context.get('expense_distribution'),
        intake_context.get('grants'),
    ])
    tagline = str(profile.get('tagline') or '').strip()
    if not tagline:
        if intake_context.get('anecdotal_story'):
            tagline = re.split(r'(?<=[.!?])\s+', intake_context['anecdotal_story'])[0][:180]
        elif bookkeeping_context.get('row_count'):
            tagline = 'Operational evidence now flows directly from the living worksheet into the public profile.'
        else:
            tagline = f'Community-based organisation serving {intake_context.get("location") or cbo.location or "its local community"}.'

    governance_note = str(profile.get('governance_note') or '').strip()
    if not governance_note:
        governance_bits = _dedupe_strings([
            f"Primary contact: {intake_context.get('submitter_name')} ({intake_context.get('submitter_role')})" if intake_context.get('submitter_name') else '',
            f"Banking relationship: {intake_context.get('bank_name')}" if intake_context.get('bank_name') else '',
            f"Dedicated program account: {intake_context.get('dedicated_bank_account')}" if intake_context.get('dedicated_bank_account') else '',
        ])
        governance_note = ' '.join(governance_bits[:2]) or 'Leadership and governance details are being assembled from intake responses and operational records.'

    flagship_project = dict(profile.get('flagship_project') or {})
    if not flagship_project:
        if intake_context.get('milestones'):
            flagship_project = {
                'title': 'Recent Milestones',
                'summary': str(intake_context.get('milestones') or '').strip(),
                'stats': [{'label': card['label'], 'value': card['value']} for card in operational_cards[:3]],
            }
        else:
            flagship_project = {
                'title': 'Operational Readiness',
                'summary': 'The public profile now reflects intake onboarding data alongside living bookkeeping records and digitized operational evidence.',
                'stats': [{'label': card['label'], 'value': card['value']} for card in operational_cards[:3]],
            }

    join_us = str(profile.get('join_us') or '').strip()
    if not join_us:
        join_us = (
            f"{cbo.name} is building a stronger public operating profile from its intake data and living bookkeeping worksheet. "
            'Support can help the organisation expand programs, improve operational controls, and keep impact reporting current.'
        )

    quantified_impact = _build_local_quantified_impact(bookkeeping_context, intake_context, financial_candidates)

    profile.update({
        'name': str(profile.get('name') or intake_context.get('cbo_name') or cbo.name).strip(),
        'tagline': tagline,
        'address': str(profile.get('address') or intake_context.get('address') or cbo.street_address or '').strip(),
        'location': str(profile.get('location') or intake_context.get('location') or cbo.location or '').strip(),
        'founded_year': str(profile.get('founded_year') or intake_context.get('founded_year') or cbo.founded_year or '').strip(),
        'org_type': str(profile.get('org_type') or cbo.org_type or 'Community-Based Organisation (CBO)').strip(),
        'focus_areas': focus_areas,
        'governance_note': governance_note,
        'flagship_project': flagship_project,
        'success_story': success_story,
        'join_us': join_us,
        'quantified_impact': quantified_impact,
        'social_impact_score': total_score,
        'social_impact_score_rationale': score_rationale,
        'classifications': classifications,
        'operational_metric_cards': operational_cards,
        'financial_overview_cards': financial_cards,
        'growth_charts': growth_charts,
    })

    profile['financial_data'] = {
        'total_revenue': financial_candidates['income_total']['value'] or financial_candidates['annual_budget']['value'],
        'avg_rental_fee': financial_candidates['debt_liabilities']['value'],
        'maintenance_costs': financial_candidates['expense_total']['value'],
        'damage_fees_collected': financial_candidates['cash_reserves']['value'] or financial_candidates['net_total']['value'],
        'operational_model': ' | '.join(operational_model_bits),
    }
    profile['operational_metrics'] = {
        'total_rentals': int(bookkeeping_context.get('row_count') or 0),
        'unique_borrowers': len(bookkeeping_context.get('unique_participants') or []),
        'tools_in_inventory': int(bookkeeping_context.get('document_count') or 0),
        'avg_rental_duration_days': float((bookkeeping_context.get('growth_data') or [{}])[-1].get('avg_transaction_amount', 0.0) or 0.0) if bookkeeping_context.get('growth_data') else 0.0,
        'on_time_return_rate': f"{profile['social_impact_score']}%",
        'most_popular_tool': str((bookkeeping_context.get('top_category') or {}).get('label') or '—'),
        'busiest_rental_period': _format_profile_month_label(bookkeeping_context.get('latest_month', '')) or '—',
        'maintenance_compliance': f"{int(bookkeeping_context.get('months_covered') or 0)} months",
    }

    selection_context = {
        'operational_candidates': bookkeeping_context['operational_candidates'],
        'financial_candidates': financial_candidates,
        'growth_candidates': bookkeeping_context['growth_candidates'],
        'default_operational_fields': bookkeeping_context['default_operational_fields'],
        'default_financial_fields': default_financial_fields,
        'default_growth_fields': bookkeeping_context['default_growth_fields'],
    }

    if allow_claude:
        customization = _request_claude_profile_customization(cbo, profile, intake_context, bookkeeping_context, selection_context)
        if customization:
            for key in ('tagline', 'focus_areas', 'governance_note', 'join_us', 'social_impact_score_rationale'):
                value = customization.get(key)
                if str(value or '').strip():
                    profile[key] = str(value).strip()

            if isinstance(customization.get('flagship_project'), dict) and customization['flagship_project'].get('title'):
                profile['flagship_project'] = customization['flagship_project']
            if isinstance(customization.get('success_story'), dict) and (
                customization['success_story'].get('summary') or customization['success_story'].get('quote')
            ):
                profile['success_story'] = customization['success_story']
            if customization.get('quantified_impact'):
                profile['quantified_impact'] = customization['quantified_impact'][:6]
            if customization.get('classifications'):
                profile['classifications'] = _dedupe_strings(customization['classifications']) or profile['classifications']
            if isinstance(customization.get('social_impact_score'), int):
                profile['social_impact_score'] = max(0, min(100, int(customization['social_impact_score'])))

            profile['operational_metric_cards'] = _build_metric_cards_from_selection(
                customization.get('operational_metric_cards') or profile.get('operational_metric_cards'),
                selection_context['operational_candidates'],
                selection_context['default_operational_fields'],
                limit=6,
            )
            profile['financial_overview_cards'] = _build_metric_cards_from_selection(
                customization.get('financial_overview_cards') or profile.get('financial_overview_cards'),
                selection_context['financial_candidates'],
                selection_context['default_financial_fields'],
                limit=4,
            )
            profile['growth_charts'] = _build_growth_charts_from_selection(
                customization.get('growth_charts') or profile.get('growth_charts'),
                selection_context['growth_candidates'],
                selection_context['default_growth_fields'],
                limit=4,
            )

    cbo.ai_profile_json = json.dumps(profile, default=str)
    cbo.growth_metrics_json = json.dumps(bookkeeping_context.get('growth_data') or [], default=str)
    cbo.classifications_json = json.dumps(profile.get('classifications') or [cbo.cbo_identifier or 'community'])
    cbo.social_impact_score = int(profile.get('social_impact_score', 0) or 0)
    _apply_profile_fields(cbo, profile)
    ensure_cbo_geocoded(cbo, profile=profile)
    db.session.add(cbo)
    return bookkeeping_context['summary']


def _require_funder():
    if not _user_has_funder_role(current_user):
        abort(403)


def _user_has_funder_role(user) -> bool:
    return bool(getattr(user, 'is_authenticated', False) and getattr(user, 'is_funder', False))


def _user_has_cbo_role(user) -> bool:
    return bool(getattr(user, 'is_authenticated', False) and getattr(user, 'is_cbo', False))


def _user_owns_cbo(user, cbo: CBO) -> bool:
    return bool(_user_has_cbo_role(user) and getattr(user, 'cbo_id', None) == cbo.id)


def _can_manage_bookkeeping(cbo: CBO) -> bool:
    return bool(
        current_user.is_authenticated
        and (
            _has_developer_access()
            or _user_has_funder_role(current_user)
            or _user_owns_cbo(current_user, cbo)
        )
    )


def _active_portal_role() -> str:
    if not current_user.is_authenticated:
        return ''

    selected = str(session.get('active_role') or '').strip().lower()
    if selected in {'funder', 'cbo'} and current_user.has_role(selected):
        return selected
    if _user_has_cbo_role(current_user):
        return 'cbo'
    if _user_has_funder_role(current_user):
        return 'funder'
    return str(getattr(current_user, 'role', '') or '').strip().lower()


def _has_developer_access() -> bool:
    return current_user.is_authenticated and bool(session.get('developer_access'))


def _require_developer_access():
    if not _has_developer_access():
        abort(403)


def _require_feedback_access(cbo: CBO):
    if _has_developer_access():
        return
    if _user_has_funder_role(current_user):
        return
    if _user_owns_cbo(current_user, cbo):
        return
    abort(403)


def _require_bookkeeping_owner(cbo: CBO):
    _require_feedback_access(cbo)
    if not _user_owns_cbo(current_user, cbo):
        abort(403)


def _require_bookkeeping_offline_access(cbo: CBO):
    _require_feedback_access(cbo)


def _require_sms_activity_access(cbo: CBO):
    if _has_developer_access():
        return
    if _user_owns_cbo(current_user, cbo):
        return
    abort(403)


def _default_developer_sms_activity_cbo() -> CBO | None:
    preferred_cbo_id = session.get('developer_sms_cbo_id')
    if preferred_cbo_id is not None:
        try:
            preferred_cbo_id = int(preferred_cbo_id)
        except (TypeError, ValueError):
            preferred_cbo_id = None
        if preferred_cbo_id is not None:
            preferred_cbo = db.session.get(CBO, preferred_cbo_id)
            if preferred_cbo is not None:
                return preferred_cbo
        session.pop('developer_sms_cbo_id', None)

    configured_cbo_id = current_app.config.get('DEVELOPER_SMS_ACTIVITY_CBO_ID')
    if configured_cbo_id is not None:
        configured_cbo = db.session.get(CBO, configured_cbo_id)
        if configured_cbo is not None:
            return configured_cbo

    configured_slug = str(current_app.config.get('DEVELOPER_SMS_ACTIVITY_CBO_SLUG') or '').strip().lower()
    if configured_slug:
        configured_cbo = CBO.query.filter_by(slug=configured_slug).first()
        if configured_cbo is not None:
            return configured_cbo

    cbo_user_counts: dict[int, int] = {}
    for user in User.query.filter_by(role='cbo').all():
        if user.cbo_id is None:
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


def _bookkeeping_mobile_scan_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=BOOKKEEPING_MOBILE_SCAN_SALT)


def _bookkeeping_offline_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=BOOKKEEPING_OFFLINE_SALT)


def _is_local_request_host(hostname: str) -> bool:
    return hostname in {'localhost', '127.0.0.1', '0.0.0.0'}


def _is_resolvable_public_base(base_url: str) -> bool:
    hostname = (urlparse(base_url).hostname or '').strip().lower()
    if not hostname or _is_local_request_host(hostname):
        return False
    try:
        socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return True


def _normalize_public_base(base_url: str) -> str:
    candidate = str(base_url or '').strip().rstrip('/')
    if not candidate:
        return ''
    if '://' not in candidate:
        candidate = f'https://{candidate}'
    return candidate.rstrip('/')


def _is_supported_auto_tunnel_host(hostname: str) -> bool:
    normalized = str(hostname or '').strip().lower()
    return (
        normalized.endswith('.trycloudflare.com')
        or normalized.endswith('.ngrok-free.dev')
        or normalized.endswith('.ngrok-free.app')
        or normalized.endswith('.ngrok.app')
    )


def _managed_cloudflared_origin_url() -> str:
    host = (current_app.config.get('APP_HOST') or '127.0.0.1').strip()
    if host in {'0.0.0.0', '::'}:
        host = '127.0.0.1'
    port = current_app.config.get('APP_PORT', 8000)
    return f'http://{host}:{port}'


def _managed_cloudflared_process_running() -> bool:
    return MANAGED_CLOUDFLARED_PROCESS is not None and MANAGED_CLOUDFLARED_PROCESS.poll() is None


def _reset_managed_cloudflared_state() -> None:
    global MANAGED_CLOUDFLARED_PROCESS, MANAGED_CLOUDFLARED_URL, MANAGED_CLOUDFLARED_STARTED_AT
    MANAGED_CLOUDFLARED_PROCESS = None
    MANAGED_CLOUDFLARED_URL = ''
    MANAGED_CLOUDFLARED_STARTED_AT = 0.0


def _capture_managed_cloudflared_output(process: subprocess.Popen, app) -> None:
    global MANAGED_CLOUDFLARED_URL

    if not process.stdout:
        return

    try:
        for line in process.stdout:
            public_bases = _extract_public_bases(line)
            if public_bases and not MANAGED_CLOUDFLARED_URL:
                MANAGED_CLOUDFLARED_URL = public_bases[0]
                app.logger.info('Managed cloudflared tunnel ready at %s', MANAGED_CLOUDFLARED_URL)
    except Exception:
        app.logger.exception('Failed while reading managed cloudflared output.')
    finally:
        with MANAGED_CLOUDFLARED_LOCK:
            if process is MANAGED_CLOUDFLARED_PROCESS and process.poll() is not None:
                _reset_managed_cloudflared_state()


def _ensure_managed_cloudflared_tunnel() -> str:
    global MANAGED_CLOUDFLARED_PROCESS, MANAGED_CLOUDFLARED_URL, MANAGED_CLOUDFLARED_STARTED_AT

    app = current_app._get_current_object()
    cloudflared_binary = shutil.which('cloudflared')
    if not cloudflared_binary:
        return ''

    with MANAGED_CLOUDFLARED_LOCK:
        if _managed_cloudflared_process_running() and MANAGED_CLOUDFLARED_URL:
            return MANAGED_CLOUDFLARED_URL

        if _managed_cloudflared_process_running() and not MANAGED_CLOUDFLARED_URL:
            started_at = MANAGED_CLOUDFLARED_STARTED_AT
        else:
            _reset_managed_cloudflared_state()
            process = subprocess.Popen(
                [
                    cloudflared_binary,
                    'tunnel',
                    '--url',
                    _managed_cloudflared_origin_url(),
                    '--no-autoupdate',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            MANAGED_CLOUDFLARED_PROCESS = process
            MANAGED_CLOUDFLARED_STARTED_AT = time.time()
            started_at = MANAGED_CLOUDFLARED_STARTED_AT
            threading.Thread(target=_capture_managed_cloudflared_output, args=(process, app), daemon=True).start()

    deadline = started_at + 12
    while time.time() < deadline:
        if MANAGED_CLOUDFLARED_URL:
            return MANAGED_CLOUDFLARED_URL
        if not _managed_cloudflared_process_running():
            return ''
        time.sleep(0.25)

    return MANAGED_CLOUDFLARED_URL


def _extract_public_bases(raw_text: str) -> list[str]:
    candidates = []
    for match in re.finditer(r'(https://[a-z0-9][a-z0-9.-]+\.[a-z]{2,})', raw_text or '', re.IGNORECASE):
        base_url = _normalize_public_base(match.group(1))
        hostname = (urlparse(base_url).hostname or '').strip().lower()
        if base_url and _is_supported_auto_tunnel_host(hostname) and base_url not in candidates:
            candidates.append(base_url)
    return candidates


def _discover_ngrok_public_base() -> str:
    try:
        with urlopen('http://127.0.0.1:4040/api/tunnels', timeout=2) as response:
            payload = json.load(response)
        for tunnel in payload.get('tunnels', []):
            public_url = _normalize_public_base(tunnel.get('public_url', ''))
            if public_url.startswith('https://') and _is_resolvable_public_base(public_url):
                return public_url
    except Exception:
        return ''
    return ''


def _discover_cloudflared_public_base() -> str:
    configured_metrics_url = _normalize_public_base(current_app.config.get('CLOUDFLARED_METRICS_URL', ''))
    metrics_urls = []
    if configured_metrics_url:
        metrics_urls.append(configured_metrics_url)

    for port in range(20241, 20246):
        metrics_urls.append(f'http://127.0.0.1:{port}/metrics')
        metrics_urls.append(f'http://localhost:{port}/metrics')

    seen_urls = set()
    for metrics_url in metrics_urls:
        if metrics_url in seen_urls:
            continue
        seen_urls.add(metrics_url)
        try:
            with urlopen(metrics_url, timeout=1.5) as response:
                metrics_text = response.read().decode('utf-8', errors='ignore')
        except Exception:
            continue

        for public_base in _extract_public_bases(metrics_text):
            if _is_resolvable_public_base(public_base):
                return public_base

    return ''


def _discover_live_public_base() -> str:
    for provider in (_discover_ngrok_public_base, _discover_cloudflared_public_base):
        public_base = provider()
        if public_base:
            return public_base
    return ''


def _build_public_route_url(endpoint: str, **values) -> str:
    def finalize_url(base_url: str) -> str:
        full_url = f'{base_url}{url_for(endpoint, **values)}'
        if 'ngrok-free.dev' in base_url or 'ngrok.app' in base_url or 'ngrok-free.app' in base_url:
            separator = '&' if '?' in full_url else '?'
            return f'{full_url}{separator}{urlencode({"ngrok-skip-browser-warning": "1"})}'
        return full_url

    def configured_public_base() -> str:
        return (
            current_app.config.get('PUBLIC_BASE_URL')
            or current_app.config.get('PUBLIC_APP_URL')
            or current_app.config.get('PUBLIC_TUNNEL_URL')
            or current_app.config.get('NGROK_PUBLIC_URL')
            or ''
        ).strip().rstrip('/')

    configured_base = configured_public_base()
    if configured_base and _is_resolvable_public_base(configured_base):
        return finalize_url(configured_base)

    request_base = request.url_root.strip().rstrip('/')
    request_host = (request.host or '').split(':', 1)[0].lower()
    if request_base and not _is_local_request_host(request_host):
        return finalize_url(request_base)

    live_public_base = _discover_live_public_base()
    if live_public_base:
        current_app.config['PUBLIC_TUNNEL_URL'] = live_public_base
        return finalize_url(live_public_base)

    if _is_local_request_host(request_host):
        managed_public_base = _ensure_managed_cloudflared_tunnel()
        if managed_public_base:
            current_app.config['PUBLIC_TUNNEL_URL'] = managed_public_base
            return finalize_url(managed_public_base)
        return ''

    return finalize_url(request_base)


def _build_public_token_route_url(endpoint: str, **values) -> str:
    return _build_public_route_url(endpoint, **values)


def _build_bookkeeping_mobile_scan_url(token: str) -> str:
    return _build_public_token_route_url('main.bookkeeping_mobile_scan', token=token)


def _build_bookkeeping_offline_url(token: str) -> str:
    return _build_public_token_route_url('main.bookkeeping_offline_app', token=token)


def _build_intake_offline_url(token: str) -> str:
    return _build_public_token_route_url('main.intake_offline_app', token=token)


def _render_qr_code_svg(url: str) -> str:
    image = qrcode.make(url, image_factory=SvgPathImage, box_size=8, border=2)
    buffer = BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode('utf-8')


def _bookkeeping_mobile_scan_context(cbo: CBO, token: str | None = None) -> dict:
    issued_by_user_id = None
    try:
        if getattr(current_user, 'is_authenticated', False):
            issued_by_user_id = current_user.id
    except Exception:
        issued_by_user_id = None

    signed_token = token or _bookkeeping_mobile_scan_serializer().dumps({
        'cbo_id': cbo.id,
        'issued_by_user_id': issued_by_user_id,
    })
    preview_url = url_for('main.bookkeeping_mobile_scan', token=signed_token)
    public_url = _build_bookkeeping_mobile_scan_url(signed_token)
    if not public_url:
        return {
            'preview_url': preview_url,
            'url': '',
            'qr_svg': '',
            'expires_in_minutes': BOOKKEEPING_MOBILE_SCAN_MAX_AGE // 60,
            'availability_message': 'Set PUBLIC_BASE_URL to your public HTTPS app URL, or set PUBLIC_TUNNEL_URL if you need a temporary tunnel for local testing.',
        }

    return {
        'preview_url': preview_url,
        'url': public_url,
        'qr_svg': _render_qr_code_svg(public_url),
        'expires_in_minutes': BOOKKEEPING_MOBILE_SCAN_MAX_AGE // 60,
        'availability_message': '',
    }


def _bookkeeping_offline_context(cbo: CBO, token: str | None = None) -> dict:
    signed_token = token or _bookkeeping_offline_serializer().dumps({
        'cbo_id': cbo.id,
    })
    preview_url = url_for('main.bookkeeping_offline_app', token=signed_token)
    public_url = _build_bookkeeping_offline_url(signed_token)
    if not public_url:
        return {
            'token': signed_token,
            'preview_url': preview_url,
            'url': '',
            'qr_svg': '',
            'expires_in_minutes': BOOKKEEPING_OFFLINE_MAX_AGE // 60,
            'availability_message': 'Set PUBLIC_BASE_URL to your public HTTPS app URL, or set PUBLIC_TUNNEL_URL if you need a temporary tunnel for local testing.',
        }

    return {
        'token': signed_token,
        'preview_url': preview_url,
        'url': public_url,
        'qr_svg': _render_qr_code_svg(public_url),
        'expires_in_minutes': BOOKKEEPING_OFFLINE_MAX_AGE // 60,
        'availability_message': '',
    }


def _bookkeeping_mobile_scan_payload(cbo: CBO, token: str, summary: dict | None = None) -> dict:
    summary = summary or _bookkeeping_summary(cbo)
    app_url = url_for('main.bookkeeping_mobile_scan', token=token)
    return {
        'ok': True,
        'generated_at': datetime.utcnow().isoformat(),
        'cbo': {
            'id': cbo.id,
            'name': cbo.name,
            'slug': cbo.slug,
        },
        'summary': _serialize_bookkeeping_summary(summary),
        'sync': {
            'app_url': app_url,
            'submit_url': url_for('main.bookkeeping_mobile_scan_submit', token=token),
            'sw_url': url_for('main.bookkeeping_mobile_scan_service_worker', token=token),
            'max_files': int(current_app.config.get('BOOKKEEPING_MAX_FILES', 5) or 5),
            'max_pdf_pages': int(current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10) or 10),
            'expires_in_minutes': BOOKKEEPING_MOBILE_SCAN_MAX_AGE // 60,
        },
    }


def _load_bookkeeping_mobile_scan_cbo(token: str) -> CBO:
    serializer = _bookkeeping_mobile_scan_serializer()
    try:
        payload = serializer.loads(token, max_age=BOOKKEEPING_MOBILE_SCAN_MAX_AGE)
    except SignatureExpired:
        abort(410, description='This mobile bookkeeping scan link has expired.')
    except BadSignature:
        abort(404)

    cbo_id = payload.get('cbo_id')
    if not cbo_id:
        abort(404)
    return CBO.query.get_or_404(cbo_id)


def _load_bookkeeping_offline_cbo(token: str) -> CBO:
    serializer = _bookkeeping_offline_serializer()
    try:
        payload = serializer.loads(token, max_age=BOOKKEEPING_OFFLINE_MAX_AGE)
    except SignatureExpired:
        abort(410, description='This bookkeeping offline app link has expired.')
    except BadSignature:
        abort(404)

    cbo_id = payload.get('cbo_id')
    if not cbo_id:
        abort(404)
    return CBO.query.get_or_404(cbo_id)


def _intake_offline_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=INTAKE_OFFLINE_SALT)


def _offline_intake_form_id(cbo: CBO) -> str:
    return f'offline-intake-cbo-{cbo.id}'


def _intake_offline_context(cbo: CBO, token: str | None = None) -> dict:
    signed_token = token or _intake_offline_serializer().dumps({
        'cbo_id': cbo.id,
    })
    preview_url = url_for('main.intake_offline_app', token=signed_token)
    public_url = _build_intake_offline_url(signed_token)
    if not public_url:
        return {
            'token': signed_token,
            'preview_url': preview_url,
            'url': '',
            'qr_svg': '',
            'expires_in_minutes': INTAKE_OFFLINE_MAX_AGE // 60,
            'availability_message': 'Set PUBLIC_BASE_URL to your public HTTPS app URL, or set PUBLIC_TUNNEL_URL if you need a temporary tunnel for local testing.',
        }

    return {
        'token': signed_token,
        'preview_url': preview_url,
        'url': public_url,
        'qr_svg': _render_qr_code_svg(public_url),
        'expires_in_minutes': INTAKE_OFFLINE_MAX_AGE // 60,
        'availability_message': '',
    }


def _intake_offline_payload(cbo: CBO, token: str) -> dict:
    return {
        'cbo': {
            'id': cbo.id,
            'name': cbo.name,
        },
        'form': get_intake_form_schema(),
        'sync': {
            'app_url': url_for('main.intake_offline_app', token=token),
            'submit_url': url_for('main.intake_offline_submit', token=token),
            'sw_url': url_for('main.intake_offline_service_worker', token=token),
            'max_bookkeeping_pdf_pages': current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10),
        },
    }


def _load_intake_offline_cbo(token: str) -> CBO:
    serializer = _intake_offline_serializer()
    try:
        payload = serializer.loads(token, max_age=INTAKE_OFFLINE_MAX_AGE)
    except SignatureExpired:
        abort(410, description='This intake app link has expired.')
    except BadSignature:
        abort(404)

    cbo_id = payload.get('cbo_id')
    if not cbo_id:
        abort(404)
    return CBO.query.get_or_404(cbo_id)


def _normalize_offline_intake_value(value) -> str:
    if isinstance(value, list):
        return '; '.join(str(item).strip() for item in value if str(item).strip())
    return str(value or '').strip()


def _intake_response_source_label(raw_response) -> str:
    if not isinstance(raw_response, dict):
        return 'Google Form'
    source = str(raw_response.get('source') or '').strip().lower()
    if source == 'offline_intake':
        return 'Kumbu Intake App'
    return 'Google Form'


def _build_offline_intake_answer_items(form_values: dict) -> list[dict]:
    answers = []
    for field in get_intake_form_schema().get('fields', []):
        title = str(field.get('title') or '').strip()
        if not title:
            continue
        value = _normalize_offline_intake_value(form_values.get(field.get('id')))
        answers.append({
            'question_id': field.get('id', ''),
            'title': title,
            'kind': str(field.get('question_type') or '').lower(),
            'answer_type': 'text',
            'values': [value] if value else [],
        })
    return answers


def _build_offline_intake_submission_payload(cbo: CBO, submission_id: str, form_values: dict, upload_items: list[dict], created_at: str, submitted_at: str) -> dict:
    schema = get_intake_form_schema()
    upload_field_lookup = {
        str(field.get('id') or ''): field
        for field in schema.get('upload_fields', [])
        if str(field.get('id') or '')
    }

    answers = _build_offline_intake_answer_items(form_values)
    file_uploads = []
    sanitized_uploads = []

    for upload_item in upload_items:
        field_id = str(upload_item.get('field_id') or '').strip()
        file_id = str(upload_item.get('file_id') or '').strip()
        if not field_id or not file_id:
            continue
        field = upload_field_lookup.get(field_id) or {}
        title = str(field.get('title') or upload_item.get('title') or field_id).strip()
        file_name = str(upload_item.get('file_name') or '').strip()
        mime_type = str(upload_item.get('mime_type') or '').strip()

        answers.append({
            'question_id': field_id,
            'title': title,
            'kind': 'file_upload',
            'answer_type': 'file_upload',
            'files': [{
                'file_id': file_id,
                'file_name': file_name,
                'mime_type': mime_type,
            }],
        })
        file_uploads.append({
            'question_id': field_id,
            'question_title': title,
            'file_id': file_id,
            'file_name': file_name,
            'mime_type': mime_type,
        })
        sanitized_uploads.append({
            'field_id': field_id,
            'title': title,
            'file_id': file_id,
            'file_name': file_name,
            'mime_type': mime_type,
            'size': int(upload_item.get('size') or 0),
        })

    respondent_email = _normalize_offline_intake_value(form_values.get('email_address'))
    return {
        'form_id': _offline_intake_form_id(cbo),
        'response_id': submission_id,
        'respondent_email': respondent_email,
        'create_time': created_at,
        'submitted_at': submitted_at,
        'answers': answers,
        'file_uploads': file_uploads,
        'raw_response': {
            'source': 'offline_intake',
            'submission_id': submission_id,
            'submitted_at': submitted_at,
            'created_at': created_at,
            'form_values': {
                str(key): _normalize_offline_intake_value(value)
                for key, value in (form_values or {}).items()
            },
            'uploads': sanitized_uploads,
        },
    }


def _process_bookkeeping_uploads(
    cbo: CBO,
    uploads,
    uploaded_by_user_id: int | None,
    combine_related_pages: bool = False,
    upload_batch_id: str | None = None,
    source_channel_override: str | None = None,
    client_submission_id: str | None = None,
    include_in_workspace: bool | None = None,
    document_date_override: str | None = None,
    workspace_period_key: str | None = None,
) -> tuple[int, list[str], dict]:
    files = [uploaded for uploaded in uploads if uploaded and uploaded.filename]
    if not files:
        return 0, ['Choose a document photo or PDF to upload.'], _bookkeeping_summary(cbo)

    max_files = current_app.config.get('BOOKKEEPING_MAX_FILES', 5)
    if len(files) > max_files:
        return 0, [f'Upload up to {max_files} bookkeeping files at a time.'], _bookkeeping_summary(cbo)

    successes = 0
    failures = []
    upload_batch_id = str(upload_batch_id or uuid.uuid4().hex).strip() or uuid.uuid4().hex
    max_pdf_pages = current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10)

    if combine_related_pages:
        for uploaded in files:
            try:
                prepared = prepare_uploaded_document(uploaded, max_pdf_pages=max_pdf_pages)
            except DocumentIngestionError as exc:
                failures.append(str(exc))
                continue

            try:
                _process_bookkeeping_document(
                    cbo=cbo,
                    filename=prepared['source_filename'],
                    mime_type=prepared['source_mime_type'],
                    source_bytes=prepared['source_bytes'],
                    page_images=prepared['pages'],
                    source_channel=source_channel_override or prepared.get('source_channel', 'grouped_upload'),
                    uploaded_by_user_id=uploaded_by_user_id,
                    upload_batch_id=upload_batch_id,
                    client_submission_id=client_submission_id,
                    related_page_upload=True,
                    include_in_workspace=include_in_workspace,
                    document_date_override=document_date_override,
                    workspace_period_key=workspace_period_key,
                )
                successes += 1
            except BookkeepingExtractionError as exc:
                db.session.rollback()
                failures.append(f"{prepared['source_filename']}: {exc}")
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception('Failed to process grouped bookkeeping upload for CBO %s', cbo.id)
                failures.append(f"{prepared['source_filename']}: {exc}")

        if successes:
            _refresh_bookkeeping_audits(cbo)
            summary = _refresh_cbo_operational_profile(cbo, allow_claude=True)
            db.session.commit()
        else:
            summary = _bookkeeping_summary(cbo)
        if successes:
            sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))
        return successes, failures, summary

    for uploaded in files:
        try:
            prepared = prepare_uploaded_document(uploaded, max_pdf_pages=max_pdf_pages)
        except DocumentIngestionError as exc:
            failures.append(str(exc))
            continue

        try:
            _process_bookkeeping_document(
                cbo=cbo,
                filename=prepared['source_filename'],
                mime_type=prepared['source_mime_type'],
                source_bytes=prepared['source_bytes'],
                page_images=prepared['pages'],
                source_channel=source_channel_override or prepared.get('source_channel', 'web_upload'),
                uploaded_by_user_id=uploaded_by_user_id,
                upload_batch_id=upload_batch_id,
                client_submission_id=client_submission_id,
                related_page_upload=False,
                include_in_workspace=include_in_workspace,
                document_date_override=document_date_override,
                workspace_period_key=workspace_period_key,
            )
            successes += 1
        except BookkeepingExtractionError as exc:
            db.session.rollback()
            failures.append(f"{prepared['source_filename']}: {exc}")
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception('Failed to process bookkeeping upload for CBO %s', cbo.id)
            failures.append(f"{prepared['source_filename']}: {exc}")

    if successes:
        _refresh_bookkeeping_audits(cbo)
        summary = _refresh_cbo_operational_profile(cbo, allow_claude=True)
        db.session.commit()
    else:
        summary = _bookkeeping_summary(cbo)
    if successes:
        sync_bookkeeping_summary_to_firestore(cbo, _serialize_bookkeeping_summary(summary))
    return successes, failures, summary


def _community_feedback_summary(cbo: CBO) -> dict:
    subscribers = CommunitySubscriber.query.filter_by(cbo_id=cbo.id, status='active').all()
    completed_feedback = CommunityFeedback.query.filter_by(cbo_id=cbo.id, status='completed').order_by(
        CommunityFeedback.completed_at.desc()
    ).all()

    ratings = [item.rating for item in completed_feedback if item.rating is not None]
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
    recent_quotes = []
    for item in completed_feedback:
        if not item.anecdote:
            continue
        recent_quotes.append({
            'quote': item.anecdote,
            'rating': item.rating,
            'submitted_at': item.completed_at.strftime('%Y-%m-%d') if item.completed_at else '',
        })
        if len(recent_quotes) == 3:
            break

    return {
        'keyword': get_cbo_keyword(cbo),
        'prompt': cbo.community_prompt,
        'subscribers': len(subscribers),
        'responses': len(completed_feedback),
        'avg_rating': avg_rating,
        'recent_quotes': recent_quotes,
        'last_completed_at': completed_feedback[0].completed_at if completed_feedback else None,
        'last_firestore_sync_at': completed_feedback[0].firestore_synced_at if completed_feedback else None,
    }


def _parse_google_timestamp(value: str | None) -> datetime | None:
    raw_value = str(value or '').strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _normalize_google_form_upload_kind(question_title: str) -> str:
    normalized = re.sub(r'[^a-z0-9]+', ' ', str(question_title or '').lower()).strip()
    if normalized == GENERAL_IMAGE_UPLOAD_TITLE.lower() or (
        'general' in normalized and 'cbo' in normalized and 'image' in normalized
    ):
        return 'general_image'
    if normalized == BOOKKEEPING_UPLOAD_TITLE.lower() or 'bookkeeping' in normalized:
        return 'bookkeeping_document'
    return 'other'


def _google_form_upload_extension(filename: str, mime_type: str) -> str:
    extension = os.path.splitext(secure_filename(filename or 'google-form-upload'))[1].lower()
    if extension:
        return extension
    return {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/heic': '.heic',
        'image/heif': '.heif',
        'application/pdf': '.pdf',
    }.get((mime_type or '').lower(), '.bin')


def _store_google_form_upload_file(cbo: CBO, filename: str, mime_type: str, file_bytes: bytes) -> dict:
    return store_supporting_file(
        cbo_id=cbo.id,
        filename=filename or f'google-form-upload{_google_form_upload_extension(filename, mime_type)}',
        file_bytes=file_bytes,
        mime_type=mime_type,
        object_prefix='google_form_uploads',
        local_upload_dir=current_app.config.get('GOOGLE_FORM_UPLOAD_DIR'),
    )


def _google_form_upload_absolute_path(stored_path: str) -> str:
    if not stored_path:
        return ''
    if os.path.isabs(stored_path):
        return stored_path
    return os.path.join(current_app.root_path, stored_path)


def _normalize_form_answer_title(value: str) -> str:
    normalized = re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower())
    return re.sub(r'\s+', ' ', normalized).strip()


def _google_form_answer_lookup(answer_items: list[dict]) -> dict[str, dict]:
    lookup = {}
    for answer in answer_items or []:
        if not isinstance(answer, dict) or answer.get('answer_type') != 'text':
            continue
        title = str(answer.get('title') or '').strip()
        values = [str(value).strip() for value in (answer.get('values') or []) if str(value).strip()]
        if not title or not values:
            continue
        lookup[_normalize_form_answer_title(title)] = {
            'title': title,
            'values': values,
            'value': '; '.join(values),
        }
    return lookup


def _google_form_answer_value(answer_lookup: dict[str, dict], *titles: str) -> str:
    for title in titles:
        answer = answer_lookup.get(_normalize_form_answer_title(title)) or {}
        value = str(answer.get('value') or '').strip()
        if value:
            return value
    return ''


def _split_intake_list_value(value: str) -> list[str]:
    normalized = str(value or '').replace('\r', '\n')
    for separator in [';', '|', '•']:
        normalized = normalized.replace(separator, '\n')

    items = []
    seen = set()
    for chunk in normalized.split('\n'):
        for piece in chunk.split(','):
            cleaned = re.sub(r'\s+', ' ', piece).strip(' -:\t')
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(cleaned)
    return items


def _titleize_workspace_label(value: str) -> str:
    cleaned = re.sub(r'\s+', ' ', str(value or '').strip(' :-\t'))
    if not cleaned:
        return ''
    tokens = []
    for token in cleaned.split(' '):
        token = token.strip()
        if not token:
            continue

        lowered = token.lower()
        if lowered in {'cbo', 'sms', 'id', 'qty', 'kes'}:
            tokens.append(lowered.upper())
            continue
        if token.isupper() or any(character.isupper() for character in token[1:]):
            tokens.append(token)
            continue
        tokens.append(token.capitalize())

    return ' '.join(tokens)


def _append_unique_workspace_column(columns: list[str], label: str) -> None:
    normalized = _titleize_workspace_label(label)
    if not normalized:
        return
    existing_keys = {column.lower() for column in columns}
    if normalized.lower() in existing_keys:
        return
    columns.append(normalized)


def _normalize_cbo_slug(name: str) -> str:
    normalized = re.sub(r'[^a-z0-9]+', '-', str(name or '').strip().lower()).strip('-')
    return normalized or 'cbo'


def _unique_cbo_slug(name: str, current_cbo_id: int | None = None) -> str:
    base_slug = _normalize_cbo_slug(name)
    candidate = base_slug
    suffix = 2
    while True:
        existing = CBO.query.filter_by(slug=candidate).first()
        if existing is None or (current_cbo_id is not None and existing.id == current_cbo_id):
            return candidate
        candidate = f'{base_slug}-{suffix}'
        suffix += 1


def _normalize_workspace_document_column(label: str) -> str:
    raw = re.sub(r'\s+', ' ', str(label or '').strip()).upper()
    if not raw:
        return ''

    direct_map = {
        'SN': 'S/N',
        'S/N': 'S/N',
        'MF': 'M/F',
        'M F': 'M/F',
        'NAME /TOOL IID': 'NAME/TOOL ID',
        'NAME / TOOL IID': 'NAME/TOOL ID',
        'NAME /TOOL ID': 'NAME/TOOL ID',
        'NAME/TOOL IID': 'NAME/TOOL ID',
        'NAME / TOOL ID': 'NAME/TOOL ID',
        'AMT PAD': 'AMT PAID',
        'TOTAL AMY DUE': 'TOTAL AMT DUE',
    }
    if raw in direct_map:
        return direct_map[raw]

    if 'FARMER NAME' in raw:
        return 'FARMER NAME'
    if raw.endswith(' MF') or raw.endswith(' M/F') or ' M/F' in raw:
        return 'M/F'
    if 'NO ID' in raw or raw.endswith(' ID NO'):
        return 'NO ID'
    if 'PHONE' in raw:
        return 'NO PHONE'
    if 'TOOL' in raw and ('NAME' in raw or 'ID' in raw):
        return 'NAME/TOOL ID'
    if 'RENTAL END' in raw and 'DATE' in raw:
        return 'RENTAL END DATE'
    if raw.startswith('SIGN') and '2' in raw:
        return 'SIGN 2'
    if raw.startswith('SIGN'):
        return 'SIGN'
    if 'DAYS' in raw:
        return 'DAYS'
    if 'RENTAL DATE' in raw:
        return 'RENTAL DATE'
    if 'RENTAL FEE' in raw:
        return 'RENTAL FEE'
    if 'PREVIOUS' in raw and 'BAL' in raw:
        return 'PREVIOUS OUTSTANDING BAL'
    if 'TOTAL' in raw and 'DUE' in raw and ('AMT' in raw or 'AMY' in raw):
        return 'TOTAL AMT DUE'
    if ('AMT' in raw or raw.startswith('AMT')) and ('PAID' in raw or 'PAD' in raw):
        return 'AMT PAID'
    if 'PAYMENT MODE' in raw:
        return 'PAYMENT MODE'

    return _titleize_workspace_label(raw)


def _provisioned_google_form_cbo_name(cbo: CBO, answer_lookup: dict[str, dict], response_record: GoogleFormResponse) -> str:
    submitted_name = _google_form_answer_value(answer_lookup, 'CBO Name')
    if submitted_name:
        return submitted_name

    submitter_name = _google_form_answer_value(answer_lookup, 'Your full name')
    if submitter_name:
        return f'{submitter_name} CBO'

    email = str(response_record.respondent_email or '').strip().lower()
    if '@' in email:
        local_part = re.sub(r'[-_.]+', ' ', email.split('@', 1)[0]).strip()
        if local_part:
            return f'{local_part.title()} CBO'

    return f'{cbo.name} Intake Submission'


def _build_google_form_target_cbo(source_cbo: CBO, cbo_name: str) -> CBO:
    return CBO(
        name=cbo_name,
        slug=_unique_cbo_slug(cbo_name),
        cbo_identifier=source_cbo.cbo_identifier or 'community',
        community_prompt=source_cbo.community_prompt or '',
        community_feedback_enabled=bool(source_cbo.community_feedback_enabled),
        org_type=source_cbo.org_type or 'Community-Based Organisation (CBO)',
        focus_areas=source_cbo.focus_areas or '',
        classifications_json=json.dumps([source_cbo.cbo_identifier or 'community']),
    )


def _ensure_google_form_target_cbo(source_cbo: CBO, response_record: GoogleFormResponse, answer_lookup: dict[str, dict]) -> CBO:
    target_cbo = None

    if response_record.provisioned_cbo_id:
        existing_cbo = db.session.get(CBO, response_record.provisioned_cbo_id)
        if existing_cbo is not None and existing_cbo.id != source_cbo.id:
            target_cbo = existing_cbo

    if target_cbo is None and response_record.provisioned_user and response_record.provisioned_user.cbo_id:
        existing_cbo = response_record.provisioned_user.cbo
        if existing_cbo is not None and existing_cbo.id != source_cbo.id:
            target_cbo = existing_cbo

    if target_cbo is None:
        submitted_email = _google_form_answer_value(answer_lookup, 'Email address') or response_record.respondent_email
        email = str(submitted_email or '').strip().lower()
        existing_user = User.query.filter_by(email=email).first() if email else None
        if existing_user and existing_user.has_role('cbo') and existing_user.cbo_id and existing_user.cbo_id != source_cbo.id:
            target_cbo = existing_user.cbo

    if target_cbo is None:
        target_cbo = _build_google_form_target_cbo(
            source_cbo,
            _provisioned_google_form_cbo_name(source_cbo, answer_lookup, response_record),
        )
        db.session.add(target_cbo)
        db.session.flush()

    response_record.provisioned_cbo_id = target_cbo.id
    return target_cbo


def _format_kes_amount(value) -> str:
    raw_text = str(value or '').strip()
    if not raw_text:
        return ''
    amount = _parse_money_value(raw_text)
    if amount == 0.0 and raw_text not in {'0', '0.0', '0.00'}:
        return raw_text
    if abs(amount - round(amount)) < 0.01:
        return f'KSh {amount:,.0f}'
    return f'KSh {amount:,.2f}'


def _merge_google_form_profile(cbo: CBO, answer_lookup: dict[str, dict], response_record: GoogleFormResponse) -> dict:
    profile = _safe_json(cbo.ai_profile_json)
    leadership = dict(profile.get('leadership') or {})
    financial_data = dict(profile.get('financial_data') or {})
    flagship_project = dict(profile.get('flagship_project') or {})
    success_story = dict(profile.get('success_story') or {})

    submitter_name = _google_form_answer_value(answer_lookup, 'Your full name')
    submitter_role = _google_form_answer_value(answer_lookup, 'Your position / role in the CBO')
    cbo_name = _google_form_answer_value(answer_lookup, 'CBO Name') or cbo.name
    founded_year = _google_form_answer_value(answer_lookup, 'Year Incorporated')
    office_address = _google_form_answer_value(answer_lookup, 'CBO Office Address')
    program_locations = _google_form_answer_value(answer_lookup, 'CBO Program Locations')
    whatsapp_number = _google_form_answer_value(answer_lookup, 'WhatsApp phone number')
    email_address = _google_form_answer_value(answer_lookup, 'Email address') or response_record.respondent_email
    bank_name = _google_form_answer_value(answer_lookup, 'Financial Institution (Bank) Name')
    bank_contact = _google_form_answer_value(answer_lookup, 'Financial Institution (Bank) Contact Information')
    dedicated_bank_account = _google_form_answer_value(
        answer_lookup,
        'Do you have a registered bank account dedicated to CBO programs and activity?',
    )
    full_budget = _google_form_answer_value(answer_lookup, 'Full CBO budget (past year)')
    total_expenses = _google_form_answer_value(answer_lookup, 'Total CBO expenses (past year)')
    expense_distribution = _google_form_answer_value(answer_lookup, 'Describe how expenses were distributed')
    debt_liabilities = _google_form_answer_value(answer_lookup, 'CBO debt / liabilities')
    cash_reserves = _google_form_answer_value(answer_lookup, 'Full CBO cash reserves')
    grants = _google_form_answer_value(
        answer_lookup,
        'Describe past and present grants obtained (donor, amount, dates, purpose)',
    )
    milestones = _google_form_answer_value(answer_lookup, 'Describe any milestones achieved in the past three years')
    anecdotal_story = _google_form_answer_value(answer_lookup, ANECDOTAL_STORY_TITLE)
    references = _google_form_answer_value(
        answer_lookup,
        'Please list three references: name, contact information (WhatsApp / email), and relationship with the CBO',
    )
    additional_tracking_fields = _split_intake_list_value(
        _google_form_answer_value(answer_lookup, ADDITIONAL_TRACKING_FIELDS_TITLE)
    )

    location = program_locations or office_address or profile.get('location') or cbo.location
    tagline = str(profile.get('tagline') or '').strip()
    if anecdotal_story:
        tagline = re.split(r'(?<=[.!?])\s+', anecdotal_story.strip())[0][:180]
    elif not tagline and location:
        tagline = f'Community-based organisation serving {location}.'

    if submitter_name:
        role_lower = submitter_role.lower()
        if any(keyword in role_lower for keyword in ['chair', 'chairperson', 'founder']):
            leadership['chairperson'] = submitter_name
        elif any(keyword in role_lower for keyword in ['finance', 'treasurer', 'account']):
            leadership['finance_lead'] = submitter_name
        else:
            leadership['program_director'] = submitter_name

    if full_budget:
        financial_data['total_revenue'] = _format_kes_amount(full_budget)
    if debt_liabilities:
        financial_data['avg_rental_fee'] = _format_kes_amount(debt_liabilities) or debt_liabilities
    if total_expenses:
        financial_data['maintenance_costs'] = _format_kes_amount(total_expenses)
    if cash_reserves:
        financial_data['damage_fees_collected'] = _format_kes_amount(cash_reserves)

    operational_model_notes = [value for value in [dedicated_bank_account, expense_distribution, grants] if value]
    if operational_model_notes:
        financial_data['operational_model'] = ' | '.join(operational_model_notes)

    if milestones:
        flagship_project = {
            'title': 'Recent Milestones',
            'summary': milestones,
            'stats': flagship_project.get('stats', []),
        }

    if anecdotal_story:
        success_story = {
            'title': 'Community Story',
            'summary': anecdotal_story,
        }

    intake_uploads = []
    for upload in response_record.uploads:
        intake_uploads.append({
            'id': upload.id,
            'filename': upload.original_filename,
            'kind': upload.upload_kind,
            'sync_status': upload.sync_status,
            'processed_at': upload.processed_at.isoformat() if upload.processed_at else '',
        })

    profile.update({
        'name': cbo_name,
        'tagline': tagline,
        'address': office_address or profile.get('address', ''),
        'location': location,
        'founded_year': founded_year or profile.get('founded_year', ''),
        'org_type': profile.get('org_type') or cbo.org_type or 'Community-Based Organisation (CBO)',
        'focus_areas': profile.get('focus_areas') or 'Community development, local service delivery',
        'leadership': leadership,
        'financial_data': financial_data,
        'flagship_project': flagship_project,
        'success_story': success_story,
        'join_us': profile.get('join_us') or 'Contact the organisation to collaborate, support programs, or request more information.',
        'classifications': profile.get('classifications') or [cbo.cbo_identifier or 'community'],
        'intake_summary': {
            'submitter_name': submitter_name,
            'submitter_role': submitter_role,
            'whatsapp_number': whatsapp_number,
            'email_address': email_address,
            'program_locations': program_locations,
            'bank_name': bank_name,
            'bank_contact': bank_contact,
            'dedicated_bank_account': dedicated_bank_account,
            'full_budget': _format_kes_amount(full_budget),
            'total_expenses': _format_kes_amount(total_expenses),
            'expense_distribution': expense_distribution,
            'debt_liabilities': _format_kes_amount(debt_liabilities) or debt_liabilities,
            'cash_reserves': _format_kes_amount(cash_reserves),
            'grants': grants,
            'milestones': milestones,
            'references': references,
            'additional_tracking_fields': additional_tracking_fields,
        },
        'intake_uploads': intake_uploads,
    })
    return profile


def _generate_bookkeeping_workspace_template(cbo: CBO, answer_lookup: dict[str, dict]) -> dict:
    existing_template = _safe_json(cbo.bookkeeping_template_json)
    columns = []
    document_types = []
    primary_document_type = str(existing_template.get('primary_document_type') or '').strip()
    layout_candidates: dict[tuple[str, ...], dict] = {}

    for document in BookkeepingDocument.query.filter_by(cbo_id=cbo.id).order_by(BookkeepingDocument.created_at.asc()).all():
        document_type = (document.document_type or 'unknown').replace('_', ' ').title()
        if document_type not in document_types:
            document_types.append(document_type)

        extracted = _safe_json(document.extracted_data_json)
        normalized_columns = []
        for column in extracted.get('detected_columns', []) if isinstance(extracted, dict) else []:
            _append_unique_workspace_column(normalized_columns, _normalize_workspace_document_column(column))
        if not normalized_columns:
            continue

        key = tuple(normalized_columns)
        candidate = layout_candidates.get(key)
        if candidate is None:
            candidate = {
                'count': 0,
                'columns': normalized_columns,
                'document_type': document_type,
                'last_seen': datetime.min,
            }
            layout_candidates[key] = candidate
        candidate['count'] += 1
        candidate['last_seen'] = max(candidate['last_seen'], document.processed_at or document.created_at or datetime.min)
        candidate['document_type'] = document_type

    if layout_candidates:
        primary_layout = max(
            layout_candidates.values(),
            key=lambda item: (item['count'], len(item['columns']), item['last_seen']),
        )
        primary_document_type = str(primary_layout.get('document_type') or '').strip()
        for column in primary_layout.get('columns') or []:
            _append_unique_workspace_column(columns, column)
    else:
        for existing_column in existing_template.get('columns') or []:
            _append_unique_workspace_column(columns, existing_column)

    if not columns:
        for default_column in ['Date', 'Description', 'Category', 'Amount']:
            _append_unique_workspace_column(columns, default_column)

    for default_column in ['Date', 'Description', 'Amount', 'Notes']:
        _append_unique_workspace_column(columns, default_column)

    custom_fields = []
    for existing_field in existing_template.get('custom_fields') or []:
        normalized = _titleize_workspace_label(existing_field)
        if normalized and normalized not in custom_fields:
            custom_fields.append(normalized)
    for custom_field in _split_intake_list_value(_google_form_answer_value(answer_lookup, ADDITIONAL_TRACKING_FIELDS_TITLE)):
        normalized = _titleize_workspace_label(custom_field)
        if normalized and normalized not in custom_fields:
            custom_fields.append(normalized)

    for custom_field in custom_fields:
        _append_unique_workspace_column(columns, custom_field)

    return {
        'columns': columns,
        'custom_fields': custom_fields,
        'source_document_types': document_types,
        'primary_document_type': primary_document_type,
        'worksheets': _normalize_workspace_template_worksheets(existing_template.get('worksheets') or []),
        'generated_at': datetime.utcnow().isoformat(),
        'source': 'google_form_sync',
    }


def _upsert_google_form_user(cbo: CBO, email: str, display_name: str, source_cbo_id: int | None = None) -> tuple[User, bool]:
    user = User.query.filter_by(email=email).first()
    created = False

    if user and user.has_role('cbo') and user.cbo_id and user.cbo_id != cbo.id:
        if source_cbo_id and user.cbo_id == source_cbo_id:
            user.cbo_id = cbo.id
        else:
            raise RuntimeError('That email is already linked to a different CBO account.')

    if user is None:
        user = User(
            email=email,
            role='cbo',
            account_status='pending_claim',
            display_name=display_name or cbo.name,
            cbo_id=cbo.id,
        )
        user.set_password(uuid.uuid4().hex, temporary=True)
        db.session.add(user)
        db.session.flush()
        created = True
        return user, created

    if user.has_role('funder') and not user.has_role('cbo'):
        user.role = User.ROLE_FUNDER_CBO
        user.cbo_id = cbo.id
        if display_name and not user.display_name:
            user.display_name = display_name
        db.session.add(user)
        db.session.flush()
        return user, created

    if not user.has_role('cbo'):
        raise RuntimeError('That email already belongs to a non-CBO account in Kumbu Connect.')

    if not user.cbo_id:
        user.cbo_id = cbo.id
    if user.account_status != 'active':
        user.account_status = 'pending_claim'
        user.password_is_temporary = True
    if display_name and (not user.display_name or user.account_status != 'active'):
        user.display_name = display_name
    db.session.add(user)
    db.session.flush()
    return user, created


def _provision_google_form_response(source_cbo: CBO, response_record: GoogleFormResponse) -> dict:
    answer_lookup = _google_form_answer_lookup(_safe_json_list(response_record.answers_json))
    submitted_email = _google_form_answer_value(answer_lookup, 'Email address') or response_record.respondent_email
    email = str(submitted_email or '').strip().lower()
    if not email:
        raise RuntimeError('No email address was submitted, so a pre-built CBO account could not be created.')

    target_cbo = _ensure_google_form_target_cbo(source_cbo, response_record, answer_lookup)
    display_name = _google_form_answer_value(answer_lookup, 'Your full name') or target_cbo.name
    cbo_name = _google_form_answer_value(answer_lookup, 'CBO Name') or target_cbo.name
    if cbo_name:
        target_cbo.name = cbo_name
        if not (target_cbo.slug or '').strip():
            target_cbo.slug = _unique_cbo_slug(cbo_name, current_cbo_id=target_cbo.id)

    profile = _merge_google_form_profile(target_cbo, answer_lookup, response_record)
    _refresh_cbo_operational_profile(
        target_cbo,
        seed_profile=profile,
        response_record=response_record,
        answer_lookup=answer_lookup,
        allow_claude=True,
    )

    target_cbo.bookkeeping_template_json = json.dumps(
        _generate_bookkeeping_workspace_template(target_cbo, answer_lookup),
        default=str,
    )

    user, created = _upsert_google_form_user(target_cbo, email, display_name, source_cbo_id=source_cbo.id)
    response_record.provisioned_user_id = user.id
    response_record.provisioned_cbo_id = target_cbo.id
    response_record.provisioning_status = 'provisioned'
    response_record.provisioning_error = ''
    response_record.provisioned_at = datetime.utcnow()

    db.session.add(target_cbo)
    db.session.add(response_record)
    db.session.commit()
    return {
        'user_id': user.id,
        'cbo_id': target_cbo.id,
        'cbo_slug': target_cbo.slug,
        'created_user': created,
        'email': email,
    }


def _merge_intake_sync_results(result: dict, item_result: dict) -> None:
    for key in (
        'responses_synced',
        'new_responses',
        'new_uploads',
        'processed_general_images',
        'processed_bookkeeping_documents',
        'provisioned_accounts',
    ):
        result[key] += int(item_result.get(key) or 0)
    result['failures'].extend(item_result.get('failures') or [])


def _read_cached_intake_upload_bytes(file_payload: dict, inline_uploads: dict[str, dict]) -> dict:
    file_id = str(file_payload.get('file_id') or '').strip()
    inline_upload = inline_uploads.get(file_id)
    if inline_upload is not None:
        return {
            'file_id': file_id,
            'file_name': inline_upload.get('file_name') or file_payload.get('file_name') or '',
            'mime_type': inline_upload.get('mime_type') or file_payload.get('mime_type') or '',
            'web_view_link': inline_upload.get('web_view_link', ''),
            'bytes': inline_upload.get('bytes') or b'',
        }
    return download_drive_file(file_id)


def _ingest_intake_response_payload(cbo: CBO, response_payload: dict, inline_uploads: dict[str, dict] | None = None) -> dict:
    inline_uploads = inline_uploads or {}
    result = {
        'responses_synced': 0,
        'new_responses': 0,
        'new_uploads': 0,
        'processed_general_images': 0,
        'processed_bookkeeping_documents': 0,
        'provisioned_accounts': 0,
        'failures': [],
        'duplicate_response': False,
    }

    response_id = str(response_payload.get('response_id') or '').strip()
    if not response_id:
        raise RuntimeError('Intake submission is missing a response id.')

    form_id = str(response_payload.get('form_id') or cbo.intake_form_id or _offline_intake_form_id(cbo)).strip()
    response_record = GoogleFormResponse.query.filter_by(
        form_id=form_id,
        response_id=response_id,
    ).first()
    answer_lookup = _google_form_answer_lookup(response_payload.get('answers') or [])
    is_new_response = response_record is None
    if response_record is None:
        response_record = GoogleFormResponse(
            cbo_id=cbo.id,
            form_id=form_id,
            response_id=response_id,
        )
    else:
        result['duplicate_response'] = True

    response_record.cbo_id = cbo.id
    response_record.form_id = form_id
    response_record.response_id = response_id
    response_record.respondent_email = (
        _google_form_answer_value(answer_lookup, 'Email address')
        or response_payload.get('respondent_email', '')
    )
    response_record.response_created_at = _parse_google_timestamp(response_payload.get('create_time'))
    response_record.response_submitted_at = _parse_google_timestamp(response_payload.get('submitted_at'))
    response_record.answers_json = json.dumps(response_payload.get('answers') or [])
    response_record.raw_response_json = json.dumps(response_payload.get('raw_response') or {})
    response_record.sync_status = 'synced'
    response_record.sync_error = ''
    response_record.synced_at = datetime.utcnow()
    db.session.add(response_record)
    db.session.commit()

    result['responses_synced'] += 1
    if is_new_response:
        result['new_responses'] += 1

    try:
        target_cbo = _ensure_google_form_target_cbo(cbo, response_record, answer_lookup)
        response_record.cbo_id = target_cbo.id
        db.session.add(target_cbo)
        db.session.add(response_record)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            'Failed to prepare target CBO for intake response %s owned by CBO %s',
            response_record.id,
            cbo.id,
        )
        response_record = db.session.get(GoogleFormResponse, response_record.id)
        if response_record:
            response_record.provisioning_status = 'failed'
            response_record.provisioning_error = f'Could not create a separate CBO workspace: {exc}'
            response_record.provisioned_at = datetime.utcnow()
            db.session.add(response_record)
            db.session.commit()
        result['failures'].append(
            f"Preparing a separate CBO profile for {response_payload.get('respondent_email') or response_payload.get('response_id')}: {exc}"
        )
        return result

    bookkeeping_changed_cbo_ids = set()
    for file_payload in response_payload.get('file_uploads') or []:
        drive_file_id = str(file_payload.get('file_id') or '').strip()
        if not drive_file_id:
            continue

        upload = GoogleFormUpload.query.filter_by(drive_file_id=drive_file_id).first()
        is_new_upload = upload is None
        if upload is None:
            upload = GoogleFormUpload(
                cbo_id=target_cbo.id,
                google_form_response_id=response_record.id,
                drive_file_id=drive_file_id,
            )

        upload.cbo_id = target_cbo.id
        upload.google_form_response_id = response_record.id
        upload.question_id = file_payload.get('question_id', '')
        upload.question_title = file_payload.get('question_title', '')
        upload.upload_kind = _normalize_google_form_upload_kind(upload.question_title)
        if upload.bookkeeping_document_id:
            upload.upload_kind = 'bookkeeping_document'
        upload.original_filename = file_payload.get('file_name', '')
        upload.mime_type = file_payload.get('mime_type', '') or _guess_mime_type(upload.original_filename)
        upload.sync_status = upload.sync_status or 'pending'
        db.session.add(upload)
        db.session.commit()

        if is_new_upload:
            result['new_uploads'] += 1

        needs_bookkeeping_processing = upload.upload_kind == 'bookkeeping_document' and not upload.bookkeeping_document_id
        needs_general_image_cache = upload.upload_kind == 'general_image' and not upload.stored_path
        if not needs_bookkeeping_processing and not needs_general_image_cache:
            continue

        try:
            downloaded = _read_cached_intake_upload_bytes(file_payload, inline_uploads)
        except Exception as exc:
            db.session.rollback()
            upload = GoogleFormUpload.query.get(upload.id)
            if upload:
                upload.sync_status = 'failed'
                upload.processing_error = str(exc)
                upload.processed_at = datetime.utcnow()
                db.session.add(upload)
                db.session.commit()
            result['failures'].append(f"{upload.original_filename or upload.drive_file_id}: {exc}")
            continue

        upload.drive_file_url = upload.drive_file_url or downloaded.get('web_view_link', '')

        if needs_bookkeeping_processing:
            try:
                prepared = prepare_document_bytes(
                    downloaded.get('file_name') or upload.original_filename or 'bookkeeping-upload',
                    downloaded.get('mime_type') or upload.mime_type,
                    downloaded.get('bytes') or b'',
                    max_pdf_pages=current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10),
                )
                document = _process_bookkeeping_document(
                    cbo=target_cbo,
                    filename=prepared['source_filename'],
                    mime_type=prepared['source_mime_type'],
                    source_bytes=prepared['source_bytes'],
                    page_images=prepared['pages'],
                    source_channel='offline_intake_upload' if inline_uploads else 'google_form_upload',
                    uploaded_by_user_id=None,
                    upload_batch_id=response_record.response_id,
                    related_page_upload=False,
                )
                upload.bookkeeping_document_id = document.id
                upload.sync_status = 'processed'
                upload.processing_error = ''
                upload.processed_at = datetime.utcnow()
                db.session.add(upload)
                db.session.commit()
                bookkeeping_changed_cbo_ids.add(target_cbo.id)
                result['processed_bookkeeping_documents'] += 1
            except (DocumentIngestionError, BookkeepingExtractionError, RuntimeError, ValueError) as exc:
                db.session.rollback()
                upload = GoogleFormUpload.query.get(upload.id)
                if upload:
                    upload.sync_status = 'failed'
                    upload.processing_error = str(exc)
                    upload.processed_at = datetime.utcnow()
                    db.session.add(upload)
                    db.session.commit()
                result['failures'].append(f"{upload.original_filename or upload.drive_file_id}: {exc}")
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception('Failed to process intake bookkeeping upload %s', upload.drive_file_id)
                upload = GoogleFormUpload.query.get(upload.id)
                if upload:
                    upload.sync_status = 'failed'
                    upload.processing_error = str(exc)
                    upload.processed_at = datetime.utcnow()
                    db.session.add(upload)
                    db.session.commit()
                result['failures'].append(f"{upload.original_filename or upload.drive_file_id}: {exc}")
            continue

        if needs_general_image_cache:
            try:
                stored = _store_google_form_upload_file(
                    target_cbo,
                    downloaded.get('file_name') or upload.original_filename or 'intake-image',
                    downloaded.get('mime_type') or upload.mime_type,
                    downloaded.get('bytes') or b'',
                )
                upload.stored_path = stored['stored_path']
                upload.storage_backend = stored['storage_backend']
                upload.sync_status = 'processed'
                upload.processing_error = ''
                upload.processed_at = datetime.utcnow()
                db.session.add(upload)
                db.session.commit()
                result['processed_general_images'] += 1
            except RuntimeError as exc:
                db.session.rollback()
                upload = GoogleFormUpload.query.get(upload.id)
                if upload:
                    upload.sync_status = 'failed'
                    upload.processing_error = str(exc)
                    upload.processed_at = datetime.utcnow()
                    db.session.add(upload)
                    db.session.commit()
                result['failures'].append(f"{upload.original_filename or upload.drive_file_id}: {exc}")
            except Exception as exc:
                db.session.rollback()
                current_app.logger.exception('Failed to cache intake general image upload %s', upload.drive_file_id)
                upload = GoogleFormUpload.query.get(upload.id)
                if upload:
                    upload.sync_status = 'failed'
                    upload.processing_error = str(exc)
                    upload.processed_at = datetime.utcnow()
                    db.session.add(upload)
                    db.session.commit()
                result['failures'].append(f"{upload.original_filename or upload.drive_file_id}: {exc}")

    was_provisioned = bool(response_record.provisioned_user_id) and response_record.provisioning_status == 'provisioned'
    try:
        provision_result = _provision_google_form_response(cbo, response_record)
        if provision_result.get('user_id') and not was_provisioned:
            result['provisioned_accounts'] += 1
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            'Failed to provision intake response %s for CBO %s',
            response_record.id,
            cbo.id,
        )
        response_record = GoogleFormResponse.query.get(response_record.id)
        if response_record:
            response_record.provisioning_status = 'failed'
            response_record.provisioning_error = str(exc)
            response_record.provisioned_at = datetime.utcnow()
            db.session.add(response_record)
            db.session.commit()
        result['failures'].append(
            f"Provisioning {response_payload.get('respondent_email') or response_payload.get('response_id')}: {exc}"
        )

    for changed_cbo_id in bookkeeping_changed_cbo_ids:
        changed_cbo = db.session.get(CBO, changed_cbo_id)
        if changed_cbo is None:
            continue
        _refresh_bookkeeping_audits(changed_cbo)
        changed_summary = _refresh_cbo_operational_profile(
            changed_cbo,
            response_record=response_record if changed_cbo.id == target_cbo.id else None,
            answer_lookup=answer_lookup if changed_cbo.id == target_cbo.id else None,
            allow_claude=True,
        )
        db.session.commit()
        sync_bookkeeping_summary_to_firestore(changed_cbo, _serialize_bookkeeping_summary(changed_summary))

    return result


def _sync_google_form_responses(cbo: CBO) -> dict:
    if not cbo.intake_form_id:
        raise RuntimeError('No intake form exists for this CBO yet.')

    ensure_intake_form_upload_guidance(cbo.intake_form_id)
    bundle = get_form_response_bundle(cbo.intake_form_id)
    result = {
        'responses_synced': 0,
        'new_responses': 0,
        'new_uploads': 0,
        'processed_general_images': 0,
        'processed_bookkeeping_documents': 0,
        'provisioned_accounts': 0,
        'failures': [],
    }
    for response_payload in bundle.get('responses') or []:
        item_result = _ingest_intake_response_payload(cbo, response_payload)
        _merge_intake_sync_results(result, item_result)

    return result


def _google_form_response_summary(cbo: CBO) -> dict:
    responses = GoogleFormResponse.query.filter_by(cbo_id=cbo.id).all()
    responses.sort(
        key=lambda item: item.response_submitted_at or item.response_created_at or item.created_at or datetime.min,
        reverse=True,
    )

    cards = []
    last_synced_at = None
    general_image_count = 0
    bookkeeping_upload_count = 0
    digitized_bookkeeping_count = 0
    provisioned_account_count = 0

    for response in responses:
        answers = _safe_json_list(response.answers_json)
        raw_response = _safe_json(response.raw_response_json)
        text_answers = []
        submitter_name = ''

        for answer in answers:
            if not isinstance(answer, dict) or answer.get('answer_type') != 'text':
                continue
            values = [str(value).strip() for value in (answer.get('values') or []) if str(value).strip()]
            if not values:
                continue
            display_value = '; '.join(values)
            title = str(answer.get('title') or '').strip()
            if title == 'Your full name' and not submitter_name:
                submitter_name = display_value
            text_answers.append({
                'title': title,
                'value': display_value,
            })

        general_uploads = []
        bookkeeping_uploads = []
        other_uploads = []

        uploads = sorted(
            response.uploads,
            key=lambda item: item.created_at or datetime.min,
        )

        for upload in uploads:
            file_url = ''
            can_preview = False
            if upload.stored_path or upload.bookkeeping_document_id:
                file_url = url_for('main.google_form_upload_file', upload_id=upload.id)
                can_preview = (
                    (upload.mime_type or '').startswith('image/')
                    or bool(upload.bookkeeping_document and (upload.bookkeeping_document.mime_type or '').startswith('image/'))
                )

            bookkeeping_card = None
            if upload.bookkeeping_document:
                extracted = _safe_json(upload.bookkeeping_document.extracted_data_json)
                entries = extracted.get('bookkeeping_entries', []) if isinstance(extracted, dict) else []
                audit = extracted.get('audit', {}) if isinstance(extracted, dict) else {}
                bookkeeping_card = {
                    'document_id': upload.bookkeeping_document.id,
                    'document_type': (upload.bookkeeping_document.document_type or 'unknown').replace('_', ' ').title(),
                    'document_date': upload.bookkeeping_document.document_date or '',
                    'summary_text': upload.bookkeeping_document.summary_text or '',
                    'income_total': float(upload.bookkeeping_document.total_income or 0.0),
                    'expense_total': float(upload.bookkeeping_document.total_expenses or 0.0),
                    'net_total': float(upload.bookkeeping_document.net_amount or 0.0),
                    'entry_count': len(entries),
                    'audit_issue_count': len(audit.get('issues', []) if isinstance(audit, dict) else []),
                }
                digitized_bookkeeping_count += 1

            upload_card = {
                'id': upload.id,
                'question_title': upload.question_title,
                'original_filename': upload.original_filename,
                'mime_type': upload.mime_type,
                'file_url': file_url,
                'can_preview': can_preview,
                'sync_status': upload.sync_status,
                'processing_error': upload.processing_error,
                'processed_at': upload.processed_at,
                'drive_file_url': upload.drive_file_url,
                'bookkeeping': bookkeeping_card,
            }

            if upload.bookkeeping_document_id or upload.upload_kind == 'bookkeeping_document':
                bookkeeping_upload_count += 1
                bookkeeping_uploads.append(upload_card)
            elif upload.upload_kind == 'general_image':
                general_image_count += 1
                general_uploads.append(upload_card)
            else:
                other_uploads.append(upload_card)

        cards.append({
            'response': response,
            'source_label': _intake_response_source_label(raw_response),
            'submitter_name': submitter_name,
            'submitted_at': response.response_submitted_at or response.response_created_at,
            'text_answers': text_answers,
            'general_uploads': general_uploads,
            'bookkeeping_uploads': bookkeeping_uploads,
            'other_uploads': other_uploads,
            'sync_error': response.sync_error,
            'provisioning_status': response.provisioning_status,
            'provisioning_error': response.provisioning_error,
            'provisioned_user_email': response.provisioned_user.email if response.provisioned_user else '',
            'provisioned_cbo_name': response.provisioned_cbo.name if response.provisioned_cbo else '',
            'provisioned_cbo_slug': response.provisioned_cbo.slug if response.provisioned_cbo else '',
        })
        if response.provisioning_status == 'provisioned' and response.provisioned_user_id:
            provisioned_account_count += 1
        last_synced_at = max(filter(None, [last_synced_at, response.synced_at]), default=response.synced_at)

    return {
        'response_count': len(responses),
        'general_image_count': general_image_count,
        'bookkeeping_upload_count': bookkeeping_upload_count,
        'digitized_bookkeeping_count': digitized_bookkeeping_count,
        'provisioned_account_count': provisioned_account_count,
        'last_synced_at': last_synced_at,
        'responses': cards,
    }


def _google_form_activity_summary(cbo: CBO, recent_cutoff: datetime | None = None) -> dict:
    responses = GoogleFormResponse.query.filter_by(cbo_id=cbo.id).all()
    last_submitted_at = None
    recent_response_count = 0

    for response in responses:
        submitted_at = response.response_submitted_at or response.response_created_at or response.created_at
        if submitted_at and (last_submitted_at is None or submitted_at > last_submitted_at):
            last_submitted_at = submitted_at
        if recent_cutoff and submitted_at and submitted_at >= recent_cutoff:
            recent_response_count += 1

    return {
        'response_count': len(responses),
        'recent_response_count': recent_response_count,
        'last_submitted_at': last_submitted_at,
    }


def _developer_sms_activity_context() -> dict:
    try:
        recent_intake_days = max(
            int(current_app.config.get('DEVELOPER_SMS_ACTIVITY_RECENT_INTAKE_DAYS', DEVELOPER_SMS_ACTIVITY_RECENT_INTAKE_DAYS) or DEVELOPER_SMS_ACTIVITY_RECENT_INTAKE_DAYS),
            1,
        )
    except (TypeError, ValueError):
        recent_intake_days = DEVELOPER_SMS_ACTIVITY_RECENT_INTAKE_DAYS

    recent_cutoff = datetime.utcnow() - timedelta(days=recent_intake_days)
    featured_cbo = _default_developer_sms_activity_cbo()
    featured_cbo_id = featured_cbo.id if featured_cbo else None
    featured_cbo_in_cards = False
    add_cbo_source = featured_cbo
    if add_cbo_source is None:
        add_cbo_source = CBO.query.filter(
            db.or_(
                CBO.intake_form_responder_url != '',
                CBO.intake_form_id != '',
            )
        ).order_by(CBO.name.asc()).first()
    if add_cbo_source is None:
        add_cbo_source = CBO.query.order_by(CBO.name.asc()).first()

    add_cbo_intake = None
    if add_cbo_source is not None:
        add_cbo_intake = {
            'offline': _intake_offline_context(add_cbo_source),
            'form_url': add_cbo_source.intake_form_responder_url or '',
            'form_edit_url': add_cbo_source.intake_form_edit_url or '',
        }

    cbo_cards = []
    totals = {
        'cbos': 0,
        'active_users': 0,
        'subscribers': 0,
        'responses': 0,
        'intake_responses': 0,
        'recent_intake_cbos': 0,
    }

    for cbo in CBO.query.order_by(CBO.name.asc()).all():
        feedback = _community_feedback_summary(cbo)
        intake = _google_form_activity_summary(cbo, recent_cutoff=recent_cutoff)
        active_user_count = User.query.filter(
            User.cbo_id == cbo.id,
            User.account_status == 'active',
            User.role.in_([User.ROLE_CBO, User.ROLE_FUNDER_CBO]),
        ).count()

        has_sms_activity = bool(feedback['subscribers'] or feedback['responses'])
        has_recent_intake = intake['recent_response_count'] > 0
        if not (active_user_count > 0 or has_sms_activity or has_recent_intake):
            continue

        last_activity_at = max(
            (
                value
                for value in [feedback.get('last_completed_at'), intake.get('last_submitted_at')]
                if value is not None
            ),
            default=None,
        )
        is_featured = cbo.id == featured_cbo_id
        if is_featured:
            featured_cbo_in_cards = True

        cbo_cards.append({
            'cbo': cbo,
            'feedback': feedback,
            'intake': intake,
            'active_user_count': active_user_count,
            'has_sms_activity': has_sms_activity,
            'has_recent_intake': has_recent_intake,
            'is_featured': is_featured,
            'last_activity_at': last_activity_at,
        })

        totals['cbos'] += 1
        totals['active_users'] += active_user_count
        totals['subscribers'] += feedback['subscribers']
        totals['responses'] += feedback['responses']
        totals['intake_responses'] += intake['response_count']
        if has_recent_intake:
            totals['recent_intake_cbos'] += 1

    cbo_cards.sort(key=lambda item: item['cbo'].name.lower())
    cbo_cards.sort(
        key=lambda item: (
            item['last_activity_at'] or datetime.min,
            item['active_user_count'],
            item['feedback']['responses'],
            item['feedback']['subscribers'],
            item['intake']['recent_response_count'],
        ),
        reverse=True,
    )

    return {
        'cbo_cards': cbo_cards,
        'totals': totals,
        'featured_cbo': featured_cbo if featured_cbo_in_cards else None,
        'recent_intake_days': recent_intake_days,
        'add_cbo_source': add_cbo_source,
        'add_cbo_intake': add_cbo_intake,
    }


def _bookkeeping_summary(cbo: CBO) -> dict:
    documents = BookkeepingDocument.query.filter_by(cbo_id=cbo.id).order_by(BookkeepingDocument.created_at.desc()).all()
    category_totals = defaultdict(lambda: {'label': '', 'amount': 0.0, 'count': 0, 'entry_type': 'unknown'})
    document_cards = []
    total_income = 0.0
    total_expenses = 0.0
    total_net = 0.0
    total_entries = 0
    total_audit_issues = 0
    last_processed_at = None

    for document in documents:
        extracted = _safe_json(document.extracted_data_json)
        entries = extracted.get('bookkeeping_entries', []) if isinstance(extracted, dict) else []
        flags = extracted.get('quality_flags', []) if isinstance(extracted, dict) else []
        transcribed_rows = extracted.get('transcribed_rows', []) if isinstance(extracted, dict) else []
        detected_columns = extracted.get('detected_columns', []) if isinstance(extracted, dict) else []
        audit = extracted.get('audit', {}) if isinstance(extracted, dict) else {}
        if isinstance(extracted, dict) and not audit:
            audit = audit_bookkeeping_document(extracted, cbo)
        audit_issues = audit.get('issues', []) if isinstance(audit, dict) else []
        flagged_cells = audit.get('flagged_cells', []) if isinstance(audit, dict) else []
        flagged_cells_by_row = defaultdict(lambda: defaultdict(list))
        doc_categories = defaultdict(float)

        for flagged_cell in flagged_cells:
            try:
                row_number = int(flagged_cell.get('row_number') or 0)
            except (TypeError, ValueError):
                continue
            column = str(flagged_cell.get('column') or '').strip()
            message = str(flagged_cell.get('message') or '').strip()
            if row_number and column and message:
                flagged_cells_by_row[row_number][column].append(message)

        annotated_rows = []
        for row in transcribed_rows:
            if not isinstance(row, dict):
                continue
            row_number = int(row.get('row_number') or 0)
            annotated_row = dict(row)
            annotated_row['audit_cells'] = dict(flagged_cells_by_row.get(row_number, {}))
            annotated_rows.append(annotated_row)

        for entry in entries:
            amount = _parse_money_value(entry.get('amount'))
            category = _humanize_category(entry.get('category') or 'other')
            entry_type = str(entry.get('entry_type') or 'unknown')
            key = f'{entry_type}:{category}'
            category_totals[key]['label'] = category
            category_totals[key]['amount'] += amount
            category_totals[key]['count'] += 1
            category_totals[key]['entry_type'] = entry_type
            doc_categories[category] += amount

        total_entries += len(entries)
        total_income += float(document.total_income or 0.0)
        total_expenses += float(document.total_expenses or 0.0)
        total_net += float(document.net_amount or 0.0)
        total_audit_issues += len(audit_issues)
        last_processed_at = max(filter(None, [last_processed_at, document.processed_at]), default=document.processed_at)

        document_cards.append({
            'document': document,
            'entry_count': len(entries),
            'workspace_period_label': _workspace_period_label(
                _normalize_workspace_period_key(getattr(document, 'workspace_period_key', ''))
                or _workspace_period_key_for_value(document.document_date)
                or _workspace_period_key_for_value(document.processed_at or document.created_at)
            ),
            'has_source_file': bool((document.stored_path or '').strip()),
            'has_image': bool((document.stored_path or '').strip()) or bool((getattr(document, 'storage_object_path', '') or '').strip()),
            'audit_issue_count': len(audit_issues),
            'audit_flags': audit_issues[:8],
            'organization_name': extracted.get('organization_name', ''),
            'document_title': extracted.get('document_title', ''),
            'raw_text': extracted.get('raw_text', ''),
            'extraction_notes': extracted.get('extraction_notes', ''),
            'detected_columns': detected_columns,
            'transcribed_rows': annotated_rows,
            'normalized_entries': entries,
            'top_categories': [
                {'label': label, 'amount': amount}
                for label, amount in sorted(doc_categories.items(), key=lambda item: item[1], reverse=True)[:3]
            ],
            'quality_flags': [str(flag).strip() for flag in flags if str(flag).strip()][:3],
        })

    # Group documents by type for folder view
    documents_by_type = defaultdict(list)
    for card in document_cards:
        doc_type = (card['document'].document_type or 'unknown').replace('_', ' ').title()
        documents_by_type[doc_type].append(card)

    workspace_template = _safe_json(cbo.bookkeeping_template_json)
    if not workspace_template.get('columns'):
        workspace_template = _generate_bookkeeping_workspace_template(cbo, {})

    workspace_columns = []
    for column in workspace_template.get('columns') or []:
        _append_unique_workspace_column(workspace_columns, column)

    raw_workspace_entries = _safe_json_list(cbo.bookkeeping_workspace_entries_json)
    document_seed_entries = _build_document_workspace_seed_entries(documents, workspace_columns)

    for raw_entry in raw_workspace_entries:
        if not isinstance(raw_entry, dict):
            continue
        values = raw_entry.get('values') or {}
        if not isinstance(values, dict):
            continue
        for column in values.keys():
            _append_unique_workspace_column(workspace_columns, column)

    if not workspace_columns:
        for default_column in ['Date', 'Description', 'Category', 'Amount', 'Notes']:
            _append_unique_workspace_column(workspace_columns, default_column)

    document_type_lookup = {
        int(document.id): _normalize_workspace_document_type_key(document.document_type)
        for document in documents
    }
    primary_workspace_document_type_key = _normalize_workspace_document_type_key(workspace_template.get('primary_document_type'))
    template_worksheets = _normalize_workspace_template_worksheets(workspace_template.get('worksheets') or [])
    template_worksheet_lookup = {
        worksheet['key']: worksheet
        for worksheet in template_worksheets
    }

    workspace_entry_index = {}
    for seed_entry in document_seed_entries:
        normalized_seed_entry = _normalize_workspace_entry_record(seed_entry, workspace_columns)
        workspace_entry_index[normalized_seed_entry['row_id']] = normalized_seed_entry

    for raw_entry in raw_workspace_entries:
        if not isinstance(raw_entry, dict):
            continue
        normalized_entry = _normalize_workspace_entry_record(raw_entry, workspace_columns)
        workspace_entry_index[normalized_entry['row_id']] = normalized_entry

    workspace_entries = [
        entry
        for entry in workspace_entry_index.values()
        if not entry.get('is_deleted') and _workspace_row_has_values(entry.get('values') or {})
    ]
    workspace_entries.sort(key=_workspace_entry_sort_key)

    workspace_period_index = {}
    workspace_type_index = {}
    for entry in workspace_entries:
        document_type_key = _normalize_workspace_document_type_key(entry.get('workspace_document_type_key'))
        if not document_type_key:
            source_document_id = int(entry.get('source_document_id') or 0)
            if source_document_id:
                document_type_key = document_type_lookup.get(source_document_id, '')
        if not document_type_key:
            if entry.get('entry_source') == 'document_import':
                document_type_key = primary_workspace_document_type_key or 'unknown'
            else:
                document_type_key = primary_workspace_document_type_key or 'manual_general'
        entry['workspace_document_type_key'] = document_type_key
        entry['workspace_document_type_label'] = _workspace_document_type_label(document_type_key)

        period_key = _normalize_workspace_period_key(entry.get('workspace_period_key'))
        if not period_key:
            period_key = _workspace_period_key_for_value(entry.get('created_at')) or datetime.utcnow().strftime('%Y-%m')
            entry['workspace_period_key'] = period_key
        entry['workspace_period_label'] = _workspace_period_label(period_key)

        if period_key not in workspace_period_index:
            workspace_period_index[period_key] = {
                'key': period_key,
                'label': _workspace_period_label(period_key),
                'row_count': 0,
                'manual_row_count': 0,
                'imported_row_count': 0,
            }

        workspace_period_index[period_key]['row_count'] += 1
        if entry.get('entry_source') == 'document_import':
            workspace_period_index[period_key]['imported_row_count'] += 1
        else:
            workspace_period_index[period_key]['manual_row_count'] += 1

        type_bucket = workspace_type_index.setdefault(document_type_key, {
            'key': document_type_key,
            'label': entry['workspace_document_type_label'],
            'row_count': 0,
            'manual_row_count': 0,
            'imported_row_count': 0,
            'period_index': {},
            'entries': [],
        })
        type_bucket['row_count'] += 1
        if entry.get('entry_source') == 'document_import':
            type_bucket['imported_row_count'] += 1
        else:
            type_bucket['manual_row_count'] += 1

        type_period_bucket = type_bucket['period_index'].setdefault(period_key, {
            'key': period_key,
            'label': _workspace_period_label(period_key),
            'row_count': 0,
            'manual_row_count': 0,
            'imported_row_count': 0,
        })
        type_period_bucket['row_count'] += 1
        if entry.get('entry_source') == 'document_import':
            type_period_bucket['imported_row_count'] += 1
        else:
            type_period_bucket['manual_row_count'] += 1

        type_bucket['entries'].append(entry)

    for worksheet in template_worksheets:
        worksheet_key = str(worksheet.get('key') or '').strip()
        if not worksheet_key:
            continue
        worksheet_label = str(worksheet.get('label') or _workspace_document_type_label(worksheet_key)).strip()
        type_bucket = workspace_type_index.setdefault(worksheet_key, {
            'key': worksheet_key,
            'label': worksheet_label,
            'row_count': 0,
            'manual_row_count': 0,
            'imported_row_count': 0,
            'period_index': {},
            'entries': [],
        })
        if worksheet_label and (not type_bucket.get('label') or type_bucket['label'] == _workspace_document_type_label(worksheet_key)):
            type_bucket['label'] = worksheet_label

        for period_key in worksheet.get('periods') or []:
            normalized_period_key = _normalize_workspace_period_key(period_key)
            if not normalized_period_key:
                continue
            workspace_period_index.setdefault(normalized_period_key, {
                'key': normalized_period_key,
                'label': _workspace_period_label(normalized_period_key),
                'row_count': 0,
                'manual_row_count': 0,
                'imported_row_count': 0,
            })
            type_bucket['period_index'].setdefault(normalized_period_key, {
                'key': normalized_period_key,
                'label': _workspace_period_label(normalized_period_key),
                'row_count': 0,
                'manual_row_count': 0,
                'imported_row_count': 0,
            })

    default_workspace_period_key = ''
    if workspace_period_index:
        workspace_periods = sorted(workspace_period_index.values(), key=lambda item: item['key'], reverse=True)
        default_workspace_period_key = workspace_periods[0]['key']
    else:
        default_workspace_period_key = datetime.utcnow().strftime('%Y-%m')
        workspace_periods = [{
            'key': default_workspace_period_key,
            'label': _workspace_period_label(default_workspace_period_key),
            'row_count': 0,
            'manual_row_count': 0,
            'imported_row_count': 0,
        }]

    workspace_type_groups = []
    for type_bucket in workspace_type_index.values():
        group_periods = sorted(type_bucket['period_index'].values(), key=lambda item: item['key'], reverse=True)
        worksheet_definition = template_worksheet_lookup.get(type_bucket['key']) or {}
        preferred_type_period_key = _normalize_workspace_period_key(worksheet_definition.get('default_period_key'))
        available_group_period_keys = {period['key'] for period in group_periods}
        default_type_period_key = preferred_type_period_key if preferred_type_period_key in available_group_period_keys else (group_periods[0]['key'] if group_periods else default_workspace_period_key)
        workspace_type_groups.append({
            'key': type_bucket['key'],
            'label': type_bucket['label'],
            'row_count': type_bucket['row_count'],
            'manual_row_count': type_bucket['manual_row_count'],
            'imported_row_count': type_bucket['imported_row_count'],
            'periods': group_periods or [{
                'key': default_type_period_key,
                'label': _workspace_period_label(default_type_period_key),
                'row_count': 0,
                'manual_row_count': 0,
                'imported_row_count': 0,
            }],
            'default_period_key': default_type_period_key,
            'default_period_label': _workspace_period_label(default_type_period_key),
            'entries': type_bucket['entries'],
        })

    preferred_workspace_type_key = primary_workspace_document_type_key or (workspace_type_groups[0]['key'] if workspace_type_groups else 'manual_general')
    workspace_type_groups.sort(key=lambda item: (
        0 if item['key'] == preferred_workspace_type_key else 1,
        item['label'].lower(),
    ))

    if not workspace_type_groups:
        workspace_type_groups = [{
            'key': preferred_workspace_type_key or 'manual_general',
            'label': _workspace_document_type_label(preferred_workspace_type_key or 'manual_general'),
            'row_count': 0,
            'manual_row_count': 0,
            'imported_row_count': 0,
            'periods': workspace_periods,
            'default_period_key': default_workspace_period_key,
            'default_period_label': _workspace_period_label(default_workspace_period_key),
            'entries': [],
        }]

    default_workspace_type_key = workspace_type_groups[0]['key']

    sorted_categories = sorted(category_totals.values(), key=lambda item: item['amount'], reverse=True)
    return {
        'document_count': len(documents),
        'entry_count': total_entries,
        'audit_issue_count': total_audit_issues,
        'income_total': round(total_income, 2),
        'expense_total': round(total_expenses, 2),
        'net_total': round(total_net, 2),
        'top_categories': sorted_categories[:6],
        'documents': document_cards,
        'documents_by_type': dict(documents_by_type),
        'last_processed_at': last_processed_at,
        'workspace': {
            'columns': workspace_columns,
            'custom_fields': [_titleize_workspace_label(value) for value in (workspace_template.get('custom_fields') or []) if _titleize_workspace_label(value)],
            'source_document_types': [str(value).strip() for value in (workspace_template.get('source_document_types') or []) if str(value).strip()],
            'primary_document_type': str(workspace_template.get('primary_document_type') or '').strip(),
            'generated_at': _parse_google_timestamp(workspace_template.get('generated_at')),
            'worksheets': template_worksheets,
            'periods': workspace_periods,
            'type_groups': workspace_type_groups,
            'default_type_key': default_workspace_type_key,
            'default_period_key': default_workspace_period_key,
            'default_period_label': _workspace_period_label(default_workspace_period_key),
            'row_count': len(workspace_entries),
            'entries': workspace_entries,
        },
    }


def _funding_audit_summary(cbo: CBO) -> dict:
    documents = FundingAuditDocument.query.filter_by(cbo_id=cbo.id).order_by(FundingAuditDocument.created_at.desc()).all()
    total_declared_funding = 0.0
    total_working_capital = 0.0
    verified_count = 0
    total_issue_count = 0
    last_processed_at = None
    cards = []

    for document in documents:
        payload = _safe_json(document.extracted_data_json)
        declared = payload.get('declared', {}) if isinstance(payload, dict) else {}
        analysis = payload.get('document_analysis', {}) if isinstance(payload, dict) else {}
        search_results = payload.get('search_results', {}) if isinstance(payload, dict) else {}
        source_verification = payload.get('source_verification', {}) if isinstance(payload, dict) else {}
        operational_audit = payload.get('operational_audit', {}) if isinstance(payload, dict) else {}
        audit = payload.get('audit', {}) if isinstance(payload, dict) else {}
        legitimacy = analysis.get('legitimacy_assessment', {}) if isinstance(analysis, dict) else {}
        issues = audit.get('issues', []) if isinstance(audit, dict) else []

        total_declared_funding += float(document.declared_funding_amount or 0.0)
        total_working_capital += float(document.declared_working_capital or 0.0)
        total_issue_count += len(issues)
        if document.verification_status == 'verified':
            verified_count += 1
        last_processed_at = max(filter(None, [last_processed_at, document.processed_at]), default=document.processed_at)

        cards.append({
            'document': document,
            'declared': declared,
            'analysis': analysis,
            'audit': audit,
            'issues': issues[:8],
            'search_results': (search_results.get('items') or [])[:3] if isinstance(search_results, dict) else [],
            'search_error': search_results.get('error', '') if isinstance(search_results, dict) else '',
            'search_configured': bool(search_results.get('configured')) if isinstance(search_results, dict) else False,
            'source_verification': source_verification,
            'operational_audit': operational_audit,
            'legitimacy': legitimacy,
            'quality_flags': [str(flag).strip() for flag in (analysis.get('quality_flags') or []) if str(flag).strip()][:3],
            'raw_text': analysis.get('raw_text', '') if isinstance(analysis, dict) else '',
            'downloadable': is_stored_file_available(
                storage_backend=getattr(document, 'storage_backend', 'local'),
                stored_path=document.stored_path,
            ),
        })

    observed = observed_charitable_giving(cbo)
    overall_funding_gap = bool(total_declared_funding > 0 and observed['total'] > total_declared_funding + 0.01)
    if overall_funding_gap:
        total_issue_count += 1

    return {
        'document_count': len(documents),
        'verified_count': verified_count,
        'audit_issue_count': total_issue_count,
        'declared_funding_total': round(total_declared_funding, 2),
        'working_capital_total': round(total_working_capital, 2),
        'observed_charitable_giving_total': observed['total'],
        'bookkeeping_charitable_giving_total': observed['bookkeeping_total'],
        'kobo_charitable_giving_total': observed['kobo_total'],
        'overall_funding_gap': overall_funding_gap,
        'documents': cards,
        'last_processed_at': last_processed_at,
    }


def _process_bookkeeping_document(cbo: CBO, filename: str, mime_type: str, source_bytes: bytes, page_images: list[dict], source_channel: str, uploaded_by_user_id: int | None, existing_document: BookkeepingDocument | None = None, upload_batch_id: str | None = None, client_submission_id: str | None = None, related_page_upload: bool = False, include_in_workspace: bool | None = None, document_date_override: str | None = None, workspace_period_key: str | None = None) -> BookkeepingDocument:
    if not _allowed_bookkeeping_upload(filename, mime_type):
        raise BookkeepingExtractionError('unsupported file type.')

    extracted = extract_bookkeeping_document(page_images, filename, cbo, related_page_upload=related_page_upload)
    extracted['related_page_upload'] = bool(related_page_upload)
    extracted['audit'] = audit_bookkeeping_document(extracted, cbo)
    processed_at = datetime.utcnow()
    normalized_document_date = _resolve_bookkeeping_document_date(document_date_override, extracted.get('document_date'))
    resolved_workspace_period_key = (
        _normalize_workspace_period_key(workspace_period_key)
        or _workspace_period_key_for_value(normalized_document_date)
        or processed_at.strftime('%Y-%m')
    )
    document = existing_document or BookkeepingDocument(cbo_id=cbo.id)
    document.uploaded_by_user_id = uploaded_by_user_id
    document.upload_batch_id = upload_batch_id or document.upload_batch_id or ''
    document.client_submission_id = client_submission_id or document.client_submission_id or ''
    document.original_filename = filename
    if existing_document and (document.stored_path or '').strip():
        document.storage_backend = document.storage_backend or 'legacy'
    else:
        # Store image in Firebase (or local fallback) for document image pairing
        try:
            from .firebase_storage_service import store_bookkeeping_image
            stored = store_bookkeeping_image(cbo, filename, source_bytes, mime_type)
            document.stored_path = stored['stored_path']
            document.storage_backend = stored['storage_backend']
            document.storage_bucket = stored.get('storage_bucket', '')
            document.storage_object_path = stored.get('storage_object_path', '')
        except Exception:
            current_app.logger.exception('Failed to store bookkeeping image for CBO %s', cbo.id)
            document.stored_path = ''
            document.storage_backend = 'discarded'
            document.storage_bucket = ''
            document.storage_object_path = ''
    document.mime_type = mime_type
    document.source_channel = source_channel
    if include_in_workspace is not None:
        document.include_in_workspace = bool(include_in_workspace)
    document.document_type = extracted['document_type']
    document.workspace_period_key = resolved_workspace_period_key
    document.document_date = normalized_document_date
    document.period_start = extracted['period_start']
    document.period_end = extracted['period_end']
    document.vendor_or_counterparty = extracted['vendor_or_counterparty']
    document.currency = extracted['currency']
    document.summary_text = extracted['summary']
    document.extraction_confidence = extracted['document_confidence']
    document.total_income = extracted['totals']['income']
    document.total_expenses = extracted['totals']['expenses']
    document.net_amount = extracted['totals']['net']
    document.extracted_data_json = json.dumps(extracted)
    document.processed_at = processed_at

    db.session.add(document)
    db.session.commit()
    sync_bookkeeping_document_to_firestore(document)
    return document


def _normalize_bookkeeping_document_date(raw_value) -> str:
    normalized = str(raw_value or '').strip()
    if not normalized:
        return ''
    try:
        return datetime.strptime(normalized, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return ''


def _normalize_workspace_period_key(raw_value) -> str:
    normalized = str(raw_value or '').strip()
    if not normalized:
        return ''
    if re.fullmatch(r'\d{4}-\d{2}', normalized):
        return normalized
    return _workspace_period_key_for_value(normalized)


def _workspace_period_key_for_value(raw_value) -> str:
    normalized_date = _normalize_bookkeeping_document_date(raw_value)
    if normalized_date:
        return normalized_date[:7]

    if isinstance(raw_value, datetime):
        return raw_value.strftime('%Y-%m')

    normalized_text = str(raw_value or '').strip()
    if not normalized_text:
        return ''
    try:
        parsed = datetime.fromisoformat(normalized_text.replace('Z', '+00:00'))
    except ValueError:
        return ''
    return parsed.strftime('%Y-%m')


def _workspace_period_label(period_key: str) -> str:
    normalized = _normalize_workspace_period_key(period_key)
    if not normalized:
        return 'Undated'
    try:
        parsed = datetime.strptime(normalized + '-01', '%Y-%m-%d')
    except ValueError:
        return normalized
    return parsed.strftime('%B %Y')


def _normalize_workspace_document_type_key(raw_value) -> str:
    normalized = str(raw_value or '').strip().lower().replace('-', '_')
    if not normalized:
        return ''
    normalized = re.sub(r'[^a-z0-9_\s]+', '', normalized)
    normalized = re.sub(r'\s+', '_', normalized)
    normalized = re.sub(r'_+', '_', normalized).strip('_')
    return normalized


def _workspace_document_type_label(document_type_key: str) -> str:
    normalized = _normalize_workspace_document_type_key(document_type_key)
    if not normalized:
        return 'Manual / General'
    if normalized == 'manual_general':
        return 'Manual / General'
    return normalized.replace('_', ' ').title()


def _normalize_workspace_template_worksheets(raw_worksheets) -> list[dict]:
    if not isinstance(raw_worksheets, list):
        return []

    normalized_worksheets = []
    worksheet_lookup = {}
    for raw_worksheet in raw_worksheets:
        if not isinstance(raw_worksheet, dict):
            continue

        worksheet_label = _titleize_workspace_label(
            raw_worksheet.get('label')
            or raw_worksheet.get('category')
            or raw_worksheet.get('name')
            or raw_worksheet.get('key')
        )
        worksheet_key = _normalize_workspace_document_type_key(raw_worksheet.get('key') or worksheet_label)
        if not worksheet_key:
            continue

        period_keys = []
        raw_periods = raw_worksheet.get('periods') or raw_worksheet.get('months') or []
        if not isinstance(raw_periods, list):
            raw_periods = [raw_periods]
        for raw_period in raw_periods:
            normalized_period = _normalize_workspace_period_key(raw_period)
            if normalized_period and normalized_period not in period_keys:
                period_keys.append(normalized_period)

        default_period_key = _normalize_workspace_period_key(raw_worksheet.get('default_period_key'))
        if default_period_key and default_period_key not in period_keys:
            period_keys.append(default_period_key)
        if not period_keys:
            fallback_period_key = _normalize_workspace_period_key(raw_worksheet.get('created_at')) or datetime.utcnow().strftime('%Y-%m')
            period_keys = [fallback_period_key]
        period_keys = sorted(period_keys, reverse=True)
        if default_period_key not in period_keys:
            default_period_key = period_keys[0]

        existing_definition = worksheet_lookup.get(worksheet_key)
        if existing_definition:
            existing_definition['label'] = worksheet_label or existing_definition['label']
            existing_definition['periods'] = sorted(
                {str(value).strip() for value in existing_definition['periods'] + period_keys if str(value).strip()},
                reverse=True,
            )
            if default_period_key in existing_definition['periods']:
                existing_definition['default_period_key'] = default_period_key
            continue

        normalized_definition = {
            'key': worksheet_key,
            'label': worksheet_label or _workspace_document_type_label(worksheet_key),
            'periods': period_keys,
            'default_period_key': default_period_key,
            'source': str(raw_worksheet.get('source') or 'manual').strip() or 'manual',
            'created_at': str(raw_worksheet.get('created_at') or '').strip(),
        }
        worksheet_lookup[worksheet_key] = normalized_definition
        normalized_worksheets.append(normalized_definition)

    return normalized_worksheets


def _resolve_bookkeeping_document_date(document_date_override, extracted_document_date) -> str:
    return _normalize_bookkeeping_document_date(document_date_override) or _normalize_bookkeeping_document_date(extracted_document_date)


def _parse_bookkeeping_upload_options(form_data) -> dict:
    raw_document_date = str(form_data.get('document_date') or '').strip()
    normalized_document_date = _normalize_bookkeeping_document_date(raw_document_date)
    include_in_workspace = str(form_data.get('import_into_workspace') or '').strip().lower() in {'1', 'true', 'on', 'yes'}

    errors = []
    if raw_document_date and not normalized_document_date:
        errors.append('Choose a valid bookkeeping document date.')

    return {
        'document_date': normalized_document_date,
        'include_in_workspace': include_in_workspace,
        'workspace_period_key': _workspace_period_key_for_value(normalized_document_date),
        'errors': errors,
    }


def _process_funding_audit_document(cbo: CBO, filename: str, mime_type: str, source_bytes: bytes, page_images: list[dict], source_channel: str, uploaded_by_user_id: int | None, declared: dict) -> FundingAuditDocument:
    if not _allowed_bookkeeping_upload(filename, mime_type):
        raise FundingAuditError('Unsupported file type.')

    stored = None
    try:
        stored = _store_funding_audit_source_file(cbo, filename, source_bytes)
        extracted = build_funding_audit_payload(cbo, page_images, filename, declared)

        document = FundingAuditDocument(cbo_id=cbo.id)
        document.uploaded_by_user_id = uploaded_by_user_id
        document.original_filename = filename
        document.stored_path = stored['stored_path']
        document.storage_backend = stored['storage_backend']
        document.mime_type = mime_type
        document.source_channel = source_channel
        document.document_type = (extracted.get('document_analysis') or {}).get('document_type', 'unknown')
        document.document_date = (extracted.get('document_analysis') or {}).get('document_date', '')
        document.declared_funder_name = (extracted.get('declared') or {}).get('funder_name', '')
        document.extracted_funder_name = (extracted.get('document_analysis') or {}).get('issuing_organization', '')
        document.extracted_reference_number = (extracted.get('document_analysis') or {}).get('reference_number', '')
        document.declared_period_start = (extracted.get('declared') or {}).get('period_start', '')
        document.declared_period_end = (extracted.get('declared') or {}).get('period_end', '')
        document.currency = (extracted.get('declared') or {}).get('currency', 'KES')
        document.declared_funding_amount = float((extracted.get('declared') or {}).get('funding_amount') or 0.0)
        document.declared_working_capital = float((extracted.get('declared') or {}).get('working_capital') or 0.0)
        document.summary_text = (extracted.get('document_analysis') or {}).get('summary', '')
        document.verification_status = (extracted.get('audit') or {}).get('status', 'needs_review')
        document.verification_confidence = float((extracted.get('audit') or {}).get('confidence') or 0.0)
        document.extracted_data_json = json.dumps(extracted)
        document.processed_at = datetime.utcnow()

        db.session.add(document)
        db.session.commit()
        return document
    except Exception:
        if stored:
            delete_stored_file(
                storage_backend=stored.get('storage_backend', 'local'),
                stored_path=stored.get('stored_path', ''),
            )
        raise


def _bookkeeping_render_context() -> tuple[str, bool]:
    bookkeeping_locked_pane = ''
    embedded_layout = False

    for raw_url in [request.url, request.referrer or '']:
        if not raw_url:
            continue

        query = parse_qs(urlparse(raw_url).query)
        isolated_view = str((query.get('isolated') or [''])[0]).strip().lower()
        if isolated_view == 'bookkeeping-live':
            bookkeeping_locked_pane = 'live'
        elif isolated_view == 'bookkeeping-digitized':
            bookkeeping_locked_pane = 'digitized'

        embedded_flag = str((query.get('embedded') or [''])[0]).strip().lower()
        if embedded_flag in {'1', 'true', 'yes', 'on'}:
            embedded_layout = True

        if bookkeeping_locked_pane and embedded_layout:
            break

    return bookkeeping_locked_pane, embedded_layout


def _bookkeeping_response(cbo: CBO, own: bool, summary: dict, success_message: str, failure_messages: list[str], redirect_slug: str, funding_summary: dict | None = None, extra_payload: dict | None = None):
    if _is_ajax_request():
        bookkeeping_locked_pane, _embedded_layout = _bookkeeping_render_context()
        viewer_is_funder = _user_has_funder_role(current_user) and not own
        html = render_template(
            '_bookkeeping_section.html',
            cbo=cbo,
            own=own,
            viewer_is_funder=viewer_is_funder,
            can_manage_bookkeeping=_can_manage_bookkeeping(cbo),
            bookkeeping_summary=summary,
            bookkeeping_offline=_bookkeeping_offline_context(cbo),
            funding_audit_summary=funding_summary or _funding_audit_summary(cbo),
            mobile_scan=_bookkeeping_mobile_scan_context(cbo),
            bookkeeping_locked_pane=bookkeeping_locked_pane,
        )
        payload = {
            'ok': not failure_messages,
            'html': html,
            'success_message': success_message,
            'failure_messages': failure_messages,
        }
        if extra_payload:
            payload.update(extra_payload)
        return jsonify(payload)

    if success_message:
        flash(success_message, 'success')
    if failure_messages:
        if success_message:
            flash('Some files were skipped: ' + ' '.join(failure_messages), 'info')
        else:
            flash(' '.join(failure_messages), 'danger')
    return redirect(url_for('main.cbo_profile', slug=redirect_slug))


def _is_ajax_request() -> bool:
    return request.headers.get('X-Requested-With', '').lower() == 'xmlhttprequest'


def _serialize_bookkeeping_summary(summary: dict) -> dict:
    return {
        'document_count': summary['document_count'],
        'entry_count': summary['entry_count'],
        'audit_issue_count': summary['audit_issue_count'],
        'income_total': summary['income_total'],
        'expense_total': summary['expense_total'],
        'net_total': summary['net_total'],
        'top_categories': summary['top_categories'],
        'last_processed_at': summary['last_processed_at'].isoformat() if summary['last_processed_at'] else None,
    }


def _serialize_bookkeeping_workspace(summary: dict) -> dict:
    workspace = summary.get('workspace') or {}
    generated_at = workspace.get('generated_at')
    entries = []
    for entry in workspace.get('entries') or []:
        created_at = entry.get('created_at')
        updated_at = entry.get('updated_at')
        values = entry.get('values') or {}
        if not isinstance(values, dict):
            values = {}
        entries.append({
            'row_id': str(entry.get('row_id') or ''),
            'created_at': created_at.isoformat() if created_at else '',
            'updated_at': updated_at.isoformat() if updated_at else '',
            'created_at_display': str(entry.get('created_at_display') or ''),
            'entry_source': str(entry.get('entry_source') or 'manual'),
            'source_document_id': str(entry.get('source_document_id') or ''),
            'source_row_number': int(entry.get('source_row_number') or 0),
            'workspace_document_type_key': str(entry.get('workspace_document_type_key') or ''),
            'workspace_document_type_label': str(entry.get('workspace_document_type_label') or ''),
            'workspace_period_key': str(entry.get('workspace_period_key') or ''),
            'workspace_period_label': str(entry.get('workspace_period_label') or ''),
            'values': {
                str(column): str(value or '').strip()
                for column, value in values.items()
            },
        })

    periods = []
    for period in workspace.get('periods') or []:
        if not isinstance(period, dict):
            continue
        period_key = _normalize_workspace_period_key(period.get('key'))
        periods.append({
            'key': period_key,
            'label': str(period.get('label') or _workspace_period_label(period_key)),
            'row_count': int(period.get('row_count') or 0),
            'manual_row_count': int(period.get('manual_row_count') or 0),
            'imported_row_count': int(period.get('imported_row_count') or 0),
        })

    type_groups = []
    for type_group in workspace.get('type_groups') or []:
        if not isinstance(type_group, dict):
            continue
        document_type_key = _normalize_workspace_document_type_key(type_group.get('key'))
        serialized_periods = []
        for period in type_group.get('periods') or []:
            if not isinstance(period, dict):
                continue
            period_key = _normalize_workspace_period_key(period.get('key'))
            serialized_periods.append({
                'key': period_key,
                'label': str(period.get('label') or _workspace_period_label(period_key)),
                'row_count': int(period.get('row_count') or 0),
                'manual_row_count': int(period.get('manual_row_count') or 0),
                'imported_row_count': int(period.get('imported_row_count') or 0),
            })
        type_groups.append({
            'key': document_type_key,
            'label': str(type_group.get('label') or _workspace_document_type_label(document_type_key)),
            'row_count': int(type_group.get('row_count') or 0),
            'manual_row_count': int(type_group.get('manual_row_count') or 0),
            'imported_row_count': int(type_group.get('imported_row_count') or 0),
            'periods': serialized_periods,
            'default_period_key': str(type_group.get('default_period_key') or ''),
            'default_period_label': str(type_group.get('default_period_label') or ''),
        })

    worksheets = []
    for worksheet in workspace.get('worksheets') or []:
        if not isinstance(worksheet, dict):
            continue
        worksheet_key = _normalize_workspace_document_type_key(worksheet.get('key'))
        worksheet_periods = []
        for period_key in worksheet.get('periods') or []:
            normalized_period_key = _normalize_workspace_period_key(period_key)
            if normalized_period_key and normalized_period_key not in worksheet_periods:
                worksheet_periods.append(normalized_period_key)
        default_period_key = _normalize_workspace_period_key(worksheet.get('default_period_key'))
        if default_period_key and default_period_key not in worksheet_periods:
            worksheet_periods.append(default_period_key)
        worksheets.append({
            'key': worksheet_key,
            'label': str(worksheet.get('label') or _workspace_document_type_label(worksheet_key)),
            'periods': worksheet_periods,
            'default_period_key': default_period_key or (worksheet_periods[0] if worksheet_periods else ''),
            'source': str(worksheet.get('source') or 'manual').strip() or 'manual',
        })

    return {
        'columns': [str(value).strip() for value in (workspace.get('columns') or []) if str(value).strip()],
        'custom_fields': [str(value).strip() for value in (workspace.get('custom_fields') or []) if str(value).strip()],
        'source_document_types': [str(value).strip() for value in (workspace.get('source_document_types') or []) if str(value).strip()],
        'primary_document_type': str(workspace.get('primary_document_type') or '').strip(),
        'generated_at': generated_at.isoformat() if generated_at else None,
        'worksheets': worksheets,
        'periods': periods,
        'type_groups': type_groups,
        'default_type_key': str(workspace.get('default_type_key') or ''),
        'default_period_key': str(workspace.get('default_period_key') or ''),
        'default_period_label': str(workspace.get('default_period_label') or ''),
        'row_count': int(workspace.get('row_count') or 0),
        'entries': entries,
    }


def _bookkeeping_workspace_template_context(cbo: CBO, summary: dict | None = None) -> tuple[dict, list[str], dict[str, str]]:
    summary = summary or _bookkeeping_summary(cbo)
    workspace = summary.get('workspace') or {}
    template_columns = [str(value).strip() for value in (workspace.get('columns') or []) if str(value).strip()]
    template_column_lookup = {
        _titleize_workspace_label(column).lower(): column
        for column in template_columns
    }
    return summary, template_columns, template_column_lookup


def _normalize_bookkeeping_workspace_column_update(existing_custom_fields: list[str], raw_originals, raw_labels) -> tuple[list[str], list[str], list[tuple[str, str]], list[str]]:
    next_columns = []
    next_custom_fields = []
    column_pairs = []
    errors = []
    seen_keys = set()
    existing_custom_field_keys = {
        _titleize_workspace_label(value).lower()
        for value in existing_custom_fields
        if _titleize_workspace_label(value)
    }

    raw_original_list = list(raw_originals or [])
    raw_label_list = list(raw_labels or [])
    max_length = max(len(raw_original_list), len(raw_label_list))
    for index in range(max_length):
        original_column = _titleize_workspace_label(raw_original_list[index] if index < len(raw_original_list) else '')
        next_column = _titleize_workspace_label(raw_label_list[index] if index < len(raw_label_list) else '')

        if not original_column and not next_column:
            continue
        if not next_column:
            continue

        normalized_key = next_column.lower()
        if normalized_key in seen_keys:
            errors.append(f'{next_column} appears more than once. Keep each column header unique.')
            continue

        seen_keys.add(normalized_key)
        next_columns.append(next_column)
        column_pairs.append((original_column, next_column))
        if not original_column or original_column.lower() in existing_custom_field_keys:
            next_custom_fields.append(next_column)

    if not next_columns:
        errors.append('Keep at least one workbook column.')

    return next_columns, next_custom_fields, column_pairs, errors


def _remap_bookkeeping_workspace_entries_to_columns(raw_entries: list, column_pairs: list[tuple[str, str]], next_columns: list[str]) -> list[dict]:
    remapped_entries = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue

        values = raw_entry.get('values') or {}
        if not isinstance(values, dict):
            values = {}
        value_lookup = {
            str(column_name or '').strip().lower(): str(column_value or '').strip()
            for column_name, column_value in values.items()
        }

        next_values = {}
        for original_column, next_column in column_pairs:
            if not original_column:
                next_values[next_column] = ''
                continue
            next_values[next_column] = value_lookup.get(original_column.lower(), '')

        remapped_entry = dict(raw_entry)
        remapped_entry['values'] = {
            column: str(next_values.get(column) or '').strip()
            for column in next_columns
        }
        remapped_entries.append(remapped_entry)

    return remapped_entries


def _normalize_bookkeeping_workspace_values(template_columns: list[str], template_column_lookup: dict[str, str], raw_pairs) -> dict:
    row_values = {}
    for column_name, column_value in raw_pairs:
        normalized_name = _titleize_workspace_label(column_name)
        matched_column = template_column_lookup.get(normalized_name.lower())
        if not normalized_name or not matched_column:
            continue
        row_values[matched_column] = str(column_value or '').strip()

    return {
        column: str(row_values.get(column) or '').strip()
        for column in template_columns
    }


def _workspace_row_has_values(values: dict) -> bool:
    if not isinstance(values, dict):
        return False
    return any(str(value or '').strip() for value in values.values())


def _workspace_entry_sort_key(entry: dict) -> tuple:
    created_at = entry.get('created_at') or datetime.min
    try:
        source_document_id = int(entry.get('source_document_id') or 0)
    except (TypeError, ValueError):
        source_document_id = 0
    try:
        source_row_number = int(entry.get('source_row_number') or 0)
    except (TypeError, ValueError):
        source_row_number = 0
    document_type_key = _normalize_workspace_document_type_key(entry.get('workspace_document_type_key'))
    period_key = _normalize_workspace_period_key(entry.get('workspace_period_key')) or _workspace_period_key_for_value(created_at)
    return (
        document_type_key,
        period_key,
        created_at,
        source_document_id,
        source_row_number,
        str(entry.get('row_id') or ''),
    )


def _normalize_workspace_entry_record(raw_entry: dict, workspace_columns: list[str]) -> dict:
    values = raw_entry.get('values') or {}
    if not isinstance(values, dict):
        values = {}

    created_at_raw = raw_entry.get('created_at')
    updated_at_raw = raw_entry.get('updated_at')
    created_at = created_at_raw if isinstance(created_at_raw, datetime) else _parse_google_timestamp(created_at_raw)
    updated_at = updated_at_raw if isinstance(updated_at_raw, datetime) else _parse_google_timestamp(updated_at_raw)
    entry_source = str(raw_entry.get('entry_source') or 'manual').strip() or 'manual'

    try:
        source_document_id = int(raw_entry.get('source_document_id') or 0)
    except (TypeError, ValueError):
        source_document_id = 0
    try:
        source_row_number = int(raw_entry.get('source_row_number') or 0)
    except (TypeError, ValueError):
        source_row_number = 0

    workspace_document_type_key = _normalize_workspace_document_type_key(
        raw_entry.get('workspace_document_type_key')
        or raw_entry.get('workspace_document_type')
        or raw_entry.get('document_type')
    )

    workspace_period_key = _normalize_workspace_period_key(raw_entry.get('workspace_period_key'))
    if not workspace_period_key:
        workspace_period_key = _workspace_period_key_for_value(created_at)

    is_deleted = bool(raw_entry.get('is_deleted'))
    created_at_display = 'Saved row'
    if created_at:
        created_at_display = created_at.strftime('%Y-%m-%d %H:%M UTC')
    if entry_source == 'document_import' and source_row_number:
        created_at_display = f'Document row {source_row_number}'

    return {
        'row_id': str(raw_entry.get('row_id') or uuid.uuid4().hex),
        'created_at': created_at,
        'updated_at': updated_at,
        'created_at_display': created_at_display,
        'entry_source': entry_source,
        'source_document_id': source_document_id,
        'source_row_number': source_row_number,
        'workspace_document_type_key': workspace_document_type_key,
        'workspace_document_type_label': _workspace_document_type_label(workspace_document_type_key),
        'workspace_period_key': workspace_period_key,
        'workspace_period_label': _workspace_period_label(workspace_period_key),
        'is_deleted': is_deleted,
        'values': {
            column: str(values.get(column) or '').strip()
            for column in workspace_columns
        },
    }


def _build_document_workspace_seed_entries(documents: list[BookkeepingDocument], workspace_columns: list[str]) -> list[dict]:
    seed_entries = []

    for document in sorted(documents, key=lambda item: item.created_at or datetime.min):
        if not bool(getattr(document, 'include_in_workspace', False)):
            continue

        extracted = _safe_json(document.extracted_data_json)
        transcribed_rows = extracted.get('transcribed_rows', []) if isinstance(extracted, dict) else []
        if not isinstance(transcribed_rows, list):
            continue

        workspace_period_key = _normalize_workspace_period_key(getattr(document, 'workspace_period_key', ''))
        if not workspace_period_key:
            workspace_period_key = _workspace_period_key_for_value(document.document_date) or _workspace_period_key_for_value(document.processed_at or document.created_at)
        workspace_document_type_key = _normalize_workspace_document_type_key(document.document_type) or 'unknown'

        for row in transcribed_rows:
            if not isinstance(row, dict):
                continue

            row_type = str(row.get('row_type') or '').strip().lower()
            row_number = row.get('row_number')
            try:
                row_number = int(row_number or 0)
            except (TypeError, ValueError):
                row_number = 0

            raw_cells = row.get('cells') or {}
            if not isinstance(raw_cells, dict):
                continue

            normalized_values = {}
            non_empty_count = 0
            non_signature_count = 0
            for raw_column, raw_value in raw_cells.items():
                normalized_column = _normalize_workspace_document_column(raw_column)
                if not normalized_column:
                    continue
                cleaned_value = str(raw_value or '').strip()
                normalized_values[normalized_column] = cleaned_value
                _append_unique_workspace_column(workspace_columns, normalized_column)
                if cleaned_value:
                    non_empty_count += 1
                    if 'sign' not in normalized_column.lower():
                        non_signature_count += 1

            if non_empty_count == 0:
                continue
            if row_type not in {'transaction', 'entry', 'record'} and non_signature_count < 4:
                continue
            if non_signature_count < 3:
                continue

            seed_entries.append({
                'row_id': f'document-{document.id}-row-{row_number or len(seed_entries) + 1}',
                'created_at': (document.processed_at or document.created_at or datetime.utcnow()).isoformat(),
                'updated_at': (document.updated_at or document.processed_at or document.created_at or datetime.utcnow()).isoformat(),
                'entry_source': 'document_import',
                'source_document_id': document.id,
                'source_row_number': row_number,
                'workspace_document_type_key': workspace_document_type_key,
                'workspace_period_key': workspace_period_key,
                'is_deleted': False,
                'values': normalized_values,
            })

    return seed_entries


def _normalize_bookkeeping_workspace_grid_rows(template_columns: list[str], form_data) -> list[dict]:
    row_count_raw = str(form_data.get('grid_row_count') or '').strip()
    if not row_count_raw.isdigit():
        return []

    normalized_rows = []
    max_rows = min(int(row_count_raw), 250)
    for row_index in range(max_rows):
        row_id = str(form_data.get(f'grid_row_id_{row_index}') or '').strip()
        created_at = str(form_data.get(f'grid_row_created_at_{row_index}') or '').strip()
        updated_at = str(form_data.get(f'grid_row_updated_at_{row_index}') or '').strip()
        entry_source = str(form_data.get(f'grid_row_source_{row_index}') or 'manual').strip() or 'manual'
        source_document_id = str(form_data.get(f'grid_row_document_{row_index}') or '').strip()
        source_row_number = str(form_data.get(f'grid_row_document_row_{row_index}') or '').strip()
        workspace_document_type_key = _normalize_workspace_document_type_key(form_data.get(f'grid_row_document_type_{row_index}'))
        workspace_period_key = _normalize_workspace_period_key(form_data.get(f'grid_row_period_{row_index}'))
        if not workspace_period_key:
            workspace_period_key = _workspace_period_key_for_value(created_at) or _workspace_period_key_for_value(datetime.utcnow())

        row_values = {}
        for column_index, column in enumerate(template_columns):
            row_values[column] = str(form_data.get(f'grid_cell_{row_index}_{column_index}') or '').strip()

        has_values = _workspace_row_has_values(row_values)
        if not has_values and not row_id:
            continue

        normalized_rows.append({
            'row_id': row_id or uuid.uuid4().hex,
            'created_at': created_at or datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
            'entry_source': entry_source,
            'source_document_id': source_document_id,
            'source_row_number': source_row_number,
            'workspace_document_type_key': workspace_document_type_key,
            'workspace_period_key': workspace_period_key,
            'is_deleted': bool(row_id and not has_values),
            'values': row_values,
        })

    return normalized_rows


def _normalize_bookkeeping_workspace_json_rows(template_columns: list[str], rows_payload) -> list[dict]:
    if not isinstance(rows_payload, list):
        return []

    normalized_rows = []
    for row in rows_payload[:250]:
        if not isinstance(row, dict):
            continue
        values = row.get('values') or {}
        if not isinstance(values, dict):
            values = {}
        row_values = {
            column: str(values.get(column) or '').strip()
            for column in template_columns
        }
        row_id = str(row.get('row_id') or '').strip()
        has_values = _workspace_row_has_values(row_values)
        if not has_values and not row_id:
            continue

        created_at = str(row.get('created_at') or datetime.utcnow().isoformat()).strip() or datetime.utcnow().isoformat()
        workspace_document_type_key = _normalize_workspace_document_type_key(row.get('workspace_document_type_key'))
        workspace_period_key = _normalize_workspace_period_key(row.get('workspace_period_key'))
        if not workspace_period_key:
            workspace_period_key = _workspace_period_key_for_value(created_at) or _workspace_period_key_for_value(datetime.utcnow())

        normalized_rows.append({
            'row_id': row_id or uuid.uuid4().hex,
            'created_at': created_at,
            'updated_at': datetime.utcnow().isoformat(),
            'entry_source': str(row.get('entry_source') or 'manual').strip() or 'manual',
            'source_document_id': str(row.get('source_document_id') or '').strip(),
            'source_row_number': str(row.get('source_row_number') or '').strip(),
            'workspace_document_type_key': workspace_document_type_key,
            'workspace_period_key': workspace_period_key,
            'is_deleted': bool(row_id and not has_values),
            'values': row_values,
        })

    return normalized_rows


def _replace_bookkeeping_workspace_entries(cbo: CBO, row_entries: list[dict]) -> None:
    cbo.bookkeeping_workspace_entries_json = json.dumps(row_entries, default=str)
    db.session.add(cbo)
    db.session.commit()


def _normalize_bookkeeping_workspace_sheet_rows(template_columns: list[str], form_data) -> list[dict]:
    row_count_raw = str(form_data.get('sheet_row_count') or '').strip()
    if not row_count_raw.isdigit():
        return []

    normalized_rows = []
    for row_index in range(min(int(row_count_raw), 50)):
        row_values = {}
        has_values = False
        for column_index, column in enumerate(template_columns):
            cell_value = str(form_data.get(f'sheet_cell_{row_index}_{column_index}') or '').strip()
            row_values[column] = cell_value
            has_values = has_values or bool(cell_value)
        if has_values:
            normalized_rows.append(row_values)

    return normalized_rows


def _append_bookkeeping_workspace_entry(cbo: CBO, template_columns: list[str], row_values: dict, row_id: str, created_at: str, workspace_period_key: str | None = None, workspace_document_type_key: str | None = None) -> bool:
    normalized_row_id = str(row_id or '').strip() or uuid.uuid4().hex
    normalized_created_at = str(created_at or '').strip() or datetime.utcnow().isoformat()
    normalized_period_key = _normalize_workspace_period_key(workspace_period_key) or _workspace_period_key_for_value(normalized_created_at) or _workspace_period_key_for_value(datetime.utcnow())
    normalized_document_type_key = _normalize_workspace_document_type_key(workspace_document_type_key)
    existing_entries = _safe_json_list(cbo.bookkeeping_workspace_entries_json)
    for existing_entry in existing_entries:
        if str((existing_entry or {}).get('row_id') or '').strip() == normalized_row_id:
            return False

    existing_entries.append({
        'row_id': normalized_row_id,
        'created_at': normalized_created_at,
        'workspace_document_type_key': normalized_document_type_key,
        'workspace_period_key': normalized_period_key,
        'values': {
            column: str(row_values.get(column) or '').strip()
            for column in template_columns
        },
    })
    cbo.bookkeeping_workspace_entries_json = json.dumps(existing_entries, default=str)
    db.session.add(cbo)
    db.session.commit()
    return True


def _bookkeeping_offline_payload(cbo: CBO, token: str, summary: dict | None = None) -> dict:
    summary = summary or _bookkeeping_summary(cbo)
    app_url = url_for('main.bookkeeping_offline_app', token=token)
    return {
        'ok': True,
        'generated_at': datetime.utcnow().isoformat(),
        'cbo': {
            'id': cbo.id,
            'name': cbo.name,
            'slug': cbo.slug,
        },
        'summary': _serialize_bookkeeping_summary(summary),
        'workspace': _serialize_bookkeeping_workspace(summary),
        'sync': {
            'app_url': app_url,
            'bootstrap_url': url_for('main.bookkeeping_offline_bootstrap', token=token),
            'workspace_url': url_for('main.bookkeeping_offline_sync_workspace', token=token),
            'uploads_url': url_for('main.bookkeeping_offline_sync_upload', token=token),
            'sw_url': url_for('main.bookkeeping_offline_service_worker', token=token),
            'max_files': int(current_app.config.get('BOOKKEEPING_MAX_FILES', 5) or 5),
            'max_pdf_pages': int(current_app.config.get('BOOKKEEPING_MAX_PDF_PAGES', 10) or 10),
            'expires_in_minutes': BOOKKEEPING_OFFLINE_MAX_AGE // 60,
        },
    }


def _refresh_bookkeeping_audits(cbo: CBO) -> None:
    documents = BookkeepingDocument.query.filter_by(cbo_id=cbo.id).all()
    extracted_by_id = {}
    for document in documents:
        extracted = _safe_json(document.extracted_data_json)
        if not extracted:
            continue
        extracted = refine_extracted_bookkeeping_payload(extracted)
        extracted['audit'] = audit_bookkeeping_document(extracted, cbo)
        extracted_by_id[document.id] = extracted
        document.extracted_data_json = json.dumps(extracted)
        document.total_income = float((extracted.get('totals') or {}).get('income') or 0.0)
        document.total_expenses = float((extracted.get('totals') or {}).get('expenses') or 0.0)
        document.net_amount = float((extracted.get('totals') or {}).get('net') or 0.0)
        db.session.add(document)

    for group in _bookkeeping_audit_groups(documents):
        batch_payload = [
            {
                'document_id': document.id,
                'extracted': extracted_by_id.get(document.id, _safe_json(document.extracted_data_json)),
            }
            for document in group
        ]
        batch_results = audit_bookkeeping_group(batch_payload, cbo)
        for document in group:
            extracted = extracted_by_id.get(document.id)
            if not extracted:
                continue
            audit = extracted.get('audit') or {'issues': [], 'flagged_cells': []}
            extra = batch_results.get(document.id) or {'issues': [], 'flagged_cells': []}
            audit['issues'] = _dedupe_audit_issues((audit.get('issues') or []) + (extra.get('issues') or []))
            audit['flagged_cells'] = _dedupe_flagged_cells((audit.get('flagged_cells') or []) + (extra.get('flagged_cells') or []))
            audit['issue_count'] = len(audit['issues'])
            audit['flagged_cell_count'] = len(audit['flagged_cells'])
            extracted['audit'] = audit
            document.extracted_data_json = json.dumps(extracted)
            db.session.add(document)

    db.session.commit()
    for document in documents:
        sync_bookkeeping_document_to_firestore(document)


def _bookkeeping_audit_groups(documents: list[BookkeepingDocument]) -> list[list[BookkeepingDocument]]:
    grouped = []
    by_batch = defaultdict(list)
    unbatched = []

    for document in sorted(documents, key=lambda item: item.created_at or datetime.utcnow()):
        batch_id = (document.upload_batch_id or '').strip()
        if batch_id:
            by_batch[batch_id].append(document)
        else:
            unbatched.append(document)

    grouped.extend([items for items in by_batch.values() if len(items) > 1])

    current_group = []
    for document in unbatched:
        if not current_group:
            current_group = [document]
            continue
        previous = current_group[-1]
        previous_time = previous.created_at or previous.processed_at or datetime.utcnow()
        current_time = document.created_at or document.processed_at or datetime.utcnow()
        if (current_time - previous_time).total_seconds() <= 120:
            current_group.append(document)
        else:
            if len(current_group) > 1:
                grouped.append(current_group)
            current_group = [document]
    if len(current_group) > 1:
        grouped.append(current_group)
    return grouped


def _dedupe_audit_issues(issues: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for issue in issues:
        key = (
            issue.get('code'),
            issue.get('message'),
            issue.get('row_number'),
            tuple(issue.get('columns') or []),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _dedupe_flagged_cells(cells: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for cell in cells:
        key = (
            cell.get('row_number'),
            cell.get('column'),
            cell.get('code'),
            cell.get('message'),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cell)
    return deduped


def _allowed_bookkeeping_upload(filename: str, mime_type: str) -> bool:
    allowed_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.pdf'}
    extension = os.path.splitext(filename.lower())[1]
    return extension in allowed_extensions and (
        mime_type.startswith('image/') or mime_type == 'application/pdf' or mime_type == 'application/octet-stream'
    )


def _guess_mime_type(filename: str) -> str:
    extension = os.path.splitext(filename.lower())[1]
    return {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.heic': 'image/heic',
        '.heif': 'image/heif',
        '.pdf': 'application/pdf',
    }.get(extension, 'application/octet-stream')


def _normalize_upload_filename(uploaded) -> str:
    filename = secure_filename(uploaded.filename or 'bookkeeping-image')
    extension = os.path.splitext(filename)[1].lower()
    if extension:
        return filename
    guessed_extension = {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/heic': '.heic',
        'image/heif': '.heif',
    }.get((uploaded.mimetype or '').lower(), '.jpg')
    return f'{filename}{guessed_extension}'


def _humanize_category(value: str) -> str:
    return str(value or 'other').replace('_', ' ').title()


def _parse_money_value(value) -> float:
    if value in (None, ''):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        cleaned = str(value).replace(',', '').replace('KSh', '').replace('KES', '').strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0


def _parse_nonnegative_money_arg(raw_value: str) -> float | None:
    if raw_value == '':
        return 0.0
    try:
        parsed = float(raw_value)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def _store_funding_audit_source_file(cbo: CBO, filename: str, source_bytes: bytes) -> dict:
    return store_supporting_file(
        cbo_id=cbo.id,
        filename=filename or 'funding-document.pdf',
        file_bytes=source_bytes,
        mime_type=_guess_mime_type(filename or 'funding-document.pdf'),
        object_prefix='funding_audit_uploads',
        local_upload_dir=current_app.config.get('FUNDING_AUDIT_UPLOAD_DIR'),
    )


def _build_map_pin(item: dict, ai_search_active: bool) -> dict:
    cbo = item['cbo']
    profile = item['profile'] or {}
    feedback = item['community_feedback'] or {}
    ai_match = item.get('ai_match') or {}
    impacts = []
    for impact in (profile.get('quantified_impact', []) or [])[:2]:
        impacts.append(' '.join(part for part in [impact.get('metric_value', ''), impact.get('metric_unit', '')] if part).strip())

    focus_areas = []
    for value in (profile.get('focus_areas', cbo.focus_areas) or '').split(','):
        cleaned = value.strip()
        if cleaned:
            focus_areas.append(cleaned)

    ai_overview = item.get('detailed_ai_overview') or profile.get('tagline', '')
    if not ai_overview and ai_search_active and ai_match:
        ai_overview = ai_match.get('qualitative_rationale', '') or '. '.join(ai_match.get('reasons', [])[:2])

    return {
        'slug': cbo.slug,
        'name': profile.get('name', cbo.name),
        'tagline': profile.get('tagline', ''),
        'profile_url': url_for('main.cbo_profile', slug=cbo.slug),
        'location': profile.get('location', cbo.location) or cbo.formatted_address or cbo.geocode_query or 'Location TBD',
        'formatted_address': cbo.formatted_address or cbo.geocode_query or profile.get('location', cbo.location) or 'Location TBD',
        'latitude': cbo.latitude,
        'longitude': cbo.longitude,
        'badge': item['badge'],
        'score': item['score'],
        'ai_score': ai_match.get('score', 0),
        'ai_reasons': ai_match.get('reasons', []),
        'ai_overview': ai_overview,
        'classifications': item['classifications'],
        'focus_areas': focus_areas[:4],
        'impact_highlights': impacts,
        'avg_rating': feedback.get('avg_rating'),
        'responses': feedback.get('responses', 0),
        'total_revenue': round(item['total_revenue']) if item['total_revenue'] else 0,
    }


def _truncate_ai_overview_line(text: str, limit: int = 88) -> str:
    cleaned = ' '.join((text or '').split()).strip()
    if not cleaned:
        return ''
    if len(cleaned) <= limit:
        if cleaned.endswith(('...', '.', '!', '?', '"', "'")):
            return cleaned
        return cleaned + '.'
    shortened = cleaned[:limit].rsplit(' ', 1)[0].rstrip('.,;:')
    return (shortened or cleaned[:limit]).rstrip('.,;:') + '...'


def _build_detailed_ai_overview_bullets(item: dict) -> list[str]:
    cbo = item['cbo']
    profile = item.get('profile') or {}
    feedback = item.get('community_feedback') or {}
    ai_match = item.get('ai_match') or {}

    focus_areas = [segment.strip() for segment in (profile.get('focus_areas', cbo.focus_areas) or '').split(',') if segment.strip()]
    classifications = item.get('classifications') or []
    impacts = []
    for impact in (profile.get('quantified_impact', []) or [])[:2]:
        metric_value = str(impact.get('metric_value', '')).strip()
        metric_unit = str(impact.get('metric_unit', '')).strip()
        description = str(impact.get('description', '')).strip()
        metric_summary = ' '.join(part for part in [metric_value, metric_unit] if part).strip()
        if metric_summary and description:
            impacts.append(f"{metric_summary} {description}".strip())
        elif metric_summary:
            impacts.append(metric_summary)
        elif description:
            impacts.append(description)

    response_count = feedback.get('responses', 0) or 0
    avg_rating = feedback.get('avg_rating')
    recent_quotes = feedback.get('recent_quotes') or []
    total_revenue = round(item.get('total_revenue') or 0)
    revenue_growth = item.get('revenue_growth') or 0
    ai_score = ai_match.get('score', 0)
    reasons = [reason.strip() for reason in (ai_match.get('reasons') or []) if str(reason).strip()]

    if ai_match:
        if ai_score >= 75:
            match_label = 'This looks like a strong overall match for the search.'
        elif ai_score >= 45:
            match_label = 'This looks reasonably aligned, although there are still some gaps against the full request.'
        else:
            match_label = 'This appears to be only a partial fit for the search based on the available signals.'
    else:
        match_label = 'This organisation appears in the results based on its available operational profile.'

    focus_text = ', '.join(focus_areas[:3]) if focus_areas else ', '.join(classifications[:3])
    quote = recent_quotes[0].get('quote', '').strip() if recent_quotes else ''

    bullets = [match_label]

    if reasons:
        bullets.append(f"It stands out because {reasons[0][0].lower() + reasons[0][1:] if len(reasons[0]) > 1 else reasons[0].lower()}.")
    elif focus_text:
        bullets.append(f"Its work appears to center on {focus_text}, which gives it a clearer connection to the search.")
    else:
        bullets.append('There is some baseline mission and operating data available, but the fit is supported by limited descriptive detail.')

    if impacts:
        bullets.append(f"The strongest operating evidence in the profile points to {impacts[0]}.")
    elif total_revenue:
        bullets.append(f"The profile shows about KSh {total_revenue:,.0f} in revenue, which provides at least some operational evidence behind the match.")
    elif revenue_growth:
        bullets.append(f"Recent reporting suggests revenue changed by about {revenue_growth:.1f}%, although broader operating evidence is still limited.")
    elif focus_text:
        bullets.append(f"The available profile mainly emphasizes {focus_text}, with lighter hard evidence elsewhere.")

    if avg_rating is not None:
        bullets.append(f"Community feedback is a useful signal here, with an average rating of {avg_rating}/10 from {response_count} response{'s' if response_count != 1 else ''}.")
    elif response_count:
        bullets.append(f"There are {response_count} community response{'s' if response_count != 1 else ''} on record, although they do not yet translate into a clear average rating.")
    else:
        bullets.append('Community feedback is still thin, so this match depends more on profile and operating data than on direct public sentiment.')

    if quote:
        bullets.append(f"One recent comment captures the on-the-ground perception: \"{quote}\"")
    elif len(reasons) > 1:
        bullets.append(f"A secondary reason this result surfaced is that {reasons[1][0].lower() + reasons[1][1:] if len(reasons[1]) > 1 else reasons[1].lower()}.")

    cleaned_bullets = []
    for bullet in bullets:
        normalized = _truncate_ai_overview_line(bullet, limit=180)
        if normalized:
            cleaned_bullets.append(normalized)

    unique_bullets = []
    seen = set()
    for bullet in cleaned_bullets:
        marker = bullet.lower()
        if marker in seen:
            continue
        seen.add(marker)
        unique_bullets.append(bullet)

    return unique_bullets[:4]


def _flatten_ai_overview_bullets(bullets: list[str]) -> str:
    return ' '.join((bullets or [])[:3])


def _transcript_messages(feedback: CommunityFeedback) -> list:
    try:
        transcript = json.loads(feedback.raw_transcript or '[]')
        return transcript if isinstance(transcript, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


main_bp.app_template_filter('transcript_messages')(_transcript_messages)


# ── Google Forms intake ───────────────────────────────────────────

@main_bp.route('/cbo/<int:cbo_id>/intake-form/create', methods=['POST'])
@login_required
def create_intake_form(cbo_id):
    """Create a Google Form intake form for the given CBO."""
    cbo = db.get_or_404(CBO, cbo_id)
    _require_sms_activity_access(cbo)
    if not google_forms_enabled():
        return jsonify({'error': 'Google Forms is not configured on this server.'}), 503
    if cbo.intake_form_id:
        return jsonify({
            'form_id': cbo.intake_form_id,
            'edit_url': cbo.intake_form_edit_url,
            'responder_url': cbo.intake_form_responder_url,
        })
    try:
        result = create_cbo_intake_form(cbo.name)
    except RuntimeError as exc:
        current_app.logger.error('create_intake_form error for cbo %s: %s', cbo_id, exc)
        return jsonify({'error': str(exc)}), 500
    cbo.intake_form_id = result['form_id']
    cbo.intake_form_edit_url = result['edit_url']
    cbo.intake_form_responder_url = result['responder_url']
    db.session.commit()
    return jsonify(result)


@main_bp.route('/cbo/<int:cbo_id>/intake-form/responses')
@login_required
def intake_form_responses(cbo_id):
    """Return all responses for the CBO's intake form as JSON."""
    cbo = db.get_or_404(CBO, cbo_id)
    _require_sms_activity_access(cbo)
    if not cbo.intake_form_id:
        return jsonify({'error': 'No intake form created for this CBO yet.'}), 404
    try:
        responses = get_form_responses(cbo.intake_form_id)
    except RuntimeError as exc:
        current_app.logger.error('intake_form_responses error for cbo %s: %s', cbo_id, exc)
        return jsonify({'error': str(exc)}), 500
    return jsonify({'form_id': cbo.intake_form_id, 'responses': responses})


@main_bp.route('/admin/community-feedback/<int:cbo_id>/google-form-responses/sync', methods=['POST'])
@login_required
def sync_google_form_responses(cbo_id):
    cbo = db.get_or_404(CBO, cbo_id)
    _require_sms_activity_access(cbo)
    if not cbo.intake_form_id:
        return jsonify({'error': 'No intake form exists for this CBO yet.'}), 404

    try:
        result = _sync_google_form_responses(cbo)
    except RuntimeError as exc:
        current_app.logger.error('sync_google_form_responses error for cbo %s: %s', cbo_id, exc)
        return jsonify({'error': str(exc)}), 500

    return jsonify({
        'ok': not result['failures'],
        **result,
    })
