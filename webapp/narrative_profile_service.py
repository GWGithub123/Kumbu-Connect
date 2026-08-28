"""Gemini parsing for narrative CBO documents (reports, profiles, field notes).

Ledger uploads go through Azure layout + Gemini normalization because the value
lives in table rows. Narrative documents carry prose instead, so running them
through the row pipeline produces one row per sentence. This module reads the
document as language and returns profile facts: known fields the CBO record
already models, plus free-form sections so an unfamiliar document shape still
lands on the profile.
"""
import json
import re

from flask import current_app
from google import genai
from google.genai import errors as genai_errors
from google.genai import types


class NarrativeProfileError(RuntimeError):
    """Raised when a narrative document cannot be parsed into profile facts."""


SYSTEM_PROMPT = """You read documents about Kenyan community-based organisations (CBOs) and turn them into structured profile data.

These documents are narrative: field notes, organisational reports, funding profiles, program write-ups. They are not accounting ledgers.

The reader is a funder deciding whether to give this organisation capital. Structure the output so that decision is easy to make.

Return only valid JSON matching the provided schema.

GROUNDING
- Ground every value in the document. Never invent facts, numbers, names, or dates.
- When the document says information was not captured or is unknown, omit that field entirely rather than writing "unknown".
- Keep each value faithful and readable. Light editing for grammar is fine; summarizing away specifics is not.
- Preserve the document's own vocabulary for section titles and field labels. Do not force content into a generic template.
- confidence is between 0 and 1 and reflects how explicitly the document states the fact.
- document_kind must be financial_ledger when the document is primarily a table of transactions, amounts, or inventory rows.

SECTIONS
- sections mirror the document's real structure, in document order, one section per meaningful heading.
- role routes the section into a fixed profile panel, so every CBO profile has the same shape no matter how the document was organised. Choosing the right role matters more than mirroring the document's wording. Use other only when nothing else fits.

ROLE GLOSSARY — route by what the facts are, not by the heading's name:
- identity: name, registration, location, website, founding date, origin story, history.
- mission: mission, aim, vision, values, theory of change.
- programs: what the organisation actually delivers — services, projects, activities, planned initiatives.
- impact: who is reached and what changed — beneficiary counts, geographic reach, coverage, outcomes, results, case studies, evidence.
- leadership: named people, staff, volunteers, board, team structure, and the character of leadership.
- governance: registration status, compliance, policies, audits, record keeping, decision-making rules.
- financials: budget, income, expenses, bank accounts, assets, liabilities, fees, funding, financial documents.
- partners: named partner organisations, funders, referral relationships, government affiliation.
- operations: how the work runs day to day — staffing levels, facilities, equipment, logistics, capacity constraints.
- community: how the community is consulted and what it says back — feedback mechanisms, testimony, community relationships.
- challenges: problems, barriers, gaps, and risks the organisation faces or addresses.
- strategy: future plans, growth intentions, transitions, and what capital would unlock.

Beneficiary numbers and areas covered are impact, not community. A budget figure is financials even when it appears under a heading about size.
- emphasis controls visual weight. Mark a section feature when it carries the strongest funder signal (usually mission, programs, or evidence of impact). Mark it compact when it is short administrative detail. Everything else is standard. Use feature for at most three sections.
- takeaway is one short funder-facing sentence saying why this section matters. Leave it empty for compact sections.

FIELDS
- Create one field for each labelled fact in the document. If a section contains "Beginning date", "Inspiration", "History", and "Major milestones", that is four fields, not one. Never merge several labelled facts into a single field.
- Keep the document's own label wording.

FIELD KINDS — choose the kind that matches the shape of the fact, and fill only the matching payload:
- statement: one prose fact. Fill value.
- list: several parallel points that are not entities. Fill items.
- tags: short keywords or categories, a few words each. Fill items.
- entries: a set of named things such as programs, projects, partners, or bank accounts. Fill entries with title plus detail, and status when the document indicates one.
- people: named individuals. Fill entries with title as the person's name, subtitle as their role, and detail as what they do.
- timeline: dated or sequenced events. Always use this for milestones, history, founding dates, and anything describing how the organisation developed over time. Fill entries with date and title, and detail when useful.
- quote: testimony, a case study, or a direct claim of impact about a specific person or group. Always use this for testimonials and case studies. Fill value.
- metric: a single stated figure. Fill value.
Never fill a payload that does not belong to the chosen kind.

METRICS
- quantified_metrics holds figures the document actually states, such as beneficiaries, staff counts, sites, visit frequencies, costs, prices, durations, ages, distances, or dates of founding. Copy the number as written and do not compute new ones.
- Be exhaustive. A funder judges scale from numbers, so sweep the whole document and capture every stated figure, including ones buried mid-sentence. Aim for at least eight metrics when the document supports them; only return fewer if the document genuinely states fewer.
- A figure already mentioned inside a section still belongs here. Duplication between a section and a metric is expected and wanted.
- numeric_value is the same figure as a plain number for charting. For a range such as "20 to 23", use the midpoint. Use 0 when the figure is not numeric.
- Mark at most four metrics as headline: the ones a funder would most want to see first.

FUNDER ASSESSMENT
- headline is one sentence positioning the organisation for a funder.
- strengths, risks, and capital_needs must each be drawn from the document, not from general knowledge about CBOs. Give up to four of each where the document supports them, and leave a list empty rather than padding it.
- A risk is something that would make a funder hesitate, such as thin financial controls, key-person dependency, or unproven reach. Read the organisation's own stated challenges and needs for these.
- readiness.dimensions must contain all six named dimensions exactly once. Score each 0 to 100 based only on evidence present in the document, and say in rationale what evidence drove the score. A dimension the document never addresses should score low and say so.
- readiness.score is your overall judgement of investment readiness from 0 to 100 and should be consistent with the dimensions.

JOURNEY
- journey tells the organisation's story as a sequence a funder can follow: where it came from, and exactly where it intends to go.
- journey.past holds what has already happened, oldest first: founding, registration, programs launched, partnerships formed, milestones reached. Status is done, or in_progress for work currently underway.
- journey.future holds what the organisation says it intends to do, nearest first. Status is planned when the document describes a concrete intention, or aspiration when it is a longer-term hope without specifics.
- when is the date or timeframe as the document gives it ("April 2023", "Within five years"). Leave it empty rather than inventing or estimating a date.
- summary is one short line that reads well on a timeline.
- detail is the substance a funder opens the step to read, and must be at least two full sentences. Say what the step actually involves, who drives it, what it depends on, and what it costs or unlocks, using the document's own specifics. Never restate the summary in different words; if the document gives no further specifics, say plainly what is not yet defined.
- Only include steps the document supports. Six to ten steps across both lists is typical; do not pad the story with generic nonprofit stages.
"""


