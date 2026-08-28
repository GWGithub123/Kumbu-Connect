"""Gemini vision extraction for bookkeeping documents."""
import json
import re
from io import BytesIO
from statistics import mean

from flask import current_app
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .azure_document_intelligence_service import (
    AzureDocumentIntelligenceError,
    azure_document_intelligence_configured,
    build_azure_bookkeeping_transcription,
)


SYSTEM_PROMPT = """You extract bookkeeping data from photographed paper documents for Kenyan community-based organisations.

Return only valid JSON with this structure:
{
    "document_type": "ledger|receipt|invoice|cashbook|bank_statement|expense_sheet|inventory_register|member_register|savings_register|loan_register|unknown",
  "document_date": "YYYY-MM-DD or empty string",
  "period_start": "YYYY-MM-DD or empty string",
  "period_end": "YYYY-MM-DD or empty string",
  "currency": "KES or detected currency code",
    "organization_name": "string",
    "document_title": "string",
  "vendor_or_counterparty": "string",
  "summary": "1-2 sentence summary of what this document represents",
    "raw_text": "best-effort plain text transcription, preserving uncertain marks with ?",
    "detected_columns": ["ordered column names seen in the document"],
    "transcribed_rows": [
        {
            "row_number": 1,
            "row_type": "transaction|inventory_item|member_record|header|other",
            "cells": [
                {
                    "column_name": "column name",
                    "cell_text": "cell text"
                }
            ],
            "notes": "short interpretation of the row",
            "confidence": 0.0
        }
    ],
  "document_confidence": 0.0,
  "quality_flags": ["array of short issues or ambiguities"],
  "extraction_notes": "short note about legibility or assumptions",
  "totals": {
    "income": 0.0,
    "expenses": 0.0,
    "net": 0.0
  },
  "bookkeeping_entries": [
    {
      "entry_date": "YYYY-MM-DD or empty string",
      "description": "string",
      "amount": 0.0,
    "quantity": 0.0,
    "unit": "string",
      "entry_type": "income|expense|asset|liability|equity|transfer|unknown",
      "category": "sales|donation|grant|rent|utilities|transport|supplies|payroll|maintenance|bank_fees|loan|savings|inventory|equipment|other",
      "direction": "inflow|outflow|neutral",
      "reference": "string",
      "confidence": 0.0
    }
  ]
}

Rules:
- Preserve uncertainty instead of inventing facts.
- Transcribe exactly what is visible on the page, row by row, from top to bottom.
- If a page is sideways or upside down, mentally rotate it first and then transcribe from the visually topmost row to the visually bottommost row.
- Include every visible data row, including the first row, the last row, repeated names, and repeated values.
- Do not duplicate rows when alternate rotations of the same page are provided. They are the same page shown in different orientations.
- Never perform arithmetic unless the computed result is explicitly written on the document.
- If a column is labeled fees, fee, total revenue, revenue, amount, paid, or total, copy the visible number exactly as the row amount. Do not multiply it by days, quantity, or rate unless the paper itself shows that multiplication result in the same row.
- If a row contains days rented or quantity, preserve that value separately in quantity. Do not convert it into a new amount.
- For single-column numeric sheets, treat each visible number as its own row and bookkeeping entry.
- If a value is hard to read, keep the row anyway and note the uncertainty in notes, quality_flags, or raw_text rather than dropping the row.
- Amounts must be numeric, not strings.
- Use KES when the currency is not explicit but the context suggests Kenya.
- If the image contains multiple rows, extract each meaningful bookkeeping row.
- If the document spans multiple pages or images, treat them as one single document and combine the information.
- Preserve row structure for registers, inventories, member books, and handwritten ledgers.
- Do not sample rows when the handwriting is readable. Capture all visible data rows in order.
- Do not include column-header rows in transcribed_rows or bookkeeping_entries.
- detected_columns must come from the document's visible header labels in the order they appear. Do not rename them to standard bookkeeping labels.
- The number of columns is not fixed. Preserve the real document columns even when the table is wide, sparse, irregular, or some cells are blank.
- In each transcribed row, return cells as an array of objects with column_name and cell_text.
- If a header spans multiple lines, combine the visible header words into a single label in reading order, such as Days Rented or Amt Paid.
- Blank cells are allowed. Do not force every row to populate every detected column.
- When zoomed crops or enhanced variants are provided, use them to read difficult handwriting and exact headers, but return one unified table with no duplicate rows.
- Keep summary to one sentence.
- Keep notes short and specific.
- Keep raw_text concise. When transcribed_rows already capture the table content, raw_text should only include the title, headings, and any uncertain or non-tabular text not already represented in the rows. Do not duplicate the full table in raw_text. Keep raw_text under about 1200 characters.
- bookkeeping_entries are secondary to the row transcription. If mapping bespoke register rows into the normalized bookkeeping_entries schema is unclear, leave bookkeeping_entries empty or include only the clearly supported entries. Do not invent normalized fields.
- Categorize each row conservatively.
- document_confidence and entry confidence must be between 0 and 1.
"""

NORMALIZATION_PROMPT = """You normalize bookkeeping data from an OCR/layout transcription that has already been extracted from the photographed document.

Return only valid JSON with this structure:
{
    "document_type": "ledger|receipt|invoice|cashbook|bank_statement|expense_sheet|inventory_register|member_register|savings_register|loan_register|unknown",
    "document_date": "YYYY-MM-DD or empty string",
    "period_start": "YYYY-MM-DD or empty string",
    "period_end": "YYYY-MM-DD or empty string",
    "currency": "KES or detected currency code",
    "organization_name": "string",
    "document_title": "string",
    "vendor_or_counterparty": "string",
    "summary": "1 sentence summary",
    "quality_flags": ["array of short issues or ambiguities"],
    "extraction_notes": "short note about what remains uncertain",
    "repaired_rows": [
        {
            "row_number": 1,
            "cells": [
                {
                    "column_name": "column name",
                    "cell_text": "repaired cell text"
                }
            ],
            "notes": "what was repaired and why",
            "confidence": 0.0
        }
    ],
    "bookkeeping_entries": [
        {
            "entry_date": "YYYY-MM-DD or empty string",
            "description": "string",
            "amount": 0.0,
            "quantity": 0.0,
            "unit": "string",
            "entry_type": "income|expense|asset|liability|equity|transfer|unknown",
            "category": "sales|donation|grant|rent|utilities|transport|supplies|payroll|maintenance|bank_fees|loan|savings|inventory|equipment|other",
            "direction": "inflow|outflow|neutral",
            "reference": "string",
            "confidence": 0.0,
            "source_row_numbers": [1]
        }
    ]
}

Rules:
- The provided detected_columns and transcribed_rows are the source of truth. Do not invent new rows, columns, or values.
- Use the attached ambiguous row crops only to clarify hard-to-read source rows.
- Return repaired_rows only for low-confidence or conflict-heavy rows that were explicitly provided as repair candidates.
- In repaired_rows, preserve the same row_number and the same detected columns. Do not split rows, merge rows, or create new columns.
- Only change a cell in repaired_rows when the row crop or attached cell crop supports the change. If uncertain, keep Azure's original text.
- Preserve the document-specific columns exactly as provided. Do not rename them.
- Every bookkeeping entry must be grounded in one or more visible source rows, and each entry must list those row numbers in source_row_numbers.
- Do not multiply rates by days or infer totals unless the visible source row explicitly shows that total.
- If the document is an irregular register and normalized mapping is unclear, leave bookkeeping_entries empty.
- Keep summary to one sentence.
- Keep quality_flags short and specific.
- document metadata should stay conservative; leave fields blank when unsupported by the source rows and OCR text.
"""

DOCUMENT_TYPES = (
    'ledger', 'receipt', 'invoice', 'cashbook', 'bank_statement', 'expense_sheet',
    'inventory_register', 'member_register', 'savings_register', 'loan_register', 'unknown',
)
ROW_TYPES = ('transaction', 'inventory_item', 'member_record', 'header', 'other')
ENTRY_TYPES = ('income', 'expense', 'asset', 'liability', 'equity', 'transfer', 'unknown')
ENTRY_CATEGORIES = (
    'sales', 'donation', 'grant', 'rent', 'utilities', 'transport', 'supplies',
    'payroll', 'maintenance', 'bank_fees', 'loan', 'savings', 'inventory',
    'equipment', 'other',
)
ENTRY_DIRECTIONS = ('inflow', 'outflow', 'neutral')

