"""Google Search + AI enrichment for CBO profiles, with SMS-safe verification."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

from flask import current_app
from google import genai
from google.genai import types

from .firebase_storage_service import store_supporting_file
from .google_search_service import (
    SECTION_KIND_PRIORITY,
    _program_text_is_chrome,
    download_profile_image,
    download_public_image,
    enrich_research_corpus,
    gemini_http_options,
    google_search_enabled,
    indexed_snippet_queries,
    official_homepage_url,
    search_cbo_with_gemini,
    search_google,
    without_local_proxies,
)
from .models import CBO, SmsOnboardingSession, db
from .narrative_profile_service import merge_program_lists, program_key


YES_RE = re.compile(
    r"^\s*(yes|y|yeah|yep|correct|looks good|that'?s right|all (good|correct)|ok|okay|true)\s*[.!]?\s*$",
    re.I,
)
NO_RE = re.compile(r"^\s*(no|n|wrong|incorrect|not right|false|none)\s*[.!]?\s*$", re.I)
SKIP_RE = re.compile(r"^\s*(skip|later|idk|pass)\s*[.!]?\s*$", re.I)
CLAIM_ID_RE = re.compile(r'\b(?:fact|item|number|#)?\s*(1[0-2]|[1-9])\b', re.I)
PERSON_ROLE_RE = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z.\']+){1,3})\s+(?:[-–:]\s*)?'
    r'(co-?founder|founder|chair(?:person)?|director|chief executive|ceo|'
    r'hub manager|systems engineer|product development engineer|'
    r'office administrator|research and development)\b',
    re.I,
)

PLACEHOLDER_FOCUS_AREAS = {
    'rural livelihood, subsistence agriculture, youth',
    'community development, local service delivery',
}
LEADERSHIP_PLACEHOLDERS = {
    'community leader',
    'program manager',
    'finance lead',
    'chairperson',
    'director',
}
WEB_SECTION_TITLE = 'From public sources'
MAX_CLAIMS = 10
MAX_SYNTHESIS_PAGES = 8
PREFERRED_SECTION_KINDS = {'founder', 'team', 'history', 'program', 'impact', 'about'}
ALLOWED_CLASSIFICATIONS = {
    'tools', 'education', 'healthcare', 'agriculture', 'water', 'livelihood', 'environment', 'community',
}
_PERSON_NAME_STOPWORDS = {
    'our', 'team', 'the', 'story', 'about', 'founder', 'co-founder', 'cofounder',
    'staff', 'mission', 'history', 'board', 'leadership', 'summary', 'structured',
    'data', 'search', 'snippet', 'overview', 'who', 'are', 'we',
}
SUGGESTION_FIELDS = (
    ('tagline', 'Tagline'),
    ('focus_areas', 'Focus areas'),
    ('location', 'Location'),
    ('website', 'Website'),
    ('founded_year', 'Year founded'),
    ('founder', 'Founder'),
    ('chairperson', 'Chair'),
    ('program_director', 'Director'),
    ('finance_lead', 'Finance lead'),
    ('team', 'Team'),
    ('history', 'Background / history'),
    ('flagship_summary', 'Flagship / programs'),
    ('success_story', 'Impact story'),
    ('classifications', 'Classifications'),
)

IDENTITY_PROMPT = """You decide which search results are about one community-based organisation (CBO).

The target is always a CBO: a local community group. A shared name is not enough. Reject companies, certification bodies, banks, schools, government agencies, and multinationals, and reject groups in a different place or line of work.

Return ONLY JSON:
{
  "matches_found": false,
  "reason": "one sentence a profile editor can read",
  "matching_urls": []
}

Rules:
- Copy matching_urls exactly from the supplied results. Include a URL only when that result is this CBO.
- A short name may be the acronym of a longer Kenyan CBO name. A page that uses both the short name and that longer name is a match.
- Use the known location and focus when they are real. Ignore blank fields.
- An empty location does not make a foreign company or a commercial namesake a match.
- If none of the results are this CBO, matches_found is false and matching_urls is empty.
"""

SYNTHESIS_PROMPT = """You extract cautious public facts about one community-based organisation (CBO) from labeled page sections.

The target is always a CBO, a local community group. Decide first whether these sections are about that CBO. A shared name is not enough. Companies, certification bodies, and groups in another country or sector are different organisations.

Each supplied page has already been split into sections with a heading, a kind, and the text of that section. Read the section kind first:
- founder and team sections: named people and their roles
- history sections: origin story, why the organisation started, year founded
- program and impact sections: what they run and what has happened
- about sections: identity, location, website, focus

Return ONLY JSON:
{
  "same_organisation": true,
  "summary": "1-2 sentence overview, or a sentence naming the mismatch when same_organisation is false",
  "claims": [
    {
      "id": 1,
      "headline": "short headline, max 90 characters",
      "fact": "one sentence grounded in a single section",
      "category": "founder|team|history|program|impact|identity|other",
      "source_title": "page title",
      "source_url": "https://...",
      "section_heading": "heading of the section the fact came from",
      "evidence_quote": "verbatim contiguous excerpt copied from that section, 8 to 40 words"
    }
  ],
  "profile_suggestions": {
    "tagline": "",
    "focus_areas": "",
    "location": "",
    "website": "",
    "founded_year": "",
    "founder": "",
    "chairperson": "",
    "program_director": "",
    "finance_lead": "",
    "team": [{"name": "", "role": ""}],
    "history": "",
    "classifications": [],
    "flagship_summary": "",
    "success_story": "",
    "programs": [{"name": "", "description": "", "who_it_serves": ""}],
    "milestones": [{"when": "2015", "title": "", "summary": "", "status": "done"}],
    "future_goals": [{"when": "", "title": "", "summary": "", "status": "planned"}]
  }
}

Rules:
- If the sections describe a different organisation, set same_organisation to false, return an empty claims array, leave every profile_suggestions field empty, and say so in summary.
- Use only sections about this CBO. Do not invent names, years, grants, or impact numbers.
- evidence_quote must be copied from the cited section text, not paraphrased. source_url must be that section's page link. section_heading must be that section's heading.
- Fill a profile field only when a section supports it. Leave founder, team, history, and founded_year empty when the only text is a homepage banner or a news snippet that does not state them.
- Include at most 10 claims. Cover the founder, the origin story, and each distinct program the sections support.
- When a page names separate programs, return each one in programs with the activity it describes. Do not collapse those programs into flagship_summary. Include crops, services, who is served, and numbers when a section states them.
- history is the origin story. Leave it empty when the only text is a tagline.
- milestones are separate dated events the sections state: founding, registration, a program or partnership starting, or a result tied to a year. Do not merge founding and registration into one step. when is the year or date copied from the section. status is done, or in_progress when the section says the work is still underway.
- future_goals are intentions a section states, such as a planned expansion, a target year, or a stated aim. status is planned when the page gives a concrete next step, or aspiration when it is a hope without a date. Leave future_goals empty when no section states a plan. Do not invent goals or years.
- Extract every named person a team or founder section supports.
- Prefer facts that add to the known profile. Do not repeat known profile text.
- classifications must be a subset of: tools, education, healthcare, agriculture, water, livelihood, environment, community.
- team must be an array of {name, role} objects.
"""


def research_cbo_online(name: str, location: str = '', extra_context: str = '') -> dict:
    """Search the public web and synthesise candidate profile facts."""
    cleaned_name = ' '.join((name or '').split())
    cleaned_location = ' '.join((location or '').split())
    payload = {
        'status': 'ready',
        'cbo_name': cleaned_name,
        'location': cleaned_location,
        'queried_at': datetime.utcnow().isoformat(),
        'configured': google_search_enabled(),
        'queries': [],
        'search_results': [],
        'claims': [],
        'summary': '',
        'profile_suggestions': {},
        'error': '',
    }
    if not cleaned_name:
        payload['status'] = 'skipped'
        payload['error'] = 'A CBO name is required before public-web research can run.'
        return payload

    grounded = search_cbo_with_gemini(
        cleaned_name,
        cleaned_location,
        _profile_context_for_model(extra_context),
    )
    if grounded.get('items'):
        items = grounded['items']
        errors = []
        source = grounded.get('source') or 'gemini_google_search'
        payload['queries'] = list(grounded.get('queries') or [])
        if grounded.get('summary'):
            payload['summary'] = grounded['summary']
    else:
        search_plan = _research_queries(cleaned_name, cleaned_location)
        payload['queries'] = [query for query, _restrict in search_plan]
        items, errors, source = _run_search_plan(search_plan)
    payload['source'] = source

    items = [item for item in items if _item_mentions_org(item, cleaned_name)]
    items, mismatch = _filter_items_by_identity(
        cleaned_name,
        cleaned_location,
        extra_context,
        items,
    )
    if not items:
        payload['search_results'] = []
        payload['status'] = 'ready'
        payload['identity_mismatch'] = True
        payload['summary'] = mismatch or (
            f'No public pages clearly match the CBO {cleaned_name}, so no facts were added.'
        )
        payload['error'] = errors[0] if errors and not mismatch else ''
        return payload

    official = official_homepage_url(cleaned_name, items)
    if official and source != 'gemini_google_search':
        host = urlparse(official).netloc
        follow_items, follow_errors, follow_source = _run_search_plan([
            (f'site:{host} (about OR team OR founder OR "our story" OR leadership OR history)', ''),
        ])
        items = _merge_search_items(items, follow_items)
        errors.extend(follow_errors)
        payload['source'] = follow_source or payload.get('source')
        payload['queries'].append(f'site:{host} about OR team OR founder')

    items = [item for item in items if _item_mentions_org(item, cleaned_name)]
    snippet_queries = [] if source == 'gemini_google_search' else indexed_snippet_queries(cleaned_name, items)
    if snippet_queries:
        follow_items, follow_errors, follow_source = _run_search_plan(
            [(query, '') for query in snippet_queries],
            num_results=5,
        )
        items = _merge_search_items(items, follow_items)
        errors.extend(follow_errors)
        payload['source'] = follow_source or payload.get('source')
        payload['queries'].extend(snippet_queries)
        items = [item for item in items if _item_mentions_org(item, cleaned_name)]
    matched_hosts = _hosts_for_items(items, cleaned_name)
    items = enrich_research_corpus(cleaned_name, items, fetch_limit=10, discover_related=True)
    items = [
        item for item in items
        if _item_mentions_org(item, cleaned_name) or _item_host(item) in matched_hosts
    ]
    if not items:
        payload['search_results'] = []
        payload['status'] = 'ready'
        payload['error'] = errors[0] if errors else 'No public search results were returned.'
        payload['summary'] = f'No clearly matching public pages were found for {cleaned_name}.'
        return payload

    synthesized = _synthesize_research(cleaned_name, cleaned_location, extra_context, items)
    payload['search_results'] = [_stored_search_item(item) for item in items]
    payload['summary'] = str(synthesized.get('summary') or '').strip()
    suggestions = dict(synthesized.get('profile_suggestions') or {})
    page_programs = _programs_from_pages(items)
    if page_programs or suggestions.get('programs'):
        suggestions['programs'] = _merge_program_records(page_programs, suggestions.get('programs'))
    payload['profile_suggestions'] = suggestions
    payload['media'] = _media_from_pages(items)
    payload['profile_images'] = _profile_images_from_pages(items)
    payload['claims'] = _normalize_claims(synthesized.get('claims') or [], items)
    payload['grounding_dropped_claims'] = int(synthesized.get('grounding_dropped_claims') or 0)
    return payload


def parse_web_verification_reply(message: str, claims: list[dict] | None = None) -> dict:
    """Interpret a CBO leader's SMS reply about scraped public facts."""
    text = ' '.join((message or '').split()).strip()
    claims = [dict(item) for item in (claims or []) if isinstance(item, dict)]
    result = {
        'decision': 'unknown',
        'verified_ids': [],
        'rejected_ids': [],
        'note': text,
    }
    if not text:
        return result
    if YES_RE.match(text):
        result['decision'] = 'accept_all'
        result['verified_ids'] = [int(item.get('id') or 0) for item in claims if item.get('id')]
        return result
    if NO_RE.match(text):
        result['decision'] = 'reject_all'
        result['rejected_ids'] = [int(item.get('id') or 0) for item in claims if item.get('id')]
        return result
    if SKIP_RE.match(text):
        result['decision'] = 'skip'
        return result

    mentioned = [int(match) for match in CLAIM_ID_RE.findall(text)]
    lowered = text.lower()
    reject_hint = any(word in lowered for word in ('wrong', 'no', 'incorrect', 'not', 'false', 'fix'))
    accept_hint = any(word in lowered for word in ('yes', 'correct', 'right', 'true', 'ok'))
    if mentioned:
        if reject_hint and not accept_hint:
            result['decision'] = 'partial'
            result['rejected_ids'] = mentioned
            result['verified_ids'] = [
                int(item.get('id') or 0)
                for item in claims
                if int(item.get('id') or 0) and int(item['id']) not in mentioned
            ]
            return result
        if accept_hint and not reject_hint:
            result['decision'] = 'partial'
            result['verified_ids'] = mentioned
            result['rejected_ids'] = [
                int(item.get('id') or 0)
                for item in claims
                if int(item.get('id') or 0) and int(item['id']) not in mentioned
            ]
            return result
        result['decision'] = 'partial'
        result['rejected_ids'] = mentioned
        result['verified_ids'] = [
            int(item.get('id') or 0)
            for item in claims
            if int(item.get('id') or 0) and int(item['id']) not in mentioned
        ]
        return result

    if reject_hint:
        result['decision'] = 'reject_all'
        result['rejected_ids'] = [int(item.get('id') or 0) for item in claims if item.get('id')]
        return result
    result['decision'] = 'unknown'
    return result


