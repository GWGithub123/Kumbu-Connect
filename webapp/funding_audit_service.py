"""AI-assisted verification for grant and donation funding documents."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime

from flask import current_app
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from .google_search_service import search_google
from .models import BookkeepingDocument


DOCUMENT_SYSTEM_PROMPT = """You audit grant, donation, and award documentation for Kenyan community-based organisations.

Return only valid JSON with this structure:
{
  "document_type": "grant_certificate|award_letter|donation_receipt|bank_statement|memorandum|unknown",
  "document_date": "YYYY-MM-DD or empty string",
  "period_start": "YYYY-MM-DD or empty string",
  "period_end": "YYYY-MM-DD or empty string",
  "currency": "KES or detected currency code",
  "issuing_organization": "string",
  "recipient_organization": "string",
  "document_title": "string",
  "reference_number": "string",
  "summary": "1-2 sentence summary of what this document claims",
  "raw_text": "best-effort transcription preserving uncertainty with ?",
  "extracted_funding_amount": 0.0,
  "extracted_working_capital": 0.0,
  "quality_flags": ["short issues or ambiguities"],
  "extraction_notes": "short note about legibility or assumptions",
  "document_confidence": 0.0,
  "legitimacy_assessment": {
    "document_seems_legitimate": true,
    "confidence": 0.0,
    "signals": ["short positive authenticity signals"],
    "concerns": ["short authenticity concerns"],
    "rationale": "one short explanation"
  }
}

Rules:
- Preserve uncertainty rather than inventing facts.
- Prefer exact visible names, dates, and amounts.
- If a field is not visible, leave it empty or 0.0.
- Treat logos, signatures, stamps, reference numbers, official formatting, and consistent typography as authenticity signals when present.
- Treat obvious image tampering, inconsistent names, missing issuer details, strange formatting, or unreadable key fields as concerns when present.
- Confidence values must be between 0 and 1.
"""

SOURCE_VERIFICATION_SYSTEM_PROMPT = """You assess whether a claimed funder appears to be a real organisation using supplied web search results.

Return only valid JSON with this structure:
{
  "funder_exists_online": "verified|unclear|not_found",
  "status": "verified|needs_review|flagged",
  "confidence": 0.0,
  "summary": "one sentence",
  "evidence": ["short evidence bullets"],
  "concerns": ["short concern bullets"],
  "recommended_action": "one sentence"
}