BOOKKEEPING_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'document_type': {'type': 'string', 'enum': list(DOCUMENT_TYPES)},
        'document_date': {'type': 'string'},
        'period_start': {'type': 'string'},
        'period_end': {'type': 'string'},
        'currency': {'type': 'string'},
        'organization_name': {'type': 'string'},
        'document_title': {'type': 'string'},
        'vendor_or_counterparty': {'type': 'string'},
        'summary': {'type': 'string'},
        'raw_text': {'type': 'string'},
        'detected_columns': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'transcribed_rows': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'row_number': {'type': 'integer'},
                    'row_type': {'type': 'string', 'enum': list(ROW_TYPES)},
                    'cells': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'column_name': {'type': 'string'},
                                'cell_text': {'type': 'string'},
                            },
                            'required': ['column_name', 'cell_text'],
                            'additionalProperties': False,
                        },
                    },
                    'notes': {'type': 'string'},
                    'confidence': {'type': 'number'},
                },
                'required': ['row_number', 'row_type', 'cells', 'notes', 'confidence'],
                'additionalProperties': False,
            },
        },
        'document_confidence': {'type': 'number'},
        'quality_flags': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'extraction_notes': {'type': 'string'},
        'totals': {
            'type': 'object',
            'properties': {
                'income': {'type': 'number'},
                'expenses': {'type': 'number'},
                'net': {'type': 'number'},
            },
            'required': ['income', 'expenses', 'net'],
            'additionalProperties': False,
        },
        'bookkeeping_entries': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'entry_date': {'type': 'string'},
                    'description': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'quantity': {'type': 'number'},
                    'unit': {'type': 'string'},
                    'entry_type': {'type': 'string', 'enum': list(ENTRY_TYPES)},
                    'category': {'type': 'string', 'enum': list(ENTRY_CATEGORIES)},
                    'direction': {'type': 'string', 'enum': list(ENTRY_DIRECTIONS)},
                    'reference': {'type': 'string'},
                    'confidence': {'type': 'number'},
                },
                'required': [
                    'entry_date', 'description', 'amount', 'quantity', 'unit',
                    'entry_type', 'category', 'direction', 'reference', 'confidence',
                ],
                'additionalProperties': False,
            },
        },
    },
    'required': [
        'document_type', 'document_date', 'period_start', 'period_end', 'currency',
        'organization_name', 'document_title', 'vendor_or_counterparty', 'summary',
        'raw_text', 'detected_columns', 'transcribed_rows', 'document_confidence',
        'quality_flags', 'extraction_notes', 'totals', 'bookkeeping_entries',
    ],
    'additionalProperties': False,
}

BOOKKEEPING_NORMALIZATION_SCHEMA = {
    'type': 'object',
    'properties': {
        'document_type': {'type': 'string', 'enum': list(DOCUMENT_TYPES)},
        'document_date': {'type': 'string'},
        'period_start': {'type': 'string'},
        'period_end': {'type': 'string'},
        'currency': {'type': 'string'},
        'organization_name': {'type': 'string'},
        'document_title': {'type': 'string'},
        'vendor_or_counterparty': {'type': 'string'},
        'summary': {'type': 'string'},
        'quality_flags': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'extraction_notes': {'type': 'string'},
        'repaired_rows': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'row_number': {'type': 'integer'},
                    'cells': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'column_name': {'type': 'string'},
                                'cell_text': {'type': 'string'},
                            },
                            'required': ['column_name', 'cell_text'],
                            'additionalProperties': False,
                        },
                    },
                    'notes': {'type': 'string'},
                    'confidence': {'type': 'number'},
                },
                'required': ['row_number', 'cells', 'notes', 'confidence'],
                'additionalProperties': False,
            },
        },
        'bookkeeping_entries': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'entry_date': {'type': 'string'},
                    'description': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'quantity': {'type': 'number'},
                    'unit': {'type': 'string'},
                    'entry_type': {'type': 'string', 'enum': list(ENTRY_TYPES)},
                    'category': {'type': 'string', 'enum': list(ENTRY_CATEGORIES)},
                    'direction': {'type': 'string', 'enum': list(ENTRY_DIRECTIONS)},
                    'reference': {'type': 'string'},
                    'confidence': {'type': 'number'},
                    'source_row_numbers': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                    },
                },
                'required': [
                    'entry_date', 'description', 'amount', 'quantity', 'unit',
                    'entry_type', 'category', 'direction', 'reference', 'confidence', 'source_row_numbers',
                ],
                'additionalProperties': False,
            },
        },
    },
    'required': [
        'document_type', 'document_date', 'period_start', 'period_end', 'currency',
        'organization_name', 'document_title', 'vendor_or_counterparty', 'summary',
        'quality_flags', 'extraction_notes', 'repaired_rows', 'bookkeeping_entries',
    ],
    'additionalProperties': False,
}

ALLOWED_ENTRY_TYPES = set(ENTRY_TYPES)
ALLOWED_CATEGORIES = set(ENTRY_CATEGORIES)
ALLOWED_DIRECTIONS = set(ENTRY_DIRECTIONS)
TOOL_RATE_ALIASES = ('fees', 'fee', 'dailyfee', 'rate', 'rateperday', 'rentalrate')
TOTAL_REVENUE_ALIASES = ('totalrevenue', 'revenue', 'totalincome', 'income', 'amount', 'paid', 'total')
TOOL_DAYS_ALIASES = ('days', 'daysrented', 'duration', 'totaldays')
TOOL_NAME_ALIASES = ('tooltype', 'toolname', 'tool')
TOOL_START_DATE_ALIASES = ('date', 'rentoutdate', 'startdate', 'issuedate')
TOOL_END_DATE_ALIASES = ('enddate', 'returndate', 'datein', 'duedate')
TOOL_PERSON_ALIASES = ('farmersname', 'membername', 'name', 'borrowername')
TOOL_ID_ALIASES = ('idno', 'mfidno', 'memberid', 'ref')


class BookkeepingExtractionError(RuntimeError):
    """Raised when a bookkeeping document cannot be extracted safely."""


def extract_bookkeeping_document(document_pages: list[dict], filename: str, cbo, related_page_upload: bool = False) -> dict:
    if azure_document_intelligence_configured(current_app.config):
        try:
            return _extract_bookkeeping_document_hybrid(document_pages, filename, cbo, related_page_upload=related_page_upload)
        except AzureDocumentIntelligenceError as exc:
            current_app.logger.warning(
                'Azure bookkeeping transcription failed for %s; falling back to Gemini-only extraction: %s',
                filename,
                exc,
            )
            if not _gemini_bookkeeping_api_key():
                raise BookkeepingExtractionError(str(exc)) from exc

    return _extract_bookkeeping_document_with_gemini_vision(document_pages, filename, cbo, related_page_upload=related_page_upload)


