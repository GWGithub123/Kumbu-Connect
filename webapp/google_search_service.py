"""Public web search helpers.

Uses Gemini with Google Search to find the right community organisation, then
DuckDuckGo or Bing when that search is unavailable.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from flask import current_app
from google.genai import types


GOOGLE_CUSTOM_SEARCH_URL = 'https://www.googleapis.com/customsearch/v1'
DUCKDUCKGO_HTML_URL = 'https://html.duckduckgo.com/html/'
BING_SEARCH_URL = 'https://www.bing.com/search'
PUBLIC_SEARCH_UNREACHABLE = 'Public web search is unreachable right now.'
_PROXY_ENV_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
_TAG_RE = re.compile(r'<[^>]+>')
_BLOCK_RE = re.compile(
    r'<(script|style|noscript|svg|nav|footer|iframe)(\s[^>]*)?>.*?</\1>',
    re.I | re.S,
)
_DL_RE = re.compile(r'<dl(\s[^>]*)?>(.*?)</dl>', re.I | re.S)
_DT_RE = re.compile(r'<dt(\s[^>]*)?>(.*?)</dt>', re.I | re.S)
_DD_RE = re.compile(r'<dd(\s[^>]*)?>(.*?)</dd>', re.I | re.S)
_TABLE_RE = re.compile(r'<table(\s[^>]*)?>(.*?)</table>', re.I | re.S)
_TR_RE = re.compile(r'<tr(\s[^>]*)?>(.*?)</tr>', re.I | re.S)
_CELL_RE = re.compile(r'<t[dh](\s[^>]*)?>(.*?)</t[dh]>', re.I | re.S)
_LIST_RE = re.compile(r'<[uo]l(\s[^>]*)?>(.*?)</[uo]l>', re.I | re.S)
_LI_RE = re.compile(r'<li(\s[^>]*)?>(.*?)</li>', re.I | re.S)
_MAIN_RE = re.compile(r'<(main|article)(\s[^>]*)?>(.*?)</\1>', re.I | re.S)
_LINK_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_SKIP_FETCH_HOSTS = (
    'facebook.com',
    'instagram.com',
    'linkedin.com',
    'twitter.com',
    'x.com',
    'youtube.com',
    'tiktok.com',
    'pitchbook.com',
)
INDEXED_SNIPPET_HOSTS = _SKIP_FETCH_HOSTS
_NEWS_HOSTS = (
    'bbc.com',
    'cnn.com',
    'reuters.com',
    'nytimes.com',
    'nation.africa',
    'standardmedia.co.ke',
    'the-star.co.ke',
    'businessdailyafrica.com',
    'wikipedia.org',
)
PROFILE_PATH_HINTS = (
    'about', 'about-us', 'our-story', 'ourstory', 'our-team', 'ourteam',
    'team', 'people', 'founder', 'founders', 'leadership', 'who-we-are',
    'whoweare', 'history', 'story', 'staff', 'board', 'directors', 'mission',
    'program', 'programs', 'programme', 'programmes', 'our-work', 'impact',
)
COMMON_PROFILE_PATHS = (
    '/about/',
    '/about-us/',
    '/our-story/',
    '/team/',
    '/our-team/',
    '/founders/',
    '/leadership/',
    '/who-we-are/',
    '/programs/',
    '/programmes/',
    '/our-work/',
    '/impact/',
)
_RESEARCH_HEADERS = {'User-Agent': 'KumbuConnect/1.0 (profile research)'}
_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}
RENDER_FETCH_LIMIT = 3
PAGE_EXCERPT_LIMIT = 3500
SECTION_TEXT_LIMIT = 900
TEAM_SECTION_TEXT_LIMIT = 4200
MAX_SECTIONS_PER_PAGE = 8
_HEADING_RE = re.compile(r'<h([1-3])(\s[^>]*)?>(.*?)</h\1>', re.I | re.S)
_PARAGRAPH_RE = re.compile(r'<p(\s[^>]*)?>(.*?)</p>', re.I | re.S)
_META_DESC_RES = (
    re.compile(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:name|property)=["\'](?:description|og:description)["\']',
        re.I,
    ),
)
_BOT_WALL_RE = re.compile(
    r'(security verification|verify you are not a bot|cf-browser-verification|'
    r'just a moment|enable javascript and cookies|attention required)',
    re.I,
)
_CHROME_HEADING_RE = re.compile(
    r'\b(menu|cookie|subscribe|newsletter|share this|footer|donate|shopping cart|'
    r'log ?in|sign in|follow us|quick links|skip to)\b',
    re.I,
)
_SECTION_KIND_HINTS = (
    ('founder', ('co-founder', 'cofounder', 'founder')),
    ('team', ('our team', 'the team', 'team', 'staff', 'leadership', 'our board', 'board of', 'who we are', 'directors')),
    ('history', ('our story', 'history', 'founded', 'established', 'origin')),
    ('program', (
        'program', 'programme', 'project', 'what we do', 'our work', 'services',
        'education', 'health', 'youth', 'women', 'empowerment', 'scholarship',
        'outreach', 'agriculture', 'livelihood', 'environment',
    )),
    ('impact', ('impact', 'results', 'outcomes')),
    ('about', ('about', 'mission', 'vision', 'who we')),
)
SECTION_KIND_PRIORITY = {
    'founder': 60,
    'team': 55,
    'history': 50,
    'program': 40,
    'impact': 35,
    'about': 30,
    'other': 5,
}


def get_google_search_api_key() -> str:
    return (current_app.config.get('GOOGLE_SEARCH_API_KEY') or '').strip()


def get_google_search_engine_id() -> str:
    return (current_app.config.get('GOOGLE_SEARCH_ENGINE_ID') or '').strip()


def google_custom_search_configured() -> bool:
    return bool(get_google_search_api_key() and get_google_search_engine_id())


def google_search_enabled() -> bool:
    return True


def search_google(query: str, num_results: int = 5, date_restrict: str = '') -> dict:
    cleaned_query = ' '.join((query or '').split())
    payload = {
        'configured': True,
        'source': 'google_custom_search' if google_custom_search_configured() else 'duckduckgo',
        'query': cleaned_query,
        'items': [],
        'error': '',
    }
    if not cleaned_query:
        payload['error'] = 'No search query was provided.'
        return payload

    if google_custom_search_configured():
        custom = _search_google_custom(cleaned_query, num_results, date_restrict)
        if custom.get('items'):
            return custom
        payload['error'] = str(custom.get('error') or '')

    ddg = _search_duckduckgo(cleaned_query, num_results)
    if ddg.get('items'):
        return ddg

    bing = _search_bing(cleaned_query, num_results)
    if bing.get('items'):
        return bing
    bing['error'] = PUBLIC_SEARCH_UNREACHABLE
    return bing


GROUNDED_CBO_SEARCH_PROMPT = """Find public pages about one Kenyan community-based organisation (CBO).

The name may be a short acronym for a longer local name. A company, radio station, certification body, school, or group outside Kenya that only shares the short name is a different organisation.

Return ONLY JSON:
{
  "full_name": "",
  "location": "",
  "summary": "one sentence, or why no Kenyan CBO was found",
  "official_url": "",
  "sources": [{"title": "", "url": "https://..."}]
}

