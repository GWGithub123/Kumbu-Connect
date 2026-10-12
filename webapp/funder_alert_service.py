"""Interest highlights, and Gmail alerts when new CBO work matches a saved funder profile.

Search ranking stays independent of this module. Alerts and the marketplace
highlights reel follow the interests a funder saved on their profile.
"""
from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime

from flask import current_app, has_app_context, has_request_context, url_for
from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .funder_personalization_service import (
    infer_cbo_interest_tags,
    interest_label,
    score_cbo_for_funder,
    serialize_funder_profile,
    text_overlaps_funder_interests,
)
from .gmail_service import GmailSendError, send_gmail_message
from .models import (
    CBO,
    CBOActivity,
    CBOActivityComment,
    CBOActivityLike,
    CommunityFeedback,
    FunderInterestAlert,
    FunderProfile,
    User,
    db,
)


_LISTENERS_REGISTERED = False

_KIND_TITLES = {
    'joined': 'New on Kumbu Connect',
    'highlight': 'Profile highlight',
    'story': 'Impact story',
    'flagship': 'Flagship work',
    'narrative': 'Profile update',
    'program': 'Program update',
    'feedback': 'Community story',
}


def register_funder_alert_listeners() -> None:
    global _LISTENERS_REGISTERED
    if _LISTENERS_REGISTERED:
        return
    _LISTENERS_REGISTERED = True
    event.listen(Session, 'before_flush', _capture_profile_changes)
    event.listen(Session, 'after_flush', _capture_new_records)
    event.listen(Session, 'after_commit', _dispatch_after_commit)


def highlight_items_from_profile(profile: dict | None, cbo_name: str) -> list[dict]:
    """Pull the post-like facts already stored on a CBO profile."""
    profile = profile if isinstance(profile, dict) else {}
    name = str(cbo_name or 'This organisation').strip() or 'This organisation'
    items: list[dict] = []

    def add(kind: str, body: str, title: str = '') -> None:
        cleaned = _clean_text(body)
        if len(cleaned) < 12:
            return
        items.append({
            'kind': kind,
            'title': title or _KIND_TITLES.get(kind) or 'Profile update',
            'body': cleaned[:700],
            'source_key': _source_key(kind, cleaned),
        })

    for raw in profile.get('web_highlights') or []:
        add('highlight', raw if isinstance(raw, str) else '')

    story = profile.get('success_story')
    if isinstance(story, dict):
        add('story', story.get('summary') or story.get('quote') or '')
    else:
        add('story', story or '')

    flagship = profile.get('flagship_project') if isinstance(profile.get('flagship_project'), dict) else {}
    add('flagship', flagship.get('summary') or profile.get('flagship_summary') or '')

    narrative = profile.get('narrative_profile') if isinstance(profile.get('narrative_profile'), dict) else {}
    add('narrative', narrative.get('summary') or narrative.get('headline') or '')
    programs = list(narrative.get('program_overview') or []) + list(narrative.get('programs') or [])
    for program in programs:
        if not isinstance(program, dict):
            continue
        label = _clean_text(program.get('name') or program.get('title') or '')
        detail = _clean_text(program.get('description') or program.get('summary') or '')
        if label and detail:
            add('program', f'{label}: {detail}', f'Program update from {name}')
        elif detail:
            add('program', detail, f'Program update from {name}')
        elif label:
            add('program', label, f'Program update from {name}')

    deduped = []
    seen = set()
    for item in items:
        if item['source_key'] in seen:
            continue
        seen.add(item['source_key'])
        deduped.append(item)
    return deduped


def new_profile_highlights(old_raw, new_raw, cbo_name: str) -> list[dict]:
    old_profile = _load_profile(old_raw)
    new_profile = _load_profile(new_raw)
    previous = {item['source_key'] for item in highlight_items_from_profile(old_profile, cbo_name)}
    return [
        item for item in highlight_items_from_profile(new_profile, cbo_name)
        if item['source_key'] not in previous
    ]


def build_new_cbo_email(
    *,
    cbo_name: str,
    match_summary: str,
    interest_labels: list[str],
    profile_url: str,
    contact_url: str,
) -> tuple[str, str]:
    labels = ', '.join(label for label in interest_labels if label) or 'your saved funding interests'
    subject = f'New organisation matches your funding interests: {cbo_name}'
    summary = _clean_text(match_summary) or f'{cbo_name} overlaps {labels}.'
    body = (
        f'{cbo_name} just joined Kumbu Connect and matches {labels}.\n\n'
        f'{summary}\n\n'
        f'View the profile: {profile_url}\n'
        f'Reach out to the organisation: {contact_url}\n\n'
        'This note follows the interests saved on your funder profile. '
        'A search in the marketplace is ranked separately, from the words you type.'
    )
    return subject, body


def build_update_email(
    *,
    cbo_name: str,
    title: str,
    body_text: str,
    interest_labels: list[str],
    profile_url: str,
    contact_url: str,
) -> tuple[str, str]:
    labels = ', '.join(label for label in interest_labels if label) or 'your saved funding interests'
    subject = f'New update from {cbo_name} matches your funding interests'
    excerpt = _clean_text(body_text)[:500]
    body = (
        f'{cbo_name} added something new that lines up with {labels}.\n\n'
        f'{title}\n'
        f'{excerpt}\n\n'
        f'View the profile: {profile_url}\n'
        f'Reach out to the organisation: {contact_url}\n\n'
        'You are receiving this because the update matches interests on your funder profile, '
        'not because of a search you ran.'
    )
    return subject, body