def _extract_bookkeeping_document_hybrid(document_pages: list[dict], filename: str, cbo, related_page_upload: bool = False) -> dict:
    if not document_pages:
        raise BookkeepingExtractionError('No document pages were provided for extraction.')

    current_app.logger.info(
        'Starting Azure+Gemini bookkeeping extraction for %s (%s): %d page(s), related_pages=%s',
        filename,
        cbo.name,
        len(document_pages),
        related_page_upload,
    )

    azure_payload, review_snippets = build_azure_bookkeeping_transcription(
        document_pages,
        filename,
        cbo,
        related_page_upload=related_page_upload,
    )
    current_app.logger.info(
        'Azure bookkeeping transcription for %s (%s): %d rows, %d columns, %d review row crops, confidence=%.2f',
        filename,
        cbo.name,
        len(azure_payload.get('transcribed_rows') or []),
        len(azure_payload.get('detected_columns') or []),
        len(review_snippets),
        float(azure_payload.get('document_confidence') or 0.0),
    )
    azure_payload = _apply_page_order_hints(azure_payload, document_pages)

    gemini_api_key = _gemini_bookkeeping_api_key()
    merged_payload = azure_payload
    normalization_provider = ''
    if gemini_api_key:
        try:
            merged_payload = _normalize_azure_transcription_with_gemini(
                azure_payload,
                review_snippets,
                filename,
                cbo,
                related_page_upload=related_page_upload,
            )
            normalization_provider = current_app.config.get('BOOKKEEPING_VISION_MODEL', 'gemini-3.5-flash')
        except BookkeepingExtractionError as exc:
            current_app.logger.warning(
                'Gemini bookkeeping normalization failed for %s; keeping Azure raw transcription: %s',
                filename,
                exc,
            )
            _append_extraction_note(
                merged_payload,
                'Gemini normalization was skipped after a secondary normalization error; Azure raw transcription was preserved.'
            )
    else:
        _append_extraction_note(
            merged_payload,
            'Gemini normalization was skipped because Gemini_API_Key is not configured.'
        )

    normalized = refine_extracted_bookkeeping_payload(_normalize_payload(merged_payload))
    normalized = _dedupe_single_amount_boundary_rows(normalized)
    if not normalized['bookkeeping_entries'] and not normalized['summary'] and not normalized['transcribed_rows']:
        raise BookkeepingExtractionError('No bookkeeping data could be extracted from this image.')

    normalized['model_used'] = (
        f"{current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_LAYOUT_MODEL', 'prebuilt-layout')}"
        f" + {normalization_provider}"
        if normalization_provider else
        current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_LAYOUT_MODEL', 'prebuilt-layout')
    )
    normalized['transcription_provider'] = 'azure_document_intelligence'
    normalized['normalization_provider'] = normalization_provider
    normalized['extraction_pipeline'] = 'azure_layout_read_plus_gemini_normalizer' if normalization_provider else 'azure_layout_read_only'
    normalized['review_snippet_count'] = len(review_snippets)
    return normalized


def _gemini_bookkeeping_api_key() -> str:
    return str(current_app.config.get('GEMINI_API_KEY') or '').strip()


def _gemini_bookkeeping_client(api_key: str) -> genai.Client:
    timeout_s = int(current_app.config.get('BOOKKEEPING_REQUEST_TIMEOUT', 180) or 180)
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=max(1000, timeout_s * 1000)),
    )


def _bookkeeping_vision_model() -> str:
    return str(current_app.config.get('BOOKKEEPING_VISION_MODEL') or 'gemini-3.5-flash').strip() or 'gemini-3.5-flash'


def _extract_bookkeeping_document_with_gemini_vision(document_pages: list[dict], filename: str, cbo, related_page_upload: bool = False) -> dict:
    api_key = _gemini_bookkeeping_api_key()
    if not api_key:
        raise BookkeepingExtractionError('Gemini_API_Key is not configured.')

    if not document_pages:
        raise BookkeepingExtractionError('No document pages were provided for extraction.')

    for idx, page in enumerate(document_pages, start=1):
        page_bytes = page.get('image_bytes') or b''
        try:
            with Image.open(BytesIO(page_bytes)) as img:
                w, h = img.size
                current_app.logger.info(
                    'Bookkeeping extraction page %d/%d for %s (%s): %dx%d, %d bytes, mime=%s',
                    idx, len(document_pages), filename, cbo.name, w, h, len(page_bytes), page.get('mime_type', '?'),
                )
        except Exception:
            current_app.logger.info(
                'Bookkeeping extraction page %d/%d for %s (%s): %d bytes (could not read dimensions), mime=%s',
                idx, len(document_pages), filename, cbo.name, len(page_bytes), page.get('mime_type', '?'),
            )

    client = _gemini_bookkeeping_client(api_key)
    max_output_tokens = int(current_app.config.get('BOOKKEEPING_MAX_OUTPUT_TOKENS', 12000) or 12000)
    retry_max_output_tokens = int(current_app.config.get('BOOKKEEPING_RETRY_MAX_OUTPUT_TOKENS', 24000) or 24000)

    normalized = _extract_bookkeeping_document_with_mode(
        client,
        document_pages,
        filename,
        cbo,
        related_page_upload,
        variant_mode='minimal',
        max_output_tokens=max_output_tokens,
        retry_max_output_tokens=retry_max_output_tokens,
    )

    if _should_run_detailed_bookkeeping_retry(normalized, document_pages):
        try:
            detailed = _extract_bookkeeping_document_with_mode(
                client,
                document_pages,
                filename,
                cbo,
                related_page_upload,
                variant_mode='detailed',
                max_output_tokens=max_output_tokens,
                retry_max_output_tokens=retry_max_output_tokens,
            )
        except BookkeepingExtractionError as exc:
            current_app.logger.warning(
                'Gemini detailed bookkeeping retry failed for %s: %s. Keeping minimal-pass extraction.',
                filename,
                exc,
            )
        else:
            if _is_better_bookkeeping_result(detailed, normalized):
                normalized = detailed
                _append_extraction_note(
                    normalized,
                    'Used crop-assisted retry to improve wide-ledger handwriting extraction.'
                )

    return normalized


def _normalize_azure_transcription_with_gemini(
    azure_payload: dict,
    review_snippets: list[dict],
    filename: str,
    cbo,
    related_page_upload: bool = False,
) -> dict:
    api_key = _gemini_bookkeeping_api_key()
    if not api_key:
        raise BookkeepingExtractionError('Gemini_API_Key is not configured.')

    client = _gemini_bookkeeping_client(api_key)
    max_output_tokens = int(current_app.config.get('BOOKKEEPING_MAX_OUTPUT_TOKENS', 12000) or 12000)
    retry_max_output_tokens = int(current_app.config.get('BOOKKEEPING_RETRY_MAX_OUTPUT_TOKENS', 24000) or 24000)
    review_limit = int(current_app.config.get('BOOKKEEPING_CLAUDE_REVIEW_ROW_LIMIT', 8) or 8)
    parts = _build_bookkeeping_normalization_parts(
        azure_payload,
        review_snippets[:review_limit],
        filename,
        cbo,
        related_page_upload=related_page_upload,
    )

    try:
        response = _request_bookkeeping_normalization_response(client, parts, max_output_tokens)
    except genai_errors.ClientError as exc:
        if int(getattr(exc, 'code', 0) or 0) == 429:
            raise BookkeepingExtractionError('Gemini normalization quota was exceeded. Check billing or try again later.') from exc
        detail = _gemini_error_message(exc)
        raise BookkeepingExtractionError(
            f'Gemini normalization returned an error: {getattr(exc, "code", "?")}{": " + detail if detail else "."}'
        ) from exc
    except genai_errors.ServerError as exc:
        raise BookkeepingExtractionError('Could not reach Gemini while normalizing the Azure transcription.') from exc
    except genai_errors.APIError as exc:
        raise BookkeepingExtractionError(f'Gemini normalization returned an error: {getattr(exc, "code", "?")}.') from exc
    except Exception as exc:
        if 'timeout' in str(exc).lower():
            raise BookkeepingExtractionError('Gemini normalization timed out while reviewing the Azure transcription.') from exc
        raise

    try:
        payload = _extract_gemini_payload(response)
    except json.JSONDecodeError as exc:
        if _gemini_finish_reason(response) in {'MAX_TOKENS', 'LENGTH'} and retry_max_output_tokens > max_output_tokens:
            response = _request_bookkeeping_normalization_response(client, parts, retry_max_output_tokens)
            try:
                payload = _extract_gemini_payload(response)
            except json.JSONDecodeError as retry_exc:
                raise BookkeepingExtractionError('Gemini normalization returned invalid bookkeeping JSON.') from retry_exc
        else:
            raise BookkeepingExtractionError('Gemini normalization returned invalid bookkeeping JSON.') from exc

    merged = dict(azure_payload)
    for field in ('document_type', 'document_date', 'period_start', 'period_end', 'currency', 'organization_name', 'document_title', 'vendor_or_counterparty', 'summary'):
        value = payload.get(field)
        if str(value or '').strip():
            merged[field] = value

    merged['quality_flags'] = _merge_string_lists(azure_payload.get('quality_flags') or [], payload.get('quality_flags') or [])
    merged['extraction_notes'] = ' '.join(
        text.strip()
        for text in [str(azure_payload.get('extraction_notes') or ''), str(payload.get('extraction_notes') or '')]
        if text and text.strip()
    ).strip()
    merged = _merge_gemini_row_repairs(merged, payload.get('repaired_rows') or [])
    merged['bookkeeping_entries'] = payload.get('bookkeeping_entries') or []
    return merged