_ENTRY_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'subtitle': {'type': 'string'},
        'detail': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['ongoing', 'planned', 'completed', 'at_risk', 'none']},
        'date': {'type': 'string'},
    },
    'required': ['title', 'subtitle', 'detail', 'status', 'date'],
    'additionalProperties': False,
}

_FIELD_SCHEMA = {
    'type': 'object',
    'properties': {
        'label': {'type': 'string'},
        'kind': {
            'type': 'string',
            'enum': ['statement', 'list', 'tags', 'entries', 'people', 'timeline', 'quote', 'metric'],
        },
        'value': {'type': 'string'},
        'items': {'type': 'array', 'items': {'type': 'string'}},
        'entries': {'type': 'array', 'items': _ENTRY_SCHEMA},
        'confidence': {'type': 'number'},
    },
    'required': ['label', 'kind', 'value', 'items', 'entries', 'confidence'],
    'additionalProperties': False,
}

READINESS_DIMENSIONS = (
    'governance',
    'financial_management',
    'program_delivery',
    'evidence_of_impact',
    'community_trust',
    'growth_readiness',
)

READINESS_DIMENSION_LABELS = {
    'governance': 'Governance',
    'financial_management': 'Financial Management',
    'program_delivery': 'Program Delivery',
    'evidence_of_impact': 'Evidence of Impact',
    'community_trust': 'Community Trust',
    'growth_readiness': 'Growth Readiness',
}

SECTION_ROLES = (
    'identity', 'mission', 'programs', 'impact', 'leadership', 'partners',
    'financials', 'governance', 'community', 'challenges', 'strategy', 'operations', 'other',
)

_JOURNEY_STEP_SCHEMA = {
    'type': 'object',
    'properties': {
        'when': {'type': 'string'},
        'title': {'type': 'string'},
        'summary': {'type': 'string'},
        'detail': {'type': 'string'},
        'status': {
            'type': 'string',
            'enum': ['done', 'in_progress', 'planned', 'aspiration'],
        },
    },
    'required': ['when', 'title', 'summary', 'detail', 'status'],
    'additionalProperties': False,
}

NARRATIVE_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'document_kind': {
            'type': 'string',
            'enum': ['narrative_report', 'financial_ledger', 'mixed', 'other'],
        },
        'document_title': {'type': 'string'},
        'organization_name': {'type': 'string'},
        'summary': {'type': 'string'},
        'headline': {'type': 'string'},
        'sections': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'role': {'type': 'string', 'enum': list(SECTION_ROLES)},
                    'emphasis': {'type': 'string', 'enum': ['feature', 'standard', 'compact']},
                    'takeaway': {'type': 'string'},
                    'fields': {'type': 'array', 'items': _FIELD_SCHEMA},
                },
                'required': ['title', 'role', 'emphasis', 'takeaway', 'fields'],
                'additionalProperties': False,
            },
        },
        'funder_assessment': {
            'type': 'object',
            'properties': {
                'strengths': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {'title': {'type': 'string'}, 'detail': {'type': 'string'}},
                        'required': ['title', 'detail'],
                        'additionalProperties': False,
                    },
                },
                'risks': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'title': {'type': 'string'},
                            'detail': {'type': 'string'},
                            'severity': {'type': 'string', 'enum': ['low', 'medium', 'high']},
                        },
                        'required': ['title', 'detail', 'severity'],
                        'additionalProperties': False,
                    },
                },
                'capital_needs': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {'title': {'type': 'string'}, 'detail': {'type': 'string'}},
                        'required': ['title', 'detail'],
                        'additionalProperties': False,
                    },
                },
                'readiness': {
                    'type': 'object',
                    'properties': {
                        'score': {'type': 'number'},
                        'rationale': {'type': 'string'},
                        'dimensions': {
                            'type': 'array',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'name': {'type': 'string', 'enum': list(READINESS_DIMENSIONS)},
                                    'score': {'type': 'number'},
                                    'rationale': {'type': 'string'},
                                },
                                'required': ['name', 'score', 'rationale'],
                                'additionalProperties': False,
                            },
                        },
                    },
                    'required': ['score', 'rationale', 'dimensions'],
                    'additionalProperties': False,
                },
            },
            'required': ['strengths', 'risks', 'capital_needs', 'readiness'],
            'additionalProperties': False,
        },
        'profile_updates': {
            'type': 'object',
            'properties': {
                'location': {'type': 'string'},
                'founded_year': {'type': 'string'},
                'org_type': {'type': 'string'},
                'focus_areas': {'type': 'string'},
                'mission': {'type': 'string'},
                'vision': {'type': 'string'},
                'leadership': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'role': {'type': 'string'},
                            'notes': {'type': 'string'},
                        },
                        'required': ['name', 'role', 'notes'],
                        'additionalProperties': False,
                    },
                },
                'programs': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'description': {'type': 'string'},
                            'beneficiaries': {'type': 'string'},
                            'status': {'type': 'string', 'enum': ['ongoing', 'planned', 'completed', 'unknown']},
                        },
                        'required': ['name', 'description', 'beneficiaries', 'status'],
                        'additionalProperties': False,
                    },
                },
                'partners': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'relationship': {'type': 'string'},
                        },
                        'required': ['name', 'relationship'],
                        'additionalProperties': False,
                    },
                },
                'milestones': {'type': 'array', 'items': {'type': 'string'}},
                'needs': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': [
                'location', 'founded_year', 'org_type', 'focus_areas', 'mission', 'vision',
                'leadership', 'programs', 'partners', 'milestones', 'needs',
            ],
            'additionalProperties': False,
        },
        'quantified_metrics': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'label': {'type': 'string'},
                    'value': {'type': 'string'},
                    'unit': {'type': 'string'},
                    'context': {'type': 'string'},
                    'numeric_value': {'type': 'number'},
                    'category': {
                        'type': 'string',
                        'enum': ['reach', 'capacity', 'finance', 'governance', 'time', 'other'],
                    },
                    'emphasis': {'type': 'string', 'enum': ['headline', 'standard']},
                },
                'required': ['label', 'value', 'unit', 'context', 'numeric_value', 'category', 'emphasis'],
                'additionalProperties': False,
            },
        },
        'journey': {
            'type': 'object',
            'properties': {
                'past': {'type': 'array', 'items': _JOURNEY_STEP_SCHEMA},
                'future': {'type': 'array', 'items': _JOURNEY_STEP_SCHEMA},
            },
            'required': ['past', 'future'],
            'additionalProperties': False,
        },
    },
    'required': [
        'document_kind', 'document_title', 'organization_name', 'summary', 'headline',
        'sections', 'funder_assessment', 'profile_updates', 'quantified_metrics', 'journey',
    ],
    'additionalProperties': False,
}