def render_funder_alert_email(
    *,
    eyebrow: str,
    heading: str,
    intro: str,
    detail: str,
    profile_url: str,
    contact_url: str,
    footnote: str,
    location: str = '',
    tagline: str = '',
    programs: list[dict] | None = None,
    facts: list[dict] | None = None,
    photo: dict | None = None,
) -> tuple[str, list[dict]]:
    """HTML funder alert built from the organisation's own profile, not the funder's interest list."""
    images = []
    logo_html = ''
    if photo and photo.get('data'):
        images.append(photo)
        logo_html = (
            '<td width="84" valign="top" style="padding:0 16px 0 0;">'
            f'<img src="cid:{html.escape(photo["cid"])}" alt="" width="72" height="72" '
            'style="display:block;width:72px;height:72px;border:0;border-radius:16px;background:#f6f4f1;">'
            '</td>'
        )
    location_line = (
        f'<p style="margin:10px 0 0;color:#5c6578;font-size:16px;">{html.escape(location)}</p>'
        if location else ''
    )
    tagline_line = (
        f'<p style="margin:18px 0 0;color:#1a2340;font-size:20px;line-height:1.4;">{html.escape(tagline)}</p>'
        if tagline and tagline.lower() not in (detail or '').lower() else ''
    )
    program_rows = []
    for program in (programs or [])[:4]:
        name = _clean_text(program.get('name'))
        if not name:
            continue
        blurb = _clean_text(program.get('detail'))
        blurb_html = f'<br><span style="color:#3d4660;font-weight:400;">{html.escape(blurb)}</span>' if blurb else ''
        program_rows.append(
            '<p style="margin:0 0 16px;font-size:16px;line-height:1.45;color:#1a2340;">'
            f'<strong>{html.escape(name)}</strong>{blurb_html}</p>'
        )
    programs_block = ''
    if program_rows:
        programs_block = (
            '<tr><td style="padding:8px 44px 8px;font-family:Helvetica,Arial,sans-serif;">'
            '<p style="margin:0 0 12px;color:#8a93a6;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">What they run</p>'
            + ''.join(program_rows)
            + '</td></tr>'
        )
    fact_cells = []
    for fact in (facts or [])[:4]:
        label = _clean_text(fact.get('label'))
        value = _clean_text(fact.get('value'))
        if not label or not value:
            continue
        href = str(fact.get('href') or '').strip()
        value_html = (
            f'<a href="{html.escape(href, quote=True)}" style="color:#1a2340;text-decoration:underline;">{html.escape(value)}</a>'
            if href else html.escape(value)
        )
        fact_cells.append(
            '<td style="padding:0 18px 14px 0;vertical-align:top;font-family:Helvetica,Arial,sans-serif;">'
            f'<div style="color:#8a93a6;font-size:12px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">{html.escape(label)}</div>'
            f'<div style="margin-top:4px;color:#1a2340;font-size:15px;line-height:1.4;">{value_html}</div>'
            '</td>'
        )
    facts_block = ''
    if fact_cells:
        facts_block = (
            '<tr><td style="padding:12px 44px 8px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            + ''.join(fact_cells)
            + '</tr></table></td></tr>'
        )
    document = f'''<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f6f4f1;">
  <div style="display:none;max-height:0;overflow:hidden;">{html.escape(detail or intro)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f4f1;padding:16px 8px;">
    <tr><td align="center">
      <table role="presentation" width="680" cellpadding="0" cellspacing="0" style="width:680px;max-width:680px;background:#ffffff;border-radius:24px;overflow:hidden;">
        <tr><td style="padding:36px 44px 8px;font-family:Helvetica,Arial,sans-serif;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            {logo_html}
            <td valign="top">
              <p style="margin:0 0 10px;color:#cd35d3;font-size:13px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">{html.escape(eyebrow)}</p>
              <h1 style="margin:0;font-size:40px;line-height:1.1;color:#1a2340;">{html.escape(heading)}</h1>
              {location_line}
              {tagline_line}
            </td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:18px 44px 8px;font-family:Helvetica,Arial,sans-serif;color:#243049;font-size:17px;line-height:1.55;">
          {''.join(f'<p style="margin:0 0 14px;">{html.escape(paragraph)}</p>' for paragraph in [part.strip() for part in (detail or intro).split('\n') if part.strip()][:3])}
        </td></tr>
        {programs_block}
        {facts_block}
        <tr><td style="padding:18px 44px 36px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="border-radius:999px;background:#1a2340;">
              <a href="{html.escape(profile_url, quote=True)}" style="display:inline-block;padding:14px 22px;font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;">View profile</a>
            </td>
            <td width="12"></td>
            <td style="border-radius:999px;border:1px solid #1a2340;">
              <a href="{html.escape(contact_url, quote=True)}" style="display:inline-block;padding:13px 22px;font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;color:#1a2340;text-decoration:none;">Reach out</a>
            </td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:18px 44px 22px;background:#f6f4f1;font-family:Helvetica,Arial,sans-serif;color:#6b7280;font-size:13px;line-height:1.5;">
          {html.escape(footnote)}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>'''
    return document, images


def _organisation_snapshot(cbo: CBO, profile: dict | None) -> dict:
    """Facts written by or found for the organisation, separate from a funder's saved interests."""
    profile = profile if isinstance(profile, dict) else {}
    research = _load_profile(getattr(cbo, 'web_research_json', '') or '')
    suggestions = research.get('profile_suggestions') if isinstance(research.get('profile_suggestions'), dict) else {}
    narrative = profile.get('narrative_profile') if isinstance(profile.get('narrative_profile'), dict) else {}
    leadership = profile.get('leadership') if isinstance(profile.get('leadership'), dict) else {}

    def pick(*values) -> str:
        for value in values:
            text = _clean_text(value)
            if text and 'public web mentions were found' not in text.lower():
                return text
        return ''

    tagline = pick(profile.get('tagline'), narrative.get('headline'), suggestions.get('tagline'))
    about_candidates = [
        pick(profile.get('origin_story'), profile.get('history'), suggestions.get('history')),
        pick(research.get('summary')),
    ]
    highlights = profile.get('web_highlights') or []
    if highlights:
        about_candidates.append(pick(highlights[0] if isinstance(highlights[0], str) else ''))
    about_candidates.append(pick(narrative.get('summary')))
    about = ''
    for candidate in about_candidates:
        if candidate and candidate != tagline:
            about = candidate
            break
    raw_programs = list(narrative.get('programs') or []) + list(narrative.get('program_overview') or [])
    if not raw_programs and isinstance(suggestions.get('programs'), list):
        raw_programs = suggestions.get('programs') or []
    programs = []
    image_url = ''
    for program in raw_programs:
        if not isinstance(program, dict):
            continue
        name = pick(program.get('name'), program.get('title'))
        detail = pick(program.get('description'), program.get('summary'))
        if name:
            programs.append({'name': name, 'detail': detail})
        if not image_url:
            image_url = str(program.get('image_url') or '').strip()
        if len(programs) >= 4 and image_url:
            break
    location = pick(profile.get('location'), getattr(cbo, 'location', ''), suggestions.get('location'))
    founded = pick(profile.get('founded_year'), getattr(cbo, 'founded_year', ''), suggestions.get('founded_year'))
    leader = pick(
        leadership.get('program_director'),
        getattr(cbo, 'program_director', ''),
        suggestions.get('founder'),
    )
    website = pick(profile.get('website'), suggestions.get('website'))
    focus = pick(profile.get('focus_areas'), getattr(cbo, 'focus_areas', ''), suggestions.get('focus_areas'))
    facts = []
    if founded:
        facts.append({'label': 'Founded', 'value': founded})
    if leader:
        facts.append({'label': 'Leadership', 'value': leader})
    if focus:
        facts.append({'label': 'Focus', 'value': focus})
    if website:
        facts.append({'label': 'Website', 'value': website.replace('https://', '').replace('http://', '').strip('/'), 'href': website if website.startswith('http') else f'https://{website}'})
    return {
        'tagline': tagline,
        'about': about,
        'location': location,
        'programs': programs[:4],
        'facts': facts[:4],
        'image_url': image_url if image_url.startswith('http') else '',
    }


