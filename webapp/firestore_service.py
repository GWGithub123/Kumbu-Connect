"""Optional Firestore mirroring for community feedback data."""
import json
from datetime import datetime

from flask import current_app

from .community_feedback_keywords import get_cbo_keyword, normalize_sms_keyword

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None


_BOOKKEEPING_FIRESTORE_RAW_TEXT_LIMIT = 2000
_BOOKKEEPING_FIRESTORE_NOTES_LIMIT = 25
_BOOKKEEPING_FIRESTORE_ISSUES_LIMIT = 12
_BOOKKEEPING_FIRESTORE_FLAGGED_CELLS_LIMIT = 20
_BOOKKEEPING_FIRESTORE_ROWS_LIMIT = 8
_BOOKKEEPING_FIRESTORE_ENTRIES_LIMIT = 20


def sync_subscriber_to_firestore(subscriber) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    cbo_ref = _get_cbo_ref(client, subscriber.cbo)
    _write_cbo_metadata(cbo_ref, subscriber.cbo)
    subscriber_ref = cbo_ref.collection('community_subscribers').document(str(subscriber.id))
    subscriber_ref.set({
        'subscriber_id': subscriber.id,
        'cbo_id': subscriber.cbo_id,
        'cbo_name': subscriber.cbo.name,
        'cbo_keyword': _get_cbo_firestore_key(subscriber.cbo),
        'cbo_slug': subscriber.cbo.slug,
        'phone_number': subscriber.phone_number,
        'status': subscriber.status,
        'signup_keyword': subscriber.signup_keyword,
        'signup_source': subscriber.signup_source,
        'conversation_state': subscriber.conversation_state,
        'added_at': _iso(subscriber.created_at),
        'consent_received_at': _iso(subscriber.consent_received_at),
        'last_response_at': _iso(subscriber.last_response_at),
        'last_checkin_sent_at': _iso(subscriber.last_checkin_sent_at),
        'created_at': _iso(subscriber.created_at),
        'updated_at': _iso(subscriber.updated_at),
    }, merge=True)
    return True


def sync_feedback_to_firestore(subscriber, feedback) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    try:
        transcript = json.loads(feedback.raw_transcript or '[]')
    except (json.JSONDecodeError, TypeError):
        transcript = []

    cbo_ref = _get_cbo_ref(client, subscriber.cbo)
    _write_cbo_metadata(cbo_ref, subscriber.cbo)
    feedback_ref = cbo_ref.collection('community_feedback').document(str(feedback.id))
    feedback_ref.set({
        'feedback_id': feedback.id,
        'subscriber_id': feedback.subscriber_id,
        'cbo_id': feedback.cbo_id,
        'cbo_name': subscriber.cbo.name,
        'cbo_keyword': _get_cbo_firestore_key(subscriber.cbo),
        'phone_number': subscriber.phone_number,
        'cycle_type': feedback.cycle_type,
        'delivery_channel': feedback.delivery_channel,
        'questionnaire_version': feedback.questionnaire_version,
        'status': feedback.status,
        'rating': feedback.rating,
        'help_count': feedback.help_count,
        'anecdote': feedback.anecdote,
        'raw_transcript': transcript,
        'added_at': _iso(feedback.created_at),
        'submitted_at': _iso(feedback.completed_at or feedback.created_at),
        'started_at': _iso(feedback.started_at),
        'completed_at': _iso(feedback.completed_at),
        'follow_up_due_at': _iso(feedback.follow_up_due_at),
        'created_at': _iso(feedback.created_at),
        'updated_at': _iso(feedback.updated_at),
    }, merge=True)
    feedback.firestore_synced_at = datetime.utcnow()
    from .models import db
    db.session.commit()
    return True


