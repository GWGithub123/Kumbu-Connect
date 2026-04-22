"""Azure Document Intelligence helpers for bookkeeping transcription."""
import base64
import re
import time
from io import BytesIO
from statistics import mean

import requests
from flask import current_app
from PIL import Image, ImageOps


class AzureDocumentIntelligenceError(RuntimeError):
    """Raised when Azure Document Intelligence cannot process a document."""


def azure_document_intelligence_configured(config: dict) -> bool:
    endpoint = str(config.get('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT') or '').strip()
    key = str(config.get('AZURE_DOCUMENT_INTELLIGENCE_KEY') or '').strip()
    return bool(endpoint and key)


def build_azure_bookkeeping_transcription(
    document_pages: list[dict],
    filename: str,
    cbo,
    related_page_upload: bool = False,
) -> tuple[dict, list[dict]]:
    if not azure_document_intelligence_configured(current_app.config):
        raise AzureDocumentIntelligenceError('Azure Document Intelligence is not configured.')

    detected_columns = []
    transcribed_rows = []
    quality_flags = []
    raw_text_parts = []
    review_snippets = []
    row_confidences = []
    title_candidates = []
    row_number = 1
    table_count = 0
    handwriting_pages = 0

    for page_index, page in enumerate(document_pages, start=1):
        layout_response = _analyze_document_model(
            page['image_bytes'],
            model_id=current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_LAYOUT_MODEL', 'prebuilt-layout'),
            features=_azure_features(include_high_resolution=True),
        )
        read_response = _analyze_document_model(
            page['image_bytes'],
            model_id=current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_READ_MODEL', 'prebuilt-read'),
            features=_azure_features(include_high_resolution=True),
        )

        page_payload = _build_page_transcription(
            page=page,
            page_index=page_index,
            layout_response=layout_response,
            read_response=read_response,
            starting_row_number=row_number,
        )
        row_number += len(page_payload['transcribed_rows'])
        table_count += page_payload['table_count']
        handwriting_pages += 1 if page_payload['has_handwriting'] else 0

        detected_columns = _merge_unique_strings(detected_columns, page_payload['detected_columns'])
        transcribed_rows.extend(page_payload['transcribed_rows'])
        quality_flags = _merge_unique_strings(quality_flags, page_payload['quality_flags'])
        raw_text_parts.extend(page_payload['raw_text_parts'])
        title_candidates.extend(page_payload['title_candidates'])
        review_snippets.extend(page_payload['review_snippets'])
        row_confidences.extend(
            _safe_float(row.get('confidence'))
            for row in page_payload['transcribed_rows']
            if isinstance(row, dict)
        )

    document_confidence = round(mean(row_confidences), 4) if row_confidences else 0.0
    document_title = title_candidates[0] if title_candidates else ''
    raw_text = _collapse_raw_text(raw_text_parts)

    extraction_notes = [
        'Raw rows and detected columns were transcribed with Azure Document Intelligence layout/read before bookkeeping normalization.'
    ]
    if related_page_upload:
        extraction_notes.append('Related pages were combined conservatively into one logical document.')
    if handwriting_pages:
        extraction_notes.append(f'Azure detected handwritten content on {handwriting_pages} page(s).')
    if table_count == 0 and transcribed_rows:
        extraction_notes.append('Azure did not detect a formal table on at least one page, so OCR lines were preserved as fallback rows.')

    payload = {
        'document_type': 'ledger' if table_count else 'unknown',
        'document_date': '',
        'period_start': '',
        'period_end': '',
        'currency': 'KES',
        'organization_name': cbo.name,
        'document_title': document_title,
        'vendor_or_counterparty': '',
        'summary': '',
        'raw_text': raw_text,
        'detected_columns': detected_columns,
        'transcribed_rows': transcribed_rows,
        'document_confidence': document_confidence,
        'quality_flags': quality_flags,
        'extraction_notes': ' '.join(extraction_notes).strip(),
        'totals': {
            'income': 0.0,
            'expenses': 0.0,
            'net': 0.0,
        },
        'bookkeeping_entries': [],
        'transcription_provider': 'azure_document_intelligence',
        'transcription_model': current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_LAYOUT_MODEL', 'prebuilt-layout'),
        'transcription_read_model': current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_READ_MODEL', 'prebuilt-read'),
        'source_filename': filename,
    }
    return payload, review_snippets