def organisation_logo_urls(research: dict | None) -> list[str]:
    """Logo addresses already found on the organisation's public site."""
    research = research if isinstance(research, dict) else {}
    ranked = []
    for item in research.get('profile_images') or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get('url') or '').strip()
        if not url.startswith('http'):
            continue
        blob = f"{url} {item.get('alt') or ''}".lower()
        ranked.append((0 if 'logo' in blob else 1, url))
    ranked.sort()
    return [url for _, url in ranked]


def _svg_embedded_raster(data: bytes) -> bytes | None:
    text = data.decode('utf-8', errors='ignore')
    match = re.search(r'data:image/(?:png|jpe?g|webp);base64,([A-Za-z0-9+/=\s]+)', text)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1))
    except (ValueError, TypeError):
        return None


def _fit_logo_png(data: bytes) -> bytes | None:
    try:
        from io import BytesIO
        from PIL import Image
        image = Image.open(BytesIO(data))
        image = image.convert('RGBA')
        image.thumbnail((160, 160))
        out = BytesIO()
        image.save(out, format='PNG')
    except Exception:
        return None
    payload = out.getvalue()
    return payload or None


def _organisation_logo(cbo: CBO) -> dict | None:
    """Small mark from public-web research, never a program photograph."""
    research = _load_profile(getattr(cbo, 'web_research_json', '') or '')
    for url in organisation_logo_urls(research):
        photo = _remote_photo(url)
        if photo is None and url.lower().split('?', 1)[0].endswith('.svg'):
            raw = _download_logo_bytes(url)
            if raw:
                embedded = _svg_embedded_raster(raw)
                fitted = _fit_logo_png(embedded) if embedded else None
                if fitted:
                    photo = {'cid': 'organisation', 'data': fitted, 'mime': 'image/png', 'filename': 'logo.png'}
        if photo is None:
            continue
        fitted = _fit_logo_png(photo['data'])
        if fitted:
            photo['data'] = fitted
            photo['mime'] = 'image/png'
            photo['filename'] = 'logo.png'
        photo['cid'] = 'organisation'
        return photo
    return None


def _download_logo_bytes(url: str) -> bytes | None:
    if not url.startswith('http'):
        return None
    try:
        from urllib.request import Request, urlopen
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(request, timeout=8) as response:
            return response.read(1_500_000)
    except Exception:
        return None


def _remote_photo(url: str) -> dict | None:
    if not url.startswith('http'):
        return None
    try:
        from urllib.request import Request, urlopen
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(request, timeout=8) as response:
            mime = str(response.headers.get('Content-Type') or 'image/jpeg').split(';')[0].strip()
            data = response.read(1_500_000)
    except Exception:
        return None
    if not data or not mime.startswith('image/') or 'svg' in mime:
        return None
    return {'cid': 'organisation', 'data': data, 'mime': mime, 'filename': 'organisation'}


def _organisation_photo(profile: dict | None) -> dict | None:
    image = profile.get('profile_image') if isinstance(profile, dict) else None
    if not isinstance(image, dict):
        return None
    mime = str(image.get('mime_type') or 'image/jpeg')
    if not mime.startswith('image/') or 'svg' in mime:
        return None
    if not str(image.get('stored_path') or image.get('storage_object_path') or '').strip():
        return None
    try:
        from .firebase_storage_service import get_stored_file_bytes
        data, mime = get_stored_file_bytes(
            storage_backend=image.get('storage_backend') or 'local',
            stored_path=image.get('stored_path') or '',
            mime_type=mime,
            storage_bucket=image.get('storage_bucket') or '',
            storage_object_path=image.get('storage_object_path') or '',
        )
    except Exception:
        return None
    if not data:
        return None
    return {'cid': 'organisation', 'data': data, 'mime': mime, 'filename': 'organisation'}


def ensure_historical_highlights(cbos: list, profiles_by_id: dict | None = None) -> int:
    """Backfill the reel from profiles that already exist, without sending email."""
    profiles_by_id = profiles_by_id or {}
    if not cbos:
        return 0
    cbo_ids = [cbo.id for cbo in cbos if getattr(cbo, 'id', None)]
    if not cbo_ids:
        return 0
    already = {
        row[0]
        for row in db.session.query(CBOActivity.cbo_id).filter(CBOActivity.cbo_id.in_(cbo_ids)).distinct()
    }
    created = 0
    for cbo in cbos:
        if not getattr(cbo, 'id', None) or cbo.id in already:
            continue
        profile = profiles_by_id.get(cbo.id)
        if profile is None:
            profile = _load_profile(getattr(cbo, 'ai_profile_json', '') or '')
        when = getattr(cbo, 'updated_at', None) or getattr(cbo, 'created_at', None) or datetime.utcnow()
        items = [{
            'kind': 'joined',
            'title': _KIND_TITLES['joined'],
            'body': _joined_body(cbo, profile),
            'source_key': 'joined',
        }]
        items.extend(highlight_items_from_profile(profile, cbo.name))
        for item in items:
            if _insert_activity(cbo.id, item, alert_policy='historical', created_at=when):
                created += 1
    if created:
        db.session.commit()
    return created


def photos_for_update(profile: dict | None, text: str, limit: int = 3) -> tuple[list[dict], list[dict]]:
    """Recent program photos that belong with a profile update.

    Photos whose program name appears in the update come first. Otherwise the
    newest photos on the organisation are used, so a text update still shows
    a picture when one exists.
    """
    photos = _profile_photos(profile)
    if not photos:
        return [], []
    haystack = _clean_text(text).lower()
    matched = [
        photo for photo in photos
        if photo['label'] and len(photo['label']) > 2 and photo['label'].lower() in haystack
    ]
    matched.sort(key=lambda photo: photo['created_at'], reverse=True)
    recent = sorted(photos, key=lambda photo: photo['created_at'], reverse=True)

    def pack(pool: list[dict]) -> list[dict]:
        chosen = []
        seen = set()
        for photo in pool:
            if photo['id'] in seen:
                continue
            seen.add(photo['id'])
            chosen.append({
                'id': photo['id'],
                'alt': photo['label'] or 'Organisation photo',
            })
            if len(chosen) >= limit:
                break
        return chosen

    return pack(matched), pack(recent)