Rules:
- Use web search. sources are pages you found about this CBO, not namesakes.
- official_url is the organisation's own site when one exists, otherwise empty.
- Include independent pages as well as the official site.
- If you cannot find a Kenyan CBO by this name, return an empty sources array.
"""


def _gemini_grounded_response(client, prompt: str):
    try:
        return client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )
    except Exception as exc:
        current_app.logger.warning('Gemini web search failed: %s', exc)
        return None


def search_cbo_with_gemini(name: str, location: str = '', extra_context: str = '') -> dict:
    """Search Google through Gemini and return pages about this CBO."""
    payload = {
        'source': 'gemini_google_search',
        'items': [],
        'queries': [],
        'summary': '',
        'error': '',
    }
    api_key = (current_app.config.get('GEMINI_API_KEY') or '').strip()
    if not api_key or not str(name or '').strip():
        return payload
    place = location or 'Kenya'
    try:
        from google import genai
        client = genai.Client(api_key=api_key, http_options=gemini_http_options())
        response = _gemini_grounded_response(
            client,
            GROUNDED_CBO_SEARCH_PROMPT + '\n\n' + (
                f'CBO name: {name}\n'
                f'Place: {place}\n'
                f'Known profile:\n{extra_context or "none"}'
            ),
        )
        if response is None:
            payload['error'] = PUBLIC_SEARCH_UNREACHABLE
            return payload
    except Exception as exc:
        current_app.logger.warning('Gemini web search failed for %s: %s', name, exc)
        payload['error'] = PUBLIC_SEARCH_UNREACHABLE
        return payload

    parsed = _parse_loose_json(getattr(response, 'text', '') or '')
    queries, redirect_urls = _grounding_queries_and_urls(response)
    sources = []
    official_url = ''
    summary = ''
    full_name = ''
    if isinstance(parsed, dict):
        summary = ' '.join(str(parsed.get('summary') or '').split())
        official_url = str(parsed.get('official_url') or '').strip()
        full_name = ' '.join(str(parsed.get('full_name') or '').split())
        sources = parsed.get('sources') if isinstance(parsed.get('sources'), list) else []
    if full_name and full_name.lower() not in str(name or '').lower():
        follow_response = _gemini_grounded_response(
            client,
            (
                f'Find independent public pages about {full_name} ({name}), a Kenyan community-based organisation. '
                'Include the organisation site, its program pages, and independent interviews. '
                'Name who founded it, when it started, where it works, the programs it runs, and any results or numbers. '
                'Return ONLY JSON with keys summary, official_url, and sources as a list of {title, url}.'
            ),
        )
        if follow_response is not None:
            follow_parsed = _parse_loose_json(getattr(follow_response, 'text', '') or '')
            follow_queries, follow_redirects = _grounding_queries_and_urls(follow_response)
            queries.extend(follow_queries)
            redirect_urls.extend(follow_redirects)
            if isinstance(follow_parsed, dict):
                follow_sources = follow_parsed.get('sources') if isinstance(follow_parsed.get('sources'), list) else []
                sources = list(sources or []) + follow_sources
                if not official_url:
                    official_url = str(follow_parsed.get('official_url') or '').strip()
                follow_summary = ' '.join(str(follow_parsed.get('summary') or '').split())
                if follow_summary:
                    summary = follow_summary
    payload['queries'] = queries
    payload['summary'] = summary
    items = _items_from_grounded_sources(name, sources, official_url, summary)
    if len(items) < 2:
        items = _merge_search_result_items(items, _resolve_grounding_redirects(name, redirect_urls, official_url))
    payload['items'] = items
    if not items:
        payload['error'] = summary or f'No public pages clearly match the CBO {name}.'
    return payload


def _parse_loose_json(text: str):
    cleaned = str(text or '').strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?', '', cleaned, flags=re.I).strip().strip('`').strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None


def _grounding_queries_and_urls(response) -> tuple[list[str], list[tuple[str, str]]]:
    queries = []
    urls = []
    candidates = getattr(response, 'candidates', None) or []
    if not candidates:
        return queries, urls
    meta = getattr(candidates[0], 'grounding_metadata', None)
    if meta is None:
        return queries, urls
    queries = [str(query).strip() for query in (getattr(meta, 'web_search_queries', None) or []) if str(query).strip()]
    for chunk in getattr(meta, 'grounding_chunks', None) or []:
        web = getattr(chunk, 'web', None)
        if web is None:
            continue
        uri = str(getattr(web, 'uri', '') or '').strip()
        title = str(getattr(web, 'title', '') or '').strip()
        if uri:
            urls.append((title, uri))
    return queries, urls


def _items_from_grounded_sources(name: str, sources, official_url: str, summary: str) -> list[dict]:
    official_host = urlparse(official_url).netloc.lower().removeprefix('www.')
    items = []
    seen = set()
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        link = str(source.get('url') or '').strip()
        if not link.startswith('http'):
            continue
        key = link.lower().rstrip('/')
        if key in seen:
            continue
        seen.add(key)
        host = urlparse(link).netloc.lower().removeprefix('www.')
        title = str(source.get('title') or name).strip()
        items.append({
            'title': title,
            'link': link,
            'display_link': host,
            'snippet': f'{name}. {summary or title}',
            'is_official': bool(official_host and host == official_host),
        })
    return items[:8]


def _resolve_grounding_redirects(name: str, redirects, official_url: str) -> list[dict]:
    items = []
    for title, uri in (redirects or [])[:6]:
        if 'vertexaisearch.cloud.google.com' not in uri and not uri.startswith('http'):
            continue
        try:
            response = _public_request('GET', uri, headers=_BROWSER_HEADERS, timeout=12, allow_redirects=True)
        except requests.RequestException:
            continue
        final = str(response.url or '').strip()
        if not final.startswith('http') or 'google.com' in urlparse(final).netloc:
            continue
        host = urlparse(final).netloc.lower().removeprefix('www.')
        official_host = urlparse(official_url).netloc.lower().removeprefix('www.')
        items.append({
            'title': title or name,
            'link': final,
            'display_link': host,
            'snippet': title or name,
            'is_official': bool(official_host and host == official_host),
        })
    return items


def _merge_search_result_items(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged = list(existing or [])
    seen = {str(item.get('link') or '').strip().lower().rstrip('/') for item in merged}
    for item in incoming or []:
        link = str(item.get('link') or '').strip()
        key = link.lower().rstrip('/')
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:8]


def enrich_search_items(items: list[dict], limit: int = 3) -> list[dict]:
    """Pull public-page excerpts so synthesis has more than a title."""
    return enrich_research_corpus('', items, fetch_limit=limit, discover_related=False)


def enrich_research_corpus(
    name: str,
    items: list[dict],
    fetch_limit: int = 10,
    discover_related: bool = True,
) -> list[dict]:
    """Fetch search pages and dive into official about/team/history URLs."""
    merged = _dedupe_items(items)
    if discover_related:
        official = official_homepage_url(name, merged)
        if official:
            merged = _dedupe_items(merged + collect_related_profile_items(official, name))
    ranked = sorted(merged, key=lambda item: _deep_dive_score(item, name), reverse=True)
    fetch_cap = max(int(fetch_limit or 0), 0)
    to_fetch = []
    seen_fetch = set()
    for item in ranked:
        link = str(item.get('link') or '').strip()
        if len(to_fetch) >= fetch_cap:
            break
        if not link or link in seen_fetch or not _should_fetch_page(link):
            continue
        seen_fetch.add(link)
        to_fetch.append(link)

    pages = _fetch_pages_parallel(to_fetch)
    _render_thin_pages(pages, to_fetch)
    enriched = []
    for item in ranked:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        link = str(row.get('link') or '').strip()
        html = pages.get(link) or ''
        if _html_is_bot_wall(html):
            html = ''
        if html:
            html_title = _html_title(html)
            if html_title and html_title.lower() not in {'home', 'untitled'}:
                row['title'] = html_title
            sections = extract_page_sections(html)
            programs = extract_page_programs(html, link) or programs_from_sections(sections)
            images = extract_content_images(html, link, programs)
            _attach_program_images(programs, images)
            if programs:
                row['page_programs'] = programs
            if images:
                row['images'] = images
            logos = extract_profile_images(html, link)
            if logos:
                row['profile_images'] = logos
            if sections and not _sections_are_thin(sections):
                row['sections'] = sections
                row['page_kind'] = _page_kind_from_sections(link, sections)
                row['snippet'] = _snippet_from_sections(sections)
            else:
                extra = _snippet_from_sections(sections)
                snippet = str(row.get('snippet') or '').strip()
                if extra and extra.lower() not in snippet.lower():
                    row['snippet'] = (snippet + ' ' + extra).strip()[:PAGE_EXCERPT_LIMIT]
                if not row.get('page_kind'):
                    row['page_kind'] = classify_profile_url(link)
        enriched.append(row)
    return enriched


def collect_related_profile_items(official_home: str, name: str) -> list[dict]:
    """Find about/team/history pages on the organisation's own site."""
    home = str(official_home or '').strip()
    if not home:
        return []
    html = _fetch_html(home)
    discovered = []
    if html:
        discovered.append(_item_from_url(home, f'{name} official site' if name else 'Official site'))
        for link in discover_profile_links(html, home)[:6]:
            discovered.append(_item_from_url(link, _title_from_url(link, name)))
    if len(discovered) < 2:
        for path in COMMON_PROFILE_PATHS:
            candidate = urljoin(home if home.endswith('/') else home + '/', path.lstrip('/'))
            if any(item.get('link') == candidate for item in discovered):
                continue
            page_html = _fetch_html(candidate)
            if page_html:
                discovered.append(_item_from_url(candidate, _title_from_url(candidate, name)))
            if len(discovered) >= 6:
                break
    return discovered


