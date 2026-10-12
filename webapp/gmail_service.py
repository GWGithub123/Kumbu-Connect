"""Send funder alert email through the Gmail account that already authorizes Google Forms."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .google_forms_service import _get_credentials


GMAIL_SEND_SCOPE = 'https://www.googleapis.com/auth/gmail.send'


class GmailSendError(RuntimeError):
    """The saved Google token could not send mail."""


def gmail_send_configured() -> bool:
    from .google_forms_service import google_forms_enabled

    return bool(google_forms_enabled())


def gmail_send_scope_granted() -> bool:
    """Whether the saved token was granted gmail.send, without contacting Google."""
    import json
    import os

    token_path = (current_app.config.get('GOOGLE_OAUTH_TOKEN_JSON') or '').strip()
    if not token_path or not os.path.exists(token_path):
        return False
    try:
        with open(token_path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    scopes = payload.get('scopes') or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return GMAIL_SEND_SCOPE in {str(scope) for scope in scopes}


def _image_subtype(mime_type: str) -> str:
    subtype = str(mime_type or 'image/png').split('/')[-1].lower()
    return 'jpeg' if subtype == 'jpg' else subtype


def send_gmail_message(
    to_address: str,
    subject: str,
    body_text: str,
    *,
    html_body: str | None = None,
    inline_images: list[dict] | None = None,
) -> None:
    recipient = str(to_address or '').strip()
    if not recipient:
        raise GmailSendError('No recipient address was provided.')
    if not gmail_send_scope_granted():
        raise GmailSendError(
            'Gmail send is not included in the saved Google sign-in. '
            'Run python authorize_google_forms.py again and allow Kumbu Connect to send email.'
        )
    try:
        credentials = _get_credentials([GMAIL_SEND_SCOPE])
        service = build('gmail', 'v1', credentials=credentials, cache_discovery=False)
        subject_text = str(subject or '').strip() or 'Kumbu Connect'
        plain_text = str(body_text or '').strip()
        if html_body:
            message = MIMEMultipart('alternative')
            message['To'] = recipient
            message['Subject'] = subject_text
            message.attach(MIMEText(plain_text, 'plain', 'utf-8'))
            related = MIMEMultipart('related')
            related.attach(MIMEText(str(html_body), 'html', 'utf-8'))
            for image in inline_images or []:
                payload = image.get('data')
                if not payload:
                    continue
                part = MIMEImage(payload, _subtype=_image_subtype(image.get('mime') or 'image/png'))
                part.add_header('Content-ID', f"<{image['cid']}>")
                part.add_header('Content-Disposition', 'inline', filename=image.get('filename') or 'image.png')
                related.attach(part)
            message.attach(related)
        else:
            message = EmailMessage()
            message['To'] = recipient
            message['Subject'] = subject_text
            message.set_content(plain_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        service.users().messages().send(userId='me', body={'raw': raw}).execute()
    except GmailSendError:
        raise
    except HttpError as exc:
        status = getattr(getattr(exc, 'resp', None), 'status', None)
        detail = str(exc)
        if 'has not been used' in detail.lower() or 'it is disabled' in detail.lower():
            raise GmailSendError(
                'The Gmail API is turned off for this Google Cloud project. '
                'Enable it, then the waiting organisation emails can be sent.'
            ) from exc
        if status in {401, 403}:
            raise GmailSendError(
                'Gmail send is not authorized for the saved Google token. '
                'Run python authorize_google_forms.py again with the Kumbu Gmail account '
                'so the token includes gmail.send.'
            ) from exc
        current_app.logger.warning('Gmail send failed: %s', exc)
        raise GmailSendError('Gmail could not send that message.') from exc
    except Exception as exc:
        message = str(exc)
        if 'invalid_grant' in message.lower():
            raise GmailSendError(
                'The saved Google sign-in has expired. '
                'Run python authorize_google_forms.py again and allow Kumbu Connect to send email.'
            ) from exc
        if 'invalid_scope' in message.lower() or 'insufficient' in message.lower():
            raise GmailSendError(
                'Gmail send is not authorized for the saved Google token. '
                'Run python authorize_google_forms.py again with the Kumbu Gmail account '
                'so the token includes gmail.send.'
            ) from exc
        current_app.logger.warning('Gmail send failed: %s', exc)
        raise GmailSendError(message or 'Gmail could not send that message.') from exc