def _assign_highlight_photos(cards: list[dict], used_photo_ids: set[str]) -> None:
    """Put program photos on the update that names them, then fill the rest."""

    def take(card: dict, pool: list[dict], cap: int) -> None:
        for photo in pool:
            if len(card['photos']) >= cap or photo['id'] in used_photo_ids:
                continue
            used_photo_ids.add(photo['id'])
            card['photos'].append({
                'url': url_for('main.cbo_program_photo', slug=card['cbo_slug'], photo_id=photo['id']),
                'alt': photo['alt'],
            })

    for card in cards:
        take(card, card.pop('_matched_photos', []), 3)
    for card in cards:
        if card['photos']:
            card.pop('_recent_photos', None)
            continue
        take(card, card.pop('_recent_photos', []), 2)
    for card in cards:
        card.pop('_matched_photos', None)
        card.pop('_recent_photos', None)


_FEED_POSTS_PER_CBO = 2
_PROFILE_FEED_KINDS = frozenset({'joined', 'narrative', 'highlight', 'flagship', 'story'})


def _reaction_heat(item: dict) -> int:
    return int(item.get('like_count') or 0) + int(item.get('comment_count') or 0)


def _reaction_totals(activity_ids: list[int]) -> dict[int, tuple[int, int]]:
    ids = [int(activity_id) for activity_id in activity_ids if activity_id]
    if not ids:
        return {}
    like_counts = dict(
        db.session.query(CBOActivityLike.activity_id, func.count(CBOActivityLike.id))
        .filter(CBOActivityLike.activity_id.in_(ids))
        .group_by(CBOActivityLike.activity_id)
        .all()
    )
    comment_counts = dict(
        db.session.query(CBOActivityComment.activity_id, func.count(CBOActivityComment.id))
        .filter(CBOActivityComment.activity_id.in_(ids))
        .group_by(CBOActivityComment.activity_id)
        .all()
    )
    return {
        activity_id: (int(like_counts.get(activity_id) or 0), int(comment_counts.get(activity_id) or 0))
        for activity_id in ids
    }


def select_feed_cards(candidates: list[dict], limit: int, per_cbo: int = _FEED_POSTS_PER_CBO) -> list[dict]:
    """Keep one or two posts per organisation, and lead with a mix of organisations.

    A profile writeup leads that organisation's posts when one matched, so a
    burst of program cards cannot hide the organisation itself. A post other
    organisations have liked or commented on leads instead, so funders see
    that response. The next post is the newest remaining update. Other
    organisations get a turn before a second post is added, and a repeated
    writeup is skipped.
    """
    queues: dict = {}
    order = []
    for item in candidates:
        cbo_id = item.get('cbo_id')
        if cbo_id not in queues:
            queues[cbo_id] = {'profile': [], 'other': []}
            order.append(cbo_id)
        bucket = 'profile' if item.get('kind') in _PROFILE_FEED_KINDS else 'other'
        queues[cbo_id][bucket].append(item)

    ranked: dict = {}
    for cbo_id, buckets in queues.items():
        if buckets['profile']:
            preferred = [buckets['profile'][0], *buckets['other'], *buckets['profile'][1:]]
        else:
            preferred = list(buckets['other'])
        engaged = sorted(
            (item for item in preferred if _reaction_heat(item)),
            key=_reaction_heat,
            reverse=True,
        )
        if engaged:
            engaged_ids = {id(item) for item in engaged}
            preferred = engaged + [item for item in preferred if id(item) not in engaged_ids]
        kept = []
        used = set()
        for item in preferred:
            fingerprint = _body_fingerprint(item.get('body'))
            if fingerprint and fingerprint in used:
                continue
            if fingerprint:
                used.add(fingerprint)
            kept.append(item)
        ranked[cbo_id] = kept

    selected = []
    for slot in range(max(1, per_cbo)):
        if len(selected) >= limit:
            break
        for cbo_id in order:
            if len(selected) >= limit:
                break
            posts = ranked.get(cbo_id) or []
            if slot >= len(posts):
                continue
            selected.append(posts[slot])
    return _include_reacted_posts(selected, ranked, limit, per_cbo)


def _include_reacted_posts(selected: list[dict], ranked: dict, limit: int, per_cbo: int) -> list[dict]:
    """Keep a second liked or commented post visible when the first pass is already full."""
    selected_ids = {id(item) for item in selected}
    counts: dict = {}
    for item in selected:
        counts[item.get('cbo_id')] = counts.get(item.get('cbo_id'), 0) + 1
    extras = []
    for posts in ranked.values():
        cbo_id = posts[0].get('cbo_id') if posts else None
        for item in posts:
            if id(item) in selected_ids or _reaction_heat(item) <= 0:
                continue
            if counts.get(cbo_id, 0) >= per_cbo:
                break
            extras.append(item)
            selected_ids.add(id(item))
            counts[cbo_id] = counts.get(cbo_id, 0) + 1
    if not extras:
        return selected
    pending: dict = {}
    for item in extras:
        pending.setdefault(item.get('cbo_id'), []).append(item)
    merged = []
    inserted = set()
    for item in selected:
        merged.append(item)
        for extra in pending.get(item.get('cbo_id')) or []:
            if id(extra) in inserted:
                continue
            merged.append(extra)
            inserted.add(id(extra))
        pending[item.get('cbo_id')] = []
    for leftover in pending.values():
        merged.extend(leftover)
    while len(merged) > limit:
        drop_at = next((
            index for index in range(len(merged) - 1, -1, -1)
            if _reaction_heat(merged[index]) <= 0
        ), None)
        if drop_at is None:
            break
        del merged[drop_at]
    return merged[:limit]


def _body_fingerprint(body) -> str:
    compact = re.sub(r'\s+', ' ', _clean_text(body).lower())
    return compact[:180]


