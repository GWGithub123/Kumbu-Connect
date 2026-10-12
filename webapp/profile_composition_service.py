"""Decides how a CBO profile lays itself out from the data it actually has.

The profile template used to render a fixed set of sections whose shape was
independent of how much a CBO had uploaded. A CBO with one milestone still got
the full timeline rail, a CBO with no photos still got the photo frame, and
people named in both the leadership roster and the parsed document appeared
twice. To a funder that reads as a thin organisation rather than a thin record.

This module inspects the profile blob and returns a composition: which blocks
have enough substance to stand on their own, which variant each should render,
and a narrative payload with cross-block repetition already removed. The
template asks the composition what to draw instead of deciding for itself.
"""
from __future__ import annotations

import re

# Below this many journey steps the rail's alternating above/below layout
# reserves far more vertical space than the content fills.
RAIL_MIN_STEPS = 4

# A band of panels that between them carry almost nothing reads as filler.
MIN_PANEL_FIELDS = 1

# A band is as tall as its tallest panel, so one prose-heavy panel in a third of
# the width sets the height for the whole row and leaves its neighbours sitting
# beside dead space. Past this much text a panel takes the full width instead
# and flows its fields into columns, which costs a quarter of the height.
WIDE_PANEL_CHARS = 800

_PERSON_CONNECTORS = {
    'as', 'and', 'is', 'was', 'the', 'a', 'an', 'of', 'includes', 'include',
    'including', 'plus', 'with', 'who', 'are', 'were', 'also',
}

# Fragments the parser sometimes lifts as a person's name out of a sentence
# such as "the technical staff includes Simon Lipsky".
_NON_PERSON_MARKERS = (
    'staff', 'team', 'board', 'members', 'volunteers', 'employees',
    'leadership', 'management', 'founders', 'personnel', 'committee',
)


def build_profile_composition(
    cbo,
    profile: dict | None,
    community_feedback: dict | None = None,
    *,
    viewer_is_funder: bool = False,
    own: bool = False,
    can_manage_program_photos: bool = False,
) -> dict:
    """Return the layout plan plus a de-duplicated narrative for one profile."""
    profile = profile if isinstance(profile, dict) else {}
    feedback = community_feedback if isinstance(community_feedback, dict) else {}
    narrative = profile.get('narrative_profile')
    narrative = dict(narrative) if isinstance(narrative, dict) else {}

    people = dedupe_people(narrative.get('leadership'))
    slot_leaders = _slot_leaders(profile, cbo)

    header_facts = _header_fingerprints(profile, cbo)
    highlights = _dedupe_texts(profile.get('web_highlights') or [])
    known_texts = set(header_facts) | {_fingerprint(item) for item in highlights}

    narrative['leadership'] = people
    narrative['panels'] = _prune_panels(
        narrative.get('panels') or [],
        known_people=people,
        known_texts=known_texts,
    )
    narrative['panel_bands'] = _rebuild_bands(narrative.get('panel_bands') or [], narrative['panels'])

    journey = narrative.get('journey') if isinstance(narrative.get('journey'), dict) else {}
    past_steps = [step for step in (journey.get('past') or []) if isinstance(step, dict)]
    future_steps = [step for step in (journey.get('future') or []) if isinstance(step, dict)]
    step_total = len(past_steps) + len(future_steps)
    narrative['journey_layout'] = _journey_variant(step_total)
    # Few steps means the section has room to say everything it knows, so the
    # detail goes inline rather than behind a disclosure the reader must find.
    expand_steps = step_total <= 2
    narrative['journey'] = {
        **journey,
        'past': [_prepare_journey_step(step, expand_steps) for step in past_steps],
        'future': [_prepare_journey_step(step, expand_steps) for step in future_steps],
    }

    programs = [item for item in (narrative.get('program_overview') or []) if isinstance(item, dict)]
    photo_count = sum(len(program.get('photos') or []) for program in programs)

    impact_items = [item for item in (profile.get('quantified_impact') or []) if isinstance(item, dict)]
    metrics = [item for item in (narrative.get('metrics') or []) if isinstance(item, dict)]
    kpi_cards = [item for item in (profile.get('operational_metric_cards') or []) if isinstance(item, dict)]
    financial_cards = [item for item in (profile.get('financial_overview_cards') or []) if isinstance(item, dict)]
    has_financials = bool(financial_cards or profile.get('financial_data'))

    leadership_present = bool(people or any(slot_leaders.values()))
    impact_present = bool(impact_items)

    return {
        'leadership': {
            'present': leadership_present,
            'variant': 'roster' if people else 'slots',
            'people': people,
            'slots': slot_leaders,
            'count': len(people) or sum(1 for value in slot_leaders.values() if value),
        },
        'financials': {'present': has_financials, 'count': len(financial_cards)},
        'kpis': {'present': bool(kpi_cards), 'count': len(kpi_cards)},
        'impact': {'present': impact_present, 'count': len(impact_items)},
        'metrics': {'present': bool(metrics), 'count': len(metrics)},
        'overview_columns': _overview_columns(leadership_present, has_financials, impact_present),
        'spotlight': {'present': impact_present},
        'programs': {
            'count': len(programs),
            'photo_count': photo_count,
            # An empty photo frame advertises the gap. Owners still need the
            # upload affordance, so they keep the pane.
            'show_media': bool(photo_count or can_manage_program_photos),
        },
        'journey': {
            'variant': narrative['journey_layout'],
            'past': len(past_steps),
            'future': len(future_steps),
            'total': step_total,
        },
        'highlights': highlights,
        'narrative': narrative,
        'viewer_is_funder': bool(viewer_is_funder),
        'own': bool(own),
        'feedback_count': int(feedback.get('responses') or 0),
    }


