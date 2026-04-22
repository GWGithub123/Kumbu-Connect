"""Google Custom Search helpers for verifying donor and grant organisations."""
from __future__ import annotations

import requests
from flask import current_app


GOOGLE_CUSTOM_SEARCH_URL = 'https://www.googleapis.com/customsearch/v1'


def get_google_search_api_key() -> str:
    return (current_app.config.get('GOOGLE_SEARCH_API_KEY') or '').strip()


def get_google_search_engine_id() -> str:
    return (current_app.config.get('GOOGLE_SEARCH_ENGINE_ID') or '').strip()


def google_search_enabled() -> bool:
    return bool(get_google_search_api_key() and get_google_search_engine_id())


def search_google(query: str, num_results: int = 5) -> dict:
    cleaned_query = ' '.join((query or '').split())
    payload = {
        'configured': google_search_enabled(),
        'source': 'google_custom_search',
        'query': cleaned_query,
        'items': [],
        'error': '',
    }
    if not cleaned_query:
        payload['error'] = 'No search query was provided.'
        return payload

    if not payload['configured']:
        payload['error'] = 'Google Custom Search is not configured.'
        return payload

    try:
        response = requests.get(
            GOOGLE_CUSTOM_SEARCH_URL,
            params={
                'key': get_google_search_api_key(),
                'cx': get_google_search_engine_id(),
                'q': cleaned_query,
                'num': max(1, min(int(num_results or 5), 10)),
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning('Google Custom Search failed for query %s: %s', cleaned_query, exc)
        payload['error'] = 'Google search request failed.'
        return payload

    raw_payload = response.json() if response.content else {}
    api_error = ((raw_payload or {}).get('error') or {}).get('message')
    if api_error:
        payload['error'] = str(api_error)
        return payload

    items = []
    for item in (raw_payload.get('items') or [])[: max(1, min(int(num_results or 5), 10))]:
        if not isinstance(item, dict):
            continue
        items.append({
            'title': str(item.get('title') or '').strip(),
            'link': str(item.get('link') or '').strip(),
            'display_link': str(item.get('displayLink') or '').strip(),
            'snippet': str(item.get('snippet') or '').strip(),
        })

    payload['items'] = items
    if not items:
        payload['error'] = 'No public search results were returned.'
    return payload