def guess_official_website(name: str, items: list[dict]) -> str:
    """Pick the organisation's own site from search hits."""
    for item in items or []:
        if isinstance(item, dict) and item.get('is_official') and str(item.get('link') or '').startswith('http'):
            return str(item.get('link') or '').strip()
    tokens = [token for token in re.findall(r'[a-z0-9]+', (name or '').lower()) if len(token) > 2]
    best_link = ''
    best_score = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get('link') or '').strip()
        if not _should_fetch_page(link):
            continue
        host = urlparse(link).netloc.lower().removeprefix('www.')
        if not host or any(host == skip or host.endswith('.' + skip) for skip in _NEWS_HOSTS):
            continue
        score = 0
        compact_host = host.replace('-', '').replace('.', '')
        if any(token in compact_host for token in tokens):
            score += 6
        path = urlparse(link).path.strip('/').lower()
        if not path:
            score += 2
        if any(hint in path for hint in PROFILE_PATH_HINTS):
            score += 2
        title = str(item.get('title') or '').lower()
        if name and name.lower() in title:
            score += 1
        if score > best_score:
            best_score = score
            best_link = link
    return best_link if best_score >= 6 else best_link if best_score >= 3 and tokens else ''


def official_homepage_url(name: str, items: list[dict]) -> str:
    link = guess_official_website(name, items)
    parsed = urlparse(link)
    if not parsed.netloc:
        return ''
    scheme = parsed.scheme or 'https'
    return f'{scheme}://{parsed.netloc}/'


def discover_profile_links(html: str, base_url: str) -> list[str]:
    """Pull same-site about/team/history links out of a page."""
    base_host = urlparse(base_url).netloc.lower().removeprefix('www.')
    found = []
    seen = set()
    for href, label_html in _LINK_RE.findall(html or ''):
        absolute = urljoin(base_url, unescape(href or '').split('#')[0].strip())
        if not absolute.startswith('http'):
            continue
        parsed = urlparse(absolute)
        host = parsed.netloc.lower().removeprefix('www.')
        if host != base_host:
            continue
        haystack = f'{parsed.path.lower()} {_strip_html(label_html).lower()}'
        if not any(hint in haystack for hint in PROFILE_PATH_HINTS):
            continue
        normalized = absolute.rstrip('/') + ('/' if parsed.path.endswith('/') or not parsed.path else '')
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(absolute)
    return found


_EMBEDDED_PROGRAM_RE = re.compile(
    r'"title"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"description"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"image"\s*:\s*"([^"]+)"',
)
_IMG_TAG_RE = re.compile(r'<img\b([^>]*)>', re.I | re.S)
_SKIP_IMAGE_RE = re.compile(
    r'(^|[^a-z])(logo|icons?|favicon|sprite|pixel|badge|footer|contact-bg|emoji|avatar-default|hero|banner|slider|about\d*)([^a-z]|$)',
    re.I,
)
_GENERIC_PROGRAM_HEADINGS = {
    'our programs', 'programs', 'programmes', 'our programmes', 'our work',
    'what we do', 'services', 'our services', 'overview', 'home', 'summary',
    'contact', 'donate', 'menu', 'navigation',
}
_CHROME_PROGRAM_WORDS = {
    'home', 'about', 'programs', 'programmes', 'impact', 'contact', 'donate',
    'learn', 'more', 'read', 'menu', 'navigation',
}


def extract_page_programs(html: str, page_url: str = '') -> list[dict]:
    """Read program cards that a page embeds next to their photos."""
    programs = []
    seen = set()
    for title, description, image in _EMBEDDED_PROGRAM_RE.findall(html or ''):
        name = ' '.join(_unescape_js_string(title).split())
        body = ' '.join(_unescape_js_string(description).split())
        if len(name) < 3 or len(body) < 20:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        programs.append({
            'name': name[:120],
            'description': body[:500],
            'who_it_serves': '',
            'image_url': _content_image_url(_unescape_js_string(image), page_url),
        })
        if len(programs) >= 8:
            break
    return programs


def _program_text_is_chrome(description: str) -> bool:
    words = re.findall(r"[a-z0-9']+", str(description or '').lower())
    if len(words) < 4:
        return True
    chrome = sum(word in _CHROME_PROGRAM_WORDS for word in words)
    return chrome >= len(words) * 0.6


def programs_from_sections(sections: list[dict]) -> list[dict]:
    """Turn named program sections into records the profile can confirm."""
    records = []
    seen = set()
    for section in sections or []:
        if str(section.get('kind') or '') != 'program':
            continue
        name = ' '.join(str(section.get('heading') or '').split())
        if not name or name.lower() in _GENERIC_PROGRAM_HEADINGS:
            continue
        description = ' '.join(str(section.get('text') or '').split())
        prefix = f'{name.lower()}.'
        if description.lower().startswith(prefix):
            description = description[len(prefix):].strip()
        elif description.lower().startswith(name.lower()):
            description = description[len(name):].lstrip(' .:-')
        description = re.sub(r'\b(learn more|read more|donate now)\b', ' ', description, flags=re.I)
        description = ' '.join(description.split(' '))
        if len(description) < 20 or _program_text_is_chrome(description):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        records.append({
            'name': name[:120],
            'description': description[:500],
            'who_it_serves': '',
            'image_url': '',
        })
    return records[:10]