def build_interest_highlights(funder_profile: FunderProfile | None, limit: int = 8) -> list[dict]:
    """Recent posts from organisations or updates that fit the saved funder profile."""
    serialized = serialize_funder_profile(funder_profile)
    if not serialized.get('interest_areas') and not (serialized.get('interest_brief') or {}).get('themes'):
        return []
    rows = (
        CBOActivity.query
        .order_by(CBOActivity.created_at.desc(), CBOActivity.id.desc())
        .limit(160)
        .all()
    )
    candidates = []
    seen_cbos: dict[int, CBO] = {}
    used_photo_ids: set[str] = set()
    for row in rows:
        cbo = seen_cbos.get(row.cbo_id)
        if cbo is None:
            cbo = row.cbo or db.session.get(CBO, row.cbo_id)
            if cbo is None:
                continue
            seen_cbos[row.cbo_id] = cbo
        profile = _load_profile(cbo.ai_profile_json)
        if row.kind == 'joined':
            match = score_cbo_for_funder(cbo, funder_profile, profile)
            if not match.get('is_match'):
                continue
            labels = match.get('interest_labels') or []
        else:
            overlap = text_overlaps_funder_interests(f'{row.title} {row.body}', funder_profile)
            if not overlap.get('is_match'):
                continue
            labels = overlap.get('interest_labels') or []
            if overlap.get('theme_hits') and 'Theme match' not in labels:
                labels = list(labels) + ['Theme match']
        title = row.title or _KIND_TITLES.get(row.kind) or 'Update'
        matched_photos, recent_photos = photos_for_update(profile, f'{title} {row.body}', limit=3)
        candidates.append({
            'cbo_id': cbo.id,
            'cbo_name': str((profile or {}).get('name') or cbo.name or 'Organisation').strip(),
            'cbo_slug': cbo.slug,
            'title': title,
            'body': _clean_text(row.body)[:320],
            'kind': row.kind,
            'when_label': row.created_at.strftime('%b %d, %Y') if row.created_at else '',
            'interest_labels': labels[:3],
            'activity_id': row.id,
            'profile_url': url_for('main.cbo_profile', slug=cbo.slug),
            'photos': [],
            '_matched_photos': matched_photos,
            '_recent_photos': recent_photos,
        })
    totals = _reaction_totals([item.get('activity_id') for item in candidates])
    for item in candidates:
        likes, comments = totals.get(int(item.get('activity_id') or 0), (0, 0))
        item['like_count'] = likes
        item['comment_count'] = comments
    cards = select_feed_cards(candidates, limit)
    _assign_highlight_photos(cards, used_photo_ids)
    return cards


_REGION_STOPWORDS = {
    'kenya', 'county', 'region', 'east', 'west', 'north', 'south', 'the', 'and',
}


def region_tokens(text: str) -> set[str]:
    return {
        token for token in re.split(r'[^a-z0-9]+', str(text or '').lower())
        if len(token) > 3 and token not in _REGION_STOPWORDS
    }


def classify_peer_post(viewer_tokens: set[str], viewer_tags: set[str], other_tokens: set[str], other_tags: set[str]) -> str:
    """Place another organisation in the viewer's region lane or the similar-work lane."""
    if viewer_tokens and other_tokens and viewer_tokens & other_tokens:
        return 'region'
    if viewer_tags and other_tags and viewer_tags & other_tags:
        return 'similar'
    return ''


def attach_feed_engagement(cards: list[dict], viewer=None) -> None:
    """Add like and comment details. CBOs can respond; funders see the responses."""
    ids = [int(card['activity_id']) for card in cards if card.get('activity_id')]
    for card in cards:
        card.setdefault('like_count', 0)
        card.setdefault('comment_count', 0)
        card.setdefault('liked', False)
        card.setdefault('comments', [])
        card.setdefault('can_react', False)
    if not ids:
        return
    like_rows = CBOActivityLike.query.filter(CBOActivityLike.activity_id.in_(ids)).all()
    comment_rows = (
        CBOActivityComment.query
        .filter(CBOActivityComment.activity_id.in_(ids))
        .order_by(CBOActivityComment.created_at.asc(), CBOActivityComment.id.asc())
        .all()
    )
    likes_by_activity = {}
    for like in like_rows:
        likes_by_activity.setdefault(like.activity_id, []).append(like)
    comments_by_activity = {}
    for comment in comment_rows:
        comments_by_activity.setdefault(comment.activity_id, []).append(comment)
    viewer_id = getattr(viewer, 'id', None)
    viewer_cbo_id = getattr(viewer, 'cbo_id', None)
    viewer_is_cbo = bool(viewer is not None and viewer.has_role('cbo') and viewer_cbo_id)
    for card in cards:
        activity_id = card.get('activity_id')
        if not activity_id:
            continue
        likes = likes_by_activity.get(int(activity_id), [])
        comments = comments_by_activity.get(int(activity_id), [])
        card['like_count'] = len(likes)
        card['liked'] = any(like.user_id == viewer_id for like in likes)
        card['comment_count'] = len(comments)
        card['comments'] = [_serialize_comment(comment) for comment in comments[-3:]]
        card['can_react'] = bool(viewer_is_cbo and card.get('cbo_id') != viewer_cbo_id)


_PEER_QUERY_STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'near', 'that', 'this', 'cbo', 'cbos',
    'organisation', 'organisations', 'organization', 'organizations',
}


def peer_search_is_direct(query: str, *parts: str) -> bool:
    """Whether a typed search names this organisation, place, or focus in so many words."""
    tokens = [
        token for token in re.split(r'[^a-z0-9]+', str(query or '').lower())
        if len(token) > 2 and token not in _PEER_QUERY_STOPWORDS
    ]
    if not tokens:
        return False
    blob = ' '.join(str(part or '') for part in parts).lower()
    return any(token in blob for token in tokens)


def posts_for_organisations(cbos: list, viewer=None, per_cbo: int = 2, limit: int = 8) -> list[dict]:
    """Recent posts from a ranked set of organisations, for a peer-feed search."""
    cards = []
    for cbo in cbos:
        if len(cards) >= limit:
            break
        rows = (
            CBOActivity.query
            .filter(CBOActivity.cbo_id == cbo.id)
            .order_by(CBOActivity.created_at.desc(), CBOActivity.id.desc())
            .limit(per_cbo)
            .all()
        )
        profile = _load_profile(getattr(cbo, 'ai_profile_json', '') or '')
        for row in rows:
            cards.append(_public_activity_card(row, cbo, profile))
            if len(cards) >= limit:
                break
    attach_feed_engagement(cards, viewer)
    return cards