def dedupe_people(raw_people) -> list[dict]:
    """Collapse a leadership roster down to one entry per real person.

    Names are lifted out of running prose, so the same person arrives as
    "Mary Asin" and "Mary Asin as", and a sentence naming two founders arrives
    as the single name "John Gershenson, Ben Savonen".
    """
    candidates = []
    for raw_person in raw_people or []:
        if not isinstance(raw_person, dict):
            continue
        name = _clean_person_name(raw_person.get('name'))
        if not name or not _looks_like_person(name):
            continue
        candidates.append({
            'name': name,
            'role': _clean_text(raw_person.get('role')),
            'notes': _clean_text(raw_person.get('notes')),
        })

    singles = [person for person in candidates if not _split_compound_name(person['name'])]
    single_keys = {_name_key(person['name']) for person in singles}

    people: dict[str, dict] = {}
    for person in candidates:
        parts = _split_compound_name(person['name'])
        # "John Gershenson, Ben Savonen" adds nothing once both are listed alone.
        if parts and all(_name_key(part) in single_keys for part in parts):
            continue
        key = _name_key(person['name'])
        if not key:
            continue
        existing = people.get(key)
        if existing is None:
            people[key] = person
            continue
        if len(person['role']) > len(existing['role']):
            existing['role'] = person['role']
        if len(person['notes']) > len(existing['notes']):
            existing['notes'] = person['notes']
    return list(people.values())


def _clean_person_name(value) -> str:
    name = _clean_text(value)
    if not name:
        return ''
    tokens = name.split()
    # Trailing connectors are sentence glue the parser kept: "Mary Asin as".
    while tokens and tokens[-1].lower().strip(',') in _PERSON_CONNECTORS:
        tokens.pop()
    return ' '.join(tokens).strip(' ,;:')


def _looks_like_person(name: str) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in _NON_PERSON_MARKERS):
        return False
    tokens = [token for token in re.split(r'[\s,]+', name) if token]
    if not tokens:
        return False
    # A real name is capitalised; a lifted sentence fragment usually is not.
    return any(token[:1].isupper() for token in tokens)


def _split_compound_name(name: str) -> list[str]:
    parts = [part.strip() for part in re.split(r',| and ', name) if part.strip()]
    return parts if len(parts) > 1 else []


def _name_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def _slot_leaders(profile: dict, cbo) -> dict:
    leadership = profile.get('leadership') if isinstance(profile.get('leadership'), dict) else {}
    return {
        'chairperson': _clean_text(leadership.get('chairperson') or getattr(cbo, 'chairperson', '')),
        'program_director': _clean_text(leadership.get('program_director') or getattr(cbo, 'program_director', '')),
        'finance_lead': _clean_text(leadership.get('finance_lead') or getattr(cbo, 'finance_lead', '')),
    }


def _overview_columns(leadership: bool, financials: bool, impact: bool) -> int:
    """How many columns the overview band can fill without leaving a gap."""
    return sum(1 for present in (leadership, financials, impact) if present) or 1


def _prepare_journey_step(step: dict, expand: bool) -> dict:
    summary = _clean_text(step.get('summary'))
    detail = _clean_text(step.get('detail'))
    # Summaries are generated as a clipped lead-in to the detail and arrive cut
    # mid-word ("...hospital. Followi"). Showing both repeats the same sentence
    # and puts the broken fragment on the page.
    if summary and detail and _is_truncated_prefix(summary, detail):
        summary = ''
    return {**step, 'summary': summary, 'detail': detail, 'expanded': bool(expand and detail)}


def _is_truncated_prefix(summary: str, detail: str) -> bool:
    head = _fingerprint(summary)
    full = _fingerprint(detail)
    if not head or not full:
        return False
    if full.startswith(head):
        return True
    # Drop the final word, which is the one the clip cut in half.
    stem = head.rsplit(' ', 1)[0]
    return bool(stem) and full.startswith(stem)


