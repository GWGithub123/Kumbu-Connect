"""Interface language: user preference, Swahili translation cache, and page string seeding."""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .models import UiTranslation, db

i18n_bp = Blueprint('i18n', __name__)

SUPPORTED_LANGUAGES = {
    'en': 'English',
    'sw': 'Kiswahili',
}
DEFAULT_LANGUAGE = 'en'
LANGUAGE_COOKIE = 'kumbu_lang'

MAX_STRINGS_PER_REQUEST = 60
MAX_STRING_LENGTH = 1500
MAX_CHARS_PER_REQUEST = 24000
GEMINI_BATCH_SIZE = 30
CACHE_TTL_SECONDS = 300

# Hand-checked wording for the most visible interface labels. Anything else goes through the model and the cache.
SWAHILI_SEED = {
    'Marketplace': 'Soko',
    'Messages': 'Ujumbe',
    'Saved CBOs': 'CBO Zilizohifadhiwa',
    'My Profile': 'Wasifu Wangu',
    'Peer feed': 'Habari za Wenzako',
    'SMS Activity': 'Shughuli za SMS',
    'SMS Admin': 'Usimamizi wa SMS',
    'Settings': 'Mipangilio',
    'Language': 'Lugha',
    'Logout': 'Ondoka',
    'Login': 'Ingia',
    'Log in': 'Ingia',
    'Sign in': 'Ingia',
    'Email': 'Barua pepe',
    'Password': 'Nenosiri',
    'Save': 'Hifadhi',
    'Saved': 'Imehifadhiwa',
    'Search': 'Tafuta',
    'Clear': 'Futa',
    'Filters': 'Vichujio',
    'Submit': 'Tuma',
    'Cancel': 'Ghairi',
    'Close': 'Funga',
    'Open': 'Fungua',
    'Copy': 'Nakili',
    'Copy Link': 'Nakili Kiungo',
    'Delete': 'Futa',
    'Remove': 'Ondoa',
    'Retry': 'Jaribu tena',
    'Back': 'Rudi',
    'Next': 'Endelea',
    'Previous': 'Iliyotangulia',
    'Previous Page': 'Ukurasa Uliotangulia',
    'Next Page': 'Ukurasa Unaofuata',
    'View Profile': 'Tazama Wasifu',
    'View profile': 'Tazama wasifu',
    'Reach out': 'Wasiliana',
    'Map': 'Ramani',
    'Satellite': 'Setilaiti',
    'Online': 'Mtandaoni',
    'Offline': 'Nje ya mtandao',
    'Documents': 'Hati',
    'Entries': 'Maingizo',
    'Net': 'Salio',
    'Income': 'Mapato',
    'Expenses': 'Matumizi',
    'Outbox': 'Kikasha cha kutuma',
    'Take Picture': 'Piga Picha',
    'Upload from Library': 'Pakia kutoka kwenye simu',
    'Document Date': 'Tarehe ya Hati',
    'Bookkeeping': 'Uhasibu',
    'Community Feedback': 'Maoni ya Jamii',
    'No community feedback yet.': 'Bado hakuna maoni ya jamii.',
    'Social Impact': 'Athari kwa Jamii',
    'Fits your saved interests': 'Inaendana na mambo unayopendelea',
    'New update': 'Taarifa mpya',
    'Sync Now': 'Sawazisha Sasa',
    'Required': 'Inahitajika',
    'Public': 'Umma',
    'Like': 'Penda',
    'Comment': 'Toa maoni',
    'Post': 'Chapisha',
    'English': 'Kiingereza',
    'Kiswahili': 'Kiswahili',
    'How this works': 'Jinsi hii inavyofanya kazi',
}

_SKIP_TEXT = re.compile(r'^[\s\d.,:;/\\%+\-–—()#*&|·•→←↑↓×$€£@!?"\'`~^_=<>\[\]{}]*$')
_URLISH = re.compile(r'^(https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+$)', re.I)
_HAS_LETTER = re.compile(r'[A-Za-z]')

_cache_lock = threading.Lock()
_cache: dict[str, dict[str, str]] = {}
_cache_loaded_at: dict[str, float] = {}


def normalize_text(value: str) -> str:
    return ' '.join(str(value or '').split())