def build_cbo_peer_feed(cbo: CBO, viewer=None) -> dict:
    """Regional updates, similar work elsewhere, and posts other CBOs are responding to."""
    own_profile = _load_profile(getattr(cbo, 'ai_profile_json', '') or '')
    own_tokens = region_tokens(_region_text(cbo, own_profile))
    own_tags = set(infer_cbo_interest_tags(cbo, own_profile))
    rows = (
        CBOActivity.query
        .filter(CBOActivity.cbo_id != cbo.id)
        .order_by(CBOActivity.created_at.desc(), CBOActivity.id.desc())
        .limit(240)
        .all()
    )
    regional = []
    similar = []
    regional_counts = {}
    similar_counts = {}
    cards = []
    seen_cards = {}
    for row in rows:
        other = row.cbo or db.session.get(CBO, row.cbo_id)
        if other is None:
            continue
        profile = _load_profile(other.ai_profile_json)
        lane = classify_peer_post(
            own_tokens,
            own_tags,
            region_tokens(_region_text(other, profile)),
            set(infer_cbo_interest_tags(other, profile)),
        )
        if lane == 'region' and regional_counts.get(other.id, 0) < 2:
            card = seen_cards.get(row.id) or _public_activity_card(row, other, profile)
            seen_cards[row.id] = card
            regional.append(card)
            regional_counts[other.id] = regional_counts.get(other.id, 0) + 1
            cards.append(card)
        elif lane == 'similar' and similar_counts.get(other.id, 0) < 2:
            card = seen_cards.get(row.id) or _public_activity_card(row, other, profile)
            seen_cards[row.id] = card
            similar.append(card)
            similar_counts[other.id] = similar_counts.get(other.id, 0) + 1
            cards.append(card)
    attach_feed_engagement(cards, viewer)
    popular_pool = [card for card in cards if card.get('like_count') or card.get('comment_count')]
    popular = sorted(
        popular_pool,
        key=lambda card: ((card.get('like_count') or 0) + (card.get('comment_count') or 0), card.get('activity_id') or 0),
        reverse=True,
    )[:6]
    return {
        'regional': regional[:6],
        'similar': similar[:6],
        'popular': popular,
    }


def _region_text(cbo: CBO, profile: dict | None) -> str:
    profile = profile if isinstance(profile, dict) else {}
    return ' '.join(
        part for part in (
            str(getattr(cbo, 'county_region', '') or ''),
            str(profile.get('location') or ''),
            str(getattr(cbo, 'location', '') or ''),
        ) if part
    )


def _public_activity_card(row: CBOActivity, cbo: CBO, profile: dict | None) -> dict:
    title = row.title or _KIND_TITLES.get(row.kind) or 'Update'
    matched_photos, _recent = photos_for_update(profile, f'{title} {row.body}', limit=1)
    photos = [
        {
            'url': url_for('main.cbo_program_photo', slug=cbo.slug, photo_id=photo['id']),
            'alt': photo['alt'],
        }
        for photo in matched_photos[:1]
    ]
    return {
        'activity_id': row.id,
        'cbo_id': cbo.id,
        'cbo_name': str((profile or {}).get('name') or cbo.name or 'Organisation').strip(),
        'cbo_slug': cbo.slug,
        'title': title,
        'body': _clean_text(row.body)[:320],
        'kind': row.kind,
        'when_label': row.created_at.strftime('%b %d, %Y') if row.created_at else '',
        'interest_labels': [],
        'profile_url': url_for('main.cbo_profile', slug=cbo.slug),
        'photos': photos,
        'can_save': False,
        'saved': False,
        'save_url': '',
    }


def _serialize_comment(comment: CBOActivityComment) -> dict:
    user = comment.user or db.session.get(User, comment.user_id)
    organisation = ''
    if user is not None and user.cbo_id:
        cbo = user.cbo or db.session.get(CBO, user.cbo_id)
        organisation = str(getattr(cbo, 'name', '') or '')
    author = str(getattr(user, 'display_name', '') or '').strip() or organisation or 'Organisation'
    if organisation and organisation.lower() not in author.lower():
        author = f'{author} · {organisation}'
    return {
        'id': comment.id,
        'author': author,
        'body': _clean_text(comment.body)[:500],
        'when_label': comment.created_at.strftime('%b %d, %Y') if comment.created_at else '',
    }


def _capture_profile_changes(session, flush_context, instances) -> None:
    del flush_context, instances
    updates = session.info.setdefault('funder_alert_updates', [])
    for obj in list(session.dirty):
        if not isinstance(obj, CBO) or not obj.id:
            continue
        history = _attribute_history(obj, 'ai_profile_json')
        if history is None or not history.has_changes():
            continue
        old_raw = history.deleted[0] if history.deleted else ''
        updates.append({
            'cbo_id': obj.id,
            'cbo_name': obj.name,
            'old': old_raw,
            'new': obj.ai_profile_json,
        })


def _capture_new_records(session, flush_context) -> None:
    del flush_context
    new_ids = session.info.setdefault('funder_alert_new_ids', [])
    feedbacks = session.info.setdefault('funder_alert_feedback', [])
    seen_feedback = {item.get('feedback_id') for item in feedbacks}
    for obj in list(session.new):
        if isinstance(obj, CBO) and obj.id and obj.id not in new_ids:
            new_ids.append(obj.id)
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, CommunityFeedback) or not obj.id or obj.id in seen_feedback:
            continue
        anecdote = _clean_text(obj.anecdote)
        if obj.status != 'completed' or len(anecdote) < 12:
            continue
        feedbacks.append({
            'feedback_id': obj.id,
            'cbo_id': obj.cbo_id,
            'anecdote': anecdote,
        })
        seen_feedback.add(obj.id)


def _dispatch_after_commit(session) -> None:
    """Queue alert delivery until the commit that noticed the change has fully closed.

    SQLAlchemy still has that transaction in the committed state inside after_commit,
    so reading or writing here raises and fails the request that saved the change.
    """
    if session.info.get('_funder_alert_dispatching'):
        return
    new_ids = session.info.pop('funder_alert_new_ids', None)
    updates = session.info.pop('funder_alert_updates', None)
    feedbacks = session.info.pop('funder_alert_feedback', None)
    if not new_ids and not updates and not feedbacks:
        return
    if not has_app_context():
        return
    payload = (list(new_ids or []), list(updates or []), list(feedbacks or []))

    def _deliver_after_close(sess, transaction) -> None:
        if getattr(transaction, 'parent', None) is not None:
            event.listen(sess, 'after_transaction_end', _deliver_after_close, once=True)
            return
        if sess.info.get('_funder_alert_dispatching'):
            return
        sess.info['_funder_alert_dispatching'] = True
        try:
            _deliver_alerts(*payload)
        except Exception:
            current_app.logger.exception('Funder interest alerts could not be delivered')
            db.session.rollback()
        finally:
            sess.info.pop('_funder_alert_dispatching', None)

    event.listen(session, 'after_transaction_end', _deliver_after_close, once=True)