def apply_verification_decision(research: dict, decision: dict) -> dict:
    updated = json.loads(json.dumps(research or {}, default=str))
    claims = updated.get('claims') if isinstance(updated.get('claims'), list) else []
    choice = str(decision.get('decision') or '').strip()
    verified_ids = {int(value) for value in (decision.get('verified_ids') or []) if str(value).isdigit() or isinstance(value, int)}
    rejected_ids = {int(value) for value in (decision.get('rejected_ids') or []) if str(value).isdigit() or isinstance(value, int)}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = int(claim.get('id') or 0)
        if choice == 'accept_all' or claim_id in verified_ids:
            claim['verified'] = True
            claim['rejected'] = False
        elif choice in {'reject_all', 'skip'} or claim_id in rejected_ids:
            claim['verified'] = False
            claim['rejected'] = choice != 'skip'
        else:
            claim['verified'] = False
            claim['rejected'] = False
    if choice == 'accept_all':
        updated['status'] = 'verified'
    elif choice == 'reject_all':
        updated['status'] = 'rejected'
    elif choice == 'skip':
        updated['status'] = 'skipped'
    elif choice == 'partial':
        updated['status'] = 'verified' if any(claim.get('verified') for claim in claims if isinstance(claim, dict)) else 'pending_review'
    updated['verification'] = {
        'decision': choice,
        'note': str(decision.get('note') or '').strip(),
        'verified_at': datetime.utcnow().isoformat(),
    }
    updated['claims'] = claims
    return updated


def apply_web_research_to_cbo(
    cbo: CBO,
    research: dict | None = None,
    only_verified: bool = True,
    suggestion_keys: list[str] | None = None,
    media_ids: list[str] | None = None,
    profile_image_id: str | None = None,
) -> dict:
    """Merge confirmed public facts into a CBO profile without wiping existing content."""
    payload = research if isinstance(research, dict) else _parse_json(getattr(cbo, 'web_research_json', '') or '{}')
    profile = _parse_json(cbo.ai_profile_json or '{}')
    if not isinstance(profile, dict):
        profile = {}

    claims = [item for item in (payload.get('claims') or []) if isinstance(item, dict)]
    accepted = [item for item in claims if item.get('verified')] if only_verified else [
        item for item in claims if not item.get('rejected')
    ]
    newly_accepted = [item for item in accepted if not item.get('applied')]
    suggestions = payload.get('profile_suggestions') if isinstance(payload.get('profile_suggestions'), dict) else {}
    has_verified = bool(accepted)
    explicit_keys = suggestion_keys is not None
    apply_keys = set(suggestion_keys) if explicit_keys else (
        {key for key, _label in SUGGESTION_FIELDS} if has_verified else set()
    )
    already_applied = {
        str(key).strip()
        for key in (payload.get('applied_suggestion_keys') or [])
        if str(key).strip()
    }
    labels_by_key = dict(SUGGESTION_FIELDS)
    applied_labels = []
    page_programs = _program_records(suggestions.get('programs'))
    program_keys = {f"program:{program_key(item['name'])}" for item in page_programs}
    milestone_steps = _journey_step_records(suggestions.get('milestones'), future=False)
    future_steps = _journey_step_records(suggestions.get('future_goals'), future=True)
    journey_keys = {f"milestone:{_journey_key(step)}" for step in milestone_steps}
    journey_keys |= {f"goal:{_journey_key(step)}" for step in future_steps}
    if not explicit_keys and has_verified:
        apply_keys |= program_keys | journey_keys

    def _should_apply(key: str, current_value, *, treat_placeholder: bool = False) -> bool:
        if key not in apply_keys:
            return False
        if explicit_keys:
            return True
        if treat_placeholder:
            return _is_sparse_profile_value(current_value)
        return not str(current_value or '').strip()

    leadership = profile.get('leadership') if isinstance(profile.get('leadership'), dict) else {}
    narrative = profile.get('narrative_profile') if isinstance(profile.get('narrative_profile'), dict) else {}

    focus_areas = str(suggestions.get('focus_areas') or '').strip()
    if focus_areas and _should_apply('focus_areas', cbo.focus_areas or profile.get('focus_areas'), treat_placeholder=True):
        cbo.focus_areas = focus_areas[:500]
        profile['focus_areas'] = cbo.focus_areas
        applied_labels.append(labels_by_key['focus_areas'])

    tagline = str(suggestions.get('tagline') or '').strip()
    if tagline and _should_apply('tagline', profile.get('tagline')):
        profile['tagline'] = tagline[:180]
        if not str(narrative.get('headline') or '').strip():
            narrative['headline'] = tagline[:180]
        applied_labels.append(labels_by_key['tagline'])

    location = str(suggestions.get('location') or '').strip()
    if location and _should_apply('location', cbo.location or profile.get('location')):
        cbo.location = location[:255]
        profile['location'] = cbo.location
        applied_labels.append(labels_by_key['location'])

    website = str(suggestions.get('website') or '').strip()
    if website and _should_apply('website', profile.get('website')):
        profile['website'] = website[:500]
        applied_labels.append(labels_by_key['website'])

    founded_year = str(suggestions.get('founded_year') or '').strip()
    if founded_year and _should_apply('founded_year', cbo.founded_year or profile.get('founded_year')):
        cbo.founded_year = founded_year[:10]
        profile['founded_year'] = cbo.founded_year
        applied_labels.append(labels_by_key['founded_year'])

    history = str(suggestions.get('history') or '').strip()
    if history and _should_apply('history', profile.get('origin_story')):
        profile['origin_story'] = history
        applied_labels.append(labels_by_key['history'])

    founder = str(suggestions.get('founder') or '').strip()
    chairperson = str(suggestions.get('chairperson') or '').strip()
    program_director = str(suggestions.get('program_director') or '').strip()
    finance_lead = str(suggestions.get('finance_lead') or '').strip()
    team = _normalize_team(suggestions.get('team'))

    if chairperson and _should_apply('chairperson', leadership.get('chairperson') or getattr(cbo, 'chairperson', '')):
        leadership['chairperson'] = chairperson[:255]
        cbo.chairperson = leadership['chairperson']
        applied_labels.append(labels_by_key['chairperson'])
    if program_director and _should_apply(
        'program_director',
        leadership.get('program_director') or getattr(cbo, 'program_director', ''),
    ):
        leadership['program_director'] = program_director[:255]
        cbo.program_director = leadership['program_director']
        applied_labels.append(labels_by_key['program_director'])
    elif founder and _should_apply('founder', leadership.get('program_director') or getattr(cbo, 'program_director', '')):
        leadership['program_director'] = founder[:255]
        cbo.program_director = leadership['program_director']
        applied_labels.append(labels_by_key['founder'])
    elif founder and 'founder' in apply_keys:
        applied_labels.append(labels_by_key['founder'])
    if finance_lead and _should_apply('finance_lead', leadership.get('finance_lead') or getattr(cbo, 'finance_lead', '')):
        leadership['finance_lead'] = finance_lead[:255]
        cbo.finance_lead = leadership['finance_lead']
        applied_labels.append(labels_by_key['finance_lead'])

    if team and 'team' in apply_keys:
        narrative['leadership'] = _merge_people(narrative.get('leadership') or [], team)
        applied_labels.append(labels_by_key['team'])
    elif founder and 'founder' in apply_keys:
        narrative['leadership'] = _merge_people(
            narrative.get('leadership') or [],
            [{'name': founder, 'role': 'Founder'}],
        )

    if leadership:
        profile['leadership'] = leadership

    flagship_summary = str(suggestions.get('flagship_summary') or '').strip()
    existing_flagship = profile.get('flagship_project') if isinstance(profile.get('flagship_project'), dict) else {}
    if flagship_summary and 'flagship_summary' in apply_keys:
        if _is_sparse_profile_value(cbo.flagship_summary) and not existing_flagship.get('summary'):
            cbo.flagship_summary = flagship_summary
            profile['flagship_project'] = {
                'title': existing_flagship.get('title') or 'Public program snapshot',
                'summary': flagship_summary,
                'stats': existing_flagship.get('stats') or [],
            }
        applied_labels.append(labels_by_key['flagship_summary'])

    success_story = str(suggestions.get('success_story') or '').strip()
    existing_story = profile.get('success_story') if isinstance(profile.get('success_story'), dict) else {}
    if success_story and 'success_story' in apply_keys:
        if _is_sparse_profile_value(cbo.success_story) and not existing_story.get('summary'):
            cbo.success_story = success_story
            profile['success_story'] = {
                'title': existing_story.get('title') or 'Public impact note',
                'summary': success_story,
            }
        applied_labels.append(labels_by_key['success_story'])

    classifications = [
        str(item).strip().lower()
        for item in (suggestions.get('classifications') or [])
        if str(item).strip()
    ]
    existing_classes = []
    try:
        parsed_classes = json.loads(cbo.classifications_json or '[]')
        if isinstance(parsed_classes, list):
            existing_classes = parsed_classes
    except (TypeError, ValueError, json.JSONDecodeError):
        existing_classes = []
    if classifications and 'classifications' in apply_keys:
        merged = list(dict.fromkeys(
            [str(item).strip().lower() for item in existing_classes if str(item).strip()]
            + classifications
        ))
        cbo.classifications_json = json.dumps(merged)
        profile['classifications'] = merged
        applied_labels.append(labels_by_key['classifications'])

    narrative, applied_program_keys = _apply_program_suggestions(
        narrative, page_programs, apply_keys, applied_labels,
    )
    added_photos = _apply_research_media(cbo, narrative, payload, media_ids, explicit_keys, has_verified)
    if added_photos:
        applied_labels.append(
            f'{added_photos} program photo{"s" if added_photos != 1 else ""}'
        )
    if _apply_profile_image(cbo, profile, payload, profile_image_id, explicit_keys, has_verified):
        applied_labels.append('Profile image')

    for claim in newly_accepted:
        fact = str(claim.get('fact') or claim.get('headline') or '').strip()
        if fact:
            _append_unique_highlight(profile, fact)
            applied_labels.append(str(claim.get('headline') or fact)[:90])
        claim['applied'] = True

    if apply_keys or newly_accepted:
        _file_web_facts_into_narrative(
            narrative,
            profile,
            suggestions,
            apply_keys,
            newly_accepted,
            payload.get('summary') or '',
        )
        profile['narrative_profile'] = narrative
    elif added_photos:
        profile['narrative_profile'] = narrative

    if applied_labels:
        unique_labels = list(dict.fromkeys(applied_labels))
        used_keys = {
            key for key in apply_keys
            if key in dict(SUGGESTION_FIELDS) and (
                (key == 'team' and _normalize_team(suggestions.get('team')))
                or (key == 'classifications' and _as_string_list(suggestions.get(key)))
                or str(suggestions.get(key) or '').strip()
            )
        }
        used_keys |= applied_program_keys
        used_keys |= {key for key in journey_keys if key in apply_keys}
        payload['applied_suggestion_keys'] = sorted(already_applied | used_keys)
        previous = payload.get('applied_receipt') if isinstance(payload.get('applied_receipt'), dict) else {}
        previous_count = int(previous.get('added_count') or 0)
        payload['applied_receipt'] = {
            'at': datetime.utcnow().isoformat(),
            'added_count': previous_count + len(unique_labels),
            'latest_count': len(unique_labels),
            'labels': unique_labels[:12],
        }

    profile['public_sources'] = serialize_web_research_for_display(payload, accepted)
    cbo.ai_profile_json = json.dumps(profile)
    cbo.web_research_json = json.dumps(payload, default=str)
    cbo.web_research_refreshed_at = datetime.utcnow()
    if any(item.get('verified') for item in claims):
        cbo.web_research_verified_at = datetime.utcnow()
    db.session.add(cbo)
    return profile