def _journey_variant(step_total: int) -> str:
    if not step_total:
        return 'none'
    return 'rail' if step_total >= RAIL_MIN_STEPS else 'compact'


def _prune_panels(panels: list, *, known_people: list[dict], known_texts: set[str]) -> list[dict]:
    """Drop panel content the profile already shows somewhere more prominent."""
    people_keys = {_name_key(person.get('name')) for person in known_people}
    pruned = []
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        groups = []
        for group in panel.get('groups') or []:
            if not isinstance(group, dict):
                continue
            fields = []
            for field in group.get('fields') or []:
                refined = _refine_field(field, people_keys=people_keys, known_texts=known_texts)
                if refined:
                    fields.append(refined)
            if fields:
                groups.append({**group, 'fields': fields})
        if not groups:
            continue
        field_count = sum(len(group['fields']) for group in groups)
        if field_count < MIN_PANEL_FIELDS:
            continue
        weight = sum(_field_chars(field) for group in groups for field in group['fields'])
        pruned.append({
            **panel,
            'groups': groups,
            'show_headings': len(groups) > 1,
            'field_count': field_count,
            'weight': weight,
            'wide': field_count > 6 or weight >= WIDE_PANEL_CHARS,
        })
    return pruned


def _field_chars(field) -> int:
    """Rough rendered length of a field, used to predict how tall it will run."""
    if not isinstance(field, dict):
        return 0
    total = len(str(field.get('value') or ''))
    total += sum(len(str(item)) for item in field.get('items') or [])
    for entry in field.get('entries') or []:
        if not isinstance(entry, dict):
            continue
        total += sum(
            len(str(entry.get(key) or ''))
            for key in ('title', 'subtitle', 'detail')
        )
    return total


def _refine_field(field, *, people_keys: set[str], known_texts: set[str]) -> dict | None:
    """Strip already-shown content from a field, or drop it when nothing is left."""
    if not isinstance(field, dict):
        return None
    kind = field.get('kind')

    if kind == 'people':
        entries = []
        for entry in field.get('entries') or []:
            if not isinstance(entry, dict):
                continue
            name = _clean_person_name(entry.get('title'))
            # Sentence fragments the parser mistook for names were dropped from
            # the roster, so they must not survive here either.
            if not name or not _looks_like_person(name) or _split_compound_name(name):
                continue
            if _name_key(name) in people_keys:
                continue
            entries.append({**entry, 'title': name})
        return {**field, 'entries': entries} if entries else None

    if kind in {'statement', 'quote', 'metric'}:
        value = field.get('value')
        if not value or _fingerprint(value) in known_texts:
            return None
        return field

    if kind in {'list', 'tags'}:
        items = [item for item in field.get('items') or [] if _fingerprint(item) not in known_texts]
        return {**field, 'items': items} if items else None

    if field.get('entries') or field.get('items') or field.get('value'):
        return field
    return None


def _rebuild_bands(bands: list, panels: list) -> list[dict]:
    """Re-slot the surviving panels so a band emptied by pruning disappears."""
    by_role = {panel.get('role'): panel for panel in panels if isinstance(panel, dict)}
    rebuilt = []
    for band in bands or []:
        if not isinstance(band, dict):
            continue
        members = [
            by_role[panel.get('role')]
            for panel in band.get('panels') or []
            if isinstance(panel, dict) and panel.get('role') in by_role
        ]
        if not members:
            continue
        narrow = [panel for panel in members if not panel.get('wide')]
        rebuilt.append({
            **band,
            'panels': members,
            'columns': min(max(len(narrow), 1), 3),
        })
    return rebuilt


def _header_fingerprints(profile: dict, cbo) -> set[str]:
    """Text already visible in the profile header, so panels need not repeat it."""
    values = [
        profile.get('tagline'),
        profile.get('origin_story'),
        profile.get('website'),
        profile.get('location') or getattr(cbo, 'location', ''),
        profile.get('focus_areas') or getattr(cbo, 'focus_areas', ''),
        profile.get('org_type') or getattr(cbo, 'org_type', ''),
        profile.get('founded_year') or getattr(cbo, 'founded_year', ''),
        getattr(cbo, 'name', ''),
    ]
    return {fingerprint for fingerprint in (_fingerprint(value) for value in values) if fingerprint}


def _dedupe_texts(values) -> list[str]:
    seen = set()
    unique = []
    for value in values or []:
        text = _clean_text(value)
        fingerprint = _fingerprint(text)
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(text)
    return unique


def _fingerprint(value) -> str:
    """Compare prose by its words, ignoring punctuation and casing drift."""
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def _clean_text(value) -> str:
    return ' '.join(str(value or '').split()).strip()
