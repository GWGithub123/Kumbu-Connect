"""Lightweight bookkeeping audit helpers used by the profile views."""

from datetime import datetime


TOOL_RATE_ALIASES = ('fees', 'fee', 'dailyfee', 'rate', 'rateperday', 'rentalrate')
TOOL_DAYS_ALIASES = ('days', 'daysrented', 'duration', 'totaldays')
TOOL_START_DATE_ALIASES = ('date', 'rentoutdate', 'startdate', 'issuedate')
TOOL_END_DATE_ALIASES = ('enddate', 'returndate', 'datein', 'duedate')
TOOL_TOTAL_ALIASES = ('totalrevenue', 'revenue', 'totalincome', 'income', 'paid', 'total', 'amount')


def audit_bookkeeping_document(extracted: dict | None, cbo) -> dict:
    payload = extracted if isinstance(extracted, dict) else {}
    entries = payload.get('bookkeeping_entries')
    rows = payload.get('transcribed_rows')
    totals = payload.get('totals')
    related_page_upload = bool(payload.get('related_page_upload'))

    issues = []
    flagged_cells = []

    has_rows = isinstance(rows, list) and any(isinstance(row, dict) for row in rows)

    if (not isinstance(entries, list) or not entries) and not has_rows:
        issues.append({
            'code': 'missing_entries',
            'message': 'No structured bookkeeping entries were extracted from this document.',
            'row_number': None,
            'columns': [],
        })

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            confidence = row.get('confidence')
            row_number = row.get('row_number')
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = None

            if confidence_value is not None and confidence_value < 0.45:
                message = 'Low-confidence transcription. Review this row before relying on it.'
                issues.append({
                    'code': 'low_confidence_row',
                    'message': message,
                    'row_number': row_number,
                    'columns': [],
                })
                flagged_cells.append({
                    'row_number': row_number,
                    'column': 'row',
                    'code': 'low_confidence_row',
                    'message': message,
                })

        tool_row_audit = _audit_tool_lending_rows(rows)
        issues.extend(tool_row_audit['issues'])
        flagged_cells.extend(tool_row_audit['flagged_cells'])

    if isinstance(entries, list):
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue

            amount = entry.get('amount')
            try:
                amount_value = float(amount)
            except (TypeError, ValueError):
                amount_value = None

            if amount_value is None:
                message = 'Amount is missing or not numeric.'
                issues.append({
                    'code': 'invalid_amount',
                    'message': message,
                    'row_number': index,
                    'columns': ['amount'],
                })
                flagged_cells.append({
                    'row_number': index,
                    'column': 'amount',
                    'code': 'invalid_amount',
                    'message': message,
                })

            if not str(entry.get('description') or '').strip():
                message = 'Description is blank for this bookkeeping entry.'
                issues.append({
                    'code': 'missing_description',
                    'message': message,
                    'row_number': index,
                    'columns': ['description'],
                })
                flagged_cells.append({
                    'row_number': index,
                    'column': 'description',
                    'code': 'missing_description',
                    'message': message,
                })

            grounding = _audit_normalized_entry_grounding(entry, rows)
            issues.extend(grounding['issues'])
            flagged_cells.extend(grounding['flagged_cells'])

    if isinstance(totals, dict) and isinstance(entries, list) and entries:
        income_total = _safe_float(totals.get('income'))
        expense_total = _safe_float(totals.get('expenses'))
        entry_income = 0.0
        entry_expenses = 0.0

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            amount_value = _safe_float(entry.get('amount'))
            entry_type = str(entry.get('entry_type') or 'unknown').lower()
            direction = str(entry.get('direction') or '').lower()

            if entry_type == 'income' or direction == 'inflow':
                entry_income += amount_value
            elif entry_type == 'expense' or direction == 'outflow':
                entry_expenses += amount_value

        if abs(entry_income - income_total) > 0.01:
            issues.append({
                'code': 'income_total_mismatch',
                'message': 'Income total does not match the extracted entry rows.',
                'row_number': None,
                'columns': ['amount'],
            })
        if abs(entry_expenses - expense_total) > 0.01:
            issues.append({
                'code': 'expense_total_mismatch',
                'message': 'Expense total does not match the extracted entry rows.',
                'row_number': None,
                'columns': ['amount'],
            })

    issues = _dedupe_issues(issues)
    flagged_cells = _dedupe_flagged_cells(flagged_cells)

    return {
        'issues': issues,
        'flagged_cells': flagged_cells,
        'issue_count': len(issues),
        'flagged_cell_count': len(flagged_cells),
        'inventory_baseline': getattr(cbo, 'tool_inventory_total', None),
    }