def serialize_web_research_for_display(
    research: dict | None,
    accepted_claims: list[dict] | None = None,
    cbo: CBO | None = None,
) -> dict:
    payload = research if isinstance(research, dict) else {}
    all_claims = [item for item in (payload.get('claims') or []) if isinstance(item, dict)]
    pending_claims = [
        item for item in all_claims
        if not item.get('verified') and not item.get('rejected') and not item.get('applied')
    ]
    applied_claims = [
        item for item in all_claims
        if item.get('verified') or item.get('applied')
    ]
    articles = []
    for item in (payload.get('search_results') or [])[:6]:
        if not isinstance(item, dict):
            continue
        articles.append({
            'title': str(item.get('title') or '').strip(),
            'link': str(item.get('link') or '').strip(),
            'display_link': str(item.get('display_link') or item.get('displayLink') or '').strip(),
            'snippet': str(item.get('snippet') or '').strip()[:280],
        })
    suggestions = payload.get('profile_suggestions') if isinstance(payload.get('profile_suggestions'), dict) else {}
    applied_keys = {
        str(key).strip()
        for key in (payload.get('applied_suggestion_keys') or [])
        if str(key).strip()
    }
    suggestion_items = []
    if cbo is not None:
        suggestion_items = [
            item for item in _serialize_suggestion_items(cbo, suggestions)
            if item.get('key') not in applied_keys and not _suggestion_already_on_profile(item)
        ]
    applied_media = {
        str(item).strip()
        for item in (payload.get('applied_media_ids') or [])
        if str(item).strip()
    }
    media_items = [
        item for item in (payload.get('media') or [])
        if isinstance(item, dict) and str(item.get('id') or '') and str(item.get('id')) not in applied_media
        and str(item.get('url') or '').startswith('https://')
    ]
    applied_profile_image = str(payload.get('applied_profile_image_id') or '').strip()
    profile_images = [
        item for item in (payload.get('profile_images') or [])
        if isinstance(item, dict) and str(item.get('id') or '') and str(item.get('id')) != applied_profile_image
        and str(item.get('url') or '').startswith('https://')
    ]
    receipt = payload.get('applied_receipt') if isinstance(payload.get('applied_receipt'), dict) else {}
    return {
        'summary': str(payload.get('summary') or '').strip(),
        'status': str(payload.get('status') or '').strip(),
        'queried_at': str(payload.get('queried_at') or '').strip(),
        'configured': bool(payload.get('configured')),
        'error': str(payload.get('error') or '').strip(),
        'claims': pending_claims,
        'applied_claims': applied_claims,
        'articles': articles,
        'suggestions': suggestions,
        'suggestion_items': suggestion_items,
        'media_items': media_items,
        'profile_images': profile_images,
        'applied_receipt': receipt,
        'pending_review': bool(pending_claims or suggestion_items or media_items or profile_images),
        'has_results': bool(
            pending_claims or suggestion_items or media_items or profile_images or applied_claims or articles
            or receipt or str(payload.get('summary') or '').strip()
        ),
    }


def load_cbo_web_research(cbo: CBO) -> dict:
    return _parse_json(getattr(cbo, 'web_research_json', '') or '{}')


def web_research_is_stale(cbo: CBO, max_days: int | None = None) -> bool:
    days = max_days
    if days is None:
        days = int(current_app.config.get('WEB_RESEARCH_REFRESH_DAYS', 45) or 45)
    refreshed = getattr(cbo, 'web_research_refreshed_at', None)
    if refreshed is None:
        return True
    return refreshed < datetime.utcnow() - timedelta(days=max(int(days), 1))


def refresh_cbo_web_research(cbo: CBO, extra_context: str = '', force: bool = False) -> dict:
    """Re-run public search and keep previously verified claims when they still appear."""
    existing = load_cbo_web_research(cbo)
    if not force and existing.get('status') in {'ready', 'verified', 'pending_review'} and not web_research_is_stale(cbo):
        return existing

    location = cbo.location or cbo.county_region or ''
    profile = _parse_json(cbo.ai_profile_json or '{}')
    if isinstance(profile, dict) and not location:
        location = str(profile.get('location') or '')
    context = extra_context or build_cbo_research_context(cbo)
    fresh = research_cbo_online(cbo.name, location=location, extra_context=context)
    previous_verified = {
        _claim_fingerprint(item)
        for item in (existing.get('claims') or [])
        if isinstance(item, dict) and item.get('verified')
    }
    for claim in fresh.get('claims') or []:
        if not isinstance(claim, dict):
            continue
        if _claim_fingerprint(claim) in previous_verified:
            claim['verified'] = True
            claim['rejected'] = False
            claim['applied'] = True
    new_unverified = [
        item for item in (fresh.get('claims') or [])
        if isinstance(item, dict) and not item.get('verified')
    ]
    if new_unverified:
        fresh['status'] = 'pending_review'
    elif any(item.get('verified') for item in (fresh.get('claims') or []) if isinstance(item, dict)):
        fresh['status'] = 'verified'
    fresh['applied_suggestion_keys'] = list(existing.get('applied_suggestion_keys') or [])
    if existing.get('applied_receipt'):
        fresh['applied_receipt'] = existing.get('applied_receipt')
    apply_web_research_to_cbo(cbo, fresh, only_verified=True, suggestion_keys=[])
    db.session.commit()
    return fresh


def refresh_stale_cbo_web_profiles(limit: int = 25, force: bool = False) -> dict:
    refreshed = 0
    skipped = 0
    errors = []
    cbos = CBO.query.order_by(CBO.updated_at.desc()).limit(max(int(limit or 25), 1)).all()
    for cbo in cbos:
        try:
            if not force and not web_research_is_stale(cbo):
                skipped += 1
                continue
            refresh_cbo_web_research(cbo, force=force)
            refreshed += 1
        except Exception as exc:
            current_app.logger.exception('Failed to refresh public-web research for CBO %s', cbo.id)
            errors.append(f'{cbo.name}: {exc}')
            db.session.rollback()
    return {'refreshed': refreshed, 'skipped': skipped, 'errors': errors, 'evaluated': len(cbos)}


def format_web_verification_sms(cbo_name: str, research: dict) -> str:
    claims = [item for item in (research.get('claims') or []) if isinstance(item, dict)][:3]
    lines = [
        f'We found a few public notes about {cbo_name}. Please check they are actually about your CBO.',
        '',
    ]
    for claim in claims:
        headline = str(claim.get('headline') or claim.get('fact') or '').strip()
        source = str(claim.get('source_title') or '').strip()
        suffix = f' ({source})' if source else ''
        lines.append(f"{claim.get('id')}) {headline}{suffix}")
    if research.get('summary') and len(claims) < 3:
        lines.append('')
        lines.append(str(research.get('summary')).strip()[:180])
    lines.extend([
        '',
        'Reply YES if these look right, NO if they are wrong, SKIP to ignore them, or tell us which numbers to fix — for example "2 is wrong".',
    ])
    message = '\n'.join(lines)
    return message[:1500]


def store_session_web_research(session: SmsOnboardingSession, research: dict) -> None:
    session.web_research_json = json.dumps(research or {}, default=str)
    db.session.add(session)


def load_session_web_research(session: SmsOnboardingSession) -> dict:
    return _parse_json(getattr(session, 'web_research_json', '') or '{}')