# Prose documents carry far more alphabetic text per page than scanned ledgers,
# whose text layer is usually absent or a thin scatter of numbers.
_MIN_TEXT_CHARS = 600
_MIN_ALPHA_RATIO = 0.55
_MAX_PROMPT_CHARS = 120000

_QUOTE_LABEL_MARKERS = ('testimonial', 'case study', 'case studies', 'quote', 'in their words')

_UNKNOWN_MARKERS = (
    'not captured',
    'not captured in field notes',
    'not yet captured',
    'unknown',
    'n/a',
    'none captured',
    'not provided',
    'not specified',
    'not available',
)


def narrative_parsing_configured() -> bool:
    return bool(_gemini_api_key())


def document_text_from_pages(text_pages: list[str]) -> str:
    return '\n\n'.join(str(page or '').strip() for page in (text_pages or []) if str(page or '').strip()).strip()


def looks_like_narrative_document(document_text: str) -> bool:
    """Cheap gate so scanned ledgers never reach the narrative parser."""
    text = str(document_text or '').strip()
    if len(text) < _MIN_TEXT_CHARS:
        return False

    meaningful = re.sub(r'\s', '', text)
    if not meaningful:
        return False

    alpha_ratio = sum(1 for char in meaningful if char.isalpha()) / len(meaningful)
    if alpha_ratio < _MIN_ALPHA_RATIO:
        return False

    # Prose runs in sentences; a ledger text layer is mostly short fragments.
    words = text.split()
    return len(words) >= 120


def extract_narrative_profile(document_text: str, filename: str, cbo) -> dict:
    api_key = _gemini_api_key()
    if not api_key:
        raise NarrativeProfileError('Gemini_API_Key is not configured.')

    text = str(document_text or '').strip()
    if not text:
        raise NarrativeProfileError('No readable text was found in this document.')

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=max(1000, _request_timeout() * 1000)),
    )

    prompt = (
        f'Parse this document about {cbo.name} into structured profile data. '
        f'Filename: {filename}. '
        'Mirror the document\'s own headings in sections, and only fill profile_updates keys the document supports.\n\n'
        'DOCUMENT:\n'
        f'{text[:_MAX_PROMPT_CHARS]}'
    )

    try:
        response = client.models.generate_content(
            model=_narrative_model(),
            contents=[types.Part.from_text(text=prompt)],
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=_max_output_tokens(),
                system_instruction=SYSTEM_PROMPT,
                response_mime_type='application/json',
                response_json_schema=NARRATIVE_OUTPUT_SCHEMA,
            ),
        )
    except genai_errors.ClientError as exc:
        if int(getattr(exc, 'code', 0) or 0) == 429:
            raise NarrativeProfileError('Gemini quota was exceeded while reading this report. Check billing or try again later.') from exc
        raise NarrativeProfileError(f'Gemini rejected this report: {_error_message(exc)}') from exc
    except genai_errors.ServerError as exc:
        raise NarrativeProfileError('Could not reach Gemini while reading this report. Try again in a moment.') from exc
    except genai_errors.APIError as exc:
        raise NarrativeProfileError(f'Gemini returned an error while reading this report: {getattr(exc, "code", "?")}.') from exc

    payload = _parse_response_json(response)
    return normalize_narrative_payload(payload, filename=filename)