def audit_bookkeeping_group(batch_payload: list[dict] | None, cbo) -> dict:
    items = batch_payload if isinstance(batch_payload, list) else []
    results = {
        item.get('document_id'): {
            'issues': [],
            'flagged_cells': [],
            'issue_count': 0,
            'flagged_cell_count': 0,
            'inventory_baseline': getattr(cbo, 'tool_inventory_total', None),
        }
        for item in items
        if isinstance(item, dict) and item.get('document_id') is not None
    }
    seen_descriptions = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        document_id = item.get('document_id')
        extracted = item.get('extracted') if isinstance(item.get('extracted'), dict) else {}
        entries = extracted.get('bookkeeping_entries')
        issues = []
        flagged_cells = []

        if isinstance(entries, list):
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    continue
                description = str(entry.get('description') or '').strip().lower()
                amount = round(_safe_float(entry.get('amount')), 2)
                date_value = str(entry.get('entry_date') or '').strip()
                duplicate_key = (description, amount, date_value)
                prior_document_id = seen_descriptions.get(duplicate_key)

                if description and prior_document_id is not None:
                    message = f'Possible duplicate entry also appears in document {prior_document_id}.'
                    issues.append({
                        'code': 'possible_duplicate_entry',
                        'message': message,
                        'row_number': index,
                        'columns': ['description', 'amount'],
                    })
                    flagged_cells.append({
                        'row_number': index,
                        'column': 'description',
                        'code': 'possible_duplicate_entry',
                        'message': message,
                    })
                elif description:
                    seen_descriptions[duplicate_key] = document_id

        result = results.setdefault(document_id, {
            'issues': [],
            'flagged_cells': [],
            'issue_count': 0,
            'flagged_cell_count': 0,
            'inventory_baseline': getattr(cbo, 'tool_inventory_total', None),
        })
        result['issues'].extend(issues)
        result['flagged_cells'].extend(flagged_cells)

    grouped_results = _audit_related_page_group(items)
    for document_id, extra in grouped_results.items():
        result = results.setdefault(document_id, {
            'issues': [],
            'flagged_cells': [],
            'issue_count': 0,
            'flagged_cell_count': 0,
            'inventory_baseline': getattr(cbo, 'tool_inventory_total', None),
        })
        result['issues'].extend(extra.get('issues') or [])
        result['flagged_cells'].extend(extra.get('flagged_cells') or [])

    for result in results.values():
        result['issues'] = _dedupe_issues(result['issues'])
        result['flagged_cells'] = _dedupe_flagged_cells(result['flagged_cells'])
        result['issue_count'] = len(result['issues'])
        result['flagged_cell_count'] = len(result['flagged_cells'])

    return results