def _deliver_alerts(new_ids: list[int], updates: list[dict], feedbacks: list[dict]) -> None:
    new_id_set = {int(cbo_id) for cbo_id in new_ids if cbo_id}
    pending_sends: list[CBOActivity] = []

    for cbo_id in new_id_set:
        cbo = db.session.get(CBO, cbo_id)
        if cbo is None:
            continue
        profile = _load_profile(cbo.ai_profile_json)
        joined = {
            'kind': 'joined',
            'title': _KIND_TITLES['joined'],
            'body': _joined_body(cbo, profile),
            'source_key': 'joined',
        }
        row = _insert_activity(cbo.id, joined, alert_policy='send')
        if row is not None:
            pending_sends.append(row)
        for item in highlight_items_from_profile(profile, cbo.name):
            _insert_activity(cbo.id, item, alert_policy='historical')

    for update in updates:
        cbo_id = int(update.get('cbo_id') or 0)
        if not cbo_id or cbo_id in new_id_set:
            continue
        for item in new_profile_highlights(update.get('old'), update.get('new'), update.get('cbo_name') or ''):
            row = _insert_activity(cbo_id, item, alert_policy='send')
            if row is not None:
                pending_sends.append(row)

    for feedback in feedbacks:
        cbo_id = int(feedback.get('cbo_id') or 0)
        anecdote = _clean_text(feedback.get('anecdote'))
        if not cbo_id or len(anecdote) < 12:
            continue
        item = {
            'kind': 'feedback',
            'title': _KIND_TITLES['feedback'],
            'body': anecdote[:700],
            'source_key': _source_key('feedback', f"{int(feedback.get('feedback_id') or 0)} {anecdote}"),
        }
        policy = 'historical' if cbo_id in new_id_set else 'send'
        row = _insert_activity(cbo_id, item, alert_policy=policy)
        if row is not None and policy == 'send':
            pending_sends.append(row)

    intro_sent: set[tuple[int, int]] = set()
    for activity in pending_sends:
        _email_matching_funders(activity, intro_sent=intro_sent)
    db.session.commit()


def resend_failed_interest_alerts() -> int:
    """Send one introduction email for each organisation whose earlier alerts failed."""
    failed_rows = (
        FunderInterestAlert.query
        .filter(FunderInterestAlert.status == 'failed')
        .order_by(FunderInterestAlert.id.asc())
        .all()
    )
    if not failed_rows:
        return 0
    intro_sent: set[tuple[int, int]] = set()
    for alert in failed_rows:
        activity = alert.activity or db.session.get(CBOActivity, alert.activity_id)
        if activity is None:
            continue
        _email_matching_funders(activity, intro_sent=intro_sent)
    sent_pairs = {
        (row.user_id, row.cbo_id)
        for row in FunderInterestAlert.query.filter(FunderInterestAlert.status == 'sent').all()
    }
    for alert in failed_rows:
        if alert.status != 'failed':
            continue
        if (alert.user_id, alert.cbo_id) in sent_pairs:
            alert.status = 'skipped'
            alert.error = 'Included in the new-organisation email.'
    db.session.commit()
    return len(intro_sent)


def _email_matching_funders(activity: CBOActivity, intro_sent: set[tuple[int, int]] | None = None) -> None:
    cbo = activity.cbo or db.session.get(CBO, activity.cbo_id)
    if cbo is None:
        return
    if intro_sent is None:
        intro_sent = set()
    profile = _load_profile(cbo.ai_profile_json)
    funders = (
        User.query
        .filter(User.role.in_([User.ROLE_FUNDER, User.ROLE_FUNDER_CBO]))
        .filter(User.account_status == 'active')
        .all()
    )
    gmail_blocked = False
    for user in funders:
        if user.cbo_id and user.cbo_id == cbo.id:
            continue
        pair = (user.id, cbo.id)
        if pair in intro_sent:
            continue
        funder_profile = user.funder_profile or FunderProfile.query.filter_by(user_id=user.id).first()
        recipient = _funder_alert_email(user, funder_profile)
        if not recipient:
            continue
        profile_url, contact_url = _profile_links(cbo.slug)
        prior_sent = FunderInterestAlert.query.filter_by(
            user_id=user.id,
            cbo_id=cbo.id,
            status='sent',
        ).first()
        introduced = False
        first_notice = prior_sent is None
        if prior_sent is None:
            match = score_cbo_for_funder(cbo, funder_profile, profile)
            if not match.get('is_match'):
                if activity.kind == 'joined':
                    continue
            else:
                matched_labels = [interest_label(item) for item in (match.get('overlap') or [])]
                matched_labels = matched_labels or list(match.get('interest_labels') or [])[:6]
                subject, body = build_new_cbo_email(
                    cbo_name=cbo.name,
                    match_summary=match.get('summary') or '',
                    interest_labels=matched_labels,
                    profile_url=profile_url,
                    contact_url=contact_url,
                )
                alert_eyebrow = 'New organisation'
                alert_intro = f'{cbo.name} just joined Kumbu Connect.'
                alert_detail = ''
                alert_footnote = (
                    'This note follows the interests saved on your funder profile. '
                    'A search in the marketplace is ranked separately, from the words you type.'
                )
                introduced = True
        if not introduced:
            if activity.kind == 'joined':
                continue
            overlap = text_overlaps_funder_interests(f'{activity.title} {activity.body}', funder_profile)
            if not overlap.get('is_match'):
                continue
            update_labels = list(overlap.get('interest_labels') or [])
            subject, body = build_update_email(
                cbo_name=cbo.name,
                title=activity.title or 'Profile update',
                body_text=activity.body or '',
                interest_labels=update_labels,
                profile_url=profile_url,
                contact_url=contact_url,
            )
            alert_eyebrow = 'New update'
            alert_intro = activity.title or 'Profile update'
            alert_detail = activity.body or ''
            alert_footnote = (
                'You are receiving this because the update matches interests on your funder profile, '
                'not because of a search you ran.'
            )
        if gmail_blocked:
            _record_unsent(user, cbo, activity, 'Gmail send is not authorized for the saved Google token.')
            continue
        snapshot = _organisation_snapshot(cbo, profile)
        detail = alert_detail or snapshot.get('about') or alert_intro
        if alert_eyebrow == 'New update' and snapshot.get('about') and snapshot['about'] not in detail:
            detail = f'{detail}\n\n{snapshot["about"]}'
        photo = _organisation_logo(cbo)
        html_body, inline_images = render_funder_alert_email(
            eyebrow=alert_eyebrow,
            heading=cbo.name,
            intro=alert_intro,
            detail=detail,
            profile_url=profile_url,
            contact_url=contact_url,
            footnote=alert_footnote,
            location=snapshot.get('location') or '',
            tagline=snapshot.get('tagline') or '',
            programs=snapshot.get('programs') or [],
            facts=snapshot.get('facts') or [],
            photo=photo,
        )
        error = _send_or_record(
            user, cbo, activity, subject, body, recipient,
            html_body=html_body,
            inline_images=inline_images,
        )
        if first_notice and not error:
            intro_sent.add(pair)
        if error and any(phrase in error.lower() for phrase in ('not authorized', 'not included', 'expired', 'sign-in')):
            gmail_blocked = True


