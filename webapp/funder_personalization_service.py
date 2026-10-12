"""Funder interest profiles, preference memory, and CBO profile layout tailoring."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime

from .models import CBO, FunderPreferenceEvent, FunderProfile, SavedCBO, db


FUNDER_INTEREST_CHOICES = (
    ('agriculture', 'Agriculture & food systems'),
    ('education', 'Education'),
    ('healthcare', 'Health'),
    ('water', 'Water & sanitation'),
    ('livelihood', 'Livelihoods & jobs'),
    ('environment', 'Environment & climate'),
    ('youth', 'Youth'),
    ('women', 'Women & gender'),
    ('community', 'Community development'),
    ('governance', 'Governance & transparency'),
    ('finance', 'Financial systems & grants'),
)

FUNDER_POPULATION_CHOICES = (
    ('smallholder_farmers', 'Smallholder farmers'),
    ('women_led', 'Women-led groups'),
    ('youth', 'Youth and young people'),
    ('school_children', 'School-age children'),
    ('urban_informal', 'Urban informal settlements'),
    ('pastoralists', 'Pastoralist communities'),
    ('persons_disabilities', 'Persons with disabilities'),
    ('community_wide', 'Whole communities'),
)

FUNDER_OUTCOME_CHOICES = (
    ('food_security', 'Food security'),
    ('income', 'Household income'),
    ('education_access', 'Education access'),
    ('health_outcomes', 'Health outcomes'),
    ('climate_resilience', 'Climate resilience'),
    ('water_access', 'Safe water access'),
    ('jobs', 'Jobs and livelihoods'),
    ('gender_equity', 'Gender equity'),
)

SECTOR_INTERESTS = frozenset({
    'agriculture', 'education', 'healthcare', 'water', 'livelihood', 'environment',
})
CROSS_CUTTING_INTERESTS = frozenset({
    'youth', 'women', 'community', 'governance', 'finance',
})

INTEREST_KEYWORDS = {
    'agriculture': ('agriculture', 'agricultural', 'farm', 'farmer', 'smallholder', 'seed', 'livestock', 'harvest', 'acre', 'irrigation', 'crop'),
    'education': ('education', 'school', 'student', 'learning', 'literacy', 'homework', 'tutoring', 'classroom'),
    'healthcare': ('health', 'clinic', 'patient', 'medical', 'wellness', 'maternal'),
    'water': ('water', 'sanitation', 'hygiene', 'borehole'),
    'livelihood': ('livelihood', 'income', 'enterprise', 'employment', 'job', 'jobs'),
    'environment': ('environment', 'climate', 'conservation', 'tree', 'sustainab'),
    'youth': ('youth', 'young people'),
    'women': ('women', 'woman', 'gender', 'girl'),
    'community': ('community development', 'community-led', 'grassroot'),
    'governance': ('governance', 'transparency', 'accountab'),
    'finance': ('grantmaking', 'endowment', 'financial systems'),
}

POPULATION_KEYWORDS = {
    'smallholder_farmers': ('smallholder', 'farmer', 'farmers', 'acre'),
    'women_led': ('women-led', 'women', 'woman', 'girl'),
    'youth': ('youth', 'young people'),
    'school_children': ('student', 'school', 'pupil', 'homework', 'children'),
    'urban_informal': ('kibera', 'informal settlement', 'slum', 'urban'),
    'pastoralists': ('pastoral', 'livestock', 'herder'),
    'persons_disabilities': ('disabilit', 'accessible learning'),
    'community_wide': ('community',),
}

OUTCOME_KEYWORDS = {
    'food_security': ('food security', 'harvest', 'nutrition', 'hunger'),
    'income': ('income', 'livelihood', 'earnings', 'revenue'),
    'education_access': ('education', 'literacy', 'school', 'learning'),
    'health_outcomes': ('health', 'clinic', 'maternal', 'wellness'),
    'climate_resilience': ('climate', 'conservation', 'resilien', 'drought'),
    'water_access': ('water', 'borehole', 'sanitation'),
    'jobs': ('job', 'employment', 'livelihood'),
    'gender_equity': ('women', 'gender', 'girl'),
}

PLACEHOLDER_FOCUS_AREAS = {
    'rural livelihood, subsistence agriculture, youth',
    'rural livelihood, subsistence agriculture, youth empowerment',
    'community development, local service delivery',
}

STOPWORDS = {
    'that', 'this', 'with', 'from', 'they', 'them', 'your', 'about', 'into', 'have',
    'their', 'what', 'when', 'where', 'which', 'while', 'would', 'could', 'should',
    'kenya', 'kenyan', 'community', 'based', 'organisation', 'organization', 'cbo',
    'and', 'the', 'for', 'are', 'our', 'you', 'who', 'how',
}

SECTION_KEYS = ('kpis', 'overview', 'stories', 'programs', 'narrative', 'growth', 'score', 'public_sources')

INTEREST_SECTION_BOOSTS = {
    'agriculture': {'programs': 3, 'kpis': 2, 'stories': 1},
    'education': {'programs': 3, 'kpis': 2, 'narrative': 1},
    'healthcare': {'kpis': 3, 'programs': 2, 'score': 1},
    'water': {'programs': 3, 'kpis': 2, 'overview': 1},
    'livelihood': {'kpis': 3, 'stories': 2, 'growth': 2},
    'environment': {'programs': 3, 'narrative': 2, 'score': 1},
    'youth': {'stories': 3, 'programs': 2, 'kpis': 1},
    'women': {'stories': 3, 'programs': 2, 'narrative': 1},
    'community': {'stories': 4, 'score': 2, 'public_sources': 1},
    'governance': {'overview': 4, 'narrative': 2},
    'finance': {'overview': 4, 'growth': 3, 'kpis': 1},
}

DEFAULT_SECTION_ORDER = {
    'kpis': 10,
    'overview': 20,
    'stories': 30,
    'programs': 40,
    'narrative': 50,
    'growth': 60,
    'score': 70,
    'public_sources': 80,
}


def interest_choice_values() -> list[str]:
    return [item[0] for item in FUNDER_INTEREST_CHOICES]


def population_choice_values() -> list[str]:
    return [item[0] for item in FUNDER_POPULATION_CHOICES]


def outcome_choice_values() -> list[str]:
    return [item[0] for item in FUNDER_OUTCOME_CHOICES]


def interest_label(value: str) -> str:
    return next((label for key, label in FUNDER_INTEREST_CHOICES if key == value), value.replace('_', ' ').title())


def population_label(value: str) -> str:
    return next((label for key, label in FUNDER_POPULATION_CHOICES if key == value), value.replace('_', ' ').title())


def _normalize_contact_email(value) -> str:
    email = str(value or '').strip()
    if not email:
        return ''
    if len(email) > 255 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        raise ValueError('Enter a valid preferred contact email, or leave it blank to use your login email.')
    return email


def outcome_label(value: str) -> str:
    return next((label for key, label in FUNDER_OUTCOME_CHOICES if key == value), value.replace('_', ' ').title())


def ensure_funder_profile(user) -> FunderProfile | None:
    if user is None or not getattr(user, 'is_funder', False):
        return None
    profile = getattr(user, 'funder_profile', None)
    if profile is not None:
        return profile
    profile = FunderProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        profile = FunderProfile(
            user_id=user.id,
            organization_name=str(getattr(user, 'display_name', '') or '').strip(),
        )
        db.session.add(profile)
        db.session.commit()
    return profile


def serialize_funder_profile(profile: FunderProfile | None) -> dict:
    empty_brief = {
        'themes': '',
        'populations': [],
        'outcomes': [],
        'evidence_needed': '',
    }
    if profile is None:
        return {
            'organization_name': '',
            'contact_email': '',
            'website': '',
            'country_region': '',
            'about_text': '',
            'interest_areas': [],
            'interest_labels': [],
            'geographic_focus': [],
            'grant_size_min': None,
            'grant_size_max': None,
            'grant_currency': 'KES',
            'preference_memory': {},
            'interest_brief': empty_brief,
            'complete': False,
        }
    interests = [item for item in _as_list(profile.interest_areas_json) if item in set(interest_choice_values())]
    geos = _as_list(profile.geographic_focus_json)
    memory = _as_dict(profile.preference_memory_json)
    brief = _normalize_interest_brief(getattr(profile, 'interest_brief_json', '') or '{}')
    return {
        'organization_name': profile.organization_name or '',
        'contact_email': getattr(profile, 'contact_email', '') or '',
        'website': profile.website or '',
        'country_region': profile.country_region or '',
        'about_text': profile.about_text or '',
        'interest_areas': interests,
        'interest_labels': [interest_label(item) for item in interests],
        'geographic_focus': geos,
        'grant_size_min': profile.grant_size_min,
        'grant_size_max': profile.grant_size_max,
        'grant_currency': profile.grant_currency or 'KES',
        'preference_memory': memory,
        'interest_brief': brief,
        'complete': bool(interests or profile.about_text or brief.get('themes')),
    }


def update_funder_profile(user, form_data: dict) -> FunderProfile:
    profile = ensure_funder_profile(user)
    if profile is None:
        raise RuntimeError('A funder account is required to save this profile.')
    contact_email = _normalize_contact_email(form_data.get('contact_email'))
    profile.organization_name = str(form_data.get('organization_name') or '').strip()[:255]
    profile.contact_email = contact_email
    profile.website = str(form_data.get('website') or '').strip()[:500]
    profile.country_region = str(form_data.get('country_region') or '').strip()[:255]
    profile.about_text = str(form_data.get('about_text') or '').strip()[:4000]
    interests = [
        value for value in _as_list(form_data.get('interest_areas'))
        if value in set(interest_choice_values())
    ]
    geos = [item.strip() for item in _split_focus(form_data.get('geographic_focus')) if item.strip()][:8]
    profile.interest_areas_json = json.dumps(interests)
    profile.geographic_focus_json = json.dumps(geos)
    profile.grant_size_min = _parse_optional_float(form_data.get('grant_size_min'))
    profile.grant_size_max = _parse_optional_float(form_data.get('grant_size_max'))
    currency = str(form_data.get('grant_currency') or 'KES').strip().upper()[:12]
    profile.grant_currency = currency or 'KES'
    brief = {
        'themes': str(form_data.get('interest_themes') or '').strip()[:2000],
        'populations': [
            value for value in _as_list(form_data.get('priority_populations'))
            if value in set(population_choice_values())
        ][:8],
        'outcomes': [
            value for value in _as_list(form_data.get('priority_outcomes'))
            if value in set(outcome_choice_values())
        ][:8],
        'evidence_needed': str(form_data.get('evidence_needed') or '').strip()[:2000],
    }
    profile.interest_brief_json = json.dumps(brief)
    profile.updated_at = datetime.utcnow()
    db.session.add(profile)
    db.session.commit()
    return profile


def build_funder_search_context(serialized: dict | None) -> dict:
    data = serialized if isinstance(serialized, dict) else {}
    brief = data.get('interest_brief') if isinstance(data.get('interest_brief'), dict) else {}
    labels = list(data.get('interest_labels') or [])
    populations = [population_label(item) for item in (brief.get('populations') or [])]
    outcomes = [outcome_label(item) for item in (brief.get('outcomes') or [])]
    themes = str(brief.get('themes') or '').strip()
    about = str(data.get('about_text') or '').strip()
    evidence = str(brief.get('evidence_needed') or '').strip()
    search_terms = []
    for source in (
        labels,
        populations,
        outcomes,
        _theme_terms(themes),
        _theme_terms(about),
        _theme_terms(evidence),
        data.get('geographic_focus') or [],
    ):
        for item in source:
            text = str(item or '').strip()
            if text and text.lower() not in {term.lower() for term in search_terms}:
                search_terms.append(text)
    parts = []
    if labels:
        parts.append(', '.join(labels))
    if populations:
        parts.append('Populations: ' + ', '.join(populations))
    if outcomes:
        parts.append('Outcomes: ' + ', '.join(outcomes))
    if themes:
        parts.append(themes)
    if about:
        parts.append(about)
    return {
        'interest_areas': list(data.get('interest_areas') or []),
        'interest_labels': labels,
        'geographic_focus': list(data.get('geographic_focus') or []),
        'themes': themes,
        'populations': populations,
        'outcomes': outcomes,
        'about': about,
        'evidence_needed': evidence,
        'search_terms': search_terms[:16],
        'summary': '. '.join(parts)[:800],
    }


def apply_funder_context_to_search_plan(search_plan: dict | None, funder_context: dict | None) -> dict:
    """Keep a marketplace search limited to what was typed.

    Saved funder interests used to be copied into the search plan. A search
    that differed from those interests then looked like a weak match. Profile
    fit is scored on its own by score_cbo_for_funder.
    """
    del funder_context
    return dict(search_plan or {})


def record_funder_event(user, event_type: str, cbo: CBO | None = None, payload: dict | None = None) -> None:
    if user is None or not getattr(user, 'is_funder', False):
        return
    event = FunderPreferenceEvent(
        user_id=user.id,
        cbo_id=getattr(cbo, 'id', None),
        event_type=str(event_type or 'view_profile')[:40],
        payload_json=json.dumps(payload or {}, default=str),
    )
    db.session.add(event)
    db.session.flush()
    refresh_funder_memory(user)
    db.session.commit()


def refresh_funder_memory(user) -> dict:
    profile = ensure_funder_profile(user)
    if profile is None:
        return {}
    events = (
        FunderPreferenceEvent.query
        .filter_by(user_id=user.id)
        .order_by(FunderPreferenceEvent.created_at.desc())
        .limit(80)
        .all()
    )
    saved_ids = {
        entry.cbo_id
        for entry in SavedCBO.query.filter_by(user_id=user.id).all()
        if entry.cbo_id
    }
    interest_counter = Counter()
    geo_counter = Counter()
    event_counts = Counter()
    for event in events:
        event_counts[event.event_type] += 1
        cbo = event.cbo if event.cbo_id else None
        if cbo is None and event.cbo_id:
            cbo = db.session.get(CBO, event.cbo_id)
        if cbo is None:
            continue
        for interest in infer_cbo_interest_tags(cbo):
            weight = 3 if event.event_type in {'save_cbo', 'contact'} else 1
            if event.event_type == 'unsave_cbo':
                weight = 0
            interest_counter[interest] += weight
        location = str(cbo.location or cbo.county_region or '').strip()
        if location:
            geo_counter[location] += 1

    declared = _as_list(profile.interest_areas_json)
    learned = [key for key, _count in interest_counter.most_common(6) if key not in declared]
    memory = {
        'updated_at': datetime.utcnow().isoformat(),
        'declared_interests': declared,
        'learned_interests': learned[:4],
        'saved_cbo_count': len(saved_ids),
        'event_counts': dict(event_counts),
        'frequent_geographies': [name for name, _count in geo_counter.most_common(4)],
    }
    profile.preference_memory_json = json.dumps(memory)
    db.session.add(profile)
    return memory


def infer_cbo_interest_tags(cbo: CBO, profile: dict | None = None) -> list[str]:
    blob = _cbo_search_blob(cbo, profile)
    tags = []
    for interest, keywords in INTEREST_KEYWORDS.items():
        if any(_contains_term(blob, keyword) for keyword in keywords):
            tags.append(interest)
    for item in _cbo_classifications(cbo, profile):
        if item in INTEREST_KEYWORDS and item not in tags:
            tags.append(item)
    return tags


def score_cbo_for_funder(cbo: CBO, funder_profile: FunderProfile | None, profile: dict | None = None) -> dict:
    serialized = serialize_funder_profile(funder_profile)
    declared = list(serialized.get('interest_areas') or [])
    learned = list((serialized.get('preference_memory') or {}).get('learned_interests') or [])
    brief = serialized.get('interest_brief') if isinstance(serialized.get('interest_brief'), dict) else {}
    cbo_tags = infer_cbo_interest_tags(cbo, profile)
    overlap = [item for item in declared if item in cbo_tags]
    sector_overlap = [item for item in overlap if item in SECTOR_INTERESTS]
    extra_overlap = [item for item in overlap if item not in SECTOR_INTERESTS]
    declared_sectors = [item for item in declared if item in SECTOR_INTERESTS]
    blob = _cbo_search_blob(cbo, profile)
    requirements = _parse_funder_requirements(serialized)
    theme_hits = _matching_theme_terms(brief.get('themes') or '', blob)
    theme_hits = list(dict.fromkeys(theme_hits))
    population_hits = [
        item for item in (brief.get('populations') or [])
        if any(_contains_term(blob, keyword) for keyword in POPULATION_KEYWORDS.get(item) or ())
    ]
    outcome_hits = [
        item for item in (brief.get('outcomes') or [])
        if any(_contains_term(blob, keyword) for keyword in OUTCOME_KEYWORDS.get(item) or ())
    ]
    geo_focus = [item.lower() for item in (serialized.get('geographic_focus') or [])]
    location = ' '.join([
        str((profile or {}).get('location') or ''),
        str(cbo.location or ''),
        str(cbo.county_region or ''),
    ]).lower()
    geo_hit = any(term and term in location for term in geo_focus)

    if declared_sectors:
        is_match = bool(sector_overlap)
    else:
        is_match = bool(overlap or len(theme_hits) >= 2)

    score = 0
    if sector_overlap:
        score += 48 + (16 * (len(sector_overlap) - 1))
    if extra_overlap and is_match:
        score += 8 * len(extra_overlap)
    if theme_hits and is_match:
        score += min(18, 6 * len(theme_hits))
    if population_hits and is_match:
        score += 8 * len(population_hits)
    if outcome_hits and is_match:
        score += 6 * len(outcome_hits)
    if geo_hit:
        score += 8 if is_match else 3
    learned_overlap = [item for item in learned if item in cbo_tags and item not in declared]
    score += min(10, 4 * len(learned_overlap))

    explanation = _explain_funder_match(
        cbo,
        profile,
        declared=declared,
        sector_overlap=sector_overlap,
        extra_overlap=extra_overlap,
        theme_hits=theme_hits,
        population_hits=population_hits,
        outcome_hits=outcome_hits,
        geo_hit=geo_hit,
        is_match=is_match,
        cbo_tags=cbo_tags,
        blob=blob,
        brief=brief,
        serialized=serialized,
        geo_focus=geo_focus,
        requirements=requirements,
    )
    return {
        'score': int(score),
        'display_score': min(100, int(score)),
        'overlap': overlap,
        'is_match': is_match,
        'reasons': explanation['reasons'],
        'gap_reasons': explanation['gap_reasons'],
        'summary': explanation['summary'],
        'headline': explanation['headline'],
        'interest_labels': [interest_label(item) for item in declared],
        'has_profile_interests': bool(
            declared
            or str(brief.get('themes') or '').strip()
            or brief.get('populations')
            or brief.get('outcomes')
        ),
    }


def text_overlaps_funder_interests(text: str, funder_profile: FunderProfile | dict | None) -> dict:
    """Whether a short update mentions the funder's saved interests.

    Declared sectors, populations, and free-text themes count. Learned
    behaviour from browsing does not, so alerts follow the profile they wrote.
    """
    if isinstance(funder_profile, dict):
        serialized = funder_profile
    else:
        serialized = serialize_funder_profile(funder_profile)
    brief = serialized.get('interest_brief') if isinstance(serialized.get('interest_brief'), dict) else {}
    declared = [item for item in (serialized.get('interest_areas') or []) if item in INTEREST_KEYWORDS]
    blob = re.sub(r'\s+', ' ', str(text or '')).strip().lower()
    hits = [
        item for item in declared
        if any(_contains_term(blob, keyword) for keyword in INTEREST_KEYWORDS.get(item) or ())
    ]
    theme_hits = _matching_theme_terms(str(brief.get('themes') or ''), blob) if blob else []
    population_hits = [
        item for item in (brief.get('populations') or [])
        if any(_contains_term(blob, keyword) for keyword in POPULATION_KEYWORDS.get(item) or ())
    ]
    return {
        'is_match': bool(hits or theme_hits or population_hits),
        'hits': hits,
        'theme_hits': list(dict.fromkeys(theme_hits)),
        'population_hits': population_hits,
        'interest_labels': [interest_label(item) for item in hits],
    }


def build_funder_profile_layout(user, cbo: CBO, profile: dict | None = None) -> dict | None:
    if user is None or not getattr(user, 'is_funder', False):
        return None
    funder_profile = ensure_funder_profile(user)
    serialized = serialize_funder_profile(funder_profile)
    declared = list(serialized.get('interest_areas') or [])
    brief = serialized.get('interest_brief') if isinstance(serialized.get('interest_brief'), dict) else {}
    if not declared and not brief.get('themes') and not serialized.get('about_text'):
        return None

    scores = {key: 0 for key in SECTION_KEYS}
    for interest in declared:
        for section, boost in (INTEREST_SECTION_BOOSTS.get(interest) or {}).items():
            scores[section] = scores.get(section, 0) + boost

    ranked = sorted(SECTION_KEYS, key=lambda key: (-scores.get(key, 0), DEFAULT_SECTION_ORDER[key]))
    section_order = {key: index + 1 for index, key in enumerate(ranked)}
    emphasized = [key for key in ranked if scores.get(key, 0) >= 3][:3]
    collapsed = [key for key in ranked[-2:] if key not in emphasized and key in {'growth', 'narrative', 'public_sources'}]
    match = score_cbo_for_funder(cbo, funder_profile, profile)
    labels = [interest_label(item) for item in declared[:4]]
    return {
        'section_order': section_order,
        'emphasized': emphasized,
        'collapsed': collapsed,
        'headline': match.get('headline') or 'Arranged around your funding interests',
        'reasons': match.get('reasons') or [],
        'gap_reasons': match.get('gap_reasons') or [],
        'summary': match.get('summary') or '',
        'is_match': bool(match.get('is_match')),
        'interest_labels': labels,
        'cbo_name': str((profile or {}).get('name') or cbo.name or 'This CBO').strip(),
        'match': match,
        'kpi_priority': declared,
    }


def prioritize_metric_cards(cards: list, interests: list[str] | None = None) -> list:
    if not cards or not interests:
        return list(cards or [])
    keywords = []
    for interest in interests:
        keywords.extend(INTEREST_KEYWORDS.get(interest) or (interest,))

    def _score(card) -> int:
        if not isinstance(card, dict):
            return 0
        text = f"{card.get('label') or ''} {card.get('detail') or ''} {card.get('value') or ''}".lower()
        return sum(1 for keyword in keywords if _contains_term(text, keyword))

    return sorted(list(cards), key=_score, reverse=True)


def _explain_funder_match(
    cbo: CBO,
    profile: dict | None,
    *,
    declared: list[str],
    sector_overlap: list[str],
    extra_overlap: list[str],
    theme_hits: list[str],
    population_hits: list[str],
    outcome_hits: list[str],
    geo_hit: bool,
    is_match: bool,
    cbo_tags: list[str] | None = None,
    blob: str = '',
    brief: dict | None = None,
    serialized: dict | None = None,
    geo_focus: list[str] | None = None,
    requirements: dict | None = None,
) -> dict:
    name = str((profile or {}).get('name') or cbo.name or 'This CBO').strip()
    declared_labels = [interest_label(item) for item in declared]
    facts = _cbo_evidence_facts(cbo, profile)
    reasons = []
    if is_match:
        headline = f'{name} is a strong match for your {", ".join(declared_labels[:2]) or "funding"} focus'
        if sector_overlap:
            labels = [interest_label(item) for item in sector_overlap]
            evidence = _first_fact_for(facts, sector_overlap)
            if evidence:
                reasons.append(f'This fits {", ".join(labels)} because {evidence}')
            elif facts.get('tagline'):
                reasons.append(f'This fits {", ".join(labels)}: {facts["tagline"]}.')
            else:
                reasons.append(
                    f'{name} is described around {", ".join(labels)}, which you selected as a funding area.'
                )
        metric = _first_metric_for(facts, sector_overlap or declared)
        if metric:
            reasons.append(f'From their operating record: {metric}.')
        program = facts.get('program')
        if program:
            reasons.append(f'Program example: {program}')
        story = facts.get('story')
        if story:
            reasons.append(f'Impact example: {story}')
        if population_hits:
            reasons.append(
                'They work with '
                + ', '.join(population_label(item) for item in population_hits)
                + ', which matches the populations you fund.'
            )
        if outcome_hits:
            reasons.append(
                'Their profile points to '
                + ', '.join(outcome_label(item) for item in outcome_hits)
                + ', aligned with outcomes you listed.'
            )
        if theme_hits:
            reasons.append(
                'Their own description uses your interest language: '
                + ', '.join(f'“{item}”' for item in theme_hits[:3])
                + '.'
            )
        if geo_hit:
            reasons.append(f'They operate in {cbo.location or cbo.county_region}, a geography you listed.')
        years_note = _years_requirement_reason(cbo, profile, requirements or {}, is_gap=False)
        if years_note:
            reasons.append(years_note)
        leadership_note = _leadership_requirement_reason(cbo, profile, requirements or {}, is_gap=False)
        if leadership_note:
            reasons.append(leadership_note)
        summary = reasons[0] if reasons else f'{name} overlaps your declared funding interests.'
        if facts.get('tagline') and facts.get('tagline') not in summary:
            headline = f'{name}: {facts["tagline"]}'
            if len(headline) > 140:
                headline = f'{name} is a strong match for your {declared_labels[0] if declared_labels else "funding"} focus'
    else:
        actual = facts.get('focus') or facts.get('tagline')
        focus_labels = ', '.join(declared_labels[:3]) if declared_labels else 'your saved interests'
        headline = f'{name} is not a close match for {focus_labels}'
        if actual:
            reasons.append(f'Their profile is about {actual}, which does not overlap the sectors you selected.')
        if extra_overlap and declared and any(item in SECTOR_INTERESTS for item in declared):
            reasons.append(
                'Shared themes like '
                + ', '.join(interest_label(item) for item in extra_overlap)
                + ' are not enough on their own when your core focus is '
                + ', '.join(interest_label(item) for item in declared if item in SECTOR_INTERESTS)
                + '.'
            )
        if not reasons:
            reasons.append(
                f'Nothing in this profile clearly supports {focus_labels}, so it is ranked below closer fits.'
            )
        summary = reasons[0]
    gap_reasons = _explain_funder_gaps(
        cbo,
        profile,
        facts=facts,
        declared=declared,
        sector_overlap=sector_overlap,
        cbo_tags=cbo_tags or [],
        blob=blob,
        brief=brief or {},
        serialized=serialized or {},
        geo_focus=geo_focus or [],
        geo_hit=geo_hit,
        population_hits=population_hits,
        outcome_hits=outcome_hits,
        theme_hits=theme_hits,
        requirements=requirements or {},
    )
    return {
        'headline': headline,
        'summary': summary,
        'reasons': reasons[:4],
        'gap_reasons': gap_reasons[:8],
    }


def _explain_funder_gaps(
    cbo: CBO,
    profile: dict | None,
    *,
    facts: dict,
    declared: list[str],
    sector_overlap: list[str],
    cbo_tags: list[str],
    blob: str,
    brief: dict,
    serialized: dict,
    geo_focus: list[str],
    geo_hit: bool,
    population_hits: list[str],
    outcome_hits: list[str],
    theme_hits: list[str],
    requirements: dict | None = None,
) -> list[str]:
    gaps = []
    years_gap = _years_requirement_reason(cbo, profile, requirements or {}, is_gap=True)
    if years_gap:
        gaps.append(years_gap)
    leadership_gap = _leadership_requirement_reason(cbo, profile, requirements or {}, is_gap=True)
    if leadership_gap:
        gaps.append(leadership_gap)
    classifications = _cbo_classifications(cbo, profile)
    missing_sectors = [item for item in declared if item in SECTOR_INTERESTS and item not in sector_overlap]
    if missing_sectors:
        gaps.append(
            'This profile does not show work in '
            + ', '.join(interest_label(item) for item in missing_sectors)
            + ', which you listed as a funding area.'
        )
    extra_sectors = [
        item for item in cbo_tags
        if item in SECTOR_INTERESTS and item not in declared
    ]
    extra_labels = [interest_label(item) for item in extra_sectors[:3]]
    if extra_labels:
        gaps.append(
            'A substantial part of this CBO is also '
            + ', '.join(extra_labels)
            + ', which sits outside the sectors you selected.'
        )
    if 'tools' in classifications and 'agriculture' in declared and 'tools' not in declared:
        gaps.append(
            'Their operating model is tool access and rental. Agriculture appears mainly as the farmers who borrow equipment, not as a production or food-systems programme of its own.'
        )
    missing_pops = [item for item in (brief.get('populations') or []) if item not in population_hits]
    if missing_pops:
        gaps.append(
            'The profile does not clearly reach '
            + ', '.join(population_label(item) for item in missing_pops)
            + ', a population you said you fund.'
        )
    missing_outcomes = [item for item in (brief.get('outcomes') or []) if item not in outcome_hits]
    if missing_outcomes:
        gaps.append(
            'There is little evidence here for '
            + ', '.join(outcome_label(item) for item in missing_outcomes)
            + ', which you named as a priority outcome.'
        )
    if geo_focus and not geo_hit:
        location = str(
            (profile or {}).get('location')
            or cbo.location
            or cbo.county_region
            or 'an unlisted location'
        ).strip()
        gaps.append(
            f'They operate in {location}, not in {", ".join(serialized.get("geographic_focus") or geo_focus)}, which you listed as geographic focus.'
        )
    missed_themes = [
        term for term in _theme_terms(str(brief.get('themes') or ''))
        if term.lower() not in {item.lower() for item in theme_hits} and not _contains_term(blob, term)
    ][:4]
    if missed_themes:
        gaps.append(
            'Your stated interest in '
            + ', '.join(f'“{item}”' for item in missed_themes[:3])
            + ' does not appear in this CBO’s description or programmes.'
        )
    evidence_needed = str(brief.get('evidence_needed') or '').strip()
    missed_evidence = [
        term for term in _theme_terms(evidence_needed)
        if not _contains_term(blob, term)
    ][:3]
    if missed_evidence:
        gaps.append(
            'The diligence evidence you asked for is thin here: nothing obvious on '
            + ', '.join(missed_evidence)
            + '.'
        )
    if 'agriculture' in declared and not _first_metric_for(facts, ['agriculture']):
        gaps.append(
            'The visible operating numbers are not agriculture results (farmer reach, acres, harvest, or livestock). Bookkeeping activity alone is not proof of food-systems impact.'
        )
    if not facts.get('program') and not facts.get('story'):
        gaps.append('There is no clear programme write-up or impact story to verify how the work is delivered.')
    if not gaps:
        gaps.append(
            'Even with some overlap, this profile still needs a closer read against your grant size, evidence standard, and how much of the work is actually in your priority sector.'
        )
    return gaps


NOISY_METRIC_LABELS = (
    'ledger',
    'manual row',
    'digitized document',
    'months covered',
    'top category',
)


def _cbo_evidence_facts(cbo: CBO, profile: dict | None) -> dict:
    data = profile if isinstance(profile, dict) else {}
    focus = _clean_focus(data.get('focus_areas') or cbo.focus_areas)
    tagline = _readable_text(data.get('tagline'))
    flagship = data.get('flagship_project') if isinstance(data.get('flagship_project'), dict) else {}
    program = _readable_text(
        flagship.get('summary')
        or flagship.get('title')
        or data.get('flagship_summary')
        or cbo.flagship_summary
    )
    story = _readable_text(data.get('success_story') or cbo.success_story)
    classifications = _cbo_classifications(cbo, data)
    metrics = []
    for card in (data.get('operational_metric_cards') or [])[:8]:
        if not isinstance(card, dict):
            continue
        value = str(card.get('value') or '').strip()
        label = str(card.get('label') or '').strip()
        if not value or not label or value in {'0', '—', '-'}:
            continue
        if any(noise in label.lower() for noise in NOISY_METRIC_LABELS):
            continue
        metrics.append(f'{value} {label.lower()}')
    for impact in (data.get('quantified_impact') or [])[:3]:
        if not isinstance(impact, dict):
            continue
        metric_value = str(impact.get('metric_value') or '').strip()
        description = str(impact.get('description') or impact.get('metric_unit') or '').strip()
        text = ' '.join(part for part in [metric_value, description] if part).strip()
        if text:
            metrics.append(text)
    return {
        'focus': focus,
        'tagline': tagline.rstrip('.'),
        'program': _clip_sentence(program, 180),
        'story': _clip_sentence(story, 180),
        'classifications': ', '.join(classifications[:4]),
        'metrics': metrics,
    }


def _first_fact_for(facts: dict, interests: list[str]) -> str:
    snippets = [
        facts.get('tagline'),
        facts.get('program'),
        facts.get('story'),
        facts.get('focus'),
        *list(facts.get('metrics') or []),
    ]
    for snippet in snippets:
        text = str(snippet or '').strip()
        if not text or _looks_like_tag_list(text):
            continue
        blob = text.lower()
        if any(
            _contains_term(blob, keyword)
            for interest in interests
            for keyword in INTEREST_KEYWORDS.get(interest) or ()
        ):
            return _clip_sentence(text, 160)
    return ''


def _first_metric_for(facts: dict, interests: list[str]) -> str:
    for metric in facts.get('metrics') or []:
        blob = metric.lower()
        if any(
            _contains_term(blob, keyword)
            for interest in interests
            for keyword in INTEREST_KEYWORDS.get(interest) or ()
        ):
            return metric
    return ''


def _cbo_search_blob(cbo: CBO, profile: dict | None = None) -> str:
    data = profile if isinstance(profile, dict) else {}
    parts = [
        cbo.name,
        _clean_focus(cbo.focus_areas),
        cbo.location,
        cbo.flagship_summary,
        cbo.success_story,
        cbo.classifications_json,
        str(data.get('tagline') or ''),
        _clean_focus(data.get('focus_areas')),
        json.dumps(data.get('classifications') or []),
        json.dumps(data.get('flagship_project') or {}),
        str(data.get('success_story') or ''),
        json.dumps(data.get('operational_metric_cards') or []),
        json.dumps(data.get('quantified_impact') or []),
    ]
    return ' '.join(str(part or '') for part in parts).lower()


def _cbo_classifications(cbo: CBO, profile: dict | None = None) -> list[str]:
    classes = _as_list(cbo.classifications_json)
    if isinstance(profile, dict):
        classes.extend(_as_list(profile.get('classifications')))
    identifier = str(getattr(cbo, 'cbo_identifier', '') or '').strip().lower()
    if identifier:
        classes.append(identifier)
    cleaned = []
    for item in classes:
        normalized = str(item or '').strip().lower()
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


def _readable_text(value) -> str:
    if isinstance(value, dict):
        for key in ('summary', 'story', 'text', 'quote', 'description', 'body'):
            text = str(value.get(key) or '').strip()
            if text:
                return ' '.join(text.split())
        return ''
    if isinstance(value, list):
        for item in value:
            text = _readable_text(item)
            if text:
                return text
        return ''
    text = str(value or '').strip()
    if text.startswith('{') or text.startswith('['):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if parsed is not None:
            return _readable_text(parsed)
    return ' '.join(text.split())


def _looks_like_tag_list(value: str) -> bool:
    parts = [part.strip() for part in str(value or '').split(',') if part.strip()]
    return len(parts) >= 2 and all(len(part.split()) <= 2 for part in parts)


def _clean_focus(value) -> str:
    text = ' '.join(str(value or '').split()).strip()
    if text.lower() in PLACEHOLDER_FOCUS_AREAS:
        return ''
    return text


def _contains_term(blob: str, term: str) -> bool:
    needle = str(term or '').strip().lower()
    haystack = str(blob or '').lower()
    if not needle or not haystack:
        return False
    if ' ' in needle:
        return needle in haystack
    return re.search(rf'(?<![a-z0-9]){re.escape(needle)}[a-z]{{0,10}}(?![a-z0-9])', haystack) is not None


def _theme_terms(text: str) -> list[str]:
    parts = []
    for chunk in re.split(r'[,;\n]+', str(text or '')):
        for token in re.findall(r"[A-Za-z][A-Za-z-]{3,}", chunk):
            lowered = token.lower()
            if lowered in STOPWORDS:
                continue
            if lowered not in {item.lower() for item in parts}:
                parts.append(token)
        phrase = ' '.join(chunk.split()).strip()
        if phrase and ' ' in phrase and phrase.lower() not in STOPWORDS:
            parts.append(phrase)
    return parts[:12]


def _matching_theme_terms(text: str, blob: str) -> list[str]:
    hits = []
    for term in _theme_terms(text):
        if _contains_term(blob, term) and term.lower() not in {item.lower() for item in hits}:
            hits.append(term)
    return hits[:6]


def _clip_sentence(value: str, limit: int) -> str:
    text = ' '.join(str(value or '').split()).strip()
    if not text:
        return ''
    if len(text) <= limit:
        return text.rstrip('.')
    clipped = text[: limit - 1].rsplit(' ', 1)[0]
    return (clipped or text[: limit - 1]).rstrip('.,;:') + '…'


def _normalize_interest_brief(raw_value) -> dict:
    parsed = _as_dict(raw_value)
    populations = [item for item in _as_list(parsed.get('populations')) if item in set(population_choice_values())]
    outcomes = [item for item in _as_list(parsed.get('outcomes')) if item in set(outcome_choice_values())]
    return {
        'themes': str(parsed.get('themes') or '').strip()[:2000],
        'populations': populations,
        'outcomes': outcomes,
        'evidence_needed': str(parsed.get('evidence_needed') or '').strip()[:2000],
    }


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _split_focus(value)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _split_focus(value) -> list[str]:
    return [part.strip() for part in re.split(r'[,;\n]+', str(value or '')) if part.strip()]


def _parse_optional_float(value):
    raw = str(value or '').strip().replace(',', '')
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_funder_requirements(serialized: dict | None) -> dict:
    data = serialized if isinstance(serialized, dict) else {}
    brief = data.get('interest_brief') if isinstance(data.get('interest_brief'), dict) else {}
    text = ' '.join([
        str(data.get('about_text') or ''),
        str(brief.get('themes') or ''),
        str(brief.get('evidence_needed') or ''),
    ])
    return {
        'min_years': _parse_min_years_required(text),
        'wants_leadership': bool(re.search(
            r'\b(strong\s+leadership|leadership\s+team|experienced\s+(?:board|leaders?|management)|named\s+leaders?)\b',
            text,
            re.I,
        )),
        'source_text': ' '.join(text.split()),
    }


def _parse_min_years_required(text: str) -> int | None:
    blob = str(text or '')
    patterns = (
        r'(?:at\s+least|minimum|min\.?|no\s+less\s+than|over|more\s+than)\s+(\d+)\s*\+?\s*years?',
        r'(\d+)\s*\+\s*years?',
        r'(\d+)\s*years?\s+(?:of\s+)?(?:operation|operating|experience|existence|track\s+record)',
    )
    for pattern in patterns:
        match = re.search(pattern, blob, re.I)
        if not match:
            continue
        try:
            years = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 1 <= years <= 80:
            return years
    return None


def _parse_founded_year(cbo: CBO, profile: dict | None) -> int | None:
    raw = str((profile or {}).get('founded_year') or getattr(cbo, 'founded_year', '') or '').strip()
    match = re.search(r'(19|20)\d{2}', raw)
    if not match:
        return None
    year = int(match.group(0))
    current = datetime.utcnow().year
    if 1900 <= year <= current:
        return year
    return None


def _cbo_years_operating(cbo: CBO, profile: dict | None) -> int | None:
    founded = _parse_founded_year(cbo, profile)
    if founded is None:
        return None
    return max(0, datetime.utcnow().year - founded)


def _cbo_named_leaders(cbo: CBO, profile: dict | None) -> list[str]:
    data = profile if isinstance(profile, dict) else {}
    leadership = data.get('leadership') if isinstance(data.get('leadership'), dict) else {}
    names = []
    for value in (
        leadership.get('chairperson') or getattr(cbo, 'chairperson', ''),
        leadership.get('program_director') or getattr(cbo, 'program_director', ''),
        leadership.get('finance_lead') or getattr(cbo, 'finance_lead', ''),
        data.get('chairperson'),
        data.get('program_director'),
    ):
        name = str(value or '').strip()
        if name and name.lower() not in {item.lower() for item in names}:
            names.append(name)
    return names


def _years_requirement_reason(cbo: CBO, profile: dict | None, requirements: dict, *, is_gap: bool) -> str:
    min_years = requirements.get('min_years')
    if not min_years:
        return ''
    founded = _parse_founded_year(cbo, profile)
    years = _cbo_years_operating(cbo, profile)
    if years is None or founded is None:
        return (
            f'You asked for at least {min_years} years of operation, but this profile does not list a founding year, so that bar cannot be verified.'
            if is_gap else ''
        )
    if years < min_years:
        return (
            f'You asked for at least {min_years} years of operation. This CBO was founded in {founded}, so it has about {years} year{"s" if years != 1 else ""} of operating history.'
            if is_gap else ''
        )
    if is_gap:
        return ''
    return f'They have been operating since {founded} ({years} years), which meets your {min_years}-year minimum.'


def _leadership_requirement_reason(cbo: CBO, profile: dict | None, requirements: dict, *, is_gap: bool) -> str:
    if not requirements.get('wants_leadership'):
        return ''
    leaders = _cbo_named_leaders(cbo, profile)
    if len(leaders) >= 2:
        return '' if is_gap else 'Named leadership is on file: ' + ', '.join(leaders[:3]) + '.'
    if is_gap:
        if leaders:
            return (
                f'You asked for a strong leadership team, but this profile only names {leaders[0]}. Chair, programme, and finance leads are not fully listed.'
            )
        return 'You asked for a strong leadership team, but this profile does not name chair, programme, or finance leads.'
    return ''