def _filter_items_by_identity(
    name: str,
    location: str,
    extra_context: str,
    items: list[dict],
) -> tuple[list[dict], str]:
    """Keep only results Gemini judges to be this CBO.

    A failed model call leaves the results in place. A clear mismatch drops them.
    """
    if not items:
        return [], ''
    api_key = (current_app.config.get('GEMINI_API_KEY') or '').strip()
    if not api_key:
        return items, ''
    compact = []
    for item in items[:12]:
        link = str(item.get('link') or '').strip()
        if not link:
            continue
        compact.append({
            'title': str(item.get('title') or '').strip(),
            'link': link,
            'snippet': _item_evidence_text(item)[:360],
        })
    if not compact:
        return items, ''
    place = location or 'Kenya'
    try:
        with without_local_proxies():
            client = genai.Client(api_key=api_key, http_options=gemini_http_options())
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    IDENTITY_PROMPT,
                    (
                        f'CBO name: {name}\n'
                        f'Search place: {place}\n'
                        f'Known profile:\n{_profile_context_for_model(extra_context)}\n\n'
                        f'Search results JSON:\n{json.dumps(compact, indent=2)}'
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type='application/json',
                ),
            )
        parsed = _parse_model_json(getattr(response, 'text', '') or '')
    except Exception:
        current_app.logger.exception('Gemini CBO identity screen failed for %s', name)
        return items, ''
    if not isinstance(parsed, dict):
        return items, ''
    if _json_flag(parsed.get('matches_found')) is not False:
        urls = [str(url).strip() for url in (parsed.get('matching_urls') or []) if str(url).strip()]
        allowed = {_canonical_url(url) for url in urls}
        allowed.discard('')
        kept = [item for item in items if _canonical_url(item.get('link') or '') in allowed]
        if kept:
            return kept, ''
        return items, ''
    reason = ' '.join(str(parsed.get('reason') or '').split()).strip()
    if not reason:
        reason = f'The public pages found for {name} describe a different organisation, so no facts were added.'
    current_app.logger.info('Rejected public-web namesake for %s: %s', name, reason)
    return [], reason


def _rejected_identity_result(name: str, parsed: dict) -> dict | None:
    """Drop facts when the model says the pages are a different organisation."""
    if not _organisation_rejected(parsed):
        return None
    summary = ' '.join(str(parsed.get('summary') or '').split()).strip()
    if not summary:
        summary = (
            f'The public pages found for {name} describe a different organisation, so no facts were added.'
        )
    return {
        'summary': summary,
        'claims': [],
        'profile_suggestions': {},
        'grounding_dropped_claims': 0,
    }


def _organisation_rejected(parsed: dict) -> bool:
    if not isinstance(parsed, dict):
        return False
    if _json_flag(parsed.get('same_organisation')) is False:
        return True
    summary = str(parsed.get('summary') or '').lower()
    cues = (
        'different organisation',
        'different organization',
        'not the same organisation',
        'not the same organization',
        'another organisation',
        'another organization',
        'not this organisation',
        'not this organization',
        'not this cbo',
    )
    return any(cue in summary for cue in cues)


def _json_flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = str(value or '').strip().lower()
    if text in {'true', 'yes'}:
        return True
    if text in {'false', 'no'}:
        return False
    return None


def _profile_context_for_model(extra_context: str) -> str:
    lines = []
    for line in (extra_context or '').splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        value = cleaned.split(':', 1)[-1].strip().lower() if ':' in cleaned else ''
        if value in PLACEHOLDER_FOCUS_AREAS or value in LEADERSHIP_PLACEHOLDERS:
            continue
        lines.append(cleaned)
    return '\n'.join(lines) or 'none'


def _research_queries(name: str, location: str) -> list[tuple[str, str]]:
    place = location or 'Kenya'
    return [
        (f'"{name}" {place} (CBO OR "community-based" OR "community organisation" OR "community organization")', ''),
        (f'{name} {place} CBO OR NGO OR "community group"', ''),
        (f'{name} founder OR co-founder OR team OR leadership OR "our story" {place}', ''),
        (f'{name} programs OR projects {place} community', ''),
        (f'{name} history OR founded OR established {place} CBO', ''),
        (f'{name} {place} news OR impact', 'y5'),
    ]


def _run_search_plan(search_plan: list[tuple[str, str]], num_results: int = 8) -> tuple[list[dict], list[str], str]:
    items = []
    errors = []
    source = ''
    if not search_plan:
        return items, errors, source
    app = current_app._get_current_object()

    def _one(query: str, date_restrict: str) -> dict:
        with app.app_context():
            return search_google(query, num_results=num_results, date_restrict=date_restrict)

    workers = min(4, len(search_plan))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, query, date_restrict) for query, date_restrict in search_plan]
        for future in as_completed(futures):
            result = future.result() or {}
            source = result.get('source') or source
            if result.get('error') and not result.get('items'):
                errors.append(str(result.get('error')))
            items = _merge_search_items(items, result.get('items') or [])
    return items, errors, source


def _merge_search_items(existing: list[dict], incoming) -> list[dict]:
    merged = list(existing or [])
    seen = {str(item.get('link') or '').strip().lower().rstrip('/') for item in merged}
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get('link') or '').strip()
        key = link.lower().rstrip('/') if link else str(item.get('title') or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _synthesize_research(name: str, location: str, extra_context: str, items: list[dict]) -> dict:
    heuristic = _heuristic_synthesize(name, items)
    api_key = (current_app.config.get('GEMINI_API_KEY') or '').strip()
    if not api_key:
        return heuristic
    selected = select_synthesis_pages(name, items)
    if not selected:
        current_app.logger.info('No name-matched page sections to synthesise for %s', name)
        return heuristic
    try:
        compact_items = [
            {
                'title': page.get('title'),
                'link': page.get('link'),
                'display_link': page.get('display_link'),
                'page_kind': page.get('page_kind'),
                'sections': page.get('sections') or [],
            }
            for page in selected
        ]
        with without_local_proxies():
            client = genai.Client(
                api_key=api_key,
                http_options=gemini_http_options(),
            )
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    SYNTHESIS_PROMPT,
                    (
                        f'Organisation name: {name}\n'
                        'Organisation type: community-based organisation (CBO)\n'
                        f'Known location: {location or "unknown"}\n'
                        f'Existing profile context:\n{_profile_context_for_model(extra_context)}\n\n'
                        'The pages below are already split into labeled sections. '
                        'Pull founder, team, origin/history, each dated milestone, any stated future goal, '
                        'each named program, website, location, and year founded from those sections.\n\n'
                        f'Sectioned pages JSON:\n{json.dumps(compact_items, indent=2)}'
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type='application/json',
                ),
            )
        parsed = _parse_model_json(getattr(response, 'text', '') or '')
        if not isinstance(parsed, dict):
            raise ValueError('Web research synthesis was not a JSON object')
        rejected = _rejected_identity_result(name, parsed)
        if rejected:
            return rejected
        grounded = _ground_model_output(name, parsed, selected)
        if grounded.get('dropped_claims'):
            current_app.logger.info(
                'Dropped %s ungrounded web-research claims for %s',
                grounded['dropped_claims'],
                name,
            )
        merged = dict(heuristic)
        merged['summary'] = grounded.get('summary') or heuristic.get('summary') or ''
        if isinstance(parsed.get('claims'), list):
            merged['claims'] = grounded.get('claims') or []
        else:
            merged['claims'] = heuristic.get('claims') or []
        merged['profile_suggestions'] = _merge_suggestions(
            grounded.get('profile_suggestions') or {},
            heuristic.get('profile_suggestions') or {},
        )
        merged['grounding_dropped_claims'] = int(grounded.get('dropped_claims') or 0)
        return merged
    except Exception:
        current_app.logger.exception('Gemini CBO web-research synthesis failed for %s', name)
        return heuristic


def select_synthesis_pages(name: str, items: list[dict], max_pages: int = MAX_SYNTHESIS_PAGES) -> list[dict]:
    """Choose sectioned pages that mention this organisation, covering team, history, and programs."""
    prepared = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        page = _page_for_synthesis(name, item)
        if page:
            prepared.append(page)
    prepared.sort(key=lambda page: page.get('score') or 0, reverse=True)
    selected = []
    seen = set()

    def add(page: dict) -> None:
        key = _canonical_url(page.get('link') or '') or str(page.get('title') or '').strip().lower()
        if not key or key in seen or len(selected) >= max_pages:
            return
        seen.add(key)
        selected.append(page)

    for kinds in ({'founder', 'team'}, {'history'}, {'program', 'impact'}):
        for page in prepared:
            if any(section.get('kind') in kinds for section in page.get('sections') or []):
                add(page)
                break
    for page in prepared:
        add(page)
    for page in selected:
        page.pop('score', None)
    return selected


def _page_for_synthesis(name: str, item: dict) -> dict | None:
    sections = []
    for raw in item.get('sections') or []:
        if not isinstance(raw, dict):
            continue
        text = ' '.join(str(raw.get('text') or '').split())
        if not text:
            continue
        kind = str(raw.get('kind') or 'other').strip().lower() or 'other'
        sections.append({
            'heading': str(raw.get('heading') or 'Overview').strip()[:120] or 'Overview',
            'kind': kind,
            'text': text,
        })
    from_snippet = False
    if not sections:
        snippet = ' '.join(str(item.get('snippet') or '').split())
        if snippet:
            kind = str(item.get('page_kind') or 'other').strip().lower() or 'other'
            sections = [{'heading': 'Search snippet', 'kind': kind, 'text': snippet[:900]}]
            from_snippet = True
    preferred = [
        section for section in sections
        if section['kind'] in PREFERRED_SECTION_KINDS
        and section['heading'] not in {'Summary', 'Structured data'}
    ]
    body = [
        section for section in sections
        if section['heading'] not in {'Summary', 'Structured data'} and len(section['text']) > 80
    ]
    long_other = [
        section for section in sections
        if section['kind'] == 'other' and len(section['text']) > 400
    ]
    if preferred:
        sections = preferred + long_other
    elif body:
        sections = body
    rank = {'founder': 0, 'team': 1, 'history': 2, 'program': 3, 'impact': 4, 'about': 5}
    sections = sorted(sections, key=lambda section: rank.get(section['kind'], 9))[:8]
    if not _item_mentions_org({**item, 'sections': sections}, name):
        return None
    score = sum(SECTION_KIND_PRIORITY.get(section['kind'], 1) for section in sections)
    if from_snippet:
        score -= 30
    return {
        'title': str(item.get('title') or '').strip(),
        'link': str(item.get('link') or '').strip(),
        'display_link': str(item.get('display_link') or '').strip(),
        'page_kind': str(item.get('page_kind') or '').strip(),
        'sections': sections,
        'score': score,
    }


def _ground_model_output(name: str, parsed: dict, pages: list[dict]) -> dict:
    claims, dropped = _ground_claims(parsed.get('claims') or [], pages)
    summary = str(parsed.get('summary') or '').strip()
    if summary and not _summary_supported(summary, pages, name):
        summary = ''
    return {
        'summary': summary,
        'claims': claims,
        'profile_suggestions': _ground_suggestions(parsed.get('profile_suggestions') or {}, pages),
        'dropped_claims': dropped,
    }


