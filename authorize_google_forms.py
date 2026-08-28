"""Run a one-time Google user OAuth flow for Google Forms and save the token locally.

Usage:
    source .venv/bin/activate
    python authorize_google_forms.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from webapp import create_app


SCOPES = [
    'https://www.googleapis.com/auth/forms.body',
    'https://www.googleapis.com/auth/forms.responses.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
]
AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
DEFAULT_REDIRECT_HOST = os.environ.get('GOOGLE_FORMS_AUTH_REDIRECT_HOST', '127.0.0.1').strip() or '127.0.0.1'
DEFAULT_REDIRECT_PORT = int(os.environ.get('GOOGLE_FORMS_AUTH_REDIRECT_PORT', '8765'))
DEFAULT_REDIRECT_PATH = os.environ.get('GOOGLE_FORMS_AUTH_REDIRECT_PATH', '/google/forms/oauth/callback').strip() or '/google/forms/oauth/callback'


class _OAuthCallbackState:
    def __init__(self):
        self.event = threading.Event()
        self.params: dict[str, str] = {}


def _base64url_sha256(raw_text: str) -> str:
    digest = hashlib.sha256(raw_text.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest).decode('utf-8').rstrip('=')


def _make_handler(state: _OAuthCallbackState):
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            state.params = {key: values[0] for key, values in query.items() if values}

            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            if state.params.get('error'):
                self.wfile.write(b'The authentication flow failed. You may close this window.')
            else:
                self.wfile.write(b'The authentication flow has completed. You may close this window.')
            state.event.set()

    return OAuthCallbackHandler


def _redirect_uri() -> str:
    path = DEFAULT_REDIRECT_PATH
    if not path.startswith('/'):
        path = f'/{path}'
    return f'http://{DEFAULT_REDIRECT_HOST}:{DEFAULT_REDIRECT_PORT}{path}'


def _authorize_via_browser(client_id: str, client_secret: str = '') -> dict[str, object]:
    state = _OAuthCallbackState()
    server = HTTPServer((DEFAULT_REDIRECT_HOST, DEFAULT_REDIRECT_PORT), _make_handler(state))
    redirect_uri = _redirect_uri()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _base64url_sha256(code_verifier)
    auth_url = f'{AUTH_URI}?{urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": ' '.join(SCOPES),
        "state": secrets.token_urlsafe(24),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
        "access_type": "offline",
    })}'

    print(f'Please visit this URL to authorize this application: {auth_url}')
    print(f'Configured redirect URI: {redirect_uri}')
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    if not state.event.wait(timeout=300):
        server.shutdown()
        server.server_close()
        raise RuntimeError('Timed out waiting for the Google OAuth callback.')

    server.shutdown()
    server.server_close()

    if state.params.get('error'):
        raise RuntimeError(f'Google OAuth failed: {state.params.get("error")}')

    code = state.params.get('code')
    if not code:
        raise RuntimeError('Google OAuth callback did not include an authorization code.')

    response = requests.post(
        TOKEN_URI,
        data={
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'code_verifier': code_verifier,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
        },
        timeout=30,
    )
    payload = response.json() if response.content else {}
    if not response.ok:
        raise RuntimeError(f'Google token exchange failed: {payload or response.text}')

    refresh_token = payload.get('refresh_token')
    if not refresh_token:
        raise RuntimeError(
            'Google token exchange succeeded but did not return a refresh token. '
            'Revoke the app in Google Account permissions and retry if needed.'
        )

    scopes = str(payload.get('scope') or '').split() or SCOPES
    return {
        'type': 'authorized_user',
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'token_uri': TOKEN_URI,
        'scopes': scopes,
    }


def main() -> int:
    app = create_app()
    with app.app_context():
        client_secret_path = (app.config.get('GOOGLE_OAUTH_CLIENT_SECRET_JSON') or '').strip()
        client_id = (app.config.get('GOOGLE_OAUTH_CLIENT_ID') or '').strip()
        client_secret = (app.config.get('GOOGLE_OAUTH_CLIENT_SECRET') or '').strip()
        token_path = (app.config.get('GOOGLE_OAUTH_TOKEN_JSON') or '').strip()

    if not token_path:
        print('GOOGLE_OAUTH_TOKEN_JSON is not set.')
        return 1

    print('Google Forms authorization settings:')
    print(f'  Redirect URI: {_redirect_uri()}')

    if client_secret_path:
        if not os.path.exists(client_secret_path):
            print(f'OAuth client secret file does not exist: {client_secret_path}')
            return 1
        with open(client_secret_path, encoding='utf-8') as handle:
            client_payload = json.load(handle)
        installed = (client_payload.get('installed') or client_payload.get('web') or {})
        client_id = str(installed.get('client_id') or '').strip()
        client_secret = str(installed.get('client_secret') or client_secret or '').strip()
        if not client_id:
            print('OAuth client secret file is missing client_id.')
            return 1
    elif not client_id:
        print('Set GOOGLE_OAUTH_CLIENT_ID or GOOGLE_OAUTH_CLIENT_SECRET_JSON in .env.')
        return 1

    try:
        token_payload = _authorize_via_browser(client_id, client_secret)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    token_file = Path(token_path)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(token_payload, indent=2), encoding='utf-8')

    print(f'Saved Google Forms OAuth token to: {token_file}')
    granted_scopes = token_payload.get('scopes') or []
    print('Granted scopes:')
    for scope in granted_scopes:
        print(f'  - {scope}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())