Rules:
- Use only the provided search results and document analysis.
- If the evidence is thin or mixed, return unclear and needs_review.
- If there are no relevant public results, use not_found unless the organisation is too generic to judge.
- Confidence values must be between 0 and 1.
"""

OUTFLOW_KEYWORDS = (
    'charit', 'charity', 'donat', 'support', 'scholar', 'relief',
    'aid', 'bursar', 'grant_given', 'grant_disbursed', 'beneficiary', 'giving',
)
INCOMING_KEYWORDS = (
    'receive', 'received', 'funding', 'funder', 'capital', 'income', 'revenue', 'grant_received',
)
DESCRIPTION_OUTFLOW_WORDS = (
    'charity', 'charitable', 'donation', 'donated', 'support', 'scholarship',
    'relief', 'aid', 'bursary', 'beneficiary', 'grant',
)


class FundingAuditError(RuntimeError):
    """Raised when a funding verification document cannot be processed safely."""


def build_funding_audit_payload(cbo, document_pages: list[dict], filename: str, declared: dict | None = None) -> dict:
    normalized_declared = _normalize_declared(declared)
    extracted = extract_funding_document(document_pages, filename, cbo, normalized_declared)
    search_query = _build_funder_search_query(
        normalized_declared.get('funder_name') or extracted.get('issuing_organization', ''),
        cbo.name,
    )
    search_results = search_google(search_query, num_results=5)
    source_verification = verify_funding_source(cbo, normalized_declared, extracted, search_results)
    operational_audit = evaluate_operational_funding_consistency(cbo, normalized_declared, extracted)
    audit = _build_funding_audit(normalized_declared, extracted, source_verification, operational_audit)
    return {
        'declared': normalized_declared,
        'document_analysis': extracted,
        'search_results': search_results,
        'source_verification': source_verification,
        'operational_audit': operational_audit,
        'audit': audit,
    }


def extract_funding_document(document_pages: list[dict], filename: str, cbo, declared: dict) -> dict:
    api_key = (current_app.config.get('OPENAI_API_KEY') or '').strip()
    if not api_key:
        raise FundingAuditError('OPENAI_API_KEY is not configured.')
    if not document_pages:
        raise FundingAuditError('No funding document pages were provided for extraction.')

    client = OpenAI(api_key=api_key)
    content = [{
        'type': 'text',
        'text': (
            f'Analyze this funding document for {cbo.name}. '
            f'Declared funder: {declared.get("funder_name") or "unknown"}. '
            f'Declared funding amount: {declared.get("funding_amount", 0.0)} {declared.get("currency") or "KES"}. '
            f'Declared working capital: {declared.get("working_capital", 0.0)} {declared.get("currency") or "KES"}. '
            f'Declared period: {declared.get("period_start") or "unknown"} to {declared.get("period_end") or "unknown"}. '
            f'Filename: {filename}. Return the exact JSON shape from the system prompt.'
        ),
    }]
    for index, page in enumerate(document_pages, start=1):
        image_b64 = base64.b64encode(page['image_bytes']).decode('ascii')
        data_url = f"data:{page['mime_type']};base64,{image_b64}"
        content.append({'type': 'text', 'text': f'Page {index} of {len(document_pages)}.'})
        content.append({'type': 'image_url', 'image_url': {'url': data_url}})

    try:
        response = client.chat.completions.create(
            model=current_app.config.get('OPENAI_VISION_MODEL', 'gpt-4.1'),
            response_format={'type': 'json_object'},
            temperature=0.1,
            max_tokens=3500,
            timeout=current_app.config.get('OPENAI_REQUEST_TIMEOUT', 60),
            messages=[
                {'role': 'system', 'content': DOCUMENT_SYSTEM_PROMPT},
                {'role': 'user', 'content': content},
            ],
        )
    except RateLimitError as exc:
        raise FundingAuditError('OpenAI funding verification quota was exceeded. Check billing or try again later.') from exc
    except APITimeoutError as exc:
        raise FundingAuditError('OpenAI funding verification timed out while reading this document.') from exc
    except APIConnectionError as exc:
        raise FundingAuditError('Could not reach OpenAI to verify this funding document.') from exc
    except APIStatusError as exc:
        raise FundingAuditError(f'OpenAI funding verification returned an error: {exc.status_code}.') from exc

    message = response.choices[0].message.content if response.choices else '{}'
    try:
        parsed = json.loads(message or '{}')
    except json.JSONDecodeError as exc:
        raise FundingAuditError('OpenAI returned invalid JSON for the funding document.') from exc

    return _normalize_document_analysis(parsed)


def verify_funding_source(cbo, declared: dict, extracted: dict, search_results: dict) -> dict:
    api_key = (current_app.config.get('OPENAI_API_KEY') or '').strip()
    if not search_results.get('configured'):
        return {
            'funder_exists_online': 'unclear',
            'status': 'needs_review',
            'confidence': 0.0,
            'summary': 'Google Custom Search is not configured, so the funder could not be verified online.',
            'evidence': [],
            'concerns': ['External web verification is unavailable in this environment.'],
            'recommended_action': 'Configure Google Custom Search and re-run verification.',
        }

    if not api_key:
        return {
            'funder_exists_online': 'unclear',
            'status': 'needs_review',
            'confidence': 0.0,
            'summary': 'OpenAI is not configured, so the search evidence could not be assessed.',
            'evidence': [],
            'concerns': ['AI review is unavailable in this environment.'],
            'recommended_action': 'Configure OpenAI and re-run verification.',
        }

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=current_app.config.get('OPENAI_VISION_MODEL', 'gpt-4.1'),
            response_format={'type': 'json_object'},
            temperature=0.1,
            max_tokens=1200,
            timeout=current_app.config.get('OPENAI_REQUEST_TIMEOUT', 60),
            messages=[
                {'role': 'system', 'content': SOURCE_VERIFICATION_SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': json.dumps({
                        'cbo_name': cbo.name,
                        'declared': declared,
                        'document_analysis': extracted,
                        'search_results': search_results,
                    }, default=str),
                },
            ],
        )
        message = response.choices[0].message.content if response.choices else '{}'
        parsed = json.loads(message or '{}')
    except (json.JSONDecodeError, RateLimitError, APITimeoutError, APIConnectionError, APIStatusError) as exc:
        current_app.logger.warning('Funding source verification fell back to manual review for %s: %s', cbo.slug, exc)
        parsed = {
            'funder_exists_online': 'unclear',
            'status': 'needs_review',
            'confidence': 0.0,
            'summary': 'Search results were collected, but the AI verification step failed.',
            'evidence': [],
            'concerns': ['The automated review step failed and requires manual review.'],
            'recommended_action': 'Inspect the search results and document manually.',
        }

    existence = str(parsed.get('funder_exists_online') or 'unclear').strip().lower()
    if existence not in {'verified', 'unclear', 'not_found'}:
        existence = 'unclear'
    status = str(parsed.get('status') or 'needs_review').strip().lower()
    if status not in {'verified', 'needs_review', 'flagged'}:
        status = 'needs_review'
    return {
        'funder_exists_online': existence,
        'status': status,
        'confidence': _clamp_confidence(parsed.get('confidence')),
        'summary': str(parsed.get('summary') or '').strip(),
        'evidence': _clean_string_list(parsed.get('evidence')),
        'concerns': _clean_string_list(parsed.get('concerns')),
        'recommended_action': str(parsed.get('recommended_action') or '').strip(),
    }


def evaluate_operational_funding_consistency(cbo, declared: dict, extracted: dict) -> dict:
    period_start = declared.get('period_start') or extracted.get('period_start') or ''
    period_end = declared.get('period_end') or extracted.get('period_end') or ''
    observed = observed_charitable_giving(cbo, period_start=period_start, period_end=period_end)
    declared_funding_amount = float(
        declared.get('funding_amount') or extracted.get('extracted_funding_amount') or 0.0
    )
    exceeds = bool(declared_funding_amount > 0 and observed['total'] > declared_funding_amount + 0.01)
    if exceeds:
        summary = (
            f'Observed charitable giving of {observed["total"]:.2f} exceeds the declared funding amount '
            f'of {declared_funding_amount:.2f} for the stated period.'
        )
    else:
        summary = (
            f'Observed charitable giving totals {observed["total"]:.2f} against declared funding '
            f'of {declared_funding_amount:.2f}.'
        )
    return {
        'period_start': period_start,
        'period_end': period_end,
        'declared_funding_amount': round(declared_funding_amount, 2),
        'observed_charitable_giving': observed['total'],
        'bookkeeping_component': observed['bookkeeping_total'],
        'kobo_component': observed['kobo_total'],
        'exceeds_declared_funding': exceeds,
        'summary': summary,
    }


def observed_charitable_giving(cbo, period_start: str = '', period_end: str = '') -> dict:
    start_date = _parse_date(period_start)
    end_date = _parse_date(period_end)
    bookkeeping_total = round(_bookkeeping_charitable_giving(cbo, start_date, end_date), 2)
    kobo_total = round(_kobo_charitable_giving(cbo, start_date, end_date), 2)
    return {
        'bookkeeping_total': bookkeeping_total,
        'kobo_total': kobo_total,
        'total': round(bookkeeping_total + kobo_total, 2),
    }


def _build_funding_audit(declared: dict, extracted: dict, source_verification: dict, operational_audit: dict) -> dict:
    issues = []

    declared_amount = float(declared.get('funding_amount') or 0.0)
    extracted_amount = float(extracted.get('extracted_funding_amount') or 0.0)
    if declared_amount <= 0:
        issues.append({'code': 'missing_declared_amount', 'message': 'A declared funding amount was not provided.'})
    if extracted_amount > 0 and declared_amount > 0:
        allowed_gap = max(100.0, declared_amount * 0.15)
        if abs(declared_amount - extracted_amount) > allowed_gap:
            issues.append({'code': 'funding_amount_mismatch', 'message': 'The uploaded document amount does not closely match the declared funding amount.'})

    declared_funder = str(declared.get('funder_name') or '').strip()
    extracted_funder = str(extracted.get('issuing_organization') or '').strip()
    if declared_funder and extracted_funder and not _names_roughly_match(declared_funder, extracted_funder):
        issues.append({'code': 'funder_name_mismatch', 'message': 'The uploaded document issuer does not closely match the declared funder name.'})

    legitimacy = extracted.get('legitimacy_assessment') or {}
    if legitimacy.get('document_seems_legitimate') is False:
        issues.append({'code': 'document_legitimacy', 'message': 'The uploaded certificate shows signs that require manual authenticity review.'})

    if source_verification.get('status') == 'flagged':
        issues.append({'code': 'funder_not_verified', 'message': source_verification.get('summary') or 'The stated funder could not be verified online.'})
    elif source_verification.get('status') == 'needs_review':
        issues.append({'code': 'funder_review_needed', 'message': source_verification.get('summary') or 'The stated funder needs manual review.'})

    if operational_audit.get('exceeds_declared_funding'):
        issues.append({'code': 'charitable_giving_overrun', 'message': operational_audit.get('summary') or 'Observed charitable giving exceeds declared funding.'})

    overall_status = 'verified'
    if any(issue['code'] in {'funder_not_verified', 'document_legitimacy', 'charitable_giving_overrun'} for issue in issues):
        overall_status = 'flagged'
    elif issues:
        overall_status = 'needs_review'

    overall_confidence = min(
        1.0,
        max(
            _clamp_confidence(source_verification.get('confidence')),
            _clamp_confidence((legitimacy or {}).get('confidence')),
        ),
    )
    return {
        'issues': issues,
        'issue_count': len(issues),
        'status': overall_status,
        'confidence': overall_confidence,
    }


def _normalize_declared(declared: dict | None) -> dict:
    declared = declared or {}
    return {
        'funder_name': str(declared.get('funder_name') or '').strip(),
        'funding_amount': round(_coerce_amount(declared.get('funding_amount')), 2),
        'working_capital': round(_coerce_amount(declared.get('working_capital')), 2),
        'period_start': _normalize_date(declared.get('period_start')),
        'period_end': _normalize_date(declared.get('period_end')),
        'currency': str(declared.get('currency') or 'KES').strip().upper() or 'KES',
    }


def _normalize_document_analysis(payload: dict) -> dict:
    legitimacy = payload.get('legitimacy_assessment') if isinstance(payload.get('legitimacy_assessment'), dict) else {}
    return {
        'document_type': _normalize_choice(payload.get('document_type'), {
            'grant_certificate', 'award_letter', 'donation_receipt', 'bank_statement', 'memorandum', 'unknown'
        }, 'unknown'),
        'document_date': _normalize_date(payload.get('document_date')),
        'period_start': _normalize_date(payload.get('period_start')),
        'period_end': _normalize_date(payload.get('period_end')),
        'currency': str(payload.get('currency') or 'KES').strip().upper() or 'KES',
        'issuing_organization': str(payload.get('issuing_organization') or '').strip(),
        'recipient_organization': str(payload.get('recipient_organization') or '').strip(),
        'document_title': str(payload.get('document_title') or '').strip(),
        'reference_number': str(payload.get('reference_number') or '').strip(),
        'summary': str(payload.get('summary') or '').strip(),
        'raw_text': str(payload.get('raw_text') or '').strip(),
        'extracted_funding_amount': round(_coerce_amount(payload.get('extracted_funding_amount')), 2),
        'extracted_working_capital': round(_coerce_amount(payload.get('extracted_working_capital')), 2),
        'quality_flags': _clean_string_list(payload.get('quality_flags')),
        'extraction_notes': str(payload.get('extraction_notes') or '').strip(),
        'document_confidence': _clamp_confidence(payload.get('document_confidence')),
        'legitimacy_assessment': {
            'document_seems_legitimate': bool(legitimacy.get('document_seems_legitimate', False)),
            'confidence': _clamp_confidence(legitimacy.get('confidence')),
            'signals': _clean_string_list(legitimacy.get('signals')),
            'concerns': _clean_string_list(legitimacy.get('concerns')),
            'rationale': str(legitimacy.get('rationale') or '').strip(),
        },
    }


def _build_funder_search_query(funder_name: str, cbo_name: str) -> str:
    cleaned_funder = ' '.join((funder_name or '').split())
    if not cleaned_funder:
        return ''
    return f'"{cleaned_funder}" grant organization donor {cbo_name} Kenya'


def _bookkeeping_charitable_giving(cbo, start_date: datetime | None, end_date: datetime | None) -> float:
    total = 0.0
    for document in BookkeepingDocument.query.filter_by(cbo_id=cbo.id).all():
        try:
            extracted = json.loads(document.extracted_data_json or '{}')
        except (json.JSONDecodeError, TypeError):
            extracted = {}
        for entry in extracted.get('bookkeeping_entries') or []:
            if not isinstance(entry, dict):
                continue
            if not _is_charitable_bookkeeping_entry(entry):
                continue
            entry_date = _parse_date(entry.get('entry_date'))
            if (start_date or end_date) and not entry_date:
                continue
            if not _date_in_range(entry_date, start_date, end_date):
                continue
            total += _coerce_amount(entry.get('amount'))
    return total


def _is_charitable_bookkeeping_entry(entry: dict) -> bool:
    entry_type = str(entry.get('entry_type') or '').strip().lower()
    direction = str(entry.get('direction') or '').strip().lower()
    category = str(entry.get('category') or '').strip().lower()
    description = str(entry.get('description') or '').strip().lower()
    if entry_type != 'expense' and direction != 'outflow':
        return False
    if category in {'donation', 'grant'}:
        return True
    return any(word in description for word in DESCRIPTION_OUTFLOW_WORDS)


def _kobo_charitable_giving(cbo, start_date: datetime | None, end_date: datetime | None) -> float:
    try:
        submissions = json.loads(cbo.raw_kobo_json or '[]')
    except (json.JSONDecodeError, TypeError):
        submissions = []

    total = 0.0
    for submission in submissions if isinstance(submissions, list) else []:
        if not isinstance(submission, dict):
            continue
        submission_date = _extract_submission_date(submission)
        if (start_date or end_date) and not submission_date:
            continue
        if not _date_in_range(submission_date, start_date, end_date):
            continue
        total += _scan_for_outflow_values(submission)
    return total


def _scan_for_outflow_values(value, key_hint: str = '') -> float:
    total = 0.0
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key or '')
            if _key_suggests_outflow(key_text) and _is_numeric_like(nested):
                total += _coerce_amount(nested)
            else:
                total += _scan_for_outflow_values(nested, key_hint=key_text)
        return total
    if isinstance(value, list):
        return sum(_scan_for_outflow_values(item, key_hint=key_hint) for item in value)
    if key_hint and _key_suggests_outflow(key_hint) and _is_numeric_like(value):
        return _coerce_amount(value)
    return 0.0


def _key_suggests_outflow(key: str) -> bool:
    normalized = re.sub(r'[^a-z0-9]+', '_', str(key or '').strip().lower())
    if not normalized:
        return False
    if any(marker in normalized for marker in INCOMING_KEYWORDS):
        return False
    return any(marker in normalized for marker in OUTFLOW_KEYWORDS)


def _extract_submission_date(submission: dict) -> datetime | None:
    for key in (
        'submission_date', 'date', 'created_at', 'submitted_at', '_submission_time',
        'period_start', 'transaction_date', 'entry_date',
    ):
        date_value = submission.get(key)
        parsed = _parse_date(date_value)
        if parsed:
            return parsed
    return None


def _normalize_choice(value, allowed: set[str], fallback: str) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in allowed else fallback


def _coerce_amount(value) -> float:
    if value in (None, ''):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        cleaned = re.sub(r'[^0-9.\-]', '', str(value))
        try:
            return float(cleaned)
        except ValueError:
            return 0.0


def _normalize_date(value) -> str:
    parsed = _parse_date(value)
    return parsed.strftime('%Y-%m-%d') if parsed else ''


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace('Z', '+00:00')
    for candidate in (normalized, normalized.split('T')[0]):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    for fmt in ('%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _date_in_range(candidate: datetime | None, start_date: datetime | None, end_date: datetime | None) -> bool:
    if candidate is None:
        return not (start_date or end_date)
    if start_date and candidate.date() < start_date.date():
        return False
    if end_date and candidate.date() > end_date.date():
        return False
    return True


def _is_numeric_like(value) -> bool:
    if isinstance(value, (int, float)):
        return True
    cleaned = re.sub(r'[^0-9.\-]', '', str(value or ''))
    return bool(cleaned and re.search(r'\d', cleaned))


def _clean_string_list(values) -> list[str]:
    cleaned = []
    for value in values or []:
        text = str(value or '').strip()
        if text:
            cleaned.append(text)
    return cleaned[:8]


def _names_roughly_match(left: str, right: str) -> bool:
    left_tokens = {token for token in re.findall(r'[a-z0-9]+', left.lower()) if len(token) > 2}
    right_tokens = {token for token in re.findall(r'[a-z0-9]+', right.lower()) if len(token) > 2}
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    return len(overlap) >= max(1, min(len(left_tokens), len(right_tokens)) // 2)


def _clamp_confidence(value) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(confidence, 1.0))