def sync_bookkeeping_document_to_firestore(bookkeeping_document) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    extracted = _safe_json(bookkeeping_document.extracted_data_json)
    cbo_ref = _get_cbo_ref(client, bookkeeping_document.cbo)
    _write_cbo_metadata(cbo_ref, bookkeeping_document.cbo)
    document_ref = cbo_ref.collection('bookkeeping_documents').document(str(bookkeeping_document.id))
    try:
        document_ref.set({
            'bookkeeping_document_id': bookkeeping_document.id,
            'cbo_id': bookkeeping_document.cbo_id,
            'cbo_name': bookkeeping_document.cbo.name,
            'cbo_keyword': _get_cbo_firestore_key(bookkeeping_document.cbo),
            'upload_batch_id': getattr(bookkeeping_document, 'upload_batch_id', ''),
            'original_filename': bookkeeping_document.original_filename,
            'stored_path': bookkeeping_document.stored_path,
            'storage_backend': getattr(bookkeeping_document, 'storage_backend', 'local'),
            'storage_bucket': getattr(bookkeeping_document, 'storage_bucket', ''),
            'storage_object_path': getattr(bookkeeping_document, 'storage_object_path', ''),
            'mime_type': bookkeeping_document.mime_type,
            'source_channel': bookkeeping_document.source_channel,
            'include_in_workspace': bool(getattr(bookkeeping_document, 'include_in_workspace', False)),
            'workspace_period_key': getattr(bookkeeping_document, 'workspace_period_key', ''),
            'document_type': bookkeeping_document.document_type,
            'document_date': bookkeeping_document.document_date,
            'period_start': bookkeeping_document.period_start,
            'period_end': bookkeeping_document.period_end,
            'vendor_or_counterparty': bookkeeping_document.vendor_or_counterparty,
            'currency': bookkeeping_document.currency,
            'summary_text': bookkeeping_document.summary_text,
            'extraction_confidence': bookkeeping_document.extraction_confidence,
            'total_income': bookkeeping_document.total_income,
            'total_expenses': bookkeeping_document.total_expenses,
            'net_amount': bookkeeping_document.net_amount,
            'extracted_data': _compact_bookkeeping_extracted_for_firestore(extracted),
            'processed_at': _iso(bookkeeping_document.processed_at),
            'created_at': _iso(bookkeeping_document.created_at),
            'updated_at': _iso(bookkeeping_document.updated_at),
        }, merge=True)
        bookkeeping_document.firestore_synced_at = datetime.utcnow()
        from .models import db
        db.session.commit()
        return True
    except Exception:
        current_app.logger.exception(
            'Failed to mirror bookkeeping document %s to Firestore',
            bookkeeping_document.id,
        )
        return False


def sync_bookkeeping_summary_to_firestore(cbo, summary: dict) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    cbo_ref = _get_cbo_ref(client, cbo)
    _write_cbo_metadata(cbo_ref, cbo)
    cbo_ref.set({
        'bookkeeping_summary': summary,
        'bookkeeping_updated_at': datetime.utcnow().isoformat(),
    }, merge=True)
    return True


def delete_bookkeeping_document_from_firestore(cbo, document_id: int) -> bool:
    client = _get_firestore_client()
    if not client:
        return False

    for document_ref in _get_bookkeeping_document_refs(client, cbo, document_id):
        document_ref.delete()
    return True


def get_feedback_document_path(cbo) -> str:
    return f"cbos/{_get_cbo_firestore_key(cbo)}"


def _get_firestore_client():
    if not firebase_admin:
        return None

    service_account_path = current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not service_account_path:
        return None

    app_name = 'kumbu-connect-firestore'
    try:
        app = firebase_admin.get_app(app_name)
    except ValueError:
        options = {}
        project_id = current_app.config.get('FIREBASE_PROJECT_ID')
        if project_id:
            options['projectId'] = project_id
        app = firebase_admin.initialize_app(
            credentials.Certificate(service_account_path),
            options=options,
            name=app_name,
        )
    return firestore.client(app=app)


def _get_cbo_ref(client, cbo):
    return client.collection('cbos').document(_get_cbo_firestore_key(cbo))


def _get_bookkeeping_document_refs(client, cbo, document_id: int):
    seen_paths = set()
    document_refs = []

    for cbo_ref in _get_candidate_cbo_refs(client, cbo):
        document_ref = cbo_ref.collection('bookkeeping_documents').document(str(document_id))
        if document_ref.path in seen_paths:
            continue
        seen_paths.add(document_ref.path)
        document_refs.append(document_ref)

    return document_refs


def _get_candidate_cbo_refs(client, cbo):
    seen_paths = set()
    refs = []

    for key in _get_candidate_cbo_firestore_keys(cbo):
        cbo_ref = client.collection('cbos').document(key)
        if cbo_ref.path in seen_paths:
            continue
        seen_paths.add(cbo_ref.path)
        refs.append(cbo_ref)

    for snapshot in client.collection('cbos').where('cbo_id', '==', cbo.id).stream():
        if snapshot.reference.path in seen_paths:
            continue
        seen_paths.add(snapshot.reference.path)
        refs.append(snapshot.reference)

    return refs


def _get_candidate_cbo_firestore_keys(cbo) -> list[str]:
    candidates = []

    current = _get_cbo_firestore_key(cbo)
    if current:
        candidates.append(current)

    legacy_values = [
        getattr(cbo, 'sms_keyword', ''),
        getattr(cbo, 'cbo_identifier', ''),
        getattr(cbo, 'slug', ''),
        str(getattr(cbo, 'id', '') or ''),
    ]
    for value in legacy_values:
        normalized = normalize_sms_keyword(value)
        if normalized:
            candidates.append(normalized)

    seen = set()
    ordered = []
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _get_cbo_firestore_key(cbo) -> str:
    return get_cbo_keyword(cbo)