def _attach_program_images(programs: list[dict], images: list[dict]) -> None:
    by_name = {}
    for image in images or []:
        label = str(image.get('program') or image.get('alt') or '').strip().lower()
        if label and image.get('url') and label not in by_name:
            by_name[label] = image['url']
    for program in programs or []:
        if program.get('image_url'):
            continue
        program['image_url'] = by_name.get(str(program.get('name') or '').strip().lower(), '')


def extract_content_images(html: str, page_url: str, programs: list[dict] | None = None) -> list[dict]:
    """Collect photos of the work, skipping logos and page chrome."""
    images = []
    seen = set()

    def add(url: str, alt: str = '', program: str = '') -> None:
        cleaned = str(url or '').strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        images.append({
            'url': cleaned,
            'alt': ' '.join(str(alt or program or '').split())[:160],
            'program': ' '.join(str(program or '').split())[:120],
        })

    for program in programs or []:
        if program.get('image_url'):
            add(program['image_url'], program.get('name') or '', program.get('name') or '')
    page_title = _html_title(html)
    for raw in re.findall(r'<meta\b[^>]*property=["\']og:image["\'][^>]*>', html or '', re.I):
        content = _attr_value(raw, 'content')
        url = _content_image_url(content, page_url)
        if url:
            add(url, page_title or 'Photo')
    for match in _IMG_TAG_RE.finditer(html or ''):
        tag = match.group(1)
        url = _content_image_url(_attr_value(tag, 'src') or _attr_value(tag, 'data-src'), page_url)
        if not url:
            continue
        alt = _attr_value(tag, 'alt')
        program = ''
        ahead = (html or '')[match.end():match.end() + 700]
        heading = _HEADING_RE.search(ahead)
        if heading:
            program = _strip_html(heading.group(3))
        if not program and not alt:
            continue
        add(url, alt, program)
    return images[:8]


_LOGO_HINT_RE = re.compile(r'logo|wordmark|brandmark|emblem', re.I)
_PROFILE_IMAGE_SKIP_RE = re.compile(
    r'(^|[^a-z])(sprite|pixel|badge|footer|contact-bg|hero|banner|slider|favicon)([^a-z]|$)',
    re.I,
)


def extract_profile_images(html: str, page_url: str) -> list[dict]:
    """Pull the organisation mark from a page, separate from program photos."""
    found = []
    seen = set()

    def add(raw_src: str, alt: str = '') -> None:
        url = _profile_image_url(raw_src, page_url)
        if not url or url in seen:
            return
        seen.add(url)
        found.append({
            'url': url,
            'alt': ' '.join(str(alt or 'Logo').split())[:160] or 'Logo',
        })

    for raw in _JSONLD_RE.findall(html or ''):
        try:
            data = json.loads(unescape(raw or '').strip() or 'null')
        except json.JSONDecodeError:
            continue
        for logo in _jsonld_logos(data):
            add(logo, 'Logo')
    for tag in re.findall(r'<link\b[^>]*>', html or '', re.I):
        rel = _attr_value(tag, 'rel').lower()
        href = _attr_value(tag, 'href')
        if 'apple-touch-icon' in rel and href:
            add(href, 'Logo')
    for match in _IMG_TAG_RE.finditer(html or ''):
        tag = match.group(1)
        src = _attr_value(tag, 'src') or _attr_value(tag, 'data-src')
        alt = _attr_value(tag, 'alt')
        class_name = _attr_value(tag, 'class')
        if _LOGO_HINT_RE.search(f'{src} {alt} {class_name}'):
            add(src, alt or 'Logo')
    return found[:4]