def _audit_related_page_group(items: list[dict]) -> dict:
    grouped_items = [item for item in items if isinstance(item, dict) and isinstance(item.get('extracted'), dict) and item['extracted'].get('related_page_upload')]
    if len(grouped_items) < 2:
        return {}

    register_rows = []
    revenue_rows = []
    for item_index, item in enumerate(grouped_items):
        document_id = item.get('document_id')
        rows = (item.get('extracted') or {}).get('transcribed_rows') or []
        for row in rows:
            row_info = _extract_row_info(row)
            if not row_info or not row_info['row_number']:
                continue
            enriched = {
                'document_id': document_id,
                'item_index': item_index,
                **row_info,
            }
            if row_info['has_register_context'] and (row_info['fee_per_day'] is not None or row_info['reported_days'] is not None):
                register_rows.append(enriched)
            elif row_info['reported_total'] is not None:
                revenue_rows.append(enriched)

    if not register_rows or not revenue_rows:
        return {}

    register_rows.sort(key=lambda item: (item['item_index'], item['row_number']))
    revenue_rows.sort(key=lambda item: (item['item_index'], item['row_number']))
    pair_count = min(len(register_rows), len(revenue_rows))
    if pair_count == 0:
        return {}

    results = {}
    for index in range(pair_count):
        register_row = register_rows[index]
        revenue_row = revenue_rows[index]
        if register_row['document_id'] == revenue_row['document_id']:
            continue
        if register_row['fee_per_day'] is None or register_row['reported_days'] is None or revenue_row['reported_total'] is None:
            continue

        expected_total = round(register_row['fee_per_day'] * register_row['reported_days'], 2)
        if abs(expected_total - revenue_row['reported_total']) <= 0.01:
            continue

        message = (
            f'Aligned multi-page rows do not agree. Register row {register_row["row_number"]} implies '
            f'{expected_total:.2f} from {register_row["reported_days"]:g} day(s) at {register_row["fee_per_day"]:.2f} per day, '
            f'but companion revenue row {revenue_row["row_number"]} shows {revenue_row["reported_total"]:.2f}.'
        )

        register_result = results.setdefault(register_row['document_id'], {'issues': [], 'flagged_cells': []})
        register_result['issues'].append({
            'code': 'aligned_row_total_mismatch',
            'message': message,
            'row_number': register_row['row_number'],
            'columns': [column for column in (register_row['days_column'], register_row['fee_column']) if column],
        })
        register_result['flagged_cells'].extend([
            _flagged_cell(register_row['row_number'], register_row['days_column'], 'aligned_row_total_mismatch', message),
            _flagged_cell(register_row['row_number'], register_row['fee_column'], 'aligned_row_total_mismatch', message),
        ])

        revenue_result = results.setdefault(revenue_row['document_id'], {'issues': [], 'flagged_cells': []})
        revenue_result['issues'].append({
            'code': 'aligned_row_total_mismatch',
            'message': message,
            'row_number': revenue_row['row_number'],
            'columns': [column for column in (revenue_row['total_column'],) if column],
        })
        revenue_result['flagged_cells'].append(
            _flagged_cell(revenue_row['row_number'], revenue_row['total_column'], 'aligned_row_total_mismatch', message)
        )

    return results