def _write_cbo_metadata(cbo_ref, cbo):
    cbo_ref.set({
        'cbo_id': cbo.id,
        'cbo_name': cbo.name,
        'cbo_keyword': _get_cbo_firestore_key(cbo),
        'cbo_slug': cbo.slug,
        'cbo_identifier': cbo.cbo_identifier,
        'tool_inventory_total': cbo.tool_inventory_total,
        'community_feedback_enabled': cbo.community_feedback_enabled,
        'community_prompt': cbo.community_prompt,
        'created_at': _iso(cbo.created_at),
        'updated_at': _iso(cbo.updated_at),
    }, merge=True)


def _iso(value):
    return value.isoformat() if value else None


def _truncate_text(value, limit: int) -> str:
    text = str(value or '').strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + '...'


def _compact_bookkeeping_extracted_for_firestore(extracted: dict) -> dict:
    audit = extracted.get('audit') or {}
    transcribed_rows = extracted.get('transcribed_rows') or []
    bookkeeping_entries = extracted.get('bookkeeping_entries') or []

    compact_rows = []
    for row in transcribed_rows[:_BOOKKEEPING_FIRESTORE_ROWS_LIMIT]:
        compact_rows.append({
            'row_number': row.get('row_number'),
            'row_type': row.get('row_type'),
            'confidence': row.get('confidence'),
            'cell_count': len(row.get('cells') or []),
            'notes': _truncate_text(row.get('notes'), 280),
        })

    compact_entries = []
    for entry in bookkeeping_entries[:_BOOKKEEPING_FIRESTORE_ENTRIES_LIMIT]:
        compact_entries.append({
            'entry_date': entry.get('entry_date'),
            'description': _truncate_text(entry.get('description'), 240),
            'category': entry.get('category'),
            'money_in': entry.get('money_in'),
            'money_out': entry.get('money_out'),
            'balance': entry.get('balance'),
            'confidence': entry.get('confidence'),
        })

    compact_issues = []
    for issue in (audit.get('issues') or [])[:_BOOKKEEPING_FIRESTORE_ISSUES_LIMIT]:
        compact_issues.append({
            'code': issue.get('code'),
            'message': _truncate_text(issue.get('message'), 280),
            'row_number': issue.get('row_number'),
            'columns': list(issue.get('columns') or [])[:8],
            'severity': issue.get('severity'),
        })

    compact_flagged_cells = []
    for cell in (audit.get('flagged_cells') or [])[:_BOOKKEEPING_FIRESTORE_FLAGGED_CELLS_LIMIT]:
        compact_flagged_cells.append({
            'row_number': cell.get('row_number'),
            'column': cell.get('column'),
            'code': cell.get('code'),
            'message': _truncate_text(cell.get('message'), 220),
        })

    return {
        'firestore_compacted': True,
        'document_type': extracted.get('document_type'),
        'document_title': extracted.get('document_title'),
        'document_date': extracted.get('document_date'),
        'period_start': extracted.get('period_start'),
        'period_end': extracted.get('period_end'),
        'vendor_or_counterparty': extracted.get('vendor_or_counterparty'),
        'organization_name': extracted.get('organization_name'),
        'currency': extracted.get('currency'),
        'summary': extracted.get('summary'),
        'totals': extracted.get('totals') or {},
        'document_confidence': extracted.get('document_confidence'),
        'transcription_provider': extracted.get('transcription_provider'),
        'normalization_provider': extracted.get('normalization_provider'),
        'extraction_pipeline': extracted.get('extraction_pipeline'),
        'review_snippet_count': extracted.get('review_snippet_count'),
        'related_page_upload': bool(extracted.get('related_page_upload')),
        'detected_columns': list(extracted.get('detected_columns') or [])[:50],
        'quality_flags': list(extracted.get('quality_flags') or [])[:50],
        'raw_text_excerpt': _truncate_text(extracted.get('raw_text'), _BOOKKEEPING_FIRESTORE_RAW_TEXT_LIMIT),
        'extraction_notes_preview': [
            _truncate_text(note, 220)
            for note in (extracted.get('extraction_notes') or [])[:_BOOKKEEPING_FIRESTORE_NOTES_LIMIT]
        ],
        'transcribed_row_count': len(transcribed_rows),
        'transcribed_rows_preview': compact_rows,
        'bookkeeping_entry_count': len(bookkeeping_entries),
        'bookkeeping_entries_preview': compact_entries,
        'audit': {
            'issue_count': len(audit.get('issues') or []),
            'flagged_cell_count': len(audit.get('flagged_cells') or []),
            'issues_preview': compact_issues,
            'flagged_cells_preview': compact_flagged_cells,
        },
    }


def _safe_json(text: str | None) -> dict:
    try:
        value = json.loads(text or '{}')
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}