def _ground_claims(raw_claims, pages: list[dict]) -> tuple[list[dict], int]:
    by_url = {}
    for page in pages or []:
        key = _canonical_url(page.get('link') or '')
        if key:
            by_url.setdefault(key, []).append(page)
    kept = []
    dropped = 0
    for item in raw_claims or []:
        if len(kept) >= MAX_CLAIMS:
            break
        if not isinstance(item, dict):
            dropped += 1
            continue
        matches = by_url.get(_canonical_url(item.get('source_url') or '')) or []
        quote = str(item.get('evidence_quote') or '').strip()
        heading = str(item.get('section_heading') or '').strip()
        if not matches or not _quote_in_cited_section(quote, heading, matches):
            dropped += 1
            continue
        kept.append(dict(item))
    for index, claim in enumerate(kept, start=1):
        claim['id'] = index
    return kept, dropped


def _quote_in_cited_section(quote: str, heading: str, pages: list[dict]) -> bool:
    if not heading:
        return False
    sections = [
        section
        for page in pages
        for section in (page.get('sections') or [])
        if isinstance(section, dict)
    ]
    cited = [section for section in sections if _headings_match(heading, section.get('heading') or '')]
    if not cited:
        return False
    return any(_quote_supported(quote, _section_blob(section)) for section in cited)


def _ground_suggestions(suggestions, pages: list[dict]) -> dict:
    if not isinstance(suggestions, dict):
        return {}
    corpus = _pages_corpus(pages)
    hosts = set()
    for page in pages or []:
        host = urlparse(str(page.get('link') or '')).netloc.lower().removeprefix('www.')
        if host:
            hosts.add(host)
    cleaned = {}
    for key in ('founder', 'chairperson', 'program_director', 'finance_lead'):
        value = _clean_person_name(suggestions.get(key) or '')
        if value and _fold_text(value) in corpus:
            cleaned[key] = value
    team = []
    for person in _normalize_team(suggestions.get('team')):
        person_name = _clean_person_name(person.get('name') or '')
        if person_name and _fold_text(person_name) in corpus:
            person['name'] = person_name
            team.append(person)
    if team:
        cleaned['team'] = team
    year_match = re.search(r'\b(?:19|20)\d{2}\b', str(suggestions.get('founded_year') or ''))
    if year_match and year_match.group(0) in corpus:
        cleaned['founded_year'] = year_match.group(0)
    website = str(suggestions.get('website') or '').strip()
    if website:
        host = urlparse(website if '://' in website else f'https://{website}').netloc.lower().removeprefix('www.')
        if host and (host in hosts or host in corpus):
            cleaned['website'] = website
    location = ' '.join(str(suggestions.get('location') or '').split())
    if location and _location_supported(location, corpus):
        cleaned['location'] = location
    for key in ('tagline', 'focus_areas', 'history', 'flagship_summary', 'success_story'):
        value = ' '.join(str(suggestions.get(key) or '').split())
        if value and _prose_supported(value, corpus):
            cleaned[key] = value
    labels = []
    raw_labels = suggestions.get('classifications')
    if isinstance(raw_labels, str):
        raw_labels = [part.strip() for part in raw_labels.split(',')]
    for label in raw_labels or []:
        cleaned_label = str(label).strip().lower()
        if cleaned_label in ALLOWED_CLASSIFICATIONS:
            labels.append(cleaned_label)
    if labels:
        cleaned['classifications'] = labels
    programs = []
    raw_programs = suggestions.get('programs')
    if isinstance(raw_programs, dict):
        raw_programs = [raw_programs]
    for raw in raw_programs or []:
        if not isinstance(raw, dict) or len(programs) >= 8:
            continue
        name = ' '.join(str(raw.get('name') or '').split())
        description = ' '.join(str(raw.get('description') or '').split())
        who = ' '.join(str(raw.get('who_it_serves') or raw.get('beneficiaries') or '').split())
        if len(name) < 3 or len(description) < 20 or not _prose_supported(description, corpus):
            continue
        programs.append({
            'name': name[:120],
            'description': description[:500],
            'who_it_serves': who[:180],
        })
    if programs:
        cleaned['programs'] = programs
    milestones = _ground_journey_items(suggestions.get('milestones'), corpus, future=False)
    future_goals = _ground_journey_items(suggestions.get('future_goals'), corpus, future=True)
    if milestones:
        cleaned['milestones'] = milestones
    if future_goals:
        cleaned['future_goals'] = future_goals
    return cleaned


def _ground_journey_items(raw, corpus: str, *, future: bool) -> list[dict]:
    records = []
    seen = set()
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    allowed = {'planned', 'aspiration'} if future else {'done', 'in_progress'}
    for item in items:
        if not isinstance(item, dict) or len(records) >= 8:
            continue
        title = ' '.join(str(item.get('title') or '').split())
        summary = ' '.join(str(item.get('summary') or item.get('detail') or '').split())
        when = ' '.join(str(item.get('when') or '').split())
        if len(title) < 3:
            continue
        years = re.findall(r'\b(?:19|20)\d{2}\b', f'{when} {summary}')
        if any(year not in corpus for year in years):
            continue
        if summary:
            supported = _prose_supported(summary, corpus)
        else:
            supported = _fold_text(title) in corpus
        if not supported:
            continue
        status = str(item.get('status') or '').strip().lower()
        if status not in allowed:
            status = 'aspiration' if future and not when else ('planned' if future else 'done')
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        records.append({
            'when': when[:40],
            'title': title[:120],
            'summary': summary[:400],
            'status': status,
        })
    return records


def _summary_supported(summary: str, pages: list[dict], name: str) -> bool:
    if not _text_mentions_org(summary, name):
        return False
    corpus = _pages_corpus(pages)
    people = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z.']+){1,3}\b", summary)
    if any(_fold_text(person) not in corpus for person in people):
        return False
    years = re.findall(r'\b(?:19|20)\d{2}\b', summary)
    return all(year in corpus for year in years)


def _item_mentions_org(item: dict, name: str) -> bool:
    parts = [str(item.get('title') or ''), str(item.get('snippet') or '')]
    for section in item.get('sections') or []:
        if isinstance(section, dict):
            parts.append(str(section.get('heading') or ''))
            parts.append(str(section.get('text') or ''))
    return _text_mentions_org(' '.join(parts), name)


def _item_evidence_text(item: dict) -> str:
    sections = [section for section in (item.get('sections') or []) if isinstance(section, dict)]
    if sections:
        return ' '.join(
            f"{section.get('heading') or ''} {section.get('text') or ''}".strip()
            for section in sections
        ).strip()
    return ' '.join(str(item.get('snippet') or '').split())


def _text_mentions_org(text: str, name: str) -> bool:
    cleaned = ' '.join((name or '').lower().split())
    if not cleaned:
        return False
    haystack = _normalize_compare_text(text)
    if cleaned in haystack:
        return True
    tokens = [token for token in re.findall(r'[a-z0-9]+', cleaned) if len(token) > 3]
    if not tokens:
        return cleaned in haystack
    return all(token in haystack for token in tokens)


def _pages_corpus(pages: list[dict]) -> str:
    parts = []
    for page in pages or []:
        parts.append(str(page.get('title') or ''))
        for section in page.get('sections') or []:
            if isinstance(section, dict):
                parts.append(str(section.get('heading') or ''))
                parts.append(str(section.get('text') or ''))
    return _fold_text(' '.join(parts))


def _section_blob(section: dict) -> str:
    return f"{section.get('heading') or ''} {section.get('text') or ''}"


def _headings_match(left: str, right: str) -> bool:
    first = _normalize_compare_text(left)
    second = _normalize_compare_text(right)
    return bool(first and second) and (first == second or first in second or second in first)


def _fold_text(value: str) -> str:
    text = str(value or '').translate(str.maketrans({
        '’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-',
    }))
    return _normalize_compare_text(text)


def _quote_supported(quote: str, text: str) -> bool:
    normalized_quote = _fold_text(quote).strip('"\'')
    normalized_text = _fold_text(text)
    if len(normalized_quote) < 20:
        return False
    if normalized_quote in normalized_text:
        return True
    words = [word for word in re.findall(r"[a-z0-9']+", normalized_quote) if len(word) > 2]
    size = 6 if len(words) >= 6 else len(words)
    if size < 5:
        return False
    return any(
        ' '.join(words[index:index + size]) in normalized_text
        for index in range(0, len(words) - size + 1)
    )


def _clean_person_name(name: str) -> str:
    tokens = [token for token in str(name or '').split() if token]
    while tokens and _fold_text(tokens[0]) in _PERSON_NAME_STOPWORDS:
        tokens.pop(0)
    while tokens and _fold_text(tokens[-1]) in _PERSON_NAME_STOPWORDS:
        tokens.pop()
    cleaned = ' '.join(tokens)
    parts = [part for part in re.findall(r"[a-z']+", _fold_text(cleaned)) if part]
    if len(parts) < 2 or len(parts) > 4 or any(part in _PERSON_NAME_STOPWORDS for part in parts):
        return ''
    return cleaned


def _person_in_corpus(name: str, corpus: str) -> bool:
    cleaned = _clean_person_name(name)
    return bool(cleaned) and _fold_text(cleaned) in corpus


def _location_supported(location: str, corpus: str) -> bool:
    tokens = [token for token in re.findall(r'[a-z0-9]+', location.lower()) if len(token) > 3]
    return bool(tokens) and all(token in corpus for token in tokens)


def _prose_supported(text: str, corpus: str) -> bool:
    normalized = _fold_text(text)
    if len(normalized) >= 24 and normalized in corpus:
        return True
    words = [word for word in re.findall(r"[a-z0-9']+", normalized) if len(word) > 3]
    if len(words) < 4:
        return False
    return any(' '.join(words[index:index + 4]) in corpus for index in range(0, len(words) - 3))


def _canonical_url(url: str) -> str:
    parsed = urlparse(str(url or '').strip())
    host = parsed.netloc.lower().removeprefix('www.')
    if not host:
        return ''
    path = unquote(parsed.path or '').rstrip('/').lower()
    return f'{host}{path}'


def _stored_search_item(item: dict) -> dict:
    stored = {
        'title': str(item.get('title') or '').strip(),
        'link': str(item.get('link') or '').strip(),
        'display_link': str(item.get('display_link') or '').strip(),
        'snippet': str(item.get('snippet') or '').strip()[:500],
        'page_kind': str(item.get('page_kind') or '').strip(),
    }
    sections = [
        {
            'heading': str(section.get('heading') or '').strip(),
            'kind': str(section.get('kind') or '').strip(),
        }
        for section in (item.get('sections') or [])
        if isinstance(section, dict) and str(section.get('heading') or '').strip()
    ]
    if sections:
        stored['sections'] = sections
    return stored