def _audit_tool_lending_rows(rows: list[dict]) -> dict:
    issues = []
    flagged_cells = []
    register_rows = []
    revenue_rows = []

    for row in rows:
        row_info = _extract_row_info(row)
        if not row_info:
            continue

        if row_info['has_register_context']:
            register_rows.append(row_info)

            if row_info['reported_days'] is not None and row_info['start_date'] and row_info['end_date']:
                actual_days = (row_info['end_date'] - row_info['start_date']).days
                if actual_days >= 0 and abs(actual_days - row_info['reported_days']) > 0.01:
                    message = (
                        f'Days rented does not match the provided dates. '
                        f'{_format_date(row_info["start_date"])} to {_format_date(row_info["end_date"])} spans {actual_days:g} day(s), '
                        f'but this row reports {row_info["reported_days"]:g}.'
                    )
                    issues.append({
                        'code': 'days_date_mismatch',
                        'message': message,
                        'row_number': row_info['row_number'],
                        'columns': [column for column in (row_info['start_column'], row_info['end_column'], row_info['days_column']) if column],
                    })
                    flagged_cells.extend([
                        _flagged_cell(row_info['row_number'], row_info['start_column'], 'days_date_mismatch', message),
                        _flagged_cell(row_info['row_number'], row_info['end_column'], 'days_date_mismatch', message),
                        _flagged_cell(row_info['row_number'], row_info['days_column'], 'days_date_mismatch', message),
                    ])

            if row_info['fee_per_day'] is not None and row_info['reported_days'] is not None and row_info['reported_total'] is not None:
                expected_total = round(row_info['fee_per_day'] * row_info['reported_days'], 2)
                if abs(expected_total - row_info['reported_total']) > 0.01:
                    message = (
                        f'Total revenue does not match the row arithmetic. '
                        f'{row_info["reported_days"]:g} day(s) at {row_info["fee_per_day"]:.2f} per day equals {expected_total:.2f}, '
                        f'but this row shows {row_info["reported_total"]:.2f}.'
                    )
                    issues.append({
                        'code': 'row_total_mismatch',
                        'message': message,
                        'row_number': row_info['row_number'],
                        'columns': [column for column in (row_info['days_column'], row_info['fee_column'], row_info['total_column']) if column],
                    })
                    flagged_cells.extend([
                        _flagged_cell(row_info['row_number'], row_info['days_column'], 'row_total_mismatch', message),
                        _flagged_cell(row_info['row_number'], row_info['fee_column'], 'row_total_mismatch', message),
                        _flagged_cell(row_info['row_number'], row_info['total_column'], 'row_total_mismatch', message),
                    ])
        elif row_info['reported_total'] is not None:
            revenue_rows.append(row_info)

    return {
        'issues': [issue for issue in issues if issue],
        'flagged_cells': [cell for cell in flagged_cells if cell],
    }