def _funder_alert_email(user: User, funder_profile: FunderProfile | None) -> str:
    preferred = str(getattr(funder_profile, 'contact_email', '') or '').strip()
    if preferred:
        return preferred
    return str(getattr(user, 'email', '') or '').strip()


def _send_or_record(
    user: User,
    cbo: CBO,
    activity: CBOActivity,
    subject: str,
    body: str,
    recipient: str,
    *,
    html_body: str | None = None,
    inline_images: list[dict] | None = None,
) -> str:
    existing = FunderInterestAlert.query.filter_by(user_id=user.id, activity_id=activity.id).first()
    if existing is not None and existing.status in {'sent', 'skipped'}:
        return ''
    if existing is None:
        alert = FunderInterestAlert(
            user_id=user.id,
            cbo_id=cbo.id,
            activity_id=activity.id,
            status='pending',
        )
        db.session.add(alert)
        db.session.flush()
    else:
        alert = existing
        alert.status = 'pending'
        alert.error = ''
    if not current_app.config.get('FUNDER_INTEREST_ALERTS', True):
        alert.status = 'skipped'
        alert.error = 'Funder interest emails are turned off.'
        return ''
    try:
        send_gmail_message(
            recipient,
            subject,
            body,
            html_body=html_body,
            inline_images=inline_images,
        )
    except GmailSendError as exc:
        alert.status = 'failed'
        alert.error = str(exc)[:500]
        current_app.logger.warning('Funder interest email was not sent to %s: %s', recipient, exc)
        return alert.error
    except Exception as exc:
        alert.status = 'failed'
        alert.error = str(exc)[:500]
        current_app.logger.exception('Funder interest email failed for %s', recipient)
        return alert.error
    alert.status = 'sent'
    alert.sent_at = datetime.utcnow()
    alert.error = ''
    return ''


def _record_unsent(user: User, cbo: CBO, activity: CBOActivity, error: str) -> None:
    existing = FunderInterestAlert.query.filter_by(user_id=user.id, activity_id=activity.id).first()
    if existing is not None:
        return
    db.session.add(FunderInterestAlert(
        user_id=user.id,
        cbo_id=cbo.id,
        activity_id=activity.id,
        status='failed',
        error=error[:500],
    ))


def _insert_activity(cbo_id: int, item: dict, *, alert_policy: str, created_at: datetime | None = None) -> CBOActivity | None:
    source_key = str(item.get('source_key') or '')[:220]
    if not source_key:
        return None
    existing = CBOActivity.query.filter_by(cbo_id=cbo_id, source_key=source_key).first()
    if existing is not None:
        return None
    row = CBOActivity(
        cbo_id=cbo_id,
        kind=str(item.get('kind') or 'highlight')[:32],
        title=str(item.get('title') or '')[:255],
        body=str(item.get('body') or ''),
        source_key=source_key,
        alert_policy=alert_policy,
        created_at=created_at or datetime.utcnow(),
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        return None
    return row


def _profile_photos(profile: dict | None) -> list[dict]:
    profile = profile if isinstance(profile, dict) else {}
    narrative = profile.get('narrative_profile') if isinstance(profile.get('narrative_profile'), dict) else {}
    media = narrative.get('program_media') if isinstance(narrative.get('program_media'), dict) else {}
    names = {}
    programs = list(narrative.get('program_overview') or []) + list(narrative.get('programs') or [])
    for program in programs:
        if not isinstance(program, dict):
            continue
        key = str(program.get('key') or '').strip()
        name = _clean_text(program.get('name') or program.get('title') or '')
        if key and name:
            names[key] = name
    photos = []
    for key, raw_photos in media.items():
        if not isinstance(raw_photos, list):
            continue
        label = names.get(str(key)) or _clean_text(str(key).replace('-', ' '))
        for raw_photo in raw_photos:
            if not isinstance(raw_photo, dict):
                continue
            photo_id = str(raw_photo.get('id') or '').strip()
            if not photo_id or not str(raw_photo.get('stored_path') or '').strip():
                continue
            photos.append({
                'id': photo_id,
                'label': label,
                'created_at': str(raw_photo.get('created_at') or ''),
            })
    return photos


def _joined_body(cbo: CBO, profile: dict) -> str:
    tagline = _clean_text((profile or {}).get('tagline') or '')
    focus = _clean_text((profile or {}).get('focus_areas') or getattr(cbo, 'focus_areas', '') or '')
    location = _clean_text((profile or {}).get('location') or getattr(cbo, 'location', '') or '')
    parts = [part for part in (tagline, focus, location) if part]
    if parts:
        return ' · '.join(parts)[:700]
    return f'{cbo.name} is now on Kumbu Connect.'


def _profile_links(slug: str) -> tuple[str, str]:
    def build() -> tuple[str, str]:
        return (
            url_for('main.cbo_profile', slug=slug),
            url_for('main.cbo_profile', slug=slug, contact=1, _anchor='contact-funder'),
        )

    if has_request_context():
        profile_path, contact_path = build()
    else:
        with current_app.test_request_context('/'):
            profile_path, contact_path = build()
    return _absolute(profile_path), _absolute(contact_path)


def _absolute(path: str) -> str:
    base = (
        current_app.config.get('PUBLIC_BASE_URL')
        or current_app.config.get('PUBLIC_TUNNEL_URL')
        or current_app.config.get('NGROK_PUBLIC_URL')
        or ''
    ).strip().rstrip('/')
    if not base and has_request_context():
        from flask import request
        base = request.url_root.strip().rstrip('/')
    if not base:
        host = str(current_app.config.get('APP_HOST') or '127.0.0.1').strip() or '127.0.0.1'
        port = int(current_app.config.get('APP_PORT') or 8000)
        base = f'http://{host}:{port}'
    if path.startswith('http://') or path.startswith('https://'):
        return path
    return f'{base}{path}'


def _load_profile(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _attribute_history(obj, name: str):
    try:
        from sqlalchemy import inspect as sa_inspect
        return sa_inspect(obj).attrs[name].history
    except Exception:
        return None


def _clean_text(value) -> str:
    if isinstance(value, dict):
        value = value.get('summary') or value.get('quote') or value.get('text') or ''
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _source_key(kind: str, text: str) -> str:
    fingerprint = re.sub(r'\s+', ' ', text.strip().lower())[:180]
    return f'{kind}:{fingerprint}'[:220]