def _build_bookkeeping_normalization_parts(
    azure_payload: dict,
    review_snippets: list[dict],
    filename: str,
    cbo,
    related_page_upload: bool = False,
) -> list:
    review_limit = int(current_app.config.get('BOOKKEEPING_CLAUDE_REVIEW_ROW_LIMIT', 8) or 8)
    cell_limit = int(current_app.config.get('BOOKKEEPING_CLAUDE_REVIEW_CELL_LIMIT', 4) or 4)
    repair_candidates = _select_review_snippets_for_repair(review_snippets, review_limit)
    transcription_payload = {
        'organization_name': azure_payload.get('organization_name') or cbo.name,
        'document_title': azure_payload.get('document_title') or '',
        'raw_text': azure_payload.get('raw_text') or '',
        'detected_columns': azure_payload.get('detected_columns') or [],
        'transcribed_rows': azure_payload.get('transcribed_rows') or [],
        'document_confidence': azure_payload.get('document_confidence') or 0.0,
        'quality_flags': azure_payload.get('quality_flags') or [],
        'extraction_notes': azure_payload.get('extraction_notes') or '',
        'repair_candidate_row_numbers': [snippet.get('row_number') for snippet in repair_candidates if snippet.get('row_number')],
    }

    parts = [
        types.Part.from_text(
            text=(
                'Normalize bookkeeping entries from this Azure Document Intelligence transcription. '
                f'CBO: {cbo.name}. Filename: {filename}. '
                'Preserve the provided detected_columns and transcribed_rows as the source of truth. '
                'Do not invent rows or replace the source transcription.'
                + (' These images were uploaded as related pages of the same logical document.' if related_page_upload else '')
            )
        ),
        types.Part.from_text(text=json.dumps(transcription_payload, ensure_ascii=True)),
    ]

    for snippet in repair_candidates:
        image_bytes = snippet.get('image_bytes') or b''
        mime_type = str(snippet.get('mime_type') or 'image/png')
        parts.append(types.Part.from_text(
            text=(
                f"Low-confidence repair candidate row {snippet.get('row_number')} from page {snippet.get('page_number')}, "
                f"confidence {float(snippet.get('confidence') or 0.0):.2f}. "
                f"Azure row cells: {json.dumps(snippet.get('cells') or {}, ensure_ascii=True)}. "
                f"Conflict columns: {json.dumps(snippet.get('conflict_columns') or [], ensure_ascii=True)}"
            )
        ))
        parts.append(types.Part.from_text(text=str(snippet.get('label') or 'Ambiguous source row crop')))
        if image_bytes and mime_type.startswith('image/'):
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        for cell_snippet in (snippet.get('cell_snippets') or [])[:cell_limit]:
            cell_bytes = cell_snippet.get('image_bytes') or b''
            cell_mime = str(cell_snippet.get('mime_type') or 'image/png')
            parts.append(types.Part.from_text(
                text=(
                    f"Cell repair candidate for row {cell_snippet.get('row_number')} column {cell_snippet.get('column_name')}. "
                    f"Azure text: {json.dumps(cell_snippet.get('cell_text') or '', ensure_ascii=True)}. "
                    f"Confidence {float(cell_snippet.get('confidence') or 0.0):.2f}. "
                    f"Reason: {cell_snippet.get('reason') or 'Low-confidence cell'}"
                )
            ))
            parts.append(types.Part.from_text(text=str(cell_snippet.get('label') or 'Ambiguous source cell crop')))
            if cell_bytes and cell_mime.startswith('image/'):
                parts.append(types.Part.from_bytes(data=cell_bytes, mime_type=cell_mime))

    return parts


def _select_review_snippets_for_repair(review_snippets: list[dict], review_limit: int) -> list[dict]:
    def score(snippet: dict) -> tuple[float, int]:
        confidence = float(snippet.get('confidence') or 0.0)
        cell_count = len(snippet.get('cell_snippets') or [])
        conflict_count = len(snippet.get('conflict_columns') or [])
        return (confidence, -(cell_count + conflict_count))

    selected = [snippet for snippet in review_snippets if isinstance(snippet, dict) and snippet.get('repair_recommended')]
    selected.sort(key=score)
    return selected[:max(0, review_limit)]


def _merge_gemini_row_repairs(payload: dict, repaired_rows: list[dict]) -> dict:
    rows = [dict(row) for row in (payload.get('transcribed_rows') or []) if isinstance(row, dict)]
    if not rows or not repaired_rows:
        return payload

    row_map = {int(row.get('row_number') or 0): row for row in rows if int(row.get('row_number') or 0)}
    confidence_threshold = float(current_app.config.get('BOOKKEEPING_CLAUDE_ROW_REPAIR_CONFIDENCE_THRESHOLD', 0.68) or 0.68)
    repaired_count = 0

    for repaired_row in repaired_rows:
        if not isinstance(repaired_row, dict):
            continue
        row_number = int(repaired_row.get('row_number') or 0)
        original_row = row_map.get(row_number)
        if not original_row:
            continue

        repair_confidence = _clamp_confidence(repaired_row.get('confidence'))
        if repair_confidence < confidence_threshold:
            continue

        repaired_cells = _normalize_row_cells(repaired_row.get('cells'))
        if not repaired_cells:
            continue

        original_cells = dict(original_row.get('cells') or {})
        changed_columns = []
        for column_name, repaired_text in repaired_cells.items():
            normalized_column = str(column_name or '').strip()
            if not normalized_column or normalized_column not in original_cells:
                continue
            cleaned_text = str(repaired_text or '').strip()
            if cleaned_text == original_cells.get(normalized_column, ''):
                continue
            original_cells[normalized_column] = cleaned_text
            changed_columns.append(normalized_column)

        if not changed_columns:
            continue

        original_row['cells'] = original_cells
        original_row['confidence'] = max(_clamp_confidence(original_row.get('confidence')), repair_confidence)
        repair_note = str(repaired_row.get('notes') or '').strip()
        merged_note = str(original_row.get('notes') or '').strip()
        note_bits = [bit for bit in [merged_note, repair_note, f'Gemini repaired columns: {", ".join(changed_columns)}'] if bit]
        original_row['notes'] = '; '.join(_merge_string_lists([], note_bits))[:320]
        repaired_count += 1

    if repaired_count:
        _append_extraction_note(
            payload,
            f'Gemini repaired {repaired_count} low-confidence row(s) using grounded row and cell crops from the original image.'
        )
        payload['quality_flags'] = _merge_string_lists(
            payload.get('quality_flags') or [],
            [f'Gemini applied grounded repairs to {repaired_count} low-confidence row(s).'],
        )
        payload['transcribed_rows'] = list(row_map.values())
    return payload


def _request_bookkeeping_normalization_response(client: genai.Client, parts: list, max_output_tokens: int):
    return client.models.generate_content(
        model=_bookkeeping_vision_model(),
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=max_output_tokens,
            system_instruction=NORMALIZATION_PROMPT,
            response_mime_type='application/json',
            response_json_schema=BOOKKEEPING_NORMALIZATION_SCHEMA,
        ),
    )


def _merge_string_lists(primary: list[str], secondary: list[str]) -> list[str]:
    merged = []
    for group in (primary, secondary):
        for item in group:
            text = str(item or '').strip()
            if text and text not in merged:
                merged.append(text)
    return merged