def _azure_features(include_high_resolution: bool) -> list[str]:
    configured = [
        value.strip()
        for value in str(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_FEATURES', '') or '').split(',')
        if value.strip()
    ]
    if include_high_resolution and current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_HIGH_RESOLUTION', True):
        configured.append('ocrHighResolution')
    return _merge_unique_strings([], configured)


def _build_page_transcription(
    page: dict,
    page_index: int,
    layout_response: dict,
    read_response: dict,
    starting_row_number: int,
) -> dict:
    layout_result = (layout_response or {}).get('analyzeResult') or {}
    read_result = (read_response or {}).get('analyzeResult') or {}
    read_words = _extract_read_words(read_result)
    table_bboxes = _extract_table_bboxes(layout_result)
    title_candidates = _extract_title_candidates(layout_result, table_bboxes)
    raw_text_parts = _extract_raw_text_parts(layout_result, read_result, table_bboxes)
    has_handwriting = _has_handwritten_text(read_result)

    extracted = _extract_table_rows(
        page=page,
        page_index=page_index,
        layout_result=layout_result,
        read_words=read_words,
        starting_row_number=starting_row_number,
    )

    if not extracted['transcribed_rows']:
        extracted = _extract_ocr_line_rows(
            page=page,
            page_index=page_index,
            read_result=read_result,
            starting_row_number=starting_row_number,
        )

    return {
        'detected_columns': extracted['detected_columns'],
        'transcribed_rows': extracted['transcribed_rows'],
        'quality_flags': extracted['quality_flags'],
        'review_snippets': extracted['review_snippets'],
        'raw_text_parts': raw_text_parts,
        'title_candidates': title_candidates,
        'table_count': extracted['table_count'],
        'has_handwriting': has_handwriting,
    }


def _extract_table_rows(page: dict, page_index: int, layout_result: dict, read_words: list[dict], starting_row_number: int) -> dict:
    tables = layout_result.get('tables') or []
    detected_columns = []
    transcribed_rows = []
    quality_flags = []
    review_snippets = []
    row_number = starting_row_number

    for table_index, table in enumerate(tables, start=1):
        cells = [cell for cell in (table.get('cells') or []) if isinstance(cell, dict)]
        if not cells:
            continue

        column_count = int(table.get('columnCount') or 0)
        if column_count <= 0:
            column_count = max((int(cell.get('columnIndex') or 0) + int(cell.get('columnSpan') or 1)) for cell in cells)

        header_rows = sorted({int(cell.get('rowIndex') or 0) for cell in cells if str(cell.get('kind') or '').strip() == 'columnHeader'})
        if not header_rows:
            inferred_header = _infer_header_row_index(cells)
            if inferred_header is not None:
                header_rows = [inferred_header]

        column_labels, table_flags = _build_column_labels(cells, column_count, read_words, header_rows)
        if not column_labels:
            continue
        detected_columns = _merge_unique_strings(detected_columns, column_labels)
        quality_flags = _merge_unique_strings(quality_flags, [f'Page {page_index}: {flag}' for flag in table_flags])

        data_rows = sorted({int(cell.get('rowIndex') or 0) for cell in cells if int(cell.get('rowIndex') or 0) not in header_rows})
        for layout_row_index in data_rows:
            row_cells = {label: '' for label in column_labels}
            signature_cells = {}
            row_confidences = []
            row_notes = []
            row_bboxes = []
            cell_reviews = []
            conflict_columns = []

            positioned_cells = sorted(
                [cell for cell in cells if int(cell.get('rowIndex') or 0) == layout_row_index],
                key=lambda item: int(item.get('columnIndex') or 0),
            )

            for cell in positioned_cells:
                column_index = int(cell.get('columnIndex') or 0)
                if column_index >= len(column_labels):
                    continue

                cell_text, cell_confidence, conflict = _resolve_cell_text(cell, read_words)
                row_cells[column_labels[column_index]] = cell_text
                if cell_confidence:
                    row_confidences.append(cell_confidence)
                bbox = _bbox_from_entity(cell)
                if bbox:
                    row_bboxes.append(bbox)
                if _is_signature_column(column_labels[column_index]):
                    signature_cell = _build_signature_cell_payload(page, bbox)
                    if signature_cell:
                        signature_cells[column_labels[column_index]] = signature_cell
                if conflict:
                    row_notes.append(f'OCR conflict in {column_labels[column_index]}')
                    conflict_columns.append(column_labels[column_index])
                if _cell_needs_review(column_labels[column_index], cell_text, cell_confidence, conflict):
                    cell_review = _build_cell_review_snippet(
                        page,
                        bbox,
                        row_number,
                        page_index,
                        table_index,
                        column_labels[column_index],
                        cell_text,
                        cell_confidence,
                        'OCR conflict' if conflict else 'Low-confidence cell',
                    )
                    if cell_review:
                        cell_reviews.append(cell_review)

            if not any(str(value or '').strip() for value in row_cells.values()):
                continue

            row_confidence = round(mean(row_confidences), 4) if row_confidences else 0.0
            row = {
                'row_number': row_number,
                'row_type': 'transaction' if _row_looks_transactional(row_cells) else 'other',
                'cells': row_cells,
                'signature_cells': signature_cells,
                'notes': '; '.join(_merge_unique_strings([], row_notes))[:240],
                'confidence': row_confidence,
            }
            transcribed_rows.append(row)

            if row_confidence < 0.62 or row_notes or cell_reviews:
                crop_bbox = _union_bboxes(row_bboxes)
                snippet = _build_review_snippet(
                    page,
                    crop_bbox,
                    row,
                    page_index,
                    table_index,
                    conflict_columns=conflict_columns,
                    cell_snippets=cell_reviews,
                )
                if snippet:
                    review_snippets.append(snippet)

            row_number += 1

    return {
        'detected_columns': detected_columns,
        'transcribed_rows': transcribed_rows,
        'quality_flags': quality_flags,
        'review_snippets': review_snippets,
        'table_count': len(tables),
    }


def _extract_ocr_line_rows(page: dict, page_index: int, read_result: dict, starting_row_number: int) -> dict:
    pages = [item for item in (read_result.get('pages') or []) if isinstance(item, dict)]
    lines = []
    if pages:
        lines = [line for line in (pages[0].get('lines') or []) if isinstance(line, dict)]

    detected_columns = ['Content'] if lines else []
    transcribed_rows = []
    review_snippets = []
    row_number = starting_row_number
    quality_flags = []
    if lines:
        quality_flags.append('Azure layout did not find a table, so OCR lines were preserved as fallback rows.')

    for line in lines:
        words = [word for word in (pages[0].get('words') or []) if _word_within_span(word, line.get('spans') or [])]
        confidences = [_safe_float(word.get('confidence')) for word in words if _safe_float(word.get('confidence')) > 0]
        confidence = round(mean(confidences), 4) if confidences else 0.0
        row = {
            'row_number': row_number,
            'row_type': 'other',
            'cells': {'Content': str(line.get('content') or '').strip()},
            'notes': '',
            'confidence': confidence,
        }
        if not row['cells']['Content']:
            continue
        transcribed_rows.append(row)

        if confidence < 0.62:
            bbox = _bbox_from_polygon(line.get('polygon'))
            snippet = _build_review_snippet(page, bbox, row, page_index, 0)
            if snippet:
                review_snippets.append(snippet)
        row_number += 1

    return {
        'detected_columns': detected_columns,
        'transcribed_rows': transcribed_rows,
        'quality_flags': quality_flags,
        'review_snippets': review_snippets,
        'table_count': 0,
    }


def _build_column_labels(cells: list[dict], column_count: int, read_words: list[dict], header_rows: list[int]) -> tuple[list[str], list[str]]:
    labels_by_column = {column_index: [] for column_index in range(column_count)}
    flags = []

    for cell in sorted(cells, key=lambda item: (int(item.get('rowIndex') or 0), int(item.get('columnIndex') or 0))):
        row_index = int(cell.get('rowIndex') or 0)
        if row_index not in header_rows:
            continue
        text, _confidence, _conflict = _resolve_cell_text(cell, read_words)
        if not text:
            continue

        column_index = int(cell.get('columnIndex') or 0)
        column_span = max(1, int(cell.get('columnSpan') or 1))
        for spanned_column in range(column_index, min(column_count, column_index + column_span)):
            if text not in labels_by_column[spanned_column]:
                labels_by_column[spanned_column].append(text)

    labels = []
    fallback_columns = 0
    for column_index in range(column_count):
        label = ' '.join(labels_by_column[column_index]).strip()
        label = re.sub(r'\s+', ' ', label).strip()
        if not label:
            label = f'Column {column_index + 1}'
            fallback_columns += 1
        labels.append(label)

    labels, duplicate_count = _dedupe_column_labels(labels)
    if fallback_columns:
        flags.append(f'{fallback_columns} column header(s) were missing and received fallback labels.')
    if duplicate_count:
        flags.append(f'{duplicate_count} duplicate header label(s) were disambiguated with numeric suffixes.')
    return labels, flags


def _infer_header_row_index(cells: list[dict]) -> int | None:
    row_zero_cells = [cell for cell in cells if int(cell.get('rowIndex') or 0) == 0]
    if len(row_zero_cells) < 2:
        return None
    texts = [str(cell.get('content') or '').strip() for cell in row_zero_cells if str(cell.get('content') or '').strip()]
    if not texts:
        return None

    non_numeric = 0
    for text in texts:
        if _safe_numeric(text) is None and not _looks_like_date(text):
            non_numeric += 1
    return 0 if non_numeric >= max(2, len(texts) - 1) else None


def _resolve_cell_text(cell: dict, read_words: list[dict]) -> tuple[str, float, bool]:
    layout_text = re.sub(r'\s+', ' ', str(cell.get('content') or '').strip())
    bbox = _bbox_from_entity(cell)
    overlapping_words = _words_in_bbox(read_words, bbox) if bbox else []
    read_text = ' '.join(word['content'] for word in overlapping_words).strip()
    read_confidence = round(mean(word['confidence'] for word in overlapping_words), 4) if overlapping_words else 0.0

    if read_text and not layout_text:
        return read_text, read_confidence, False
    if layout_text and not read_text:
        return layout_text, read_confidence, False
    if not layout_text and not read_text:
        return '', 0.0, False
    if _normalized_text(layout_text) == _normalized_text(read_text):
        best = read_text if len(read_text) > len(layout_text) else layout_text
        return best, read_confidence, False

    if read_confidence >= 0.72 and len(_normalized_text(read_text)) >= len(_normalized_text(layout_text)):
        return read_text, read_confidence, True
    return layout_text, max(read_confidence, 0.55 if layout_text else 0.0), True


def _extract_read_words(read_result: dict) -> list[dict]:
    words = []
    pages = [item for item in (read_result.get('pages') or []) if isinstance(item, dict)]
    if not pages:
        return words

    for word in pages[0].get('words') or []:
        if not isinstance(word, dict):
            continue
        bbox = _bbox_from_polygon(word.get('polygon'))
        if not bbox:
            continue
        text = str(word.get('content') or '').strip()
        if not text:
            continue
        words.append({
            'content': text,
            'confidence': _safe_float(word.get('confidence')),
            'bbox': bbox,
        })
    return sorted(words, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _extract_table_bboxes(layout_result: dict) -> list[tuple[float, float, float, float]]:
    bboxes = []
    for table in layout_result.get('tables') or []:
        bbox = _bbox_from_entity(table)
        if bbox:
            bboxes.append(bbox)
    return bboxes


def _extract_title_candidates(layout_result: dict, table_bboxes: list[tuple[float, float, float, float]]) -> list[str]:
    titles = []
    for paragraph in layout_result.get('paragraphs') or []:
        if not isinstance(paragraph, dict):
            continue
        role = str(paragraph.get('role') or '').strip()
        if role not in {'title', 'sectionHeading', 'pageHeader'}:
            continue
        bbox = _bbox_from_entity(paragraph)
        if bbox and _bbox_overlaps_any(bbox, table_bboxes):
            continue
        content = re.sub(r'\s+', ' ', str(paragraph.get('content') or '').strip())
        if content:
            titles.append(content)
    return _merge_unique_strings([], titles)


def _extract_raw_text_parts(layout_result: dict, read_result: dict, table_bboxes: list[tuple[float, float, float, float]]) -> list[str]:
    parts = []
    for paragraph in layout_result.get('paragraphs') or []:
        if not isinstance(paragraph, dict):
            continue
        bbox = _bbox_from_entity(paragraph)
        if bbox and _bbox_overlaps_any(bbox, table_bboxes):
            continue
        content = re.sub(r'\s+', ' ', str(paragraph.get('content') or '').strip())
        if content:
            parts.append(content)

    if not parts:
        content = str(read_result.get('content') or '').strip()
        if content:
            parts.append(content[:1200])
    return _merge_unique_strings([], parts)


def _has_handwritten_text(read_result: dict) -> bool:
    for style in read_result.get('styles') or []:
        if not isinstance(style, dict):
            continue
        if style.get('isHandwritten'):
            return True
    return False


def _build_review_snippet(
    page: dict,
    bbox,
    row: dict,
    page_index: int,
    table_index: int,
    conflict_columns: list[str] | None = None,
    cell_snippets: list[dict] | None = None,
) -> dict | None:
    if not bbox:
        return None
    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            left, top, right, bottom = _pad_bbox(bbox, width, height, padding=16)
            crop = normalized.crop((left, top, right, bottom))
            if crop.size[0] < 24 or crop.size[1] < 24:
                return None
            buffer = BytesIO()
            crop.save(buffer, format='PNG')
            return {
                'row_number': int(row.get('row_number') or 0),
                'page_number': page_index,
                'table_index': table_index,
                'confidence': _safe_float(row.get('confidence')),
                'cells': dict(row.get('cells') or {}),
                'conflict_columns': list(conflict_columns or []),
                'cell_snippets': list(cell_snippets or []),
                'repair_recommended': bool(cell_snippets or conflict_columns or _safe_float(row.get('confidence')) < 0.62),
                'mime_type': 'image/png',
                'image_bytes': buffer.getvalue(),
                'label': f'Original crop for ambiguous row {row.get("row_number")}',
            }
    except Exception:
        return None


def _build_cell_review_snippet(
    page: dict,
    bbox,
    row_number: int,
    page_index: int,
    table_index: int,
    column_name: str,
    cell_text: str,
    confidence: float,
    reason: str,
) -> dict | None:
    if not bbox:
        return None
    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            left, top, right, bottom = _pad_bbox(bbox, width, height, padding=10)
            crop = normalized.crop((left, top, right, bottom))
            if crop.size[0] < 18 or crop.size[1] < 18:
                return None
            buffer = BytesIO()
            crop.save(buffer, format='PNG')
            return {
                'row_number': int(row_number or 0),
                'page_number': page_index,
                'table_index': table_index,
                'column_name': str(column_name or '').strip(),
                'cell_text': str(cell_text or '').strip(),
                'confidence': _safe_float(confidence),
                'reason': str(reason or '').strip(),
                'mime_type': 'image/png',
                'image_bytes': buffer.getvalue(),
                'label': f'Cell crop for row {row_number} column {column_name}',
            }
    except Exception:
        return None


def _build_signature_cell_payload(page: dict, bbox) -> dict | None:
    if not bbox:
        return None
    try:
        with Image.open(BytesIO(page['image_bytes'])) as image:
            normalized = ImageOps.exif_transpose(image)
            width, height = normalized.size
            left, top, right, bottom = _pad_bbox(bbox, width, height, padding=6)
            crop = normalized.crop((left, top, right, bottom))
            if crop.size[0] < 12 or crop.size[1] < 12:
                return None
            if not _crop_has_visible_mark(crop):
                return None
            buffer = BytesIO()
            crop.save(buffer, format='PNG')
            encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
            return {
                'mime_type': 'image/png',
                'data_uri': f'data:image/png;base64,{encoded}',
            }
    except Exception:
        return None


def _analyze_document_model(image_bytes: bytes, model_id: str, features: list[str]) -> dict:
    endpoint = str(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT') or '').strip().rstrip('/')
    api_key = str(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_KEY') or '').strip()
    api_version = str(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_API_VERSION', '2024-11-30') or '2024-11-30').strip()
    locale = str(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_LOCALE', 'en-KE') or 'en-KE').strip()
    request_timeout = float(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT', 60) or 60)
    poll_timeout = float(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_POLL_TIMEOUT', 180) or 180)
    poll_interval = float(current_app.config.get('AZURE_DOCUMENT_INTELLIGENCE_POLL_INTERVAL', 1.5) or 1.5)

    analyze_url = f'{endpoint}/documentintelligence/documentModels/{model_id}:analyze'
    params = {
        '_overload': 'analyzeDocument',
        'api-version': api_version,
        'locale': locale,
        'stringIndexType': 'unicodeCodePoint',
    }
    if features:
        params['features'] = ','.join(features)

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Ocp-Apim-Subscription-Key': api_key,
    }
    payload = {
        'base64Source': base64.b64encode(image_bytes).decode('ascii'),
    }

    try:
        response = requests.post(analyze_url, params=params, headers=headers, json=payload, timeout=request_timeout)
    except requests.RequestException as exc:
        raise AzureDocumentIntelligenceError('Could not reach Azure Document Intelligence.') from exc

    if response.status_code != 202:
        raise AzureDocumentIntelligenceError(_azure_error_message(response))

    operation_location = response.headers.get('Operation-Location') or response.headers.get('operation-location')
    if not operation_location:
        raise AzureDocumentIntelligenceError('Azure Document Intelligence did not return an operation URL.')

    deadline = time.monotonic() + poll_timeout
    while True:
        try:
            poll_response = requests.get(operation_location, headers={'Ocp-Apim-Subscription-Key': api_key, 'Accept': 'application/json'}, timeout=request_timeout)
        except requests.RequestException as exc:
            raise AzureDocumentIntelligenceError('Could not poll Azure Document Intelligence results.') from exc

        if poll_response.status_code >= 400:
            raise AzureDocumentIntelligenceError(_azure_error_message(poll_response))

        payload = poll_response.json()
        status = str(payload.get('status') or '').strip().lower()
        if status == 'succeeded':
            return payload
        if status == 'failed':
            raise AzureDocumentIntelligenceError(_azure_error_message(poll_response, payload))
        if time.monotonic() >= deadline:
            raise AzureDocumentIntelligenceError('Azure Document Intelligence took too long to finish analyzing the document.')

        retry_after = poll_response.headers.get('Retry-After')
        try:
            sleep_seconds = max(poll_interval, float(retry_after)) if retry_after else poll_interval
        except ValueError:
            sleep_seconds = poll_interval
        time.sleep(sleep_seconds)


def _azure_error_message(response, payload: dict | None = None) -> str:
    body = payload
    if body is None:
        try:
            body = response.json()
        except ValueError:
            body = {}
    error = body.get('error') or {}
    message = str(error.get('message') or '').strip()
    if message:
        return f'Azure Document Intelligence error: {message}'
    return f'Azure Document Intelligence error: HTTP {response.status_code}.'


def _word_within_span(word: dict, spans: list[dict]) -> bool:
    word_spans = word.get('span') or word.get('spans') or []
    if isinstance(word_spans, dict):
        word_spans = [word_spans]
    if not word_spans or not spans:
        return False

    for word_span in word_spans:
        word_offset = int(word_span.get('offset') or 0)
        word_length = int(word_span.get('length') or 0)
        word_end = word_offset + word_length
        for span in spans:
            span_offset = int(span.get('offset') or 0)
            span_length = int(span.get('length') or 0)
            span_end = span_offset + span_length
            if word_offset >= span_offset and word_end <= span_end:
                return True
    return False


def _words_in_bbox(words: list[dict], bbox: tuple[float, float, float, float]) -> list[dict]:
    if not bbox:
        return []
    left, top, right, bottom = bbox
    selected = []
    for word in words:
        word_left, word_top, word_right, word_bottom = word['bbox']
        center_x = (word_left + word_right) / 2.0
        center_y = (word_top + word_bottom) / 2.0
        if left <= center_x <= right and top <= center_y <= bottom:
            selected.append(word)
    return sorted(selected, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _bbox_from_entity(entity: dict):
    if not isinstance(entity, dict):
        return None
    regions = entity.get('boundingRegions') or []
    for region in regions:
        bbox = _bbox_from_polygon(region.get('polygon'))
        if bbox:
            return bbox
    return _bbox_from_polygon(entity.get('polygon'))


def _bbox_from_polygon(polygon):
    if not polygon:
        return None
    if isinstance(polygon, list) and polygon and isinstance(polygon[0], dict):
        xs = [float(point.get('x') or 0.0) for point in polygon]
        ys = [float(point.get('y') or 0.0) for point in polygon]
    elif isinstance(polygon, list):
        values = [float(value) for value in polygon]
        if len(values) < 8:
            return None
        xs = values[0::2]
        ys = values[1::2]
    else:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _union_bboxes(bboxes: list[tuple[float, float, float, float]]):
    usable = [bbox for bbox in bboxes if bbox]
    if not usable:
        return None
    return (
        min(bbox[0] for bbox in usable),
        min(bbox[1] for bbox in usable),
        max(bbox[2] for bbox in usable),
        max(bbox[3] for bbox in usable),
    )


def _bbox_overlaps_any(bbox, others: list[tuple[float, float, float, float]]) -> bool:
    if not bbox:
        return False
    for other in others:
        if not other:
            continue
        if bbox[0] < other[2] and bbox[2] > other[0] and bbox[1] < other[3] and bbox[3] > other[1]:
            return True
    return False


def _pad_bbox(bbox, width: int, height: int, padding: int = 16):
    left = max(0, int(bbox[0]) - padding)
    top = max(0, int(bbox[1]) - padding)
    right = min(width, int(bbox[2]) + padding)
    bottom = min(height, int(bbox[3]) + padding)
    return left, top, right, bottom


def _row_looks_transactional(cells: dict) -> bool:
    values = [str(value or '').strip() for value in cells.values() if str(value or '').strip()]
    if not values:
        return False
    numeric_values = sum(1 for value in values if _safe_numeric(value) is not None)
    return numeric_values > 0


def _cell_needs_review(column_name: str, cell_text: str, confidence: float, conflict: bool) -> bool:
    normalized_column = _normalized_text(column_name)
    text = str(cell_text or '').strip()
    confidence_threshold = float(current_app.config.get('BOOKKEEPING_CLAUDE_CELL_REPAIR_CONFIDENCE_THRESHOLD', 0.72) or 0.72)
    if conflict or confidence < confidence_threshold:
        return True
    if not text:
        return False
    if 'phone' in normalized_column:
        digits = ''.join(character for character in text if character.isdigit())
        return len(digits) not in {9, 10, 12}
    if 'date' in normalized_column:
        return not _looks_like_date(text)
    if any(token in normalized_column for token in ('fee', 'due', 'paid', 'amt', 'amount', 'balance')):
        return _safe_numeric(text.replace('/-', '').replace('=', '')) is None
    if normalized_column in {'mf', 'gender'}:
        return _normalized_text(text) not in {'m', 'f', 'male', 'female'}
    return False


def _is_signature_column(column_name: str) -> bool:
    normalized = _normalized_text(column_name)
    return normalized.startswith('sign') or normalized in {'signature', 'signature2'}


def _crop_has_visible_mark(crop: Image.Image) -> bool:
    grayscale = crop.convert('L')
    pixels = list(grayscale.getdata())
    if not pixels:
        return False
    dark_pixels = sum(1 for value in pixels if value < 205)
    return (dark_pixels / len(pixels)) >= 0.01


def _collapse_raw_text(parts: list[str]) -> str:
    unique_parts = _merge_unique_strings([], [re.sub(r'\s+', ' ', part).strip() for part in parts if str(part or '').strip()])
    joined = ' '.join(unique_parts).strip()
    return joined[:1200]


def _dedupe_column_labels(labels: list[str]) -> tuple[list[str], int]:
    counts = {}
    deduped = []
    duplicate_count = 0
    for label in labels:
        base = re.sub(r'\s+', ' ', str(label or '').strip()) or 'Column'
        number = counts.get(base, 0) + 1
        counts[base] = number
        if number == 1:
            deduped.append(base)
            continue
        duplicate_count += 1
        deduped.append(f'{base} {number}')
    return deduped, duplicate_count


def _merge_unique_strings(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    for value in incoming:
        text = str(value or '').strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _normalized_text(value: str) -> str:
    return ''.join(character for character in str(value or '').lower() if character.isalnum())


def _looks_like_date(value: str) -> bool:
    text = str(value or '').strip()
    return bool(re.fullmatch(r'\d{1,4}[/-]\d{1,2}[/-]\d{1,4}', text))


def _safe_numeric(value) -> float | None:
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(',', '').replace('KSh', '').replace('KES', '').strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0