def _heuristic_synthesize(name: str, items: list[dict]) -> dict:
    relevant = [item for item in (items or []) if isinstance(item, dict) and _item_mentions_org(item, name)]
    claims = []
    for index, item in enumerate(relevant[:MAX_CLAIMS], start=1):
        snippet = _item_evidence_text(item)
        title = str(item.get('title') or '').strip()
        if not snippet and not title:
            continue
        claims.append({
            'id': index,
            'headline': (title or snippet)[:90],
            'fact': (snippet or title)[:280],
            'category': str(item.get('page_kind') or 'other'),
            'source_title': title,
            'source_url': str(item.get('link') or '').strip(),
            'verified': False,
            'rejected': False,
        })
    people = _heuristic_people(relevant)
    website = ''
    official = official_homepage_url(name, items)
    if official:
        website = official.rstrip('/')
    founder = next((person['name'] for person in people if 'founder' in person.get('role', '').lower()), '')
    history = _heuristic_history(relevant)
    return {
        'summary': f'Public web mentions were found for {name}.' if claims else '',
        'claims': claims,
        'profile_suggestions': {
            'website': website,
            'founder': founder,
            'team': people,
            'history': history,
        },
    }


def _heuristic_people(items: list[dict]) -> list[dict]:
    found = []
    seen = set()
    blob = ' '.join(_item_evidence_text(item) for item in items if isinstance(item, dict))
    for name, role in PERSON_ROLE_RE.findall(blob):
        cleaned_name = _clean_person_name(name)
        cleaned_role = ' '.join(role.split()).title().replace('Co-Founder', 'Co-founder')
        key = cleaned_name.lower()
        if key in seen or len(cleaned_name.split()) < 2:
            continue
        seen.add(key)
        found.append({'name': cleaned_name, 'role': cleaned_role})
    return found[:12]


def _heuristic_history(items: list[dict]) -> str:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for section in item.get('sections') or []:
            if not isinstance(section, dict):
                continue
            if section.get('kind') in {'history', 'story'} and len(str(section.get('text') or '')) > 80:
                return str(section.get('text') or '').strip()[:700]
        kind = str(item.get('page_kind') or '')
        snippet = _item_evidence_text(item)
        if kind in {'story', 'history'} and len(snippet) > 80:
            return snippet[:700]
    return ''


def _normalize_claims(raw_claims, search_items: list[dict]) -> list[dict]:
    normalized = []
    for index, item in enumerate(raw_claims[:MAX_CLAIMS], start=1):
        if not isinstance(item, dict):
            continue
        headline = str(item.get('headline') or item.get('fact') or '').strip()
        fact = str(item.get('fact') or headline).strip()
        if not headline and not fact:
            continue
        source_url = str(item.get('source_url') or '').strip()
        normalized.append({
            'id': int(item.get('id') or index),
            'headline': headline[:120],
            'fact': fact[:400],
            'category': str(item.get('category') or 'other').strip().lower()[:40],
            'source_title': str(item.get('source_title') or '').strip()[:180],
            'source_url': source_url,
            'section_heading': str(item.get('section_heading') or '').strip()[:120],
            'evidence_quote': str(item.get('evidence_quote') or '').strip()[:400],
            'verified': bool(item.get('verified')),
            'rejected': bool(item.get('rejected')),
            'applied': bool(item.get('applied')),
        })
    return normalized


def _claim_fingerprint(claim: dict) -> str:
    return re.sub(r'\s+', ' ', str(claim.get('headline') or claim.get('fact') or '').strip().lower())[:160]


def build_cbo_research_context(cbo: CBO) -> str:
    profile = _parse_json(getattr(cbo, 'ai_profile_json', '') or '{}')
    flagship = profile.get('flagship_project') if isinstance(profile.get('flagship_project'), dict) else {}
    story = profile.get('success_story') if isinstance(profile.get('success_story'), dict) else {}
    leadership = profile.get('leadership') if isinstance(profile.get('leadership'), dict) else {}
    focus = str(cbo.focus_areas or profile.get('focus_areas') or '').strip()
    if focus.lower() in PLACEHOLDER_FOCUS_AREAS:
        focus = ''
    chair = str(leadership.get('chairperson') or cbo.chairperson or '').strip()
    director = str(leadership.get('program_director') or cbo.program_director or '').strip()
    finance = str(leadership.get('finance_lead') or cbo.finance_lead or '').strip()
    lines = [
        f'Known location: {cbo.location or profile.get("location") or ""}',
        f'Known focus: {focus}',
        f'Known tagline: {profile.get("tagline") or ""}',
        f'Year founded: {cbo.founded_year or profile.get("founded_year") or ""}',
        f'Website: {profile.get("website") or ""}',
        f'Chair: {"" if chair.lower() in LEADERSHIP_PLACEHOLDERS else chair}',
        f'Director: {"" if director.lower() in LEADERSHIP_PLACEHOLDERS else director}',
        f'Finance lead: {"" if finance.lower() in LEADERSHIP_PLACEHOLDERS else finance}',
        f'Flagship: {flagship.get("summary") or cbo.flagship_summary or ""}',
        f'Impact story: {story.get("summary") or cbo.success_story or ""}',
        f'Origin story: {profile.get("origin_story") or ""}',
    ]
    return '\n'.join(line for line in lines if line.split(':', 1)[-1].strip())


def _is_sparse_profile_value(value) -> bool:
    cleaned = str(value or '').strip()
    if not cleaned:
        return True
    return cleaned.lower() in PLACEHOLDER_FOCUS_AREAS or cleaned.lower() in LEADERSHIP_PLACEHOLDERS


def _append_unique_highlight(profile: dict, text: str) -> None:
    cleaned = ' '.join(str(text or '').split()).strip()
    if not cleaned:
        return
    highlights = profile.get('web_highlights')
    if not isinstance(highlights, list):
        highlights = []
    fingerprints = {re.sub(r'\s+', ' ', str(item).strip().lower())[:160] for item in highlights}
    key = re.sub(r'\s+', ' ', cleaned.lower())[:160]
    if key in fingerprints:
        return
    highlights.append(cleaned[:280])
    profile['web_highlights'] = highlights[:12]


def _item_host(item: dict) -> str:
    return urlparse(str((item or {}).get('link') or '')).netloc.lower().removeprefix('www.')


def _hosts_for_items(items: list[dict], name: str) -> set[str]:
    return {
        _item_host(item)
        for item in items or []
        if _item_mentions_org(item, name) and _item_host(item)
    }


def _program_records(value) -> list[dict]:
    raw_items = value
    if isinstance(value, dict):
        raw_items = [value]
    records = []
    seen = set()
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        name = ' '.join(str(raw.get('name') or '').split())
        description = ' '.join(str(raw.get('description') or '').split())
        who = ' '.join(str(raw.get('who_it_serves') or raw.get('beneficiaries') or '').split())
        if len(name) < 3 or name.lower() in {'overview', 'home', 'summary', 'contact', 'donate'}:
            continue
        description = re.sub(r'\b(learn more|read more|donate now)\b', ' ', description, flags=re.I)
        description = ' '.join(description.split())
        if len(description) < 20 or _program_text_is_chrome(description):
            continue
        key = program_key(name)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            'name': name[:120],
            'description': description[:500],
            'who_it_serves': who[:180],
            'image_url': str(raw.get('image_url') or '').strip(),
        })
    return records[:10]


def _programs_from_pages(items: list[dict]) -> list[dict]:
    collected = []
    for item in items or []:
        if isinstance(item, dict):
            collected.extend(_program_records(item.get('page_programs')))
    return _merge_program_records(collected, [])


def _merge_program_records(primary, secondary) -> list[dict]:
    merged = []
    by_key = {}
    for source in (primary or [], secondary or []):
        for program in _program_records(source):
            key = program_key(program['name'])
            current = by_key.get(key)
            if current is None:
                by_key[key] = program
                merged.append(program)
                continue
            if len(program['description']) > len(current['description']):
                current['description'] = program['description']
            if program.get('who_it_serves') and not current.get('who_it_serves'):
                current['who_it_serves'] = program['who_it_serves']
            if program.get('image_url') and not current.get('image_url'):
                current['image_url'] = program['image_url']
    return merged[:10]


def _media_from_pages(items: list[dict]) -> list[dict]:
    ranked = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for image in item.get('images') or []:
            if not isinstance(image, dict):
                continue
            url = str(image.get('url') or '').strip()
            if not url.startswith('https://') or url in seen:
                continue
            seen.add(url)
            ranked.append({
                'id': hashlib.sha1(url.encode()).hexdigest()[:12],
                'url': url,
                'alt': ' '.join(str(image.get('alt') or '').split())[:160],
                'program': ' '.join(str(image.get('program') or '').split())[:120],
                'page_url': str(item.get('link') or '').strip(),
            })
    def rank(image: dict) -> tuple:
        filename = str(image.get('url') or '').rsplit('/', 1)[-1].lower()
        label = f"{image.get('program') or ''} {image.get('alt') or ''}".lower()
        if filename.startswith('about') or 'community work' in label:
            return (2, filename)
        if image.get('program'):
            return (0, filename)
        return (1, filename)

    ranked.sort(key=rank)
    return ranked[:10]


def _profile_images_from_pages(items: list[dict]) -> list[dict]:
    official_hosts = {
        _item_host(item)
        for item in items or []
        if isinstance(item, dict) and item.get('is_official') and _item_host(item)
    }
    ranked = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        host = _item_host(item)
        if official_hosts and host not in official_hosts:
            continue
        for image in item.get('profile_images') or []:
            if not isinstance(image, dict):
                continue
            url = str(image.get('url') or '').strip()
            if not url.startswith('https://') or url in seen:
                continue
            seen.add(url)
            filename = url.rsplit('/', 1)[-1].lower()
            ranked.append({
                'id': hashlib.sha1(url.encode()).hexdigest()[:12],
                'url': url,
                'alt': ' '.join(str(image.get('alt') or 'Logo').split())[:160] or 'Logo',
                'page_url': str(item.get('link') or '').strip(),
                '_score': (0 if 'logo' in filename else 1, filename),
            })
    ranked.sort(key=lambda image: image['_score'])
    for image in ranked:
        image.pop('_score', None)
    return ranked[:3]


def _apply_profile_image(cbo, profile, payload, image_id, explicit_keys, has_verified) -> bool:
    stored = [
        item for item in (payload.get('profile_images') or [])
        if isinstance(item, dict) and str(item.get('url') or '').startswith('https://')
    ]
    if image_id is None:
        chosen = stored[:1] if (not explicit_keys and has_verified) else []
    else:
        wanted = str(image_id or '').strip()
        chosen = [item for item in stored if str(item.get('id') or '') == wanted][:1]
    if not chosen:
        return False
    item = chosen[0]
    url = str(item.get('url') or '')
    downloaded = download_profile_image(url)
    if not downloaded:
        current_app.logger.info('Skipped public profile image %s', url)
        return False
    data, mime, ext = downloaded
    upload_dir = current_app.config.get('PROGRAM_PHOTO_UPLOAD_DIR')
    try:
        stored_file = store_supporting_file(
            cbo.id,
            f'profile-image.{ext}',
            data,
            mime,
            object_prefix='profile_images',
            local_upload_dir=upload_dir,
        )
    except (OSError, RuntimeError) as exc:
        current_app.logger.warning('Could not store public profile image %s: %s', url, exc)
        return False
    profile['profile_image'] = {
        'id': uuid.uuid4().hex,
        'filename': f'profile-image.{ext}',
        'mime_type': mime,
        'storage_backend': stored_file['storage_backend'],
        'stored_path': stored_file['stored_path'],
        'storage_bucket': stored_file.get('storage_bucket') or '',
        'storage_object_path': stored_file.get('storage_object_path') or '',
        'created_at': datetime.utcnow().isoformat(),
    }
    payload['applied_profile_image_id'] = str(item.get('id') or '')
    return True