def _extract_bookkeeping_document_with_mode(
    client: genai.Client,
    document_pages: list[dict],
    filename: str,
    cbo,
    related_page_upload: bool,
    variant_mode: str,
    max_output_tokens: int,
    retry_max_output_tokens: int,
) -> dict:
    parts, variant_count, total_variant_bytes = _build_bookkeeping_request_parts(
        document_pages,
        filename,
        cbo,
        related_page_upload,
        variant_mode,
    )

    current_app.logger.info(
        'Bookkeeping extraction request for %s (%s): mode=%s, %d image variants, %d bytes after Gemini optimization, timeout=%ss',
        filename,
        cbo.name,
        variant_mode,
        variant_count,
        total_variant_bytes,
        current_app.config.get('BOOKKEEPING_REQUEST_TIMEOUT', 180),
    )

    try:
        response = _request_bookkeeping_response(client, parts, max_output_tokens)
    except genai_errors.ClientError as exc:
        code = int(getattr(exc, 'code', 0) or 0)
        if code == 429:
            raise BookkeepingExtractionError('Gemini vision quota was exceeded. Check billing or try again later.') from exc
        detail = _gemini_error_message(exc)
        current_app.logger.warning('Gemini bookkeeping client error for %s: %s', filename, detail or exc)
        raise BookkeepingExtractionError(
            f'Gemini vision returned an error: {code or "?"}{": " + detail if detail else "."}'
        ) from exc
    except genai_errors.ServerError as exc:
        raise BookkeepingExtractionError('Could not reach Gemini vision service. Try again in a moment.') from exc
    except genai_errors.APIError as exc:
        raise BookkeepingExtractionError(f'Gemini vision returned an error: {getattr(exc, "code", "?")}.') from exc
    except Exception as exc:
        if 'timeout' in str(exc).lower():
            raise BookkeepingExtractionError('Gemini vision timed out while reading this document. Try a smaller file or a clearer crop.') from exc
        raise

    try:
        payload = _extract_gemini_payload(response)
    except json.JSONDecodeError as exc:
        response_text = _gemini_response_text(response)
        finish_reason = _gemini_finish_reason(response)
        current_app.logger.warning(
            'Gemini bookkeeping invalid JSON for %s: mode=%s, finish_reason=%s, text_len=%d, text_prefix=%r',
            filename,
            variant_mode,
            finish_reason,
            len(response_text),
            response_text[:300],
        )

        if finish_reason in {'MAX_TOKENS', 'LENGTH'} and retry_max_output_tokens > max_output_tokens:
            current_app.logger.info(
                'Retrying Gemini bookkeeping extraction for %s with higher max_output_tokens=%d after truncation at %d (mode=%s)',
                filename,
                retry_max_output_tokens,
                max_output_tokens,
                variant_mode,
            )
            try:
                response = _request_bookkeeping_response(client, parts, retry_max_output_tokens)
                payload = _extract_gemini_payload(response)
            except json.JSONDecodeError as retry_exc:
                retry_text = _gemini_response_text(response)
                retry_finish_reason = _gemini_finish_reason(response)
                current_app.logger.warning(
                    'Gemini bookkeeping retry still produced invalid JSON for %s: mode=%s, finish_reason=%s, text_len=%d, text_prefix=%r',
                    filename,
                    variant_mode,
                    retry_finish_reason,
                    len(retry_text),
                    retry_text[:300],
                )
                if retry_finish_reason in {'MAX_TOKENS', 'LENGTH'}:
                    raise BookkeepingExtractionError(
                        'Gemini truncated the bookkeeping JSON before finishing. This page is too dense for one pass; split the document or raise BOOKKEEPING_RETRY_MAX_OUTPUT_TOKENS.'
                    ) from retry_exc
                raise BookkeepingExtractionError('Gemini returned invalid bookkeeping JSON.') from retry_exc
        else:
            raise BookkeepingExtractionError('Gemini returned invalid bookkeeping JSON.') from exc

    normalized = refine_extracted_bookkeeping_payload(_normalize_payload(payload))
    normalized = _apply_page_order_hints(normalized, document_pages)
    normalized = _dedupe_single_amount_boundary_rows(normalized)
    if not normalized['bookkeeping_entries'] and not normalized['summary'] and not normalized['transcribed_rows']:
        raise BookkeepingExtractionError('No bookkeeping data could be extracted from this image.')

    normalized['model_used'] = _bookkeeping_vision_model()
    normalized['extraction_variant_mode'] = variant_mode
    return normalized


def _build_bookkeeping_request_parts(
    document_pages: list[dict],
    filename: str,
    cbo,
    related_page_upload: bool,
    variant_mode: str,
) -> tuple[list, int, int]:
    parts = [
        types.Part.from_text(
            text=(
                'Extract bookkeeping data from this photographed document for '
                f'{cbo.name}. The CBO identifier is {cbo.cbo_identifier or "community"}. '
                f'Filename: {filename}. This upload contains {len(document_pages)} page(s). '
                'Treat all pages as one logical document. Return the exact JSON shape from the system prompt. '
                'Do not infer per-day totals, corrected totals, or missing rows. Preserve the visible values exactly and '
                'include every readable row in top-to-bottom order.'
                + (
                    ' These pages were uploaded as related pages of the same document, so align rows across pages conservatively.'
                    if related_page_upload else
                    ''
                )
            )
        )
    ]
    variant_count = 0
    total_variant_bytes = 0

    for index, page in enumerate(document_pages, start=1):
        variants = _page_visual_variants(page, variant_mode=variant_mode)
        parts.append(types.Part.from_text(
            text=(
                f'Page {index} of {len(document_pages)}. '
                'If multiple orientations are shown below, they are alternate rotations of the same page. '
                'Use the orientation where the text is upright and keep only one copy of each visible row.'
            )
        ))
        for variant in variants:
            optimized_variant = _optimize_variant_for_vision(variant)
            variant_count += 1
            total_variant_bytes += len(optimized_variant['image_bytes'])
            parts.append(types.Part.from_text(text=str(variant.get('label') or 'Image variant')))
            parts.append(types.Part.from_bytes(
                data=optimized_variant['image_bytes'],
                mime_type=optimized_variant['mime_type'],
            ))

    return parts, variant_count, total_variant_bytes


def _extract_gemini_payload(response) -> dict:
    message = _gemini_response_text(response)
    if not message:
        raise BookkeepingExtractionError('Gemini returned an empty bookkeeping response.')
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        fragment = _extract_json_object_fragment(message)
        if fragment:
            return json.loads(fragment)
        raise


def _request_bookkeeping_response(client: genai.Client, parts: list, max_output_tokens: int):
    return client.models.generate_content(
        model=_bookkeeping_vision_model(),
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=max_output_tokens,
            system_instruction=SYSTEM_PROMPT,
            response_mime_type='application/json',
            response_json_schema=BOOKKEEPING_OUTPUT_SCHEMA,
        ),
    )


def _gemini_response_text(response) -> str:
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


def _gemini_finish_reason(response) -> str:
    try:
        candidates = getattr(response, 'candidates', None) or []
        if not candidates:
            return ''
        reason = getattr(candidates[0], 'finish_reason', None)
        return str(getattr(reason, 'name', reason) or '').strip().upper()
    except Exception:
        return ''


def _extract_json_object_fragment(text: str) -> str:
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return ''
    return text[start:end + 1]


def _gemini_error_message(exc: Exception) -> str:
    message = str(getattr(exc, 'message', '') or '').strip()
    if message:
        return message
    details = getattr(exc, 'details', None)
    if isinstance(details, dict):
        error = details.get('error') or details
        if isinstance(error, dict):
            nested = str(error.get('message') or '').strip()
            if nested:
                return nested
    return str(exc).strip()