def should_translate(text: str) -> bool:
    if not text or len(text) > MAX_STRING_LENGTH:
        return False
    if not _HAS_LETTER.search(text) or _SKIP_TEXT.match(text):
        return False
    if _URLISH.match(text):
        return False
    return True


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def current_language() -> str:
    try:
        if current_user and current_user.is_authenticated:
            preferred = str(getattr(current_user, 'preferred_language', '') or '').strip().lower()
            if preferred in SUPPORTED_LANGUAGES:
                cookie_lang = str(request.cookies.get(LANGUAGE_COOKIE) or '').strip().lower()
                if preferred == DEFAULT_LANGUAGE and cookie_lang in SUPPORTED_LANGUAGES and cookie_lang != DEFAULT_LANGUAGE:
                    return cookie_lang
                return preferred
    except Exception:
        pass
    cookie_lang = str(request.cookies.get(LANGUAGE_COOKIE) or '').strip().lower()
    if cookie_lang in SUPPORTED_LANGUAGES:
        return cookie_lang
    return DEFAULT_LANGUAGE


def _cached_translations(lang: str) -> dict[str, str]:
    now = time.time()
    with _cache_lock:
        loaded_at = _cache_loaded_at.get(lang, 0)
        if lang in _cache and now - loaded_at < CACHE_TTL_SECONDS:
            return _cache[lang]
    rows = db.session.query(UiTranslation.source_text, UiTranslation.translated_text).filter_by(lang=lang).all()
    mapping = {row.source_text: row.translated_text for row in rows}
    if lang == 'sw':
        mapping.update(SWAHILI_SEED)
    with _cache_lock:
        _cache[lang] = mapping
        _cache_loaded_at[lang] = now
    return mapping


def _remember(lang: str, pairs: dict[str, str]) -> None:
    if not pairs:
        return
    existing = {
        row.source_hash
        for row in UiTranslation.query.filter(
            UiTranslation.lang == lang,
            UiTranslation.source_hash.in_([_source_hash(text) for text in pairs]),
        ).all()
    }
    for source, translated in pairs.items():
        digest = _source_hash(source)
        if digest in existing:
            continue
        db.session.add(UiTranslation(
            lang=lang,
            source_hash=digest,
            source_text=source,
            translated_text=translated,
        ))
        existing.add(digest)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Could not store interface translations')
    with _cache_lock:
        bucket = _cache.setdefault(lang, {})
        bucket.update(pairs)
        if lang == 'sw':
            bucket.update(SWAHILI_SEED)


def _gemini_translate(texts: list[str], lang: str) -> dict[str, str]:
    api_key = current_app.config.get('GEMINI_API_KEY')
    if not api_key or not texts:
        return {}
    try:
        from google import genai
        from google.genai import types
    except Exception:
        return {}

    language_name = SUPPORTED_LANGUAGES.get(lang, lang)
    client = genai.Client(api_key=api_key)
    results: dict[str, str] = {}
    for start in range(0, len(texts), GEMINI_BATCH_SIZE):
        chunk = texts[start:start + GEMINI_BATCH_SIZE]
        numbered = {str(index): text for index, text in enumerate(chunk)}
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    (
                        f'Translate interface text from English into {language_name} as used in Kenya. '
                        'The text comes from Kumbu Connect, a web platform where community-based organisations (CBOs) '
                        'share their work and bookkeeping, and funders find organisations to support. '
                        'Use clear, everyday wording that a CBO treasurer or a funder would understand. '
                        'Keep these unchanged: the brand name Kumbu Connect, organisation names, people names, place names, '
                        'acronyms such as CBO, SMS, PDF, QR, KSh, UGX, numbers, dates written as digits, URLs, and email addresses. '
                        'Keep punctuation, leading symbols, and placeholders like {name} as they are. '
                        'If a string is already in the target language or is only a name, return it unchanged. '
                        'Return ONLY a JSON object that maps each input key to its translation.'
                    ),
                    json.dumps(numbered, ensure_ascii=False),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type='application/json',
                ),
            )
            parsed = json.loads(response.text or '{}')
        except Exception:
            current_app.logger.exception('Gemini interface translation failed')
            continue
        if not isinstance(parsed, dict):
            continue
        for key, source in numbered.items():
            translated = normalize_text(parsed.get(key) or '')
            if translated:
                results[source] = translated
    return results


def translate_strings(texts: list[str], lang: str, allow_remote: bool = True) -> dict[str, str]:
    if lang not in SUPPORTED_LANGUAGES or lang == DEFAULT_LANGUAGE:
        return {}
    cached = _cached_translations(lang)
    known_outputs = set(cached.values())
    found: dict[str, str] = {}
    missing: list[str] = []
    for raw in texts:
        text = normalize_text(raw)
        if not should_translate(text):
            continue
        if text in cached:
            found[text] = cached[text]
        elif text in known_outputs:
            found[text] = text
        elif text not in missing:
            missing.append(text)
    if missing and allow_remote:
        fresh = _gemini_translate(missing, lang)
        if fresh:
            _remember(lang, fresh)
            found.update(fresh)
    return found


