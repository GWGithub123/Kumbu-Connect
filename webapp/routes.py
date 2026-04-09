""" 
Main application routes — marketplace, profile, sync.
"""
import json
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, abort, jsonify, request, current_app
from flask_login import login_required, current_user
from .community_feedback import handle_inbound_sms, render_sms_response, send_due_checkins, validate_twilio_request
from .firestore_service import get_feedback_document_path
from .maps_service import ensure_cbo_geocoded, get_google_maps_api_key
from .models import db, CBO, CommunitySubscriber, CommunityFeedback
from .kobo_service import fetch_kobo_submissions
from .gemini_service import analyse_kobo_data, interpret_marketplace_query, rank_marketplace_candidates

main_bp = Blueprint('main', __name__)


# ── Landing ───────────────────────────────────────────────────────
@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'funder':
            return redirect(url_for('main.marketplace'))
        return redirect(url_for('main.cbo_dashboard'))
    return redirect(url_for('auth.login'))


# ── Funder marketplace ───────────────────────────────────────────
@main_bp.route('/marketplace')
@login_required
def marketplace():
    if current_user.role != 'funder':
        return redirect(url_for('main.cbo_dashboard'))

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
    if current_user.role != 'cbo' or not current_user.cbo:
        return redirect(url_for('main.marketplace'))
    cbo = current_user.cbo
    profile = _safe_json(cbo.ai_profile_json)
    community_feedback = _community_feedback_summary(cbo)
    return render_template('cbo_profile.html', cbo=cbo, profile=profile, own=True, community_feedback=community_feedback)


# ── Public / funder view of a single CBO profile ─────────────────
@main_bp.route('/cbo/<slug>')
@login_required
def cbo_profile(slug):
    cbo = CBO.query.filter_by(slug=slug).first_or_404()
    # CBOs can only view their own profile
    if current_user.role == 'cbo' and current_user.cbo_id != cbo.id:
        return redirect(url_for('main.cbo_dashboard'))
    profile = _safe_json(cbo.ai_profile_json)
    own = current_user.role == 'cbo' and current_user.cbo_id == cbo.id
    community_feedback = _community_feedback_summary(cbo)
    return render_template('cbo_profile.html', cbo=cbo, profile=profile, own=own, community_feedback=community_feedback)


# ── Sync: pull Kobo data → Gemini analysis → save profile ────────
@main_bp.route('/cbo/<int:cbo_id>/sync', methods=['POST'])
@login_required
def sync_cbo(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)

    # Only the CBO's own users or funders can trigger a sync
    if current_user.role == 'cbo' and current_user.cbo_id != cbo.id:
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

    if current_user.role != 'cbo' or current_user.cbo_id != cbo.id:
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
    if current_user.role == 'cbo' and current_user.cbo_id != cbo.id:
        abort(403)
    return jsonify(_safe_json(cbo.ai_profile_json))


@main_bp.route('/api/cbo/<slug>/community-feedback')
@login_required
def api_community_feedback(slug):
    cbo = CBO.query.filter_by(slug=slug).first_or_404()
    if current_user.role == 'cbo' and current_user.cbo_id != cbo.id:
        abort(403)
    return jsonify(_community_feedback_summary(cbo))


@main_bp.route('/admin/community-feedback')
@login_required
def community_feedback_admin():
    _require_funder()

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
        totals={
            'cbos': len(cbos),
            'subscribers': total_subscribers,
            'responses': total_responses,
        },
    )


@main_bp.route('/admin/community-feedback/run-checkins', methods=['POST'])
@login_required
def run_community_feedback_checkins():
    _require_funder()

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
    _require_feedback_access(cbo)

    subscribers = CommunitySubscriber.query.filter_by(cbo_id=cbo.id).order_by(
        CommunitySubscriber.updated_at.desc()
    ).all()
    feedback_entries = CommunityFeedback.query.filter_by(cbo_id=cbo.id).order_by(
        CommunityFeedback.created_at.desc()
    ).limit(50).all()

    latest_simulation = request.args.get('latest_reply', '').strip()
    latest_message = request.args.get('latest_message', '').strip()
    summary = _community_feedback_summary(cbo)
    return render_template(
        'community_feedback_cbo_detail.html',
        cbo=cbo,
        summary=summary,
        subscribers=subscribers,
        feedback_entries=feedback_entries,
        latest_simulation=latest_simulation,
        latest_message=latest_message,
        twilio_ready=all([
            current_app.config.get('TWILIO_ACCOUNT_SID'),
            current_app.config.get('TWILIO_AUTH_TOKEN'),
            current_app.config.get('TWILIO_PHONE_NUMBER'),
        ]),
        firestore_ready=bool(current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON')),
        firestore_document_path=get_feedback_document_path(cbo),
    )


@main_bp.route('/admin/community-feedback/<int:cbo_id>/settings', methods=['POST'])
@login_required
def update_community_feedback_settings(cbo_id):
    cbo = CBO.query.get_or_404(cbo_id)
    _require_feedback_access(cbo)

    sms_keyword = request.form.get('sms_keyword', '').strip().upper()
    community_prompt = request.form.get('community_prompt', '').strip()
    enabled = request.form.get('community_feedback_enabled') == 'on'

    if sms_keyword:
        duplicate = CBO.query.filter(CBO.id != cbo.id, CBO.sms_keyword == sms_keyword).first()
        if duplicate:
            flash(f'The SMS keyword {sms_keyword} is already in use by {duplicate.name}.', 'danger')
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
    _require_feedback_access(cbo)

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
    _require_feedback_access(cbo)

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

    keyword = cbo.sms_keyword or cbo.cbo_identifier or cbo.slug.upper()
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
        return json.loads(text or '{}')
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
def _require_funder():
    if current_user.role != 'funder':
        abort(403)


def _require_feedback_access(cbo: CBO):
    if current_user.role == 'funder':
        return
    if current_user.role == 'cbo' and current_user.cbo_id == cbo.id:
        return
    abort(403)


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
        'keyword': cbo.sms_keyword or cbo.cbo_identifier or cbo.slug.upper(),
        'prompt': cbo.community_prompt,
        'subscribers': len(subscribers),
        'responses': len(completed_feedback),
        'avg_rating': avg_rating,
        'recent_quotes': recent_quotes,
        'last_firestore_sync_at': completed_feedback[0].firestore_synced_at if completed_feedback else None,
    }


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