def _jsonld_logos(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        logo = value.get('logo')
        if isinstance(logo, str):
            found.append(logo)
        elif isinstance(logo, dict):
            url = str(logo.get('url') or logo.get('contentUrl') or '').strip()
            if url:
                found.append(url)
        for child in value.values():
            found.extend(_jsonld_logos(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_jsonld_logos(child))
    return found


def _profile_image_url(raw_src: str, page_url: str) -> str:
    src = unescape(str(raw_src or '')).strip()
    if not src or src.startswith('data:'):
        return ''
    absolute = urljoin(page_url or '', src)
    parsed = urlparse(absolute)
    if '/_next/image' in (parsed.path or ''):
        inner = unquote((parse_qs(parsed.query).get('url') or [''])[0])
        if not inner:
            return ''
        absolute = urljoin(page_url or absolute, inner)
        parsed = urlparse(absolute)
    if parsed.scheme not in {'http', 'https'}:
        return ''
    path = unquote(parsed.path or '')
    if not re.search(r'\.(?:jpe?g|png|webp|gif|svg)$', path, re.I):
        return ''
    filename = path.rsplit('/', 1)[-1]
    if _PROFILE_IMAGE_SKIP_RE.search(filename):
        return ''
    return absolute.split('?')[0]


def _unescape_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return unescape(value or '').replace('\\"', '"')


def _attr_value(tag: str, name: str) -> str:
    match = re.search(rf'''\b{name}\s*=\s*["']([^"']+)["']''', tag or '', re.I)
    return unescape(match.group(1)).strip() if match else ''


def _content_image_url(raw_src: str, page_url: str) -> str:
    src = unescape(str(raw_src or '')).strip()
    if not src or src.startswith('data:'):
        return ''
    absolute = urljoin(page_url or '', src)
    parsed = urlparse(absolute)
    if '/_next/image' in (parsed.path or ''):
        inner = unquote((parse_qs(parsed.query).get('url') or [''])[0])
        if not inner:
            return ''
        absolute = urljoin(page_url or absolute, inner)
        parsed = urlparse(absolute)
    if parsed.scheme not in {'http', 'https'}:
        return ''
    path = unquote(parsed.path or '')
    if not re.search(r'\.(?:jpe?g|png|webp|gif)$', path, re.I):
        return ''
    filename = path.rsplit('/', 1)[-1]
    if _SKIP_IMAGE_RE.search(filename):
        return ''
    return absolute.split('?')[0]


def extract_page_excerpt(html: str) -> str:
    """Join the labeled sections kept from a page."""
    return _snippet_from_sections(extract_page_sections(html))


def extract_page_sections(html: str) -> list[dict]:
    """Split a page into heading-led sections and keep the profile-bearing ones.

    A flat character cap keeps the menu and the hero, then cuts off the team
    and origin story. Sections let later ranking send those blocks through whole.
    """
    source = _readable_html(html)
    sections = _sections_from_headings(source)
    if not sections:
        sections = _sections_from_listings(source)
    if not sections:
        sections = _sections_from_paragraphs(source)
    meta = _meta_description(html)
    if meta and not _chrome_heading(meta):
        sections.insert(0, _make_section('Summary', meta, kind='about'))
    jsonld = extract_jsonld_text(html)
    if jsonld:
        kind = 'founder' if re.search(r'\bfounder\b', jsonld, re.I) else 'about'
        sections.append(_make_section('Structured data', jsonld, kind=kind))
    known_headings = {str(section.get('heading') or '').strip().lower() for section in sections}
    for program in extract_page_programs(html):
        if program['name'].lower() in known_headings:
            continue
        sections.append(_make_section(
            program['name'],
            f"{program['name']}. {program['description']}",
            kind='program',
        ))
    return _select_page_sections(sections)


def _readable_html(html: str) -> str:
    body_match = re.search(r'<body[^>]*>(.*)</body>', html or '', re.I | re.S)
    source = body_match.group(1) if body_match else (html or '')
    cleaned = _BLOCK_RE.sub(' ', source)
    mains = [match[2] for match in _MAIN_RE.findall(cleaned)]
    if mains:
        main_html = ' '.join(mains)
        if len(_strip_html(main_html)) >= 80 and len(_HEADING_RE.findall(cleaned)) <= len(_HEADING_RE.findall(main_html)) + 1:
            return main_html
    return cleaned


def _sections_from_headings(source: str) -> list[dict]:
    matches = list(_HEADING_RE.finditer(source or ''))
    if not matches:
        return []
    sections = []
    parent_kind = 'other'
    preamble = _strip_html(source[:matches[0].start()])
    if len(preamble) >= 80 and not _chrome_heading(preamble[:80]):
        section = _make_section('Overview', preamble)
        sections.append(section)
        if section['kind'] != 'other':
            parent_kind = section['kind']
    for index, match in enumerate(matches):
        level = int(match.group(1))
        heading = _strip_html(match.group(3))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        body = _strip_html(source[match.end():end])
        if _chrome_heading(heading):
            continue
        if heading and heading.lower() not in (body or '').lower():
            text = f'{heading}. {body}'.strip()
        else:
            text = body or heading
        if len(text) < 25 and (not heading or len(heading) < 3):
            continue
        inherited = parent_kind if level >= 3 else ''
        kind = _classify_section_heading(heading, text, inherited)
        if level <= 2 and kind != 'other':
            parent_kind = kind
        if len(text) < 25 and kind == 'other':
            continue
        sections.append(_make_section(heading or 'Overview', text, kind=kind))
    return sections


def _sections_from_listings(source: str) -> list[dict]:
    """Read directory cards that live in lists and tables rather than headings."""
    chunks = []
    for match in _DL_RE.finditer(source or ''):
        text = _definition_list_text(match.group(2))
        if len(text) > 40 and not _chrome_heading(text[:120]):
            chunks.append(text)
    for match in _TABLE_RE.finditer(source or ''):
        text = _table_text(match.group(2))
        if len(text) > 40 and not _chrome_heading(text[:120]):
            chunks.append(text)
    for match in _LIST_RE.finditer(source or ''):
        text = _list_text(match.group(2))
        if len(text) > 40 and not _chrome_heading(text[:120]):
            chunks.append(text)
    if not chunks:
        return []
    return [_make_section('Details', ' '.join(chunks), kind='about')]


def _definition_list_text(inner: str) -> str:
    terms = [_strip_html(match.group(2)) for match in _DT_RE.finditer(inner or '')]
    definitions = [_strip_html(match.group(2)) for match in _DD_RE.finditer(inner or '')]
    lines = []
    for index, term in enumerate(terms):
        definition = definitions[index] if index < len(definitions) else ''
        line = ': '.join(part for part in (term, definition) if part)
        if line:
            lines.append(line)
    return ' '.join(lines)


def _table_text(inner: str) -> str:
    rows = []
    for match in _TR_RE.finditer(inner or ''):
        cells = [_strip_html(cell.group(2)) for cell in _CELL_RE.finditer(match.group(2))]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(' | '.join(cells))
    return ' '.join(rows)


def _list_text(inner: str) -> str:
    items = [_strip_html(match.group(2)) for match in _LI_RE.finditer(inner or '')]
    return ' '.join(item for item in items if len(item) > 20)


def _sections_from_paragraphs(source: str) -> list[dict]:
    paragraphs = [
        _strip_html(inner)
        for _attrs, inner in _PARAGRAPH_RE.findall(source or '')
    ]
    paragraphs = [paragraph for paragraph in paragraphs if len(paragraph) >= 40 and not _chrome_heading(paragraph[:80])]
    if not paragraphs:
        text = _strip_html(source or '')
        paragraphs = [text] if len(text) >= 40 else []
    sections = []
    bucket = []
    size = 0
    for paragraph in paragraphs:
        if size + len(paragraph) > SECTION_TEXT_LIMIT and bucket:
            sections.append(_make_section('Overview', ' '.join(bucket)))
            bucket = [paragraph]
            size = len(paragraph)
        else:
            bucket.append(paragraph)
            size += len(paragraph)
        if len(sections) >= 6:
            break
    if bucket and len(sections) < 6:
        sections.append(_make_section('Overview', ' '.join(bucket)))
    return sections


def _meta_description(html: str) -> str:
    for pattern in _META_DESC_RES:
        match = pattern.search(html or '')
        if match:
            text = _strip_html(unescape(match.group(1)))
            if text:
                return text
    return ''


def _make_section(heading: str, text: str, kind: str = '') -> dict:
    cleaned_heading = ' '.join((heading or 'Overview').split())[:120] or 'Overview'
    cleaned_text = ' '.join((text or '').split())
    resolved = kind or _classify_section_heading(cleaned_heading, cleaned_text, '')
    return {'heading': cleaned_heading, 'kind': resolved, 'text': cleaned_text}


def _classify_section_heading(heading: str, text: str, parent_kind: str = '') -> str:
    kind = _kind_from_haystack(heading)
    if kind == 'other' and parent_kind in {'founder', 'team', 'history', 'program', 'impact', 'about'}:
        return parent_kind
    if kind == 'other' and (heading or '').strip().lower() in {'overview', 'summary', 'home', 'introduction', ''}:
        kind = _kind_from_haystack((text or '')[:180])
    if kind == 'other' and re.search(r'\b(co-?founder|founder)\b', f'{heading} {text[:160]}', re.I):
        return 'founder'
    return kind


def _kind_from_haystack(haystack: str) -> str:
    lowered = (haystack or '').lower()
    for kind, hints in _SECTION_KIND_HINTS:
        if any(hint in lowered for hint in hints):
            return kind
    return 'other'


def _chrome_heading(value: str) -> bool:
    return bool(_CHROME_HEADING_RE.search(value or ''))


def _merge_person_sections(sections: list[dict]) -> list[dict]:
    merged = []
    for section in sections:
        if (
            merged
            and section.get('kind') in {'team', 'founder'}
            and merged[-1].get('kind') in {'team', 'founder'}
        ):
            previous = merged[-1]
            previous['kind'] = 'founder' if 'founder' in {previous['kind'], section['kind']} else 'team'
            previous['text'] = ' '.join(
                part for part in (previous.get('text'), section.get('heading'), section.get('text')) if part
            )
            heading = str(section.get('heading') or '').strip()
            if heading and heading.lower() not in str(previous.get('heading') or '').lower():
                previous['heading'] = f"{previous['heading']} / {heading}"[:120]
            continue
        merged.append(dict(section))
    return merged


def _select_page_sections(sections: list[dict]) -> list[dict]:
    merged = _merge_person_sections([section for section in sections if str(section.get('text') or '').strip()])
    if not merged:
        return []
    chosen = []
    used = set()
    for kind in ('founder', 'team', 'history', 'program', 'impact', 'about'):
        for index, section in enumerate(merged):
            if index in used or section.get('kind') != kind:
                continue
            chosen.append(index)
            used.add(index)
            break
        if len(chosen) >= MAX_SECTIONS_PER_PAGE:
            break
    if len(chosen) < MAX_SECTIONS_PER_PAGE:
        for kind in ('program', 'impact', 'history', 'founder', 'team', 'about'):
            for index, section in enumerate(merged):
                if index in used or section.get('kind') != kind:
                    continue
                chosen.append(index)
                used.add(index)
                if len(chosen) >= MAX_SECTIONS_PER_PAGE:
                    break
            if len(chosen) >= MAX_SECTIONS_PER_PAGE:
                break
    preferred_body = any(
        merged[index].get('kind') in {'founder', 'team', 'history', 'program', 'impact'}
        and len(str(merged[index].get('text') or '')) > 40
        for index in chosen
    )
    if not preferred_body:
        extras = [
            index for index, section in enumerate(merged)
            if index not in used
            and section.get('heading') not in {'Summary', 'Structured data'}
            and len(str(section.get('text') or '')) > 80
        ]
        extras.sort(key=lambda index: len(str(merged[index].get('text') or '')), reverse=True)
        for index in extras[:2]:
            chosen.append(index)
            used.add(index)
        for index, section in enumerate(merged):
            if index in used or len(chosen) >= MAX_SECTIONS_PER_PAGE:
                continue
            if len(re.findall(r'[A-Z][a-z]+', str(section.get('heading') or ''))) >= 2:
                chosen.append(index)
                used.add(index)
    if not chosen:
        ranked = sorted(range(len(merged)), key=lambda index: len(merged[index].get('text') or ''), reverse=True)
        chosen = ranked[:2]
    trimmed = []
    for index in sorted(chosen)[:MAX_SECTIONS_PER_PAGE]:
        section = dict(merged[index])
        limit = SECTION_TEXT_LIMIT if section.get('kind') == 'about' or section.get('heading') in {'Summary', 'Structured data'} else TEAM_SECTION_TEXT_LIMIT
        section['text'] = str(section.get('text') or '')[:limit]
        trimmed.append(section)
    return trimmed


def _snippet_from_sections(sections: list[dict]) -> str:
    text = ' '.join(
        f"{section.get('heading') or ''}: {section.get('text') or ''}".strip()
        for section in sections or []
        if section.get('text')
    )
    return ' '.join(text.split())[:PAGE_EXCERPT_LIMIT]


def _page_kind_from_sections(link: str, sections: list[dict]) -> str:
    url_kind = classify_profile_url(link)
    if url_kind not in {'home', 'page'}:
        return url_kind
    best = max(sections, key=lambda section: SECTION_KIND_PRIORITY.get(section.get('kind'), 0))
    return str(best.get('kind') or url_kind)


def extract_jsonld_text(html: str) -> str:
    chunks = []
    for raw in _JSONLD_RE.findall(html or ''):
        cleaned = unescape(raw or '').strip()
        if not cleaned:
            continue
        try:
            parsed = json.loads(cleaned)
        except (TypeError, ValueError, json.JSONDecodeError):
            chunks.append(_strip_html(cleaned)[:800])
            continue
        chunks.append(_flatten_jsonld(parsed)[:1200])
    return ' '.join(chunk for chunk in chunks if chunk).strip()


def classify_profile_url(link: str) -> str:
    path = urlparse(link or '').path.lower()
    for hint in ('founder', 'team', 'about', 'story', 'history', 'leadership', 'people'):
        if hint in path:
            return hint
    if not path.strip('/'):
        return 'home'
    return 'page'


def _search_google_custom(query: str, num_results: int, date_restrict: str) -> dict:
    payload = {
        'source': 'google_custom_search',
        'query': query,
        'items': [],
        'error': '',
        'configured': True,
    }
    params = {
        'key': get_google_search_api_key(),
        'cx': get_google_search_engine_id(),
        'q': query,
        'num': max(1, min(int(num_results or 5), 10)),
    }
    if str(date_restrict or '').strip():
        params['dateRestrict'] = str(date_restrict).strip()

    try:
        with without_local_proxies():
            response = requests.get(
                GOOGLE_CUSTOM_SEARCH_URL,
                params=params,
                timeout=15,
                proxies={'http': None, 'https': None},
            )
        response.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning('Google Custom Search failed for query %s: %s', query, exc)
        payload['error'] = f'Google search request failed: {exc}'
        return payload

    raw_payload = response.json() if response.content else {}
    api_error = ((raw_payload or {}).get('error') or {}).get('message')
    if api_error:
        payload['error'] = str(api_error)
        return payload

    items = []
    for item in (raw_payload.get('items') or [])[: max(1, min(int(num_results or 5), 10))]:
        if not isinstance(item, dict):
            continue
        items.append({
            'title': str(item.get('title') or '').strip(),
            'link': str(item.get('link') or '').strip(),
            'display_link': str(item.get('displayLink') or '').strip(),
            'snippet': str(item.get('snippet') or '').strip(),
        })

    payload['items'] = items
    if not items:
        payload['error'] = 'No public search results were returned.'
    return payload


def _search_duckduckgo(query: str, num_results: int = 5) -> dict:
    payload = {
        'source': 'duckduckgo',
        'query': query,
        'items': [],
        'error': '',
        'configured': True,
    }
    try:
        response = _public_request(
            'POST',
            DUCKDUCKGO_HTML_URL,
            data={'q': query},
            headers=_BROWSER_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning('DuckDuckGo search failed for query %s: %s', query, exc)
        payload['error'] = PUBLIC_SEARCH_UNREACHABLE
        return payload
    if _duckduckgo_is_blocked(response.status_code, response.text):
        current_app.logger.warning('DuckDuckGo blocked the search for query %s', query)
        payload['error'] = PUBLIC_SEARCH_UNREACHABLE
        return payload

    items = parse_duckduckgo_html(response.text)[: max(1, min(int(num_results or 5), 10))]
    payload['items'] = items
    if not items:
        payload['error'] = 'No public search results were returned.'
    return payload


def _search_bing(query: str, num_results: int = 5) -> dict:
    payload = {
        'source': 'bing',
        'query': query,
        'items': [],
        'error': '',
        'configured': True,
    }
    try:
        response = _public_request(
            'GET',
            BING_SEARCH_URL,
            params={'q': query},
            headers=_BROWSER_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning('Bing search failed for query %s: %s', query, exc)
        payload['error'] = PUBLIC_SEARCH_UNREACHABLE
        return payload

    items = parse_bing_html(response.text)[: max(1, min(int(num_results or 5), 10))]
    payload['items'] = items
    if not items:
        payload['error'] = 'No public search results were returned.'
    return payload


def _public_request(method: str, url: str, **kwargs):
    """Reach the public web directly, then through the local proxy if that cannot resolve hosts."""
    timeout = kwargs.pop('timeout', 15)
    headers = kwargs.pop('headers', None) or _BROWSER_HEADERS
    attempts = ['env', 'direct'] if os.environ.get('CURSOR_SANDBOX') else ['direct', 'env']
    last_error = None
    for attempt in attempts:
        try:
            if attempt == 'direct':
                with without_local_proxies():
                    return requests.request(
                        method,
                        url,
                        headers=headers,
                        timeout=timeout,
                        proxies={'http': None, 'https': None},
                        **kwargs,
                    )
            return requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
    raise last_error


def _duckduckgo_is_blocked(status_code: int, html: str) -> bool:
    if int(status_code or 0) in {202, 403, 429}:
        return True
    text = (html or '').lower()
    return 'anomaly-modal' in text or 'bots use duckduckgo' in text


def parse_duckduckgo_html(html: str) -> list[dict]:
    """Extract result cards from DuckDuckGo's no-JS HTML endpoint."""
    items = []
    seen = set()
    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'(?:.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>)?',
        re.I | re.S,
    )
    for href, title_html, snippet_html in pattern.findall(html or ''):
        link = _unwrap_duckduckgo_link(unescape(href or ''))
        title = _strip_html(title_html)
        snippet = _strip_html(snippet_html)
        if not link or not link.startswith('http') or link in seen:
            continue
        host = urlparse(link).netloc.lower()
        if host.endswith('duckduckgo.com'):
            continue
        seen.add(link)
        items.append({
            'title': title,
            'link': link,
            'display_link': host,
            'snippet': snippet,
        })
    return items


def parse_bing_html(html: str) -> list[dict]:
    """Extract organic results from a Bing results page."""
    items = []
    seen = set()
    pattern = re.compile(
        r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>',
        re.I | re.S,
    )
    for match in pattern.finditer(html or ''):
        link = _unwrap_bing_link(unescape(match.group(1) or ''))
        title = _strip_html(match.group(2))
        if not link or not link.startswith('http') or link in seen:
            continue
        host = urlparse(link).netloc.lower()
        if not host or host.endswith('bing.com'):
            continue
        tail = (html or '')[match.end():match.end() + 900]
        snippet_match = re.search(r'<p[^>]*>(.*?)</p>', tail, re.I | re.S)
        snippet = _strip_html(snippet_match.group(1) if snippet_match else '')
        seen.add(link)
        items.append({
            'title': title,
            'link': link,
            'display_link': host,
            'snippet': snippet,
        })
    return items


def _unwrap_bing_link(href: str) -> str:
    if not href:
        return ''
    parsed = urlparse(href)
    wrapped = parse_qs(parsed.query).get('u') or []
    token = wrapped[0] if wrapped else ''
    if token.startswith('a1'):
        raw = token[2:]
        padding = '=' * (-len(raw) % 4)
        try:
            decoded = base64.b64decode(raw + padding).decode('utf-8', 'ignore').strip()
        except Exception:
            decoded = ''
        if decoded.startswith('http'):
            return decoded
    if href.startswith('http') and 'bing.com/ck' not in href:
        return href.strip()
    return ''


def _unwrap_duckduckgo_link(href: str) -> str:
    if not href:
        return ''
    parsed = urlparse(href)
    if 'uddg=' in href:
        values = parse_qs(parsed.query).get('uddg') or []
        if values:
            return unquote(values[0]).strip()
    return href.strip()


def _strip_html(value: str) -> str:
    return ' '.join(_TAG_RE.sub(' ', unescape(value or '')).split()).strip()


def _should_fetch_page(link: str) -> bool:
    host = urlparse(link or '').netloc.lower()
    if not host or not (link or '').startswith('http'):
        return False
    return not any(host == skip or host.endswith('.' + skip) for skip in _SKIP_FETCH_HOSTS)


def _fetch_page_excerpt(link: str) -> str:
    html = _fetch_html(link)
    return extract_page_excerpt(html) if html else ''


def _fetch_html(link: str, headers: dict | None = None, timeout: int = 8) -> str:
    try:
        response = _public_request(
            'GET',
            link,
            headers=headers or _BROWSER_HEADERS,
            timeout=timeout,
        )
        if response.status_code >= 400 or not response.text:
            return ''
        return response.text[:2500000]
    except requests.RequestException:
        return ''


def download_profile_image(url: str, max_bytes: int = 2_000_000) -> tuple[bytes, str, str] | None:
    """Download a logo, including an SVG that wraps the real mark as a PNG."""
    path = unquote(urlparse(str(url or '')).path or '')
    if path.lower().endswith('.svg'):
        return _download_svg_logo(url, max_bytes=max_bytes)
    return download_public_image(url, max_bytes=max_bytes)


def _download_svg_logo(url: str, max_bytes: int) -> tuple[bytes, str, str] | None:
    parsed = urlparse(str(url or '').strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host or host in {'localhost', '127.0.0.1'} or host.endswith('.local'):
        return None
    try:
        response = _public_request('GET', parsed.geturl(), timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    payload = response.content or b''
    if len(payload) < 80 or len(payload) > max_bytes:
        return None
    if b'<svg' not in payload[:2500].lower():
        return None
    raster = _raster_embedded_in_svg(payload, max_bytes=max_bytes)
    if raster:
        return raster
    if _svg_is_safe(payload):
        return payload, 'image/svg+xml', 'svg'
    return None


def _raster_embedded_in_svg(payload: bytes, max_bytes: int) -> tuple[bytes, str, str] | None:
    text = payload.decode('utf-8', errors='ignore')
    match = re.search(
        r'data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\s]+)',
        text,
        re.I,
    )
    if not match:
        return None
    try:
        raw = base64.b64decode(re.sub(r'\s+', '', match.group(2))[: max_bytes * 2], validate=False)
    except (ValueError, TypeError):
        return None
    if len(raw) < 2000 or len(raw) > max_bytes:
        return None
    kind = match.group(1).lower()
    if kind == 'png' and raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return raw, 'image/png', 'png'
    if kind in {'jpeg', 'jpg'} and raw.startswith(b'\xff\xd8\xff'):
        return raw, 'image/jpeg', 'jpg'
    if kind == 'webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return raw, 'image/webp', 'webp'
    return None


def _svg_is_safe(payload: bytes) -> bool:
    text = payload.decode('utf-8', errors='ignore').lower()
    if any(token in text for token in ('<script', 'javascript:', 'foreignobject', 'onload=', 'onerror=')):
        return False
    return b'<svg' in payload[:2500].lower()


def download_public_image(url: str, max_bytes: int = 2_000_000) -> tuple[bytes, str, str] | None:
    """Download one https image that research already selected."""
    parsed = urlparse(str(url or '').strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host or host in {'localhost', '127.0.0.1'} or host.endswith('.local'):
        return None
    try:
        response = _public_request('GET', parsed.geturl(), timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    final_host = (urlparse(response.url).hostname or '').lower()
    if final_host in {'localhost', '127.0.0.1'} or final_host.endswith('.local'):
        return None
    data = response.content or b''
    if len(data) < 2000 or len(data) > max_bytes:
        return None
    if data.startswith(b'\xff\xd8\xff'):
        return data, 'image/jpeg', 'jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return data, 'image/png', 'png'
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return data, 'image/gif', 'gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return data, 'image/webp', 'webp'
    return None


def load_research_html(link: str) -> str:
    """Fetch a page, retry with a browser header, then ask a wiki API if it is still empty."""
    first = _fetch_html(link)
    if first and not _html_is_thin(first):
        return first
    retried = _fetch_html(link, headers=_BROWSER_HEADERS, timeout=20)
    best = retried if retried and _html_is_thin(first) and not _html_is_thin(retried) else (first or retried)
    if best and not _html_is_thin(best):
        return best
    if _looks_like_mediawiki(link, best):
        wiki = _fetch_mediawiki_html(link)
        if wiki and not _html_is_thin(wiki):
            return wiki
    return best or ''


def _html_is_bot_wall(html: str) -> bool:
    sample = _strip_html(html or '')[:1800]
    return bool(sample and _BOT_WALL_RE.search(sample))


def _html_is_thin(html: str) -> bool:
    if not str(html or '').strip() or _html_is_bot_wall(html):
        return True
    return _sections_are_thin(extract_page_sections(html))


def _sections_are_thin(sections: list[dict]) -> bool:
    for section in sections or []:
        heading = str(section.get('heading') or '')
        if heading in {'Summary', 'Structured data'}:
            continue
        if len(str(section.get('text') or '')) > 80:
            return False
    return True


def _looks_like_mediawiki(link: str, html: str) -> bool:
    parsed = urlparse(link or '')
    host = parsed.netloc.lower()
    if host.startswith('wiki.') or '.wiki' in host or '/wiki/' in (parsed.path or '').lower():
        return True
    lowered = (html or '').lower()
    return 'mediawiki' in lowered and 'generator' in lowered


def _mediawiki_title(link: str) -> str:
    path = unquote(urlparse(link or '').path or '')
    if '/wiki/' in path:
        title = path.split('/wiki/', 1)[1]
    else:
        title = path.strip('/')
    title = title.split('?', 1)[0].strip('/')
    return title


def _fetch_mediawiki_html(link: str) -> str:
    title = _mediawiki_title(link)
    parsed = urlparse(link or '')
    if not title or not parsed.netloc:
        return ''
    origin = f'{parsed.scheme or "https"}://{parsed.netloc}'
    for api in (f'{origin}/api.php', f'{origin}/w/api.php'):
        try:
            with without_local_proxies():
                response = requests.get(
                    api,
                    params={
                        'action': 'parse',
                        'page': title,
                        'prop': 'text',
                        'format': 'json',
                        'redirects': 1,
                    },
                    headers=_BROWSER_HEADERS,
                    timeout=20,
                    proxies={'http': None, 'https': None},
                )
            if response.status_code >= 400:
                continue
            payload = response.json() if response.content else {}
        except (requests.RequestException, ValueError):
            continue
        html = ((payload.get('parse') or {}).get('text') or {}).get('*') or ''
        if html:
            return html
    return ''


def indexed_snippet_queries(name: str, items: list[dict]) -> list[str]:
    """Ask the search index for more sentences from hosts that do not serve a public page."""
    cleaned = ' '.join((name or '').split())
    if not cleaned:
        return []
    hosts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        host = urlparse(str(item.get('link') or '')).netloc.lower().removeprefix('www.')
        for blocked in INDEXED_SNIPPET_HOSTS:
            if host == blocked or host.endswith('.' + blocked):
                if blocked not in hosts:
                    hosts.append(blocked)
                break
        if len(hosts) >= 4:
            break
    return [f'"{cleaned}" site:{host}' for host in hosts]


def _render_thin_pages(pages: dict[str, str], urls: list[str]) -> None:
    rendered = 0
    for link in urls:
        if rendered >= RENDER_FETCH_LIMIT:
            return
        if not _html_is_thin(pages.get(link) or ''):
            continue
        richer = _render_html(link)
        if richer and not _html_is_bot_wall(richer) and not _html_is_thin(richer):
            pages[link] = richer
            rendered += 1


def _chrome_binary() -> str:
    candidates = [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        shutil.which('google-chrome') or '',
        shutil.which('chromium') or '',
        shutil.which('chromium-browser') or '',
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ''


def _render_html(link: str) -> str:
    binary = _chrome_binary()
    if not binary or not link:
        return ''
    try:
        completed = subprocess.run(
            [
                binary,
                '--headless=new',
                '--disable-gpu',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--virtual-time-budget=12000',
                '--timeout=20000',
                '--dump-dom',
                link,
            ],
            capture_output=True,
            text=True,
            timeout=35,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return (completed.stdout or '')[:2500000]


def _fetch_pages_parallel(urls: list[str]) -> dict[str, str]:
    pages = {}
    if not urls:
        return pages
    workers = min(4, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(load_research_html, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                pages[url] = future.result() or ''
            except Exception:
                pages[url] = ''
    return pages


def _dedupe_items(items: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get('link') or '').strip()
        key = link.lower().rstrip('/') if link else str(item.get('title') or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))
    return merged


def _deep_dive_score(item: dict, name: str) -> int:
    link = str(item.get('link') or '').lower()
    title = str(item.get('title') or '').lower()
    score = 0
    for hint in PROFILE_PATH_HINTS:
        if hint in link or hint in title:
            score += 4
    host = urlparse(link).netloc.lower().replace('-', '').replace('.', '')
    compact_name = re.sub(r'[^a-z0-9]+', '', (name or '').lower())
    if compact_name and compact_name[:6] in host:
        score += 3
    if str(item.get('page_kind') or '') in PROFILE_PATH_HINTS:
        score += 2
    return score


def _item_from_url(link: str, title: str) -> dict:
    host = urlparse(link).netloc.lower()
    return {
        'title': title,
        'link': link,
        'display_link': host,
        'snippet': '',
        'page_kind': classify_profile_url(link),
    }


def _html_title(html: str) -> str:
    match = re.search(r'<title[^>]*>(.*?)</title>', html or '', re.I | re.S)
    if not match:
        return ''
    return ' '.join(_strip_html(match.group(1)).split())[:160]


def _title_from_url(link: str, name: str) -> str:
    kind = classify_profile_url(link)
    label = {
        'about': 'About',
        'team': 'Team',
        'founder': 'Founders',
        'story': 'Our story',
        'history': 'History',
        'leadership': 'Leadership',
        'people': 'People',
        'home': 'Home',
    }.get(kind, kind.title() if kind else 'Page')
    return f'{name} {label}'.strip() if name else label


def _flatten_jsonld(value, depth: int = 0) -> str:
    if depth > 4:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ' '.join(_flatten_jsonld(item, depth + 1) for item in value if item)
    if isinstance(value, dict):
        parts = []
        for key in (
            'name', 'jobTitle', 'founder', 'employee', 'member', 'description', 'foundingDate',
            'address', 'email', 'telephone', 'areaServed', 'foundingLocation',
            'streetAddress', 'addressLocality', 'addressRegion', 'addressCountry',
        ):
            if key in value:
                parts.append(_flatten_jsonld(value.get(key), depth + 1))
        return ' '.join(part for part in parts if part)
    return ''


def gemini_http_options() -> types.HttpOptions:
    return types.HttpOptions(
        timeout=45000,
        client_args={'trust_env': False, 'proxy': None},
    )


@contextmanager
def without_local_proxies():
    """Cursor's local proxy can block outbound HTTPS; search should go direct."""
    saved = {key: os.environ.pop(key) for key in _PROXY_ENV_KEYS if key in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