def refine_extracted_bookkeeping_payload(payload: dict) -> dict:
    rows = payload.get('transcribed_rows') or []
    columns = payload.get('detected_columns') or []
    if _should_rebuild_tool_lending_entries(columns, rows):
        rebuilt_entries = _rebuild_tool_lending_entries(rows)
        if rebuilt_entries:
            payload['bookkeeping_entries'] = rebuilt_entries
            payload['totals'] = _rebuilt_income_totals(payload, rebuilt_entries)
            _append_extraction_note(
                payload,
                'Normalized entries were rebuilt directly from the transcribed register rows without multiplying visible fees by days rented.'
            )
        return payload

    if not _looks_like_single_amount_register(columns, rows):
        return payload

    rebuilt_entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get('cells') or {}
        if not isinstance(cells, dict):
            continue

        amount_column, amount_value = _find_row_number(cells, TOTAL_REVENUE_ALIASES)
        if amount_value is None:
            continue
        description = f'Row {row.get("row_number") or (len(rebuilt_entries) + 1)} total revenue'
        rebuilt_entries.append({
            'entry_date': '',
            'description': description,
            'amount': amount_value,
            'quantity': 1.0,
            'unit': amount_column or '',
            'entry_type': 'income',
            'category': 'sales',
            'direction': 'inflow',
            'reference': '',
            'confidence': _clamp_confidence(row.get('confidence')),
        })

    if not rebuilt_entries:
        return payload

    payload['bookkeeping_entries'] = rebuilt_entries
    payload['totals'] = _rebuilt_income_totals(payload, rebuilt_entries)
    _append_extraction_note(
        payload,
        'Normalized entries were rebuilt directly from the visible total revenue rows.'
    )
    return payload


def _normalize_payload(payload: dict) -> dict:
    entries = []
    for raw_entry in payload.get('bookkeeping_entries') or []:
        if not isinstance(raw_entry, dict):
            continue
        amount = _coerce_amount(raw_entry.get('amount'))
        entry_type = _normalize_choice(raw_entry.get('entry_type'), ALLOWED_ENTRY_TYPES, 'unknown')
        direction = _normalize_choice(raw_entry.get('direction'), ALLOWED_DIRECTIONS, _infer_direction(entry_type, amount))
        category = _normalize_choice(raw_entry.get('category'), ALLOWED_CATEGORIES, 'other')
        confidence = _clamp_confidence(raw_entry.get('confidence'))
        entries.append({
            'entry_date': _normalize_date(raw_entry.get('entry_date')),
            'description': str(raw_entry.get('description') or '').strip(),
            'amount': amount,
            'quantity': _coerce_amount(raw_entry.get('quantity')),
            'unit': str(raw_entry.get('unit') or '').strip(),
            'entry_type': entry_type,
            'category': category,
            'direction': direction,
            'reference': str(raw_entry.get('reference') or '').strip(),
            'confidence': confidence,
            'source_row_numbers': [
                int(value)
                for value in (raw_entry.get('source_row_numbers') or [])
                if str(value or '').strip().isdigit()
            ],
        })

    totals = payload.get('totals') or {}
    income_total = _coerce_amount(totals.get('income'))
    expense_total = _coerce_amount(totals.get('expenses'))
    net_total = _coerce_amount(totals.get('net'))

    computed_income = sum(entry['amount'] for entry in entries if entry['direction'] == 'inflow' or entry['entry_type'] == 'income')
    computed_expenses = sum(entry['amount'] for entry in entries if entry['direction'] == 'outflow' or entry['entry_type'] == 'expense')

    income_total = income_total or computed_income
    expense_total = expense_total or computed_expenses
    net_total = net_total or round(income_total - expense_total, 2)

    document_confidence = _clamp_confidence(payload.get('document_confidence'))
    if not document_confidence and entries:
        document_confidence = round(mean(entry['confidence'] for entry in entries if entry['confidence'] is not None), 4)

    quality_flags = []
    for flag in payload.get('quality_flags') or []:
        text = str(flag or '').strip()
        if text and text not in quality_flags:
            quality_flags.append(text)

    detected_columns = []
    for column in payload.get('detected_columns') or []:
        text = str(column or '').strip()
        if text and text not in detected_columns:
            detected_columns.append(text)

    transcribed_rows = []
    for raw_row in payload.get('transcribed_rows') or []:
        if not isinstance(raw_row, dict):
            continue
        row_cells = _normalize_row_cells(raw_row.get('cells'))
        row = {
            'row_number': int(raw_row.get('row_number') or 0),
            'row_type': str(raw_row.get('row_type') or 'other').strip().lower() or 'other',
            'cells': {str(key).strip(): str(value or '').strip() for key, value in row_cells.items() if str(key).strip()},
            'signature_cells': _normalize_signature_cells(raw_row.get('signature_cells')),
            'notes': str(raw_row.get('notes') or '').strip(),
            'confidence': _clamp_confidence(raw_row.get('confidence')),
        }
        if _is_header_like_row(row, detected_columns):
            continue
        transcribed_rows.append(row)

    if not document_confidence and transcribed_rows:
        document_confidence = round(mean(row['confidence'] for row in transcribed_rows if row['confidence'] is not None), 4)

    return {
        'document_type': _normalize_choice(payload.get('document_type'), {'ledger', 'receipt', 'invoice', 'cashbook', 'bank_statement', 'expense_sheet', 'inventory_register', 'member_register', 'savings_register', 'loan_register', 'unknown'}, 'unknown'),
        'document_date': _normalize_date(payload.get('document_date')),
        'period_start': _normalize_date(payload.get('period_start')),
        'period_end': _normalize_date(payload.get('period_end')),
        'currency': (str(payload.get('currency') or 'KES').strip() or 'KES').upper(),
        'organization_name': str(payload.get('organization_name') or '').strip(),
        'document_title': str(payload.get('document_title') or '').strip(),
        'vendor_or_counterparty': str(payload.get('vendor_or_counterparty') or '').strip(),
        'summary': str(payload.get('summary') or '').strip(),
        'raw_text': str(payload.get('raw_text') or '').strip(),
        'detected_columns': detected_columns,
        'transcribed_rows': transcribed_rows,
        'document_confidence': document_confidence,
        'quality_flags': quality_flags,
        'extraction_notes': str(payload.get('extraction_notes') or '').strip(),
        'totals': {
            'income': round(income_total, 2),
            'expenses': round(expense_total, 2),
            'net': round(net_total, 2),
        },
        'bookkeeping_entries': entries,
    }


def _coerce_amount(value) -> float:
    if value in (None, ''):
        return 0.0
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    cleaned = str(value).replace(',', '').replace('KSh', '').replace('KES', '').strip()
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return 0.0


def _clamp_confidence(value) -> float:
    if value in (None, ''):
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric > 1:
        numeric = numeric / 100.0
    return round(max(0.0, min(1.0, numeric)), 4)


def _normalize_date(value) -> str:
    text = str(value or '').strip()
    if len(text) == 10 and text[4:5] == '-' and text[7:8] == '-':
        return text
    return ''


def _normalize_choice(value, allowed: set[str], default: str) -> str:
    normalized = str(value or '').strip().lower().replace(' ', '_')
    return normalized if normalized in allowed else default


def _infer_direction(entry_type: str, amount: float) -> str:
    if entry_type == 'income':
        return 'inflow'
    if entry_type == 'expense':
        return 'outflow'
    if amount == 0:
        return 'neutral'
    return 'neutral'


def _is_header_like_row(row: dict, detected_columns: list[str]) -> bool:
    cells = row.get('cells') or {}
    if not cells:
        return str(row.get('row_type') or '').strip().lower() == 'header'

    normalized_columns = {_normalize_token(value) for value in detected_columns if _normalize_token(value)}
    matches = 0
    populated = 0
    for key, value in cells.items():
        normalized_key = _normalize_token(key)
        normalized_value = _normalize_token(value)
        if not normalized_value:
            continue
        populated += 1
        if normalized_value == normalized_key or normalized_value in normalized_columns:
            matches += 1

    if populated == 0:
        return str(row.get('row_type') or '').strip().lower() == 'header'

    if str(row.get('row_type') or '').strip().lower() == 'header' and matches == 0:
        return False

    return populated > 0 and matches >= max(2, int(populated * 0.6))