def _extract_row_info(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    cells = row.get('cells') or {}
    if not isinstance(cells, dict) or not cells:
        return None

    fee_column, fee_value = _find_numeric_value(cells, TOOL_RATE_ALIASES)
    days_column, reported_days = _find_numeric_value(cells, TOOL_DAYS_ALIASES)
    start_column, start_text = _find_text_value(cells, TOOL_START_DATE_ALIASES)
    end_column, end_text = _find_text_value(cells, TOOL_END_DATE_ALIASES)
    total_column, reported_total = _find_numeric_value(cells, TOOL_TOTAL_ALIASES)
    if total_column and fee_column and total_column == fee_column:
        total_column, reported_total = None, None

    return {
        'row_number': _safe_int(row.get('row_number')),
        'fee_column': fee_column,
        'fee_per_day': fee_value,
        'days_column': days_column,
        'reported_days': reported_days,
        'start_column': start_column,
        'start_date': _parse_date(start_text),
        'end_column': end_column,
        'end_date': _parse_date(end_text),
        'total_column': total_column,
        'reported_total': reported_total,
        'has_register_context': any(value is not None for value in (fee_value, reported_days)) or bool(start_text or end_text),
    }


def _find_text_value(cells: dict, aliases: tuple[str, ...]) -> tuple[str | None, str | None]:
    for column, value in cells.items():
        normalized = _normalize_token(column)
        if any(alias == normalized or alias in normalized for alias in aliases):
            text = str(value or '').strip()
            return column, text or None
    return None, None


def _find_numeric_value(cells: dict, aliases: tuple[str, ...]) -> tuple[str | None, float | None]:
    column, text = _find_text_value(cells, aliases)
    if not text:
        return column, None
    return column, _safe_numeric(text)


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_numeric_column(cells: dict) -> str | None:
    for column, value in cells.items():
        if _safe_numeric(value) is not None:
            return str(column or '').strip() or None
    return None


def _first_date_like_column(cells: dict) -> str | None:
    for column, value in cells.items():
        if _parse_date(str(value or '').strip()):
            return str(column or '').strip() or None
    return None


def _audit_normalized_entry_grounding(entry: dict, rows: list[dict] | None) -> dict:
    issues = []
    flagged_cells = []
    if not isinstance(rows, list) or not rows:
        return {'issues': issues, 'flagged_cells': flagged_cells}

    source_row_numbers = [
        _safe_int(value)
        for value in (entry.get('source_row_numbers') or [])
        if _safe_int(value)
    ]
    if not source_row_numbers:
        return {'issues': issues, 'flagged_cells': flagged_cells}

    matching_rows = [
        row for row in rows
        if isinstance(row, dict) and _safe_int(row.get('row_number')) in source_row_numbers
    ]
    if not matching_rows:
        return {'issues': issues, 'flagged_cells': flagged_cells}

    source_texts = []
    source_numbers = []
    amount_column = None
    date_column = None
    low_confidence_source = False
    for row in matching_rows:
        row_confidence = _safe_float(row.get('confidence'))
        if row_confidence and row_confidence < 0.45:
            low_confidence_source = True
        cells = row.get('cells') or {}
        if not isinstance(cells, dict):
            continue
        for value in cells.values():
            text = str(value or '').strip()
            if not text:
                continue
            source_texts.append(text)
            numeric = _safe_numeric(text)
            if numeric is not None:
                source_numbers.append(round(numeric, 2))
        if amount_column is None:
            amount_column = _first_numeric_column(cells)
        if date_column is None:
            date_column = _first_date_like_column(cells)

    entry_amount = _safe_numeric(entry.get('amount'))
    if entry_amount is not None and round(entry_amount, 2) not in source_numbers:
        message = 'Normalized amount is not grounded in the referenced source row values.'
        issues.append({
            'code': 'ungrounded_normalized_amount',
            'message': message,
            'row_number': source_row_numbers[0],
            'columns': [amount_column or 'row'],
        })
        flagged_cells.append(
            _flagged_cell(source_row_numbers[0], amount_column or 'row', 'ungrounded_normalized_amount', message)
        )

    entry_date = str(entry.get('entry_date') or '').strip()
    if entry_date and not any(entry_date in text for text in source_texts):
        message = 'Normalized date is not present in the referenced source row values.'
        issues.append({
            'code': 'ungrounded_normalized_date',
            'message': message,
            'row_number': source_row_numbers[0],
            'columns': [date_column or 'row'],
        })
        flagged_cells.append(
            _flagged_cell(source_row_numbers[0], date_column or 'row', 'ungrounded_normalized_date', message)
        )

    if low_confidence_source:
        message = 'This normalized entry depends on low-confidence Azure source rows.'
        issues.append({
            'code': 'normalized_from_low_confidence_rows',
            'message': message,
            'row_number': source_row_numbers[0],
            'columns': ['row'],
        })
        flagged_cells.append(_flagged_cell(source_row_numbers[0], 'row', 'normalized_from_low_confidence_rows', message))

    return {
        'issues': [issue for issue in issues if issue],
        'flagged_cells': [cell for cell in flagged_cells if cell],
    }


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


def _parse_date(value: str | None):
    text = str(value or '').strip()
    if not text:
        return None

    for date_format in (
        '%Y-%m-%d',
        '%m/%d/%y',
        '%m/%d/%Y',
        '%m-%d-%y',
        '%m-%d-%Y',
        '%d/%m/%y',
        '%d/%m/%Y',
        '%d-%m-%y',
        '%d-%m-%Y',
    ):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _normalize_token(value: str) -> str:
    return ''.join(character for character in str(value or '').lower() if character.isalnum())


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_date(value) -> str:
    return value.strftime('%Y-%m-%d') if value else 'unknown'


def _flagged_cell(row_number: int, column: str | None, code: str, message: str) -> dict | None:
    if not row_number or not column:
        return None
    return {
        'row_number': row_number,
        'column': column,
        'code': code,
        'message': message,
    }


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for issue in issues:
        key = (
            issue.get('code'),
            issue.get('message'),
            issue.get('row_number'),
            tuple(issue.get('columns') or []),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _dedupe_flagged_cells(cells: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for cell in cells:
        key = (
            cell.get('row_number'),
            cell.get('column'),
            cell.get('code'),
            cell.get('message'),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cell)
    return deduped