_PROTECTED_BLOCK = re.compile(
    r'<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>|<textarea\b[^>]*>.*?</textarea\s*>|<!--.*?-->',
    re.I | re.S,
)
_TEXT_NODE = re.compile(r'>([^<>]+)<')
_ATTRIBUTE = re.compile(
    r'\s(?:placeholder|title|aria-label|alt|data-unsaved-label|data-saved-label|data-confirm)="([^"]*)"',
    re.I,
)


def page_strings(document: str) -> set[str]:
    strings: set[str] = set()
    for segment in _PROTECTED_BLOCK.split(document):
        for match in _TEXT_NODE.finditer(segment):
            text = normalize_text(html.unescape(match.group(1)))
            if should_translate(text):
                strings.add(text)
        for match in _ATTRIBUTE.finditer(segment):
            text = normalize_text(html.unescape(match.group(1)))
            if should_translate(text):
                strings.add(text)
    return strings


@i18n_bp.app_context_processor
def inject_language():
    lang = current_language()
    return {
        'ui_lang': lang,
        'ui_languages': SUPPORTED_LANGUAGES,
    }


@i18n_bp.after_app_request
def seed_page_translations(response):
    if response.direct_passthrough or response.status_code >= 400:
        return response
    if not (response.mimetype or '').startswith('text/html'):
        return response
    try:
        lang = current_language()
    except Exception:
        return response
    if lang == DEFAULT_LANGUAGE:
        return response
    try:
        document = response.get_data(as_text=True)
    except Exception:
        return response
    if '</body>' not in document:
        return response

    known = translate_strings(sorted(page_strings(document)), lang, allow_remote=False)
    payload = json.dumps({'lang': lang, 'map': known}, ensure_ascii=False).replace('</', '<\\/')
    seed_tag = f'<script id="kumbu-i18n-seed" type="application/json">{payload}</script>'
    document = document.replace('</body>', seed_tag + '</body>', 1)
    document = re.sub(r'<html\b([^>]*)\blang="[^"]*"', lambda m: f'<html{m.group(1)}lang="{lang}"', document, count=1)
    document = document.replace('<html', '<html data-i18n-pending="true"', 1)
    response.set_data(document)
    return response


def _safe_next(raw: str | None) -> str:
    candidate = str(raw or '').strip()
    if candidate.startswith('/') and not candidate.startswith('//'):
        return candidate
    return url_for('main.index')


@i18n_bp.route('/language/<code>')
def set_language(code: str):
    lang = str(code or '').strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE
    if current_user.is_authenticated:
        current_user.preferred_language = lang
        db.session.commit()
    response = redirect(_safe_next(request.args.get('next')))
    response.set_cookie(
        LANGUAGE_COOKIE,
        lang,
        max_age=60 * 60 * 24 * 365,
        samesite='Lax',
        secure=request.is_secure,
    )
    return response


@i18n_bp.route('/i18n/translate', methods=['POST'])
def translate_endpoint():
    payload = request.get_json(silent=True) or {}
    lang = str(payload.get('lang') or '').strip().lower()
    texts = payload.get('texts') or []
    if lang not in SUPPORTED_LANGUAGES or not isinstance(texts, list):
        return jsonify({'ok': False, 'translations': {}}), 400

    selected: list[str] = []
    total = 0
    for raw in texts[:MAX_STRINGS_PER_REQUEST]:
        text = normalize_text(raw if isinstance(raw, str) else '')
        if not should_translate(text):
            continue
        total += len(text)
        if total > MAX_CHARS_PER_REQUEST:
            break
        selected.append(text)

    return jsonify({'ok': True, 'translations': translate_strings(selected, lang)})


@i18n_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    saved = False
    if request.method == 'POST':
        lang = str(request.form.get('preferred_language') or '').strip().lower()
        if lang in SUPPORTED_LANGUAGES:
            current_user.preferred_language = lang
            db.session.commit()
            saved = True
            response = redirect(url_for('i18n.settings', saved=1))
            response.set_cookie(
                LANGUAGE_COOKIE,
                lang,
                max_age=60 * 60 * 24 * 365,
                samesite='Lax',
                secure=request.is_secure,
            )
            return response
    return render_template(
        'settings.html',
        saved=saved or request.args.get('saved') == '1',
        selected_language=current_language(),
    )
