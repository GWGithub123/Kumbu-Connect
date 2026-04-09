"""Google Maps geocoding helpers for CBO location visualisation."""
from __future__ import annotations

from datetime import datetime

import requests
from flask import current_app

GEOCODING_URL = 'https://maps.googleapis.com/maps/api/geocode/json'


def get_google_maps_api_key() -> str:
    return (current_app.config.get('GOOGLE_MAPS_API_KEY') or '').strip()


def build_cbo_map_query(cbo, profile: dict | None = None) -> str:
    profile = profile or {}
    candidates = [
        profile.get('address', ''),
        getattr(cbo, 'street_address', '') or '',
        profile.get('location', ''),
        cbo.location or '',
        cbo.county_region or '',
    ]

    for candidate in candidates:
        normalized = _normalize_query(candidate)
        if normalized:
            return normalized
    return ''


def ensure_cbo_geocoded(cbo, profile: dict | None = None) -> bool:
    api_key = get_google_maps_api_key()
    map_query = build_cbo_map_query(cbo, profile)
    if not api_key or not map_query:
        return False

    if cbo.latitude is not None and cbo.longitude is not None and (cbo.geocode_query or '') == map_query:
        if not cbo.formatted_address:
            cbo.formatted_address = map_query
        return False

    try:
        response = requests.get(
            GEOCODING_URL,
            params={'address': map_query, 'key': api_key},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning('Google geocoding request failed for %s: %s', cbo.slug, exc)
        return False

    payload = response.json()
    status = payload.get('status')
    if status != 'OK' or not payload.get('results'):
        current_app.logger.warning('Google geocoding returned %s for %s (%s)', status, cbo.slug, map_query)
        return False

    result = payload['results'][0]
    geometry = result.get('geometry', {}).get('location', {})
    lat = geometry.get('lat')
    lng = geometry.get('lng')
    if lat is None or lng is None:
        return False

    cbo.street_address = (profile or {}).get('address', cbo.street_address) or cbo.street_address
    cbo.latitude = float(lat)
    cbo.longitude = float(lng)
    cbo.geocode_query = map_query
    cbo.formatted_address = result.get('formatted_address', map_query)
    cbo.place_id = result.get('place_id', '')
    cbo.geocoded_at = datetime.utcnow()
    return True


def _normalize_query(value: str) -> str:
    cleaned = ' '.join((value or '').replace('\n', ' ').split())
    if not cleaned:
        return ''
    if 'kenya' not in cleaned.lower():
        cleaned = f'{cleaned}, Kenya'
    return cleaned