def _apply_program_suggestions(narrative, programs, apply_keys, applied_labels):
    selected = [
        program for program in programs
        if f"program:{program_key(program['name'])}" in apply_keys
    ]
    applied = set()
    if not selected:
        return narrative, applied
    existing = merge_program_lists(narrative.get('programs') or [], [])
    by_key = {item['key']: item for item in existing}
    for program in selected:
        key = program_key(program['name'])
        previous = by_key.get(key) or {}
        description = program['description']
        if previous.get('description') and previous.get('source') not in {'', 'web', 'extracted', 'unknown'}:
            description = previous['description']
        by_key[key] = {
            'key': key,
            'name': program['name'],
            'description': description,
            'beneficiaries': program.get('who_it_serves') or previous.get('beneficiaries') or '',
            'status': previous.get('status') or 'ongoing',
            'source': previous.get('source') if previous.get('source') not in {'', 'extracted'} else 'web',
        }
        applied.add(f'program:{key}')
        applied_labels.append(program['name'])
    narrative['programs'] = list(by_key.values())
    return narrative, applied


def _apply_research_media(cbo, narrative, payload, media_ids, explicit_keys, has_verified) -> int:
    stored = [
        item for item in (payload.get('media') or [])
        if isinstance(item, dict) and str(item.get('url') or '').startswith('https://')
    ]
    if media_ids is None:
        chosen = stored if (not explicit_keys and has_verified) else []
    else:
        wanted = {str(item) for item in media_ids}
        chosen = [item for item in stored if str(item.get('id') or '') in wanted]
    if not chosen:
        return 0
    upload_dir = current_app.config.get('PROGRAM_PHOTO_UPLOAD_DIR')
    media = narrative.get('program_media') if isinstance(narrative.get('program_media'), dict) else {}
    programs = merge_program_lists(narrative.get('programs') or [], [])
    applied_ids = {str(item) for item in (payload.get('applied_media_ids') or []) if str(item).strip()}
    added = 0
    for item in chosen:
        url = str(item.get('url') or '')
        downloaded = download_public_image(url)
        if not downloaded:
            current_app.logger.info('Skipped public photo %s', url)
            continue
        data, mime, ext = downloaded
        label = item.get('program') or item.get('alt') or 'Program photo'
        key = program_key(label)
        try:
            stored_file = store_supporting_file(
                cbo.id,
                f'{key}.{ext}',
                data,
                mime,
                object_prefix='program_photos',
                local_upload_dir=upload_dir,
            )
        except (OSError, RuntimeError) as exc:
            current_app.logger.warning('Could not store public photo %s: %s', url, exc)
            continue
        photos = list(media.get(key) or [])
        if len(photos) >= 16:
            continue
        photos.append({
            'id': uuid.uuid4().hex,
            'filename': f'{key}.{ext}',
            'mime_type': mime,
            'storage_backend': stored_file['storage_backend'],
            'stored_path': stored_file['stored_path'],
            'storage_bucket': stored_file.get('storage_bucket') or '',
            'storage_object_path': stored_file.get('storage_object_path') or '',
            'created_at': datetime.utcnow().isoformat(),
        })
        media[key] = photos
        if not any(program.get('key') == key for program in programs):
            programs.append({
                'key': key,
                'name': str(label)[:120],
                'description': '',
                'beneficiaries': '',
                'status': 'ongoing',
                'source': 'photo',
            })
        if item.get('id'):
            applied_ids.add(str(item.get('id')))
        added += 1
    if added:
        narrative['program_media'] = media
        narrative['programs'] = programs
        payload['applied_media_ids'] = sorted(applied_ids)
    return added


def _serialize_suggestion_items(cbo: CBO, suggestions: dict) -> list[dict]:
    profile = _parse_json(getattr(cbo, 'ai_profile_json', '') or '{}')
    leadership = profile.get('leadership') if isinstance(profile.get('leadership'), dict) else {}
    narrative = profile.get('narrative_profile') if isinstance(profile.get('narrative_profile'), dict) else {}
    current_values = {
        'tagline': profile.get('tagline') or '',
        'focus_areas': cbo.focus_areas or profile.get('focus_areas') or '',
        'location': cbo.location or profile.get('location') or '',
        'website': profile.get('website') or '',
        'founded_year': cbo.founded_year or profile.get('founded_year') or '',
        'founder': leadership.get('program_director') or getattr(cbo, 'program_director', '') or '',
        'chairperson': leadership.get('chairperson') or getattr(cbo, 'chairperson', '') or '',
        'program_director': leadership.get('program_director') or getattr(cbo, 'program_director', '') or '',
        'finance_lead': leadership.get('finance_lead') or getattr(cbo, 'finance_lead', '') or '',
        'team': _format_team(narrative.get('leadership') or []),
        'history': profile.get('origin_story') or '',
        'flagship_summary': (profile.get('flagship_project') or {}).get('summary') if isinstance(profile.get('flagship_project'), dict) else (cbo.flagship_summary or ''),
        'success_story': (profile.get('success_story') or {}).get('summary') if isinstance(profile.get('success_story'), dict) else (cbo.success_story or ''),
        'classifications': ', '.join(_as_string_list(cbo.classifications_json)),
    }
    items = []
    for key, label in SUGGESTION_FIELDS:
        proposed = suggestions.get(key)
        if key == 'classifications':
            proposed_text = ', '.join(_as_string_list(proposed))
        elif key == 'team':
            proposed_text = _format_team(proposed)
        else:
            proposed_text = str(proposed or '').strip()
        if not proposed_text:
            continue
        current = str(current_values.get(key) or '').strip()
        items.append({
            'key': key,
            'label': label,
            'proposed': proposed_text,
            'current': current,
            'fills_gap': _is_sparse_profile_value(current),
        })
    existing_programs = {}
    for program in narrative.get('programs') or []:
        if isinstance(program, dict):
            existing_programs[program_key(program.get('key') or program.get('name') or '')] = program
    for program in _program_records(suggestions.get('programs')):
        key = f"program:{program_key(program['name'])}"
        current_program = existing_programs.get(program_key(program['name'])) or {}
        current = str(current_program.get('description') or '').strip()
        proposed = program['description']
        if program.get('who_it_serves'):
            proposed = f"{proposed} Serves: {program['who_it_serves']}"
        items.append({
            'key': key,
            'label': program['name'],
            'proposed': proposed,
            'current': current,
            'fills_gap': _is_sparse_profile_value(current),
        })
    existing_titles = {
        str(step.get('title') or '').strip().lower()
        for bucket in ('past', 'future')
        for step in ((narrative.get('journey') or {}).get(bucket) or [])
        if isinstance(step, dict)
    }
    for prefix, source, label_prefix in (
        ('milestone', suggestions.get('milestones'), 'Milestone'),
        ('goal', suggestions.get('future_goals'), 'Next step'),
    ):
        for step in _journey_step_records(source, future=prefix == 'goal'):
            key = f'{prefix}:{_journey_key(step)}'
            proposed = step['summary'] or step['title']
            if step.get('when'):
                proposed = f"{step['when']}: {proposed}"
            items.append({
                'key': key,
                'label': f"{label_prefix}: {step['title']}",
                'proposed': proposed,
                'current': step['title'] if step['title'].lower() in existing_titles else '',
                'fills_gap': step['title'].lower() not in existing_titles,
            })
    return items


def _suggestion_already_on_profile(item: dict) -> bool:
    proposed = _normalize_compare_text(item.get('proposed'))
    current = _normalize_compare_text(item.get('current'))
    if not proposed or not current:
        return False
    return proposed == current or proposed in current


def _normalize_compare_text(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip().lower())


def _as_string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [part.strip() for part in value.split(',') if part.strip()]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _normalize_team(value) -> list[dict]:
    people = []
    seen = set()
    raw_items = value
    if isinstance(value, str):
        raw_items = [{'name': part.strip(), 'role': ''} for part in value.split(';') if part.strip()]
    for raw in raw_items or []:
        if isinstance(raw, str):
            name, role = (raw.split(',', 1) + [''])[:2]
            raw = {'name': name, 'role': role}
        if not isinstance(raw, dict):
            continue
        name = str(raw.get('name') or raw.get('title') or '').strip()
        role = str(raw.get('role') or raw.get('subtitle') or '').strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        people.append({'name': name[:120], 'role': role[:120], 'notes': str(raw.get('notes') or '').strip()[:240]})
    return people[:12]


def _format_team(value) -> str:
    people = _normalize_team(value)
    parts = []
    for person in people:
        if person.get('role'):
            parts.append(f"{person['name']} ({person['role']})")
        else:
            parts.append(person['name'])
    return '; '.join(parts)


def _merge_people(existing, incoming) -> list[dict]:
    merged = _normalize_team(existing)
    seen = {person['name'].lower() for person in merged}
    for person in _normalize_team(incoming):
        key = person['name'].lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(person)
    return merged[:16]


def _merge_suggestions(primary: dict, fallback: dict) -> dict:
    merged = dict(fallback or {})
    for key, value in (primary or {}).items():
        if key == 'team':
            team = _normalize_team(value)
            merged['team'] = team or _normalize_team(merged.get('team'))
        elif key == 'classifications':
            if value:
                merged[key] = value
        elif str(value or '').strip():
            merged[key] = value
    return merged


