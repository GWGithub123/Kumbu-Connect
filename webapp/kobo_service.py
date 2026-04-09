"""
KoboToolbox API service — pulls live submission data for a given asset.
"""
import requests
from flask import current_app


def fetch_kobo_submissions(asset_id: str | None = None) -> list[dict]:
    """
    Fetch all submissions for the given KoboToolbox asset.
    Each CBO now has its own separate form, so no filtering needed.
    Returns a list of dicts (one per submission).
    """
    api_key = current_app.config['KOBO_API_KEY']
    base_url = current_app.config['KOBO_BASE_URL']
    asset_id = asset_id or current_app.config['KOBO_ASSET_ID']

    url = f"{base_url}/assets/{asset_id}/data.json"
    headers = {"Authorization": f"Token {api_key}"}

    results = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        results.extend(payload.get('results', []))
        url = payload.get('next')          # pagination
    
    return results


def fetch_asset_metadata(asset_id: str | None = None) -> dict:
    """Return the asset metadata (form name, date_created, etc.)."""
    api_key = current_app.config['KOBO_API_KEY']
    base_url = current_app.config['KOBO_BASE_URL']
    asset_id = asset_id or current_app.config['KOBO_ASSET_ID']

    url = f"{base_url}/assets/{asset_id}/"
    headers = {"Authorization": f"Token {api_key}"}

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()
