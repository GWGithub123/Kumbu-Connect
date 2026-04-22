"""Helpers for deriving stable SMS keywords for community feedback."""
import re


def normalize_sms_keyword(value: str) -> str:
    return re.sub(r'[^A-Z0-9]+', '', (value or '').upper())


def get_cbo_keyword(cbo) -> str:
    configured = normalize_sms_keyword(getattr(cbo, 'sms_keyword', ''))
    if configured:
        return configured

    base = normalize_sms_keyword(getattr(cbo, 'slug', '') or getattr(cbo, 'name', '')) or 'CBO'
    identifier = str(getattr(cbo, 'id', '') or '')
    if not identifier:
        return base[:8] or 'CBO'

    max_base_length = max(3, 8 - len(identifier))
    return f'{base[:max_base_length]}{identifier}'