def _normalize_token(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _normalize_row_cells(raw_cells) -> dict[str, str]:
    if isinstance(raw_cells, dict):
        return {
            str(key).strip(): str(value or '').strip()
            for key, value in raw_cells.items()
            if str(key).strip()
        }

    if not isinstance(raw_cells, list):
        return {}

    normalized = {}
    for item in raw_cells:
        if not isinstance(item, dict):
            continue
        column_name = str(item.get('column_name') or '').strip()
        if not column_name:
            continue
        normalized[column_name] = str(item.get('cell_text') or '').strip()
    return normalized


def _normalize_signature_cells(raw_signature_cells) -> dict[str, dict]:
    if not isinstance(raw_signature_cells, dict):
        return {}

    normalized = {}
    for column_name, payload in raw_signature_cells.items():
        normalized_column = str(column_name or '').strip()
        if not normalized_column or not isinstance(payload, dict):
            continue
        data_uri = str(payload.get('data_uri') or '').strip()
        mime_type = str(payload.get('mime_type') or '').strip()
        if not data_uri:
            continue
        normalized[normalized_column] = {
            'data_uri': data_uri,
            'mime_type': mime_type or 'image/png',
        }
    return normalized


def _looks_like_tool_lending_register(columns: list[str], rows: list[dict]) -> bool:
    normalized_columns = {_normalize_token(value) for value in columns if _normalize_token(value)}
    required = [TOOL_RATE_ALIASES, TOOL_DAYS_ALIASES, TOOL_NAME_ALIASES, TOOL_START_DATE_ALIASES]
    return all(
        any(any(alias == column or alias in column for column in normalized_columns) for alias in aliases)
        for aliases in required
    ) and bool(rows)


def _should_rebuild_tool_lending_entries(columns: list[str], rows: list[dict]) -> bool:
    if not _looks_like_tool_lending_register(columns, rows):
        return False

    fee_rows = 0
    dated_rows = 0
    identifiable_rows = 0
    eligible_rows = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get('cells') or {}
        if not isinstance(cells, dict):
            continue

        _, fee_value = _find_row_number(cells, TOOL_RATE_ALIASES)
        _, start_date = _find_row_text(cells, TOOL_START_DATE_ALIASES)
        _, end_date = _find_row_text(cells, TOOL_END_DATE_ALIASES)
        _, tool_name = _find_row_text(cells, TOOL_NAME_ALIASES)
        _, person_name = _find_row_text(cells, TOOL_PERSON_ALIASES)

        if fee_value is not None:
            fee_rows += 1
        if start_date or end_date:
            dated_rows += 1
        if tool_name or person_name:
            identifiable_rows += 1
        if fee_value is not None and (start_date or end_date) and (tool_name or person_name):
            eligible_rows += 1

    minimum_rows = max(2, min(4, max(1, len(rows) // 3)))
    return (
        fee_rows >= minimum_rows and
        dated_rows >= minimum_rows and
        identifiable_rows >= minimum_rows and
        eligible_rows >= minimum_rows
    )


def _looks_like_single_amount_register(columns: list[str], rows: list[dict]) -> bool:
    if _looks_like_tool_lending_register(columns, rows):
        return False

    normalized_columns = {_normalize_token(value) for value in columns if _normalize_token(value)}
    if not rows:
        return False

    # The row-order reversal logic is only intended for narrow single-amount sheets,
    # not wide multi-column ledgers that happen to include an amount column.
    if len(normalized_columns) > 3:
        return False

    populated_counts = [len((row.get('cells') or {})) for row in rows if isinstance((row.get('cells') or {}), dict)]
    average_populated = (sum(populated_counts) / len(populated_counts)) if populated_counts else 0.0
    if average_populated > 2.2:
        return False

    return any(
        any(alias == column or alias in column for column in normalized_columns)
        for alias in TOTAL_REVENUE_ALIASES
    )


def _rebuild_tool_lending_entries(rows: list[dict]) -> list[dict]:
    rebuilt_entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get('cells') or {}
        if not isinstance(cells, dict):
            continue

        fee_column, fee_value = _find_row_number(cells, TOOL_RATE_ALIASES)
        if fee_value is None:
            continue
        days_column, day_count = _find_row_number(cells, TOOL_DAYS_ALIASES)
        tool_column, tool_name = _find_row_text(cells, TOOL_NAME_ALIASES)
        person_column, person_name = _find_row_text(cells, TOOL_PERSON_ALIASES)
        id_column, reference = _find_row_text(cells, TOOL_ID_ALIASES)
        start_column, start_date = _find_row_text(cells, TOOL_START_DATE_ALIASES)
        end_column, end_date = _find_row_text(cells, TOOL_END_DATE_ALIASES)

        description_parts = ['Tool lending fee']
        if person_name:
            description_parts.append(f'from {person_name}')
        if tool_name:
            description_parts.append(f'for {tool_name}')

        rebuilt_entries.append({
            'entry_date': _normalize_date(end_date) or _normalize_date(start_date),
            'description': ' '.join(description_parts),
            'amount': fee_value,
            'quantity': day_count or 0.0,
            'unit': days_column or '',
            'entry_type': 'income',
            'category': 'rent',
            'direction': 'inflow',
            'reference': reference or '',
            'confidence': _clamp_confidence(row.get('confidence')),
        })

    return rebuilt_entries


def _rebuilt_income_totals(payload: dict, rebuilt_entries: list[dict]) -> dict:
    income_total = round(sum(entry['amount'] for entry in rebuilt_entries), 2)
    expenses = round(_coerce_amount(((payload.get('totals') or {}).get('expenses'))), 2)
    return {
        'income': income_total,
        'expenses': expenses,
        'net': round(income_total - expenses, 2),
    }


def _append_extraction_note(payload: dict, note: str) -> None:
    extraction_notes = str(payload.get('extraction_notes') or '').strip()
    if note not in extraction_notes:
        payload['extraction_notes'] = f'{extraction_notes} {note}'.strip()


def _apply_page_order_hints(payload: dict, document_pages: list[dict]) -> dict:
    if not _should_reverse_single_amount_rows(payload, document_pages):
        return payload

    reversed_rows = list(reversed(payload.get('transcribed_rows') or []))
    for index, row in enumerate(reversed_rows, start=1):
        if isinstance(row, dict):
            row['row_number'] = index
    payload['transcribed_rows'] = reversed_rows

    reversed_entries = list(reversed(payload.get('bookkeeping_entries') or []))
    for index, entry in enumerate(reversed_entries, start=1):
        if isinstance(entry, dict) and str(entry.get('description') or '').startswith('Row '):
            entry['description'] = f'Row {index} total revenue'
    payload['bookkeeping_entries'] = reversed_entries

    _append_extraction_note(
        payload,
        'Row order was reversed to match the original sideways page layout from the uploaded photo.'
    )
    return payload


def _dedupe_single_amount_boundary_rows(payload: dict) -> dict:
    rows = payload.get('transcribed_rows') or []
    entries = payload.get('bookkeeping_entries') or []
    columns = payload.get('detected_columns') or []

    if not _looks_like_single_amount_register(columns, rows):
        return payload
    if len(rows) < 2 or len(rows) != len(entries):
        return payload

    drop_index = None
    if _single_amount_row_value(rows[-1]) == _single_amount_row_value(rows[-2]):
        drop_index = len(rows) - 1
    elif _single_amount_row_value(rows[0]) == _single_amount_row_value(rows[1]):
        drop_index = 0

    if drop_index is None:
        return payload

    deduped_rows = [row for index, row in enumerate(rows) if index != drop_index]
    deduped_entries = [entry for index, entry in enumerate(entries) if index != drop_index]

    for index, row in enumerate(deduped_rows, start=1):
        if isinstance(row, dict):
            row['row_number'] = index
    for index, entry in enumerate(deduped_entries, start=1):
        if isinstance(entry, dict) and str(entry.get('description') or '').startswith('Row '):
            entry['description'] = f'Row {index} total revenue'

    payload['transcribed_rows'] = deduped_rows
    payload['bookkeeping_entries'] = deduped_entries
    payload['totals'] = _rebuilt_income_totals(payload, deduped_entries)
    _append_extraction_note(
        payload,
        'A likely duplicated boundary row was removed from the single-column revenue sheet.'
    )
    return payload


def _page_visual_variants(page: dict, variant_mode: str = 'minimal') -> list[dict]:
    base_variant = {
        'label': 'Original upload orientation',
        'mime_type': page['mime_type'],
        'image_bytes': page['image_bytes'],
    }

    variants = [base_variant]

    enhanced_variant = _enhanced_page_variant(page)
    if enhanced_variant:
        variants.append(enhanced_variant)

    if variant_mode == 'detailed':
        variants.extend(_column_crop_variants(page))
        variants.extend(_rotated_page_variants(page))

    return variants


def _should_run_detailed_bookkeeping_retry(payload: dict, document_pages: list[dict]) -> bool:
    if not any(_is_wide_ledger_page(page) for page in document_pages):
        return False

    rows = [row for row in (payload.get('transcribed_rows') or []) if isinstance(row, dict)]
    columns = [column for column in (payload.get('detected_columns') or []) if str(column or '').strip()]
    document_confidence = _clamp_confidence(payload.get('document_confidence'))

    if not rows:
        return True

    populated_counts = [len((row.get('cells') or {})) for row in rows if isinstance(row.get('cells'), dict)]
    average_populated = (sum(populated_counts) / len(populated_counts)) if populated_counts else 0.0
    low_confidence_rows = sum(1 for row in rows if _clamp_confidence(row.get('confidence')) < 0.55)

    if document_confidence < 0.58:
        return True
    if len(columns) >= 8 and average_populated < max(3.0, len(columns) * 0.55):
        return True
    if low_confidence_rows >= max(2, len(rows) // 3):
        return True

    return False


def _is_better_bookkeeping_result(candidate: dict, baseline: dict) -> bool:
    candidate_rows = [row for row in (candidate.get('transcribed_rows') or []) if isinstance(row, dict)]
    baseline_rows = [row for row in (baseline.get('transcribed_rows') or []) if isinstance(row, dict)]

    candidate_row_count = len(candidate_rows)
    baseline_row_count = len(baseline_rows)
    if candidate_row_count > baseline_row_count:
        return True
    if candidate_row_count < baseline_row_count:
        return False

    candidate_columns = len(candidate.get('detected_columns') or [])
    baseline_columns = len(baseline.get('detected_columns') or [])

    candidate_populated = _average_populated_cells(candidate_rows)
    baseline_populated = _average_populated_cells(baseline_rows)
    if candidate_columns > baseline_columns and candidate_populated >= baseline_populated * 0.8:
        return True

    candidate_confidence = _clamp_confidence(candidate.get('document_confidence'))
    baseline_confidence = _clamp_confidence(baseline.get('document_confidence'))
    return candidate_confidence > baseline_confidence + 0.08


def _average_populated_cells(rows: list[dict]) -> float:
    counts = [len((row.get('cells') or {})) for row in rows if isinstance(row.get('cells'), dict)]
    if not counts:
        return 0.0
    return sum(counts) / len(counts)


def _is_wide_ledger_page(page: dict) -> bool:
    if not str(page.get('mime_type') or '').startswith('image/'):
        return False

    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            return width / max(height, 1) >= 1.45
    except Exception:
        return False


def _optimize_variant_for_vision(variant: dict) -> dict:
    mime_type = str(variant.get('mime_type') or '')
    image_bytes = variant.get('image_bytes') or b''
    if not mime_type.startswith('image/') or not image_bytes:
        return variant

    max_edge = int(current_app.config.get('BOOKKEEPING_MAX_IMAGE_EDGE', 1568) or 1568)
    jpeg_quality = int(current_app.config.get('BOOKKEEPING_IMAGE_QUALITY', 85) or 85)

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            longest_edge = max(width, height)

            if longest_edge > max_edge:
                scale = max_edge / float(longest_edge)
                normalized = normalized.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )

            if normalized.mode not in ('RGB', 'L'):
                normalized = normalized.convert('RGB')
            elif normalized.mode == 'L':
                normalized = normalized.convert('RGB')

            buffer = BytesIO()
            normalized.save(buffer, format='JPEG', quality=jpeg_quality, optimize=True)
            return {
                'label': variant.get('label') or 'Image variant',
                'mime_type': 'image/jpeg',
                'image_bytes': buffer.getvalue(),
            }
    except Exception:
        return variant


def _should_reverse_single_amount_rows(payload: dict, document_pages: list[dict]) -> bool:
    if len(document_pages) != 1:
        return False
    if not _looks_like_single_amount_register(payload.get('detected_columns') or [], payload.get('transcribed_rows') or []):
        return False

    page = document_pages[0]
    if not str(page.get('mime_type') or '').startswith('image/'):
        return False

    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            return width > height
    except Exception:
        return False


def _rotated_page_variants(page: dict) -> list[dict]:
    if not str(page.get('mime_type') or '').startswith('image/'):
        return []

    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            if width <= height:
                return []

            # Wide ledger pages are often intentionally landscape. Prefer zoomed crops
            # over redundant 90-degree rotations for those documents.
            if width / max(height, 1) >= 1.6:
                return []

            clockwise = _render_rotated_variant(normalized, -90)
            counterclockwise = _render_rotated_variant(normalized, 90)
            return [
                {
                    'label': 'Same page rotated 90 degrees clockwise',
                    'mime_type': 'image/png',
                    'image_bytes': clockwise,
                },
                {
                    'label': 'Same page rotated 90 degrees counterclockwise',
                    'mime_type': 'image/png',
                    'image_bytes': counterclockwise,
                },
            ]
    except Exception:
        return []


def _enhanced_page_variant(page: dict) -> dict | None:
    if not str(page.get('mime_type') or '').startswith('image/'):
        return None

    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            grayscale = normalized.convert('L')
            contrast = ImageEnhance.Contrast(grayscale).enhance(1.8)
            sharpened = contrast.filter(ImageFilter.SHARPEN).filter(ImageFilter.SHARPEN)
            buffer = BytesIO()
            sharpened.save(buffer, format='PNG')
            return {
                'label': 'Enhanced high-contrast variant for difficult handwriting',
                'mime_type': 'image/png',
                'image_bytes': buffer.getvalue(),
            }
    except Exception:
        return None


def _column_crop_variants(page: dict) -> list[dict]:
    if not str(page.get('mime_type') or '').startswith('image/'):
        return []

    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            aspect_ratio = width / max(height, 1)
            if aspect_ratio < 1.45:
                return []

            crop_ranges = [
                ('Zoomed crop of the left-side columns', 0.00, 0.42),
                ('Zoomed crop of the middle columns', 0.29, 0.71),
                ('Zoomed crop of the right-side columns', 0.58, 1.00),
            ]

            variants = []
            for label, start_ratio, end_ratio in crop_ranges:
                left = int(width * start_ratio)
                right = int(width * end_ratio)
                if right - left < max(200, width // 5):
                    continue
                crop = normalized.crop((left, 0, right, height))
                buffer = BytesIO()
                crop.save(buffer, format='PNG')
                variants.append({
                    'label': label,
                    'mime_type': 'image/png',
                    'image_bytes': buffer.getvalue(),
                })

            return variants
    except Exception:
        return []


def _render_rotated_variant(image: Image.Image, angle: int) -> bytes:
    rotated = image.rotate(angle, expand=True)
    if rotated.mode not in ('RGB', 'RGBA'):
        rotated = rotated.convert('RGB')
    buffer = BytesIO()
    rotated.save(buffer, format='PNG')
    return buffer.getvalue()


def _single_amount_row_value(row: dict) -> float | None:
    if not isinstance(row, dict):
        return None
    cells = row.get('cells') or {}
    if not isinstance(cells, dict):
        return None
    _, amount = _find_row_number(cells, TOTAL_REVENUE_ALIASES)
    return amount


def _find_row_text(cells: dict, aliases: tuple[str, ...]) -> tuple[str | None, str | None]:
    for column, value in cells.items():
        normalized = _normalize_token(column)
        if any(alias == normalized or alias in normalized for alias in aliases):
            text = str(value or '').strip()
            return column, text or None
    return None, None


def _find_row_number(cells: dict, aliases: tuple[str, ...]) -> tuple[str | None, float | None]:
    column, text = _find_row_text(cells, aliases)
    return column, _coerce_amount(text) if text else None