def normalize_narrative_payload(payload: dict, filename: str = '') -> dict:
    payload = payload if isinstance(payload, dict) else {}
    updates = payload.get('profile_updates') if isinstance(payload.get('profile_updates'), dict) else {}

    normalized = {
        'document_kind': _clean_choice(
            payload.get('document_kind'),
            {'narrative_report', 'financial_ledger', 'mixed', 'other'},
            'narrative_report',
        ),
        'document_title': _clean_text(payload.get('document_title')),
        'organization_name': _clean_text(payload.get('organization_name')),
        'summary': _clean_text(payload.get('summary')),
        'headline': _clean_text(payload.get('headline')),
        'source_filename': filename,
        'sections': _normalize_sections(payload.get('sections')),
        'funder_assessment': _normalize_funder_assessment(payload.get('funder_assessment')),
        'profile_updates': {
            'location': _clean_text(updates.get('location')),
            'founded_year': _clean_founded_year(updates.get('founded_year')),
            'org_type': _clean_text(updates.get('org_type')),
            'focus_areas': _clean_text(updates.get('focus_areas')),
            'mission': _clean_text(updates.get('mission')),
            'vision': _clean_text(updates.get('vision')),
            'leadership': _normalize_leadership(updates.get('leadership')),
            'programs': _normalize_programs(updates.get('programs')),
            'partners': _normalize_partners(updates.get('partners')),
            'milestones': _clean_string_list(updates.get('milestones')),
            'needs': _clean_string_list(updates.get('needs')),
        },
        'quantified_metrics': _normalize_metrics(payload.get('quantified_metrics')),
        'journey': _normalize_journey(payload.get('journey')),
    }
    return normalized


def _normalize_journey(raw_journey) -> dict:
    if not isinstance(raw_journey, dict):
        return {'past': [], 'future': []}

    return {
        'past': _normalize_journey_steps(raw_journey.get('past'), {'done', 'in_progress'}, 'done'),
        'future': _normalize_journey_steps(raw_journey.get('future'), {'planned', 'aspiration'}, 'planned'),
    }


def _normalize_journey_steps(raw_steps, allowed_statuses: set, default_status: str) -> list[dict]:
    steps = []
    for raw_step in raw_steps or []:
        if not isinstance(raw_step, dict):
            continue
        title = _clean_text(raw_step.get('title'))
        if not title:
            continue
        when = _clean_text(raw_step.get('when'))
        # A step listed under past cannot be planned, whatever the model said.
        status = _clean_choice(raw_step.get('status'), allowed_statuses, default_status)
        # Work the document describes as continuing is underway, not finished.
        if status == 'done' and when.lower() in {'ongoing', 'current', 'present', 'continuing'}:
            status = 'in_progress'

        steps.append({
            'when': when,
            'title': title,
            'summary': _clean_text(raw_step.get('summary')),
            'detail': _clean_text(raw_step.get('detail')),
            'status': status,
        })
    return steps


def narrative_payload_has_content(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    updates = payload.get('profile_updates') or {}
    assessment = payload.get('funder_assessment') or {}
    return bool(
        payload.get('sections')
        or payload.get('summary')
        or payload.get('quantified_metrics')
        or assessment.get('strengths')
        or assessment.get('risks')
        or any(updates.get(key) for key in updates)
    )


# Every document invents its own headings, which is why rendering one card per
# heading produces a different, sprawling layout for each upload. Sections are
# folded into this fixed set of panels instead, so every CBO profile has the
# same shape and a funder can compare two of them side by side.
PROFILE_PANELS = (
    ('identity', 'At a Glance', 'who'),
    ('mission', 'Mission & Vision', 'who'),
    ('leadership', 'Leadership & Team', 'who'),
    ('programs', 'Programs & Services', 'work'),
    ('operations', 'Operations', 'work'),
    ('partners', 'Partnerships', 'work'),
    ('impact', 'Impact & Reach', 'results'),
    ('community', 'Community Voice', 'results'),
    ('financials', 'Finances', 'position'),
    ('governance', 'Governance & Compliance', 'position'),
    ('challenges', 'Challenges', 'position'),
    ('strategy', 'Strategy & Outlook', 'position'),
    ('other', 'Additional Detail', 'position'),
)

# Eleven similar cards in one stack give a reader nowhere to rest. Bands break
# the detail into the four questions a funder works through in order.
PANEL_BANDS = (
    ('who', 'Who They Are', 'fa-fingerprint'),
    ('work', 'What They Do', 'fa-hand-holding-heart'),
    ('results', 'What It Produces', 'fa-seedling'),
    ('position', 'Position & Outlook', 'fa-compass-drafting'),
)


def build_profile_panels(sections: list) -> list[dict]:
    """Group document sections into the fixed panel set, preserving document order.

    A panel keeps each source heading as a labelled group so that merging two
    sections does not blur which part of the report a fact came from.
    """
    grouped: dict[str, list[dict]] = {}
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        fields = [field for field in section.get('fields') or [] if _field_has_content(field)]
        if not fields and not section.get('takeaway'):
            continue

        known_roles = {role for role, _title, _band in PROFILE_PANELS}
        role = section.get('role') if section.get('role') in known_roles else 'other'
        grouped.setdefault(role, []).append({
            'heading': section.get('title') or '',
            'takeaway': section.get('takeaway') or '',
            'fields': fields,
        })

    panels = []
    for role, title, band in PROFILE_PANELS:
        groups = grouped.get(role)
        if not groups:
            continue
        field_count = sum(len(group['fields']) for group in groups)
        panels.append({
            'role': role,
            'title': title,
            'band': band,
            'groups': groups,
            # A single group's heading just restates the panel title.
            'show_headings': len(groups) > 1,
            'field_count': field_count,
            # A panel holding most of a band's substance gets the full width.
            'wide': field_count > 6,
        })
    return panels


def build_panel_bands(panels: list) -> list[dict]:
    """Split the panels into the reading order a funder works through."""
    bands = []
    for key, title, icon in PANEL_BANDS:
        members = [panel for panel in panels or [] if panel.get('band') == key]
        if not members:
            continue
        narrow = [panel for panel in members if not panel.get('wide')]
        bands.append({
            'key': key,
            'title': title,
            'icon': icon,
            'panels': members,
            # Track count follows the panels present, so a band of two does not
            # sit against an empty third column.
            'columns': min(max(len(narrow), 1), 3),
        })
    return bands


def _field_has_content(field) -> bool:
    return isinstance(field, dict) and bool(field.get('value') or field.get('items') or field.get('entries'))


def hydrate_stored_narrative(narrative: dict, profile: dict | None = None) -> dict:
    """Re-run normalization over a profile blob loaded from the database.

    Profiles parsed before a schema change are missing the semantic tags the
    renderer keys off, which would otherwise draw a label with nothing under it.
    Re-deriving those tags from the payload lets stored profiles render without
    a re-parse.
    """
    if not isinstance(narrative, dict):
        narrative = {}

    hydrated = dict(narrative)
    hydrated['sections'] = _normalize_sections(narrative.get('sections'))
    hydrated['metrics'] = _normalize_metrics(narrative.get('metrics'))
    hydrated['programs'] = _normalize_programs(narrative.get('programs'))
    hydrated['program_media'] = _normalize_program_media(narrative.get('program_media'))
    hydrated['panels'] = build_profile_panels(hydrated['sections'])

    assessment = narrative.get('assessment')
    hydrated['assessment'] = (
        _normalize_funder_assessment(assessment)
        if isinstance(assessment, dict) and assessment
        else {}
    )
    hydrated['program_overview'] = build_program_overview(hydrated, profile)
    # Program facts now have a dedicated home on the profile. Drop the raw
    # programs panel so Organisation Detail does not repeat the same cards.
    if hydrated['program_overview']:
        hydrated['panels'] = [
            panel for panel in hydrated['panels'] if panel.get('role') != 'programs'
        ]
    hydrated['panel_bands'] = build_panel_bands(hydrated['panels'])
    return hydrated


def _normalize_sections(raw_sections) -> list[dict]:
    sections = []
    for raw_section in raw_sections or []:
        if not isinstance(raw_section, dict):
            continue
        title = _clean_text(raw_section.get('title'))
        fields = [
            field for field in
            (_normalize_field(raw_field) for raw_field in raw_section.get('fields') or [])
            if field
        ]
        if not title or not fields:
            continue
        sections.append({
            'title': title,
            'role': _clean_choice(raw_section.get('role'), set(SECTION_ROLES), 'other'),
            'emphasis': _clean_choice(raw_section.get('emphasis'), {'feature', 'standard', 'compact'}, 'standard'),
            'takeaway': _clean_text(raw_section.get('takeaway')),
            'fields': fields,
        })
    return sections


def _normalize_field(raw_field) -> dict | None:
    """Keep only the payload that matches the declared kind, and re-derive the kind when it does not fit."""
    if not isinstance(raw_field, dict):
        return None

    label = _clean_text(raw_field.get('label'))
    if not label:
        return None

    value = _clean_text(raw_field.get('value'))
    if _is_unknown_value(value):
        value = ''
    items = _clean_string_list(raw_field.get('items'))
    entries = _normalize_entries(raw_field.get('entries'))

    kind = _clean_choice(
        raw_field.get('kind'),
        {'statement', 'list', 'tags', 'entries', 'people', 'timeline', 'quote', 'metric'},
        '',
    )

    # The model occasionally labels a field one way and fills a different payload.
    # Trust the payload, since that is what actually renders.
    if kind in {'entries', 'people', 'timeline'} and not entries:
        kind = 'list' if items else 'statement'
    if kind in {'list', 'tags'} and not items:
        kind = 'entries' if entries else 'statement'
    if kind in {'statement', 'quote', 'metric'} and not value:
        if entries:
            kind = 'entries'
        elif items:
            kind = 'list'
    if not kind:
        if entries:
            kind = 'entries'
        elif items:
            kind = 'list'
        else:
            kind = 'statement'

    # Testimony reads far better as a pull quote, and the model tends to default
    # these to plain prose even when the label is explicit about what they are.
    if kind == 'statement' and any(marker in label.lower() for marker in _QUOTE_LABEL_MARKERS):
        kind = 'quote'

    if kind in {'statement', 'quote', 'metric'}:
        items, entries = [], []
    elif kind in {'list', 'tags'}:
        value, entries = '', []
    else:
        value, items = '', []

    if not value and not items and not entries:
        return None

    return {
        'label': label,
        'kind': kind,
        'value': value,
        'items': items,
        'entries': entries,
        'confidence': _clamp_confidence(raw_field.get('confidence')),
    }


def _normalize_entries(raw_entries) -> list[dict]:
    entries = []
    for raw_entry in raw_entries or []:
        if not isinstance(raw_entry, dict):
            continue
        title = _clean_text(raw_entry.get('title'))
        detail = _clean_text(raw_entry.get('detail'))
        if _is_unknown_value(detail):
            detail = ''
        if not title and not detail:
            continue
        status = _clean_choice(
            raw_entry.get('status'),
            {'ongoing', 'planned', 'completed', 'at_risk', 'none'},
            'none',
        )
        entries.append({
            'title': title,
            'subtitle': _clean_text(raw_entry.get('subtitle')),
            'detail': detail,
            'status': '' if status == 'none' else status,
            'date': _clean_text(raw_entry.get('date')),
        })
    return entries


def _normalize_funder_assessment(raw_assessment) -> dict:
    assessment = raw_assessment if isinstance(raw_assessment, dict) else {}
    raw_readiness = assessment.get('readiness') if isinstance(assessment.get('readiness'), dict) else {}

    scored_by_name = {}
    for raw_dimension in raw_readiness.get('dimensions') or []:
        if not isinstance(raw_dimension, dict):
            continue
        name = _clean_choice(raw_dimension.get('name'), set(READINESS_DIMENSIONS), '')
        if name and name not in scored_by_name:
            scored_by_name[name] = {
                'name': name,
                'label': READINESS_DIMENSION_LABELS[name],
                'score': _clamp_score(raw_dimension.get('score')),
                'rationale': _clean_text(raw_dimension.get('rationale')),
            }

    # The radar chart needs every axis present, so fill any the model skipped.
    dimensions = [
        scored_by_name.get(name) or {
            'name': name,
            'label': READINESS_DIMENSION_LABELS[name],
            'score': 0,
            'rationale': 'Not addressed in this document.',
        }
        for name in READINESS_DIMENSIONS
    ]

    score = _clamp_score(raw_readiness.get('score'))
    if not score and dimensions:
        score = round(sum(dimension['score'] for dimension in dimensions) / len(dimensions))

    return {
        'strengths': _normalize_assessment_points(assessment.get('strengths')),
        'risks': _normalize_assessment_points(assessment.get('risks'), with_severity=True),
        'capital_needs': _normalize_assessment_points(assessment.get('capital_needs')),
        'readiness': {
            'score': score,
            'rationale': _clean_text(raw_readiness.get('rationale')),
            'dimensions': dimensions,
        },
    }


def _normalize_assessment_points(raw_points, with_severity: bool = False) -> list[dict]:
    points = []
    for raw_point in raw_points or []:
        if not isinstance(raw_point, dict):
            continue
        title = _clean_text(raw_point.get('title'))
        detail = _clean_text(raw_point.get('detail'))
        if not title and not detail:
            continue
        point = {'title': title or detail, 'detail': detail if title else ''}
        if with_severity:
            point['severity'] = _clean_choice(raw_point.get('severity'), {'low', 'medium', 'high'}, 'medium')
        points.append(point)
    return points


def _clamp_score(value) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0
    return int(round(min(max(score, 0.0), 100.0)))


def _normalize_leadership(raw_leadership) -> list[dict]:
    leaders = []
    for raw_leader in raw_leadership or []:
        if not isinstance(raw_leader, dict):
            continue
        name = _clean_text(raw_leader.get('name'))
        role = _clean_text(raw_leader.get('role'))
        if not name and not role:
            continue
        leaders.append({
            'name': name,
            'role': role,
            'notes': _clean_text(raw_leader.get('notes')),
        })
    return leaders


def program_key(name: str) -> str:
    """Stable slug so photos stay attached when a program is re-extracted."""
    slug = re.sub(r'[^a-z0-9]+', '-', str(name or '').strip().lower()).strip('-')
    return slug[:80] or 'program'


def _normalize_programs(raw_programs) -> list[dict]:
    programs = []
    seen = set()
    for raw_program in raw_programs or []:
        if not isinstance(raw_program, dict):
            continue
        name = _clean_text(raw_program.get('name'))
        if not name:
            continue
        key = program_key(raw_program.get('key') or name)
        if key in seen:
            continue
        seen.add(key)
        programs.append({
            'key': key,
            'name': name,
            'description': _clean_text(raw_program.get('description')),
            'beneficiaries': _clean_text(raw_program.get('beneficiaries')),
            'status': _clean_choice(
                raw_program.get('status'),
                {'ongoing', 'planned', 'completed', 'unknown'},
                'unknown',
            ),
            'source': _clean_choice(
                raw_program.get('source'),
                {'extracted', 'manual', 'flagship', 'entry', 'photo'},
                'extracted',
            ),
        })
    return programs


def merge_program_lists(existing, incoming) -> list[dict]:
    """Keep manual programs when a later document refresh only names a subset."""
    existing_programs = _normalize_programs(existing)
    incoming_programs = _normalize_programs(incoming)
    if not incoming_programs:
        return existing_programs

    previous_by_key = {program['key']: program for program in existing_programs}
    merged = []
    seen = set()
    for program in incoming_programs:
        previous = previous_by_key.get(program['key'], {})
        merged.append({
            **program,
            'source': program.get('source') or previous.get('source') or 'extracted',
        })
        seen.add(program['key'])
    for program in existing_programs:
        if program['key'] not in seen:
            merged.append(program)
    return merged


def _normalize_program_media(raw_media) -> dict[str, list[dict]]:
    if not isinstance(raw_media, dict):
        return {}

    media = {}
    for raw_key, raw_photos in raw_media.items():
        key = program_key(str(raw_key or ''))
        photos = []
        for raw_photo in raw_photos or []:
            if not isinstance(raw_photo, dict):
                continue
            photo_id = str(raw_photo.get('id') or '').strip()
            stored_path = str(raw_photo.get('stored_path') or '').strip()
            if not photo_id or not stored_path:
                continue
            photos.append({
                'id': photo_id,
                'filename': _clean_text(raw_photo.get('filename')) or 'photo',
                'mime_type': _clean_text(raw_photo.get('mime_type')) or 'image/jpeg',
                'storage_backend': _clean_text(raw_photo.get('storage_backend')) or 'local',
                'stored_path': stored_path,
                'storage_bucket': str(raw_photo.get('storage_bucket') or ''),
                'storage_object_path': str(raw_photo.get('storage_object_path') or ''),
                'created_at': str(raw_photo.get('created_at') or ''),
            })
        if photos:
            media[key] = photos
    return media


def find_program_photo(profile: dict, photo_id: str) -> tuple[str, dict] | None:
    """Locate a stored program photo by id. Returns (program_key, photo)."""
    wanted = str(photo_id or '').strip()
    if not wanted or not isinstance(profile, dict):
        return None

    narrative = profile.get('narrative_profile') if isinstance(profile.get('narrative_profile'), dict) else {}
    media = _normalize_program_media(narrative.get('program_media'))
    for key, photos in media.items():
        for photo in photos:
            if photo.get('id') == wanted:
                return key, photo
    return None


def build_program_overview(narrative: dict, profile: dict | None = None) -> list[dict]:
    """Fold structured programs, document entries, and photos into one catalog.

    The standard slots (name, status, who it serves) stay comparable across
    CBOs. Extra extracted fields remain fluid slides so an organic write-up
    still lands in the same viewer.
    """
    narrative = narrative if isinstance(narrative, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    media = _normalize_program_media(narrative.get('program_media'))
    catalog: dict[str, dict] = {}

    for program in _normalize_programs(narrative.get('programs')):
        catalog[program['key']] = _blank_overview_program(program)

    for entry in _program_entries(narrative.get('sections')):
        title = entry.get('title') or ''
        key = program_key(title)
        if not key or key == 'program' and not title:
            continue
        existing = catalog.get(key)
        if existing is None:
            catalog[key] = _blank_overview_program({
                'key': key,
                'name': title,
                'description': entry.get('detail') or '',
                'beneficiaries': entry.get('subtitle') or '',
                'status': entry.get('status') or 'unknown',
                'source': 'entry',
            })
        else:
            if not existing['description'] and entry.get('detail'):
                existing['description'] = entry['detail']
            if not existing['beneficiaries'] and entry.get('subtitle'):
                existing['beneficiaries'] = entry['subtitle']
            if existing.get('status') in {'', 'unknown'} and entry.get('status'):
                existing['status'] = entry['status']

    flagship = profile.get('flagship_project') if isinstance(profile.get('flagship_project'), dict) else {}
    flagship_title = _clean_text(flagship.get('title'))
    flagship_summary = _clean_text(flagship.get('summary'))
    if flagship_title or flagship_summary:
        key = program_key(flagship_title or 'Flagship Project')
        existing = catalog.get(key)
        if existing is None:
            catalog[key] = _blank_overview_program({
                'key': key,
                'name': flagship_title or 'Flagship Project',
                'description': flagship_summary,
                'beneficiaries': '',
                'status': 'ongoing',
                'source': 'flagship',
            })
        elif not existing['description'] and flagship_summary:
            existing['description'] = flagship_summary
        if catalog[key].get('source') == 'extracted' and not catalog[key].get('description'):
            catalog[key]['description'] = flagship_summary
        for stat in flagship.get('stats') or []:
            if not isinstance(stat, dict):
                continue
            label = _clean_text(stat.get('label'))
            value = _clean_text(stat.get('value'))
            if label and value:
                catalog[key]['stats'].append({'label': label, 'value': value})

    for key, photos in media.items():
        if key not in catalog:
            catalog[key] = _blank_overview_program({
                'key': key,
                'name': key.replace('-', ' ').title(),
                'description': '',
                'beneficiaries': '',
                'status': 'unknown',
                'source': 'photo',
            })
        catalog[key]['photos'] = photos

    for program in catalog.values():
        if program['description']:
            _append_unique_field(program['details'], {
                'label': 'Overview',
                'kind': 'statement',
                'value': program['description'],
            })
        if program['beneficiaries']:
            _append_unique_field(program['details'], {
                'label': 'Who it serves',
                'kind': 'statement',
                'value': program['beneficiaries'],
            })
        for stat in program.get('stats') or []:
            _append_unique_field(program['details'], {
                'label': stat['label'],
                'kind': 'metric',
                'value': stat['value'],
            })

    leftover_fields = []
    assigned_field_ids = set()
    for section in narrative.get('sections') or []:
        if not isinstance(section, dict):
            continue
        for field in section.get('fields') or []:
            if not _field_has_content(field):
                continue
            matches = [
                program for program in catalog.values()
                if _field_mentions_program(field, program['name'])
            ]
            if len(matches) == 1:
                _append_unique_field(matches[0]['details'], field)
                assigned_field_ids.add(id(field))
            elif section.get('role') == 'programs' and field.get('kind') != 'entries':
                leftover_fields.append(field)

    for field in leftover_fields:
        if id(field) in assigned_field_ids:
            continue
        if len(catalog) == 1:
            _append_unique_field(next(iter(catalog.values()))['details'], field)
        else:
            for program in catalog.values():
                _append_unique_field(program['shared_details'], field)

    for metric in narrative.get('metrics') or []:
        if not isinstance(metric, dict):
            continue
        haystack = f"{metric.get('label') or ''} {metric.get('context') or ''}"
        matches = [program for program in catalog.values() if _text_mentions(haystack, program['name'])]
        if len(matches) != 1:
            continue
        value = _clean_text(metric.get('value'))
        unit = _clean_text(metric.get('unit'))
        _append_unique_field(matches[0]['details'], {
            'label': metric.get('label') or 'Metric',
            'kind': 'metric',
            'value': f'{value} {unit}'.strip() if unit else value,
        })

    journey = narrative.get('journey') if isinstance(narrative.get('journey'), dict) else {}
    for step in (journey.get('past') or []) + (journey.get('future') or []):
        if not isinstance(step, dict):
            continue
        haystack = f"{step.get('title') or ''} {step.get('summary') or ''} {step.get('detail') or ''}"
        matches = [program for program in catalog.values() if _text_mentions(haystack, program['name'])]
        if len(matches) != 1:
            continue
        detail = _clean_text(step.get('detail') or step.get('summary'))
        if not detail:
            continue
        _append_unique_field(matches[0]['details'], {
            'label': step.get('title') or 'Milestone',
            'kind': 'statement',
            'value': detail,
        })

    overview = []
    for program in catalog.values():
        details = list(program['details']) + list(program['shared_details'])
        if not details and not program['photos']:
            details.append({
                'label': 'Overview',
                'kind': 'statement',
                'value': (
                    'More detail will land here as reports, notes, and photos '
                    'are uploaded for this program.'
                ),
            })
        overview.append({
            'key': program['key'],
            'name': program['name'],
            'status': program['status'] or 'unknown',
            'beneficiaries': program['beneficiaries'],
            'description': program['description'],
            'source': program['source'],
            'photos': program['photos'],
            'details': details,
        })
    return overview


def _blank_overview_program(program: dict) -> dict:
    return {
        'key': program.get('key') or program_key(program.get('name') or ''),
        'name': program.get('name') or '',
        'description': program.get('description') or '',
        'beneficiaries': program.get('beneficiaries') or '',
        'status': program.get('status') or 'unknown',
        'source': program.get('source') or 'extracted',
        'photos': [],
        'stats': [],
        'details': [],
        'shared_details': [],
    }


def _program_entries(sections) -> list[dict]:
    entries = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        if section.get('role') not in {'programs', 'impact'}:
            continue
        for field in section.get('fields') or []:
            if not isinstance(field, dict) or field.get('kind') != 'entries':
                continue
            for entry in field.get('entries') or []:
                if isinstance(entry, dict) and (entry.get('title') or entry.get('detail')):
                    entries.append(entry)
    return entries


def _field_mentions_program(field: dict, name: str) -> bool:
    return _text_mentions(_field_text(field), name)


def _field_text(field: dict) -> str:
    parts = [field.get('label') or '', field.get('value') or '']
    parts.extend(str(item) for item in (field.get('items') or []))
    for entry in field.get('entries') or []:
        if not isinstance(entry, dict):
            continue
        parts.extend([
            entry.get('title') or '',
            entry.get('subtitle') or '',
            entry.get('detail') or '',
        ])
    return ' '.join(parts)


def _text_mentions(haystack: str, name: str) -> bool:
    needle = str(name or '').strip().lower()
    if len(needle) < 4:
        return False
    return needle in str(haystack or '').lower()


def _append_unique_field(bucket: list[dict], field: dict) -> None:
    if not isinstance(field, dict) or not _field_has_content(field):
        return
    signature = (
        field.get('kind'),
        field.get('label'),
        field.get('value'),
        tuple(field.get('items') or []),
        tuple(
            (entry.get('title'), entry.get('detail'))
            for entry in (field.get('entries') or [])
            if isinstance(entry, dict)
        ),
    )
    existing = {
        (
            item.get('kind'),
            item.get('label'),
            item.get('value'),
            tuple(item.get('items') or []),
            tuple(
                (entry.get('title'), entry.get('detail'))
                for entry in (item.get('entries') or [])
                if isinstance(entry, dict)
            ),
        )
        for item in bucket
    }
    if signature not in existing:
        bucket.append(field)


def _normalize_partners(raw_partners) -> list[dict]:
    partners = []
    for raw_partner in raw_partners or []:
        if not isinstance(raw_partner, dict):
            continue
        name = _clean_text(raw_partner.get('name'))
        if not name:
            continue
        partners.append({
            'name': name,
            'relationship': _clean_text(raw_partner.get('relationship')),
        })
    return partners


def _normalize_metrics(raw_metrics) -> list[dict]:
    metrics = []
    for raw_metric in raw_metrics or []:
        if not isinstance(raw_metric, dict):
            continue
        label = _clean_text(raw_metric.get('label'))
        value = _clean_text(raw_metric.get('value'))
        if not label or not value or _is_unknown_value(value):
            continue
        try:
            numeric_value = float(raw_metric.get('numeric_value') or 0.0)
        except (TypeError, ValueError):
            numeric_value = 0.0
        metrics.append({
            'label': label,
            'value': value,
            'unit': _clean_text(raw_metric.get('unit')),
            'context': _clean_text(raw_metric.get('context')),
            'numeric_value': numeric_value,
            'category': _clean_choice(
                raw_metric.get('category'),
                {'reach', 'capacity', 'finance', 'governance', 'time', 'other'},
                'other',
            ),
            'emphasis': _clean_choice(raw_metric.get('emphasis'), {'headline', 'standard'}, 'standard'),
        })

    # Guarantee a headline row even when the model marked nothing.
    if metrics and not any(metric['emphasis'] == 'headline' for metric in metrics):
        for metric in metrics[:4]:
            metric['emphasis'] = 'headline'
    return metrics


def _clean_text(value) -> str:
    return ' '.join(str(value or '').split()).strip()


def _clean_string_list(values) -> list[str]:
    cleaned = []
    for value in values or []:
        text = _clean_text(value)
        if text and not _is_unknown_value(text) and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_choice(value, allowed: set[str], default: str) -> str:
    normalized = _clean_text(value).lower()
    return normalized if normalized in allowed else default


def _clean_founded_year(value) -> str:
    match = re.search(r'(19|20)\d{2}', str(value or ''))
    return match.group(0) if match else ''


def _is_unknown_value(value) -> bool:
    normalized = _clean_text(value).lower().rstrip('.')
    if not normalized:
        return True
    return any(normalized == marker or normalized.startswith(marker) for marker in _UNKNOWN_MARKERS)


def _clamp_confidence(value) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(max(confidence, 0.0), 1.0), 4)


def _parse_response_json(response) -> dict:
    text = _response_text(response)
    if not text:
        raise NarrativeProfileError('Gemini returned an empty response for this report.')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise NarrativeProfileError('Gemini returned invalid JSON for this report.') from exc
        raise NarrativeProfileError('Gemini returned invalid JSON for this report.')


def _response_text(response) -> str:
    try:
        text = getattr(response, 'text', None)
        if text:
            return str(text).strip()
    except Exception:
        pass

    chunks = []
    for candidate in getattr(response, 'candidates', None) or []:
        content = getattr(candidate, 'content', None)
        for part in getattr(content, 'parts', None) or []:
            part_text = getattr(part, 'text', None)
            if part_text:
                chunks.append(str(part_text))
    return ''.join(chunks).strip()


def _error_message(exc: Exception) -> str:
    message = str(getattr(exc, 'message', '') or '').strip()
    return message or str(exc).strip()


def _gemini_api_key() -> str:
    return str(current_app.config.get('GEMINI_API_KEY') or '').strip()


def _narrative_model() -> str:
    return str(current_app.config.get('NARRATIVE_DOCUMENT_MODEL') or 'gemini-3.5-flash').strip() or 'gemini-3.5-flash'


def _request_timeout() -> int:
    return int(current_app.config.get('NARRATIVE_DOCUMENT_TIMEOUT', 180) or 180)


def _max_output_tokens() -> int:
    return int(current_app.config.get('NARRATIVE_DOCUMENT_MAX_OUTPUT_TOKENS', 32000) or 32000)