def _file_web_facts_into_narrative(
    narrative: dict,
    profile: dict,
    suggestions: dict,
    apply_keys: set[str],
    accepted_claims: list[dict],
    summary: str,
) -> None:
    """Fold confirmed public facts into the fluid organisation-detail panels."""
    if summary and not str(narrative.get('summary') or '').strip():
        narrative['summary'] = summary.strip()[:400]

    identity_fields = []
    if 'location' in apply_keys and suggestions.get('location'):
        identity_fields.append(_statement_field('Location', suggestions.get('location')))
    if 'website' in apply_keys and suggestions.get('website'):
        identity_fields.append(_statement_field('Website', suggestions.get('website')))
    if 'founded_year' in apply_keys and suggestions.get('founded_year'):
        identity_fields.append(_statement_field('Year founded', suggestions.get('founded_year')))
    if 'tagline' in apply_keys and suggestions.get('tagline'):
        identity_fields.append(_statement_field('Tagline', suggestions.get('tagline')))
    history = str(suggestions.get('history') or '').strip()
    dated_steps = _selected_journey_steps(suggestions.get('milestones'), apply_keys, prefix='milestone')
    dated_steps.extend(_milestones_from_claims(accepted_claims))
    if history and ('history' in apply_keys or any(_claim_category(claim) == 'history' for claim in accepted_claims)):
        identity_fields.append(_statement_field('Origin story', history))
        if not dated_steps:
            dated_steps.append({
                'when': str(suggestions.get('founded_year') or profile.get('founded_year') or ''),
                'title': 'Organisation founded',
                'summary': history[:180],
                'detail': history,
                'status': 'done',
            })
    for step in dated_steps:
        _append_journey_step(narrative, step)
    for step in _selected_journey_steps(suggestions.get('future_goals'), apply_keys, prefix='goal'):
        _append_journey_step(narrative, step, future=True)
    identity_notes = [
        str(claim.get('fact') or claim.get('headline') or '').strip()
        for claim in accepted_claims
        if _claim_category(claim) in {'identity', 'other', ''}
    ]
    identity_notes = [note for note in identity_notes if note]
    if identity_notes:
        identity_fields.append(_list_field('Public notes', identity_notes))
    _upsert_web_section(narrative, 'identity', identity_fields, 'Public facts confirmed from the web.')

    people = []
    if 'team' in apply_keys:
        people.extend(_normalize_team(suggestions.get('team')))
    for key, role in (
        ('founder', 'Founder'),
        ('chairperson', 'Chairperson'),
        ('program_director', 'Director'),
        ('finance_lead', 'Finance lead'),
    ):
        name = str(suggestions.get(key) or '').strip()
        if name and key in apply_keys:
            people = _merge_people(people, [{'name': name, 'role': role}])
    for claim in accepted_claims:
        if _claim_category(claim) in {'founder', 'team'} and claim.get('fact'):
            people = _merge_people(people, _people_from_claim(claim))
    if people:
        narrative['leadership'] = _merge_people(narrative.get('leadership') or [], people)
        _upsert_web_section(narrative, 'leadership', [_people_field('Team', people)], 'Named people found in public sources.')

    program_notes = []
    if 'flagship_summary' in apply_keys and suggestions.get('flagship_summary'):
        program_notes.append(str(suggestions.get('flagship_summary')).strip())
        programs = [
            item for item in (narrative.get('programs') or [])
            if isinstance(item, dict)
        ]
        snapshot = {
            'key': 'public-program-snapshot',
            'name': 'Public program snapshot',
            'description': str(suggestions.get('flagship_summary')).strip(),
            'beneficiaries': '',
            'status': 'ongoing',
            'source': 'web',
        }
        replaced = False
        for index, item in enumerate(programs):
            if item.get('key') == snapshot['key']:
                programs[index] = snapshot
                replaced = True
                break
        if not replaced:
            programs.append(snapshot)
        narrative['programs'] = programs
    program_notes.extend(
        str(claim.get('fact') or '').strip()
        for claim in accepted_claims
        if _claim_category(claim) == 'program' and claim.get('fact')
    )
    program_notes = [note for note in program_notes if note]
    if program_notes:
        _upsert_web_section(narrative, 'programs', [_list_field('Programs', program_notes)])

    impact_notes = []
    if 'success_story' in apply_keys and suggestions.get('success_story'):
        impact_notes.append(str(suggestions.get('success_story')).strip())
    impact_notes.extend(
        str(claim.get('fact') or '').strip()
        for claim in accepted_claims
        if _claim_category(claim) == 'impact' and claim.get('fact')
    )
    impact_notes = [note for note in impact_notes if note]
    if impact_notes:
        _upsert_web_section(narrative, 'impact', [_list_field('Public impact notes', impact_notes)])

    history_claims = [
        str(claim.get('fact') or '').strip()
        for claim in accepted_claims
        if _claim_category(claim) == 'history' and claim.get('fact')
    ]
    if history_claims:
        _upsert_web_section(
            narrative,
            'identity',
            [_list_field('History notes', history_claims)],
            'Background gathered from public pages.',
        )


def _claim_category(claim: dict) -> str:
    raw = str(claim.get('category') or '').strip().lower()
    if raw in {'founder', 'team', 'history', 'program', 'impact', 'identity'}:
        return raw
    text = f"{claim.get('headline') or ''} {claim.get('fact') or ''}".lower()
    if any(word in text for word in ('founder', 'co-founder', 'cofounder')):
        return 'founder'
    if any(word in text for word in ('team', 'director', 'chair', 'staff', 'ceo')):
        return 'team'
    if any(word in text for word in ('history', 'founded', 'began', 'started', 'origin')):
        return 'history'
    if any(word in text for word in ('program', 'project', 'manufactur', 'service')):
        return 'program'
    return 'identity'


def _people_from_claim(claim: dict) -> list[dict]:
    fact = str(claim.get('fact') or claim.get('headline') or '').strip()
    match = PERSON_ROLE_RE.search(fact)
    if match:
        return [{'name': match.group(1), 'role': match.group(2).title()}]
    headline = str(claim.get('headline') or '').strip()
    if ':' in headline:
        role, name = headline.split(':', 1)
        if name.strip():
            return [{'name': name.strip()[:120], 'role': role.strip()[:120]}]
    return []


def _upsert_web_section(narrative: dict, role: str, fields: list[dict], takeaway: str = '') -> None:
    usable = [field for field in fields if field]
    if not usable:
        return
    sections = [section for section in (narrative.get('sections') or []) if isinstance(section, dict)]
    for section in sections:
        if section.get('role') == role and str(section.get('title') or '') == WEB_SECTION_TITLE:
            rebuilt = []
            replaced = set()
            incoming = {str(field.get('label') or '').strip().lower(): field for field in usable}
            for item in section.get('fields') or []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get('label') or '').strip().lower()
                if label in incoming:
                    rebuilt.append(incoming[label])
                    replaced.add(label)
                else:
                    rebuilt.append(item)
            for label, field in incoming.items():
                if label not in replaced:
                    rebuilt.append(field)
            section['fields'] = rebuilt
            if takeaway:
                section['takeaway'] = takeaway
            narrative['sections'] = sections
            return
    sections.append({
        'title': WEB_SECTION_TITLE,
        'role': role,
        'emphasis': 'standard',
        'takeaway': takeaway,
        'fields': usable,
    })
    narrative['sections'] = sections


def _append_journey_step(narrative: dict, step: dict, *, future: bool = False) -> None:
    journey = narrative.get('journey') if isinstance(narrative.get('journey'), dict) else {}
    bucket = 'future' if future else 'past'
    steps = [item for item in (journey.get(bucket) or []) if isinstance(item, dict)]
    title_key = str(step.get('title') or '').strip().lower()
    if not title_key or any(str(item.get('title') or '').strip().lower() == title_key for item in steps):
        return
    steps.append(step)
    if not future:
        steps.sort(key=_journey_sort_key)
    journey[bucket] = steps
    journey.setdefault('past', [])
    journey.setdefault('future', [])
    narrative['journey'] = journey


def _journey_sort_key(step: dict) -> tuple:
    years = re.findall(r'\b(?:19|20)\d{2}\b', str(step.get('when') or ''))
    return (int(years[0]) if years else 9999, str(step.get('title') or '').lower())


def _journey_key(step: dict) -> str:
    return program_key(f"{step.get('when') or ''} {step.get('title') or ''}")


def _journey_step_records(value, *, future: bool) -> list[dict]:
    items = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
    allowed = {'planned', 'aspiration'} if future else {'done', 'in_progress'}
    records = []
    seen = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = ' '.join(str(raw.get('title') or '').split())
        summary = ' '.join(str(raw.get('summary') or '').split())
        when = ' '.join(str(raw.get('when') or '').split())
        if len(title) < 3 or title.lower() in seen:
            continue
        if not summary and not when:
            continue
        status = str(raw.get('status') or '').strip().lower()
        if status not in allowed:
            status = 'aspiration' if future and not when else ('planned' if future else 'done')
        seen.add(title.lower())
        records.append({
            'when': when[:40],
            'title': title[:120],
            'summary': summary[:400],
            'detail': summary[:800],
            'status': status,
        })
    return records[:8]


def _selected_journey_steps(value, apply_keys: set[str], *, prefix: str) -> list[dict]:
    future = prefix == 'goal'
    return [
        step for step in _journey_step_records(value, future=future)
        if f'{prefix}:{_journey_key(step)}' in apply_keys
    ]


def _milestones_from_claims(claims: list[dict]) -> list[dict]:
    steps = []
    seen = set()
    for claim in claims or []:
        fact = ' '.join(str(claim.get('fact') or '').split())
        years = re.findall(r'\b(?:19|20)\d{2}\b', fact)
        if not fact or not years:
            continue
        if _claim_category(claim) not in {'history', 'impact', 'program', 'identity'}:
            continue
        lowered = fact.lower()
        titles = []
        if 'regist' in lowered and any(word in lowered for word in ('found', 'established', 'started')) and len(years) >= 2:
            titles = [('Organisation founded', years[0]), ('Official registration', years[-1])]
        elif 'regist' in lowered:
            titles = [('Official registration', years[0])]
        elif any(word in lowered for word in ('found', 'established', 'started')):
            titles = [('Organisation founded', years[0])]
        else:
            headline = ' '.join(str(claim.get('headline') or 'Public milestone').split())[:80]
            titles = [(headline, years[0] if len(years) == 1 else f'{years[0]}–{years[-1]}')]
        for title, when in titles:
            if title.lower() in seen:
                continue
            seen.add(title.lower())
            steps.append({
                'when': when,
                'title': title,
                'summary': fact[:180],
                'detail': fact[:400],
                'status': 'done',
            })
    return steps


def _statement_field(label: str, value) -> dict | None:
    cleaned = str(value or '').strip()
    if not cleaned:
        return None
    return {
        'label': label,
        'kind': 'statement',
        'value': cleaned[:800],
        'items': [],
        'entries': [],
        'confidence': 0.7,
    }


def _list_field(label: str, items: list[str]) -> dict | None:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    cleaned = list(dict.fromkeys(cleaned))[:8]
    if not cleaned:
        return None
    return {
        'label': label,
        'kind': 'list',
        'value': '',
        'items': cleaned,
        'entries': [],
        'confidence': 0.7,
    }


def _people_field(label: str, people: list[dict]) -> dict | None:
    entries = []
    for person in _normalize_team(people):
        entries.append({
            'title': person['name'],
            'subtitle': person.get('role') or '',
            'detail': person.get('notes') or '',
            'status': 'none',
            'date': '',
        })
    if not entries:
        return None
    return {
        'label': label,
        'kind': 'people',
        'value': '',
        'items': [],
        'entries': entries,
        'confidence': 0.7,
    }


def _parse_json(raw_value) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    try:
        parsed = json.loads(raw_value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _parse_model_json(text: str) -> dict:
    cleaned = (text or '').strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1]
    if cleaned.endswith('```'):
        cleaned = cleaned.rsplit('```', 1)[0]
    parsed = json.loads(cleaned.strip())
    return parsed if isinstance(parsed, dict) else {}
