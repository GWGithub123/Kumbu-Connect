"""Google Forms API service — create CBO intake forms and read responses."""
from __future__ import annotations

import os
from io import BytesIO
from typing import Any

import google.auth
from flask import current_app
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2 import credentials as oauth2_credentials
from google.oauth2 import service_account
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

_FORMS_SCOPES = [
    'https://www.googleapis.com/auth/forms.body',
    'https://www.googleapis.com/auth/forms.responses.readonly',
]

_DRIVE_SCOPES = [
    *_FORMS_SCOPES,
    'https://www.googleapis.com/auth/drive.readonly',
]

GENERAL_IMAGE_UPLOAD_TITLE = 'General CBO Images'
BOOKKEEPING_UPLOAD_TITLE = 'Bookkeeping Document Images'
ANECDOTAL_STORY_TITLE = "Share one anecdotal story that captures your CBO's impact"
ADDITIONAL_TRACKING_FIELDS_TITLE = 'Additional operations and data collection to include in your bookkeeping workspace'

_UPLOAD_GUIDANCE_ITEMS: list[dict[str, str]] = [
    {
        'title': 'Supporting Uploads',
        'description': (
            'Google Forms API cannot create file-upload questions programmatically. '
            'Open the Edit Form link after creation and add two File upload questions with the exact titles '
            f'"{GENERAL_IMAGE_UPLOAD_TITLE}" and "{BOOKKEEPING_UPLOAD_TITLE}" so Kumbu Connect can sync those files.'
        ),
    },
    {
        'title': 'Add File Upload Question: General CBO Images',
        'description': (
            f'In the form editor, insert a File upload question titled exactly "{GENERAL_IMAGE_UPLOAD_TITLE}" '
            'to collect photos of the CBO, staff, tools, or activity on the ground.'
        ),
    },
    {
        'title': 'Add File Upload Question: Bookkeeping Document Images',
        'description': (
            f'In the form editor, insert a File upload question titled exactly "{BOOKKEEPING_UPLOAD_TITLE}" '
            'to collect ledger pages, receipts, cashbooks, and other bookkeeping records for digitization.'
        ),
    },
]

_INTAKE_FIELD_SPECS: list[dict[str, Any]] = [
    {
        'id': 'full_name',
        'title': 'Your full name',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
    {
        'id': 'whatsapp_phone_number',
        'title': 'WhatsApp phone number',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
    {
        'id': 'email_address',
        'title': 'Email address',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'position_role',
        'title': 'Your position / role in the CBO',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
    {
        'id': 'cbo_name',
        'title': 'CBO Name',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
    {
        'id': 'year_incorporated',
        'title': 'Year Incorporated',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
    {
        'id': 'cbo_office_address',
        'title': 'CBO Office Address',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
    {
        'id': 'cbo_program_locations',
        'title': 'CBO Program Locations',
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'bank_name',
        'title': 'Financial Institution (Bank) Name',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'bank_contact_information',
        'title': 'Financial Institution (Bank) Contact Information',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'registered_bank_account',
        'title': 'Do you have a registered bank account dedicated to CBO programs and activity?',
        'field_type': 'choice',
        'question_type': 'CHOICE',
        'required': True,
        'choices': ['Yes', 'No'],
    },
    {
        'id': 'full_cbo_budget',
        'title': 'Full CBO budget (past year)',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'total_cbo_expenses',
        'title': 'Total CBO expenses (past year)',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'expense_distribution',
        'title': 'Describe how expenses were distributed',
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'debt_liabilities',
        'title': 'CBO debt / liabilities',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'cash_reserves',
        'title': 'Full CBO cash reserves',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': False,
        'choices': [],
    },
    {
        'id': 'grants_obtained',
        'title': 'Describe past and present grants obtained (donor, amount, dates, purpose)',
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'milestones',
        'title': 'Describe any milestones achieved in the past three years',
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'anecdotal_story',
        'title': ANECDOTAL_STORY_TITLE,
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'additional_tracking_fields',
        'title': ADDITIONAL_TRACKING_FIELDS_TITLE,
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'references',
        'title': 'Please list three references: name, contact information (WhatsApp / email), and relationship with the CBO',
        'field_type': 'long_text',
        'question_type': 'PARAGRAPH',
        'required': False,
        'choices': [],
    },
    {
        'id': 'signature_and_date',
        'title': 'Signature and Date',
        'field_type': 'short_text',
        'question_type': 'SHORT_ANSWER',
        'required': True,
        'choices': [],
    },
]

_INTAKE_UPLOAD_SPECS: list[dict[str, Any]] = [
    {
        'id': 'general_cbo_images',
        'title': GENERAL_IMAGE_UPLOAD_TITLE,
        'field_type': 'file_upload',
        'accept': 'image/*',
        'multiple': True,
        'description': 'Collect photos of the CBO, staff, tools, or activity on the ground.',
    },
    {
        'id': 'bookkeeping_document_images',
        'title': BOOKKEEPING_UPLOAD_TITLE,
        'field_type': 'file_upload',
        'accept': 'image/*,.pdf',
        'multiple': True,
        'description': 'Collect ledger pages, receipts, cashbooks, and other bookkeeping records for digitization.',
    },
]

# ── CBO intake form question definitions ──────────────────────────────────────
# Each entry: (title, question_type, required, choices)
# question_type: 'SHORT_ANSWER' | 'PARAGRAPH' | 'CHOICE'
_INTAKE_QUESTIONS: list[tuple[str, str, bool, list[str]]] = [
    (
        str(field_spec['title']),
        str(field_spec['question_type']),
        bool(field_spec.get('required')),
        list(field_spec.get('choices') or []),
    )
    for field_spec in _INTAKE_FIELD_SPECS
]


def get_intake_form_schema() -> dict[str, Any]:
    return {
        'title': 'CBO Intake Form',
        'fields': [dict(field_spec) for field_spec in _INTAKE_FIELD_SPECS],
        'upload_fields': [dict(upload_spec) for upload_spec in _INTAKE_UPLOAD_SPECS],
        'guidance_items': [dict(item) for item in _UPLOAD_GUIDANCE_ITEMS],
    }


def _default_adc_path() -> str:
    return os.path.expanduser('~/.config/gcloud/application_default_credentials.json')


def _token_path() -> str:
    return (current_app.config.get('GOOGLE_OAUTH_TOKEN_JSON') or '').strip()


def _load_authorized_user_file(file_path: str, scopes: list[str]) -> Credentials:
    return oauth2_credentials.Credentials.from_authorized_user_file(
        file_path,
        scopes=scopes,
    )


def _get_credentials(scopes: list[str] | None = None) -> Credentials:
    scopes = scopes or _FORMS_SCOPES
    authorized_user_path = (current_app.config.get('GOOGLE_AUTHORIZED_USER_JSON') or '').strip()
    oauth_client_secret_json = (current_app.config.get('GOOGLE_OAUTH_CLIENT_SECRET_JSON') or '').strip()
    oauth_client_id = (current_app.config.get('GOOGLE_OAUTH_CLIENT_ID') or '').strip()
    oauth_client_secret = (current_app.config.get('GOOGLE_OAUTH_CLIENT_SECRET') or '').strip()
    oauth_is_configured = bool(
        authorized_user_path
        or oauth_client_secret_json
        or oauth_client_id
        or oauth_client_secret
    )

    if authorized_user_path:
        if not os.path.exists(authorized_user_path):
            raise RuntimeError(
                'GOOGLE_AUTHORIZED_USER_JSON is set, but the file does not exist. '
                'Point it to an authorized-user OAuth JSON for the Gmail account that should own the forms.'
            )
        return _load_authorized_user_file(authorized_user_path, scopes)

    token_path = _token_path()
    if token_path and os.path.exists(token_path):
        return _load_authorized_user_file(token_path, scopes)

    if current_app.config.get('ALLOW_GOOGLE_ADC_FALLBACK'):
        try:
            credentials, _ = google.auth.default(scopes=scopes)
            if credentials is not None:
                return credentials
        except DefaultCredentialsError:
            pass

    if oauth_is_configured:
        raise RuntimeError(
            'Google Forms is configured to use Gmail OAuth, but no user token has been saved yet. '
            'Run python authorize_google_forms.py after setting a valid desktop OAuth client JSON or '
            'correct GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET values. The current OAuth setup '
            'has not produced a saved token file at GOOGLE_OAUTH_TOKEN_JSON.'
        )

    sa_path = current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')
    if not sa_path or not os.path.exists(sa_path):
        raise RuntimeError(
            'No Google user OAuth credential is available, and FIREBASE_SERVICE_ACCOUNT_JSON is not set '
            'or the file does not exist. Configure GOOGLE_AUTHORIZED_USER_JSON or run '
            'gcloud auth application-default login for the Gmail account that should own the forms.'
        )
    return service_account.Credentials.from_service_account_file(sa_path, scopes=scopes)


def google_forms_enabled() -> bool:
    authorized_user_path = (current_app.config.get('GOOGLE_AUTHORIZED_USER_JSON') or '').strip()
    token_path = _token_path()
    sa_path = current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON', '')
    return bool(
        (authorized_user_path and os.path.exists(authorized_user_path))
        or (token_path and os.path.exists(token_path))
        or (current_app.config.get('ALLOW_GOOGLE_ADC_FALLBACK') and os.path.exists(_default_adc_path()))
        or (sa_path and os.path.exists(sa_path))
    )


def google_forms_user_oauth_ready() -> bool:
    client_secret_path = (current_app.config.get('GOOGLE_OAUTH_CLIENT_SECRET_JSON') or '').strip()
    return bool(client_secret_path and os.path.exists(client_secret_path))


def expected_upload_question_titles() -> dict[str, str]:
    return {
        'general_image': GENERAL_IMAGE_UPLOAD_TITLE,
        'bookkeeping_document': BOOKKEEPING_UPLOAD_TITLE,
    }


def _build_service():
    creds = _get_credentials(_FORMS_SCOPES)
    return build('forms', 'v1', credentials=creds, cache_discovery=False)


def _build_drive_service():
    creds = _get_credentials(_DRIVE_SCOPES)
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _refresh_error_message(context: str, needs_drive_access: bool = False) -> str:
    if needs_drive_access:
        return (
            f'{context} failed because the saved Google OAuth token does not include the required Google Drive access. '
            'Rerun authorize_google_forms.py with the same Gmail account that owns the form, then sync again.'
        )
    return (
        f'{context} failed because the saved Google OAuth token is no longer valid for the requested Google Forms scopes. '
        'Rerun authorize_google_forms.py with the same Gmail account that owns the form, then retry.'
    )


def _make_question_item(title: str, q_type: str, required: bool, choices: list[str]) -> dict[str, Any]:
    if q_type == 'SHORT_ANSWER':
        question = {
            'required': required,
            'textQuestion': {'paragraph': False},
        }
    elif q_type == 'PARAGRAPH':
        question = {
            'required': required,
            'textQuestion': {'paragraph': True},
        }
    elif q_type == 'CHOICE':
        question = {
            'required': required,
            'choiceQuestion': {
                'type': 'RADIO',
                'options': [{'value': choice} for choice in choices],
            },
        }
    else:
        question = {'required': required, 'textQuestion': {'paragraph': False}}

    return {
        'title': title,
        'questionItem': {'question': question},
    }


def _make_text_item(title: str, description: str) -> dict[str, Any]:
    return {
        'title': title,
        'description': description,
        'textItem': {},
    }


def _question_kind(question: dict[str, Any]) -> str:
    if question.get('fileUploadQuestion') is not None:
        return 'file_upload'
    if question.get('choiceQuestion') is not None:
        return 'choice'
    if question.get('textQuestion') is not None:
        return 'paragraph' if question.get('textQuestion', {}).get('paragraph') else 'short_answer'
    if question.get('scaleQuestion') is not None:
        return 'scale'
    if question.get('dateQuestion') is not None:
        return 'date'
    if question.get('timeQuestion') is not None:
        return 'time'
    return 'unknown'


def _question_map(form_meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(form_meta.get('items') or []):
        q_item = item.get('questionItem') or {}
        question = q_item.get('question') or {}
        question_id = question.get('questionId', '')
        if not question_id:
            continue
        mapping[question_id] = {
            'title': item.get('title', question_id),
            'description': item.get('description', ''),
            'kind': _question_kind(question),
            'required': bool(question.get('required')),
            'index': index,
        }
    return mapping


def ensure_intake_form_upload_guidance(form_id: str) -> None:
    try:
        service = _build_service()
        form_meta = service.forms().get(formId=form_id).execute()
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message(f'Failed to inspect Google Form {form_id}')) from exc
    except HttpError as exc:
        raise RuntimeError(f'Failed to inspect Google Form {form_id}: {exc}') from exc

    existing_titles = {str(item.get('title', '')).strip() for item in (form_meta.get('items') or [])}
    next_index = len(form_meta.get('items') or [])
    requests_payload = []
    for guidance in _UPLOAD_GUIDANCE_ITEMS:
        if guidance['title'] in existing_titles:
            continue
        requests_payload.append({
            'createItem': {
                'item': _make_text_item(guidance['title'], guidance['description']),
                'location': {'index': next_index},
            }
        })
        next_index += 1

    if not requests_payload:
        return

    try:
        service.forms().batchUpdate(
            formId=form_id,
            body={'requests': requests_payload},
        ).execute()
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message(f'Failed to add upload guidance to Google Form {form_id}')) from exc
    except HttpError as exc:
        raise RuntimeError(f'Failed to add upload guidance to Google Form {form_id}: {exc}') from exc


def create_cbo_intake_form(cbo_name: str) -> dict[str, str]:
    """Create a new CBO intake Google Form.

    Returns a dict with keys: form_id, edit_url, responder_url.
    Raises RuntimeError on failure.
    """
    form_body = {
        'info': {
            'title': f'{cbo_name} — CBO Intake Form',
            'documentTitle': f'{cbo_name} CBO Intake',
        }
    }
    try:
        service = _build_service()
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message('Failed to create Google Form')) from exc

    try:
        created = service.forms().create(body=form_body).execute()
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message('Failed to create Google Form')) from exc
    except HttpError as exc:
        raise RuntimeError(f'Failed to create Google Form: {exc}') from exc

    form_id = created['formId']

    requests_payload = []
    for index, (title, q_type, required, choices) in enumerate(_INTAKE_QUESTIONS):
        item = _make_question_item(title, q_type, required, choices)
        requests_payload.append({
            'createItem': {
                'item': item,
                'location': {'index': index},
            }
        })

    try:
        service.forms().batchUpdate(
            formId=form_id,
            body={'requests': requests_payload},
        ).execute()
        ensure_intake_form_upload_guidance(form_id)
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message(f'Failed to add questions to Google Form {form_id}')) from exc
    except HttpError as exc:
        raise RuntimeError(f'Failed to add questions to Google Form {form_id}: {exc}') from exc

    return {
        'form_id': form_id,
        'edit_url': f'https://docs.google.com/forms/d/{form_id}/edit',
        'responder_url': f'https://docs.google.com/forms/d/{form_id}/viewform',
    }


def get_form_response_bundle(form_id: str) -> dict[str, Any]:
    try:
        service = _build_service()
        form_meta = service.forms().get(formId=form_id).execute()
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message(f'Failed to fetch form metadata for {form_id}')) from exc
    except HttpError as exc:
        raise RuntimeError(f'Failed to fetch form metadata for {form_id}: {exc}') from exc

    responses: list[dict[str, Any]] = []
    page_token = None
    try:
        while True:
            payload = service.forms().responses().list(
                formId=form_id,
                pageToken=page_token,
            ).execute()
            responses.extend(payload.get('responses') or [])
            page_token = payload.get('nextPageToken')
            if not page_token:
                break
    except RefreshError as exc:
        raise RuntimeError(_refresh_error_message(f'Failed to fetch responses for form {form_id}')) from exc
    except HttpError as exc:
        raise RuntimeError(f'Failed to fetch responses for form {form_id}: {exc}') from exc

    question_map = _question_map(form_meta)
    ordered_questions = sorted(question_map.items(), key=lambda item: item[1]['index'])
    structured_responses = []

    for response in responses:
        answers_by_id = response.get('answers') or {}
        structured_answers = []
        file_uploads = []

        for question_id, meta in ordered_questions:
            answer_obj = answers_by_id.get(question_id)
            if not answer_obj:
                continue

            text_answers = answer_obj.get('textAnswers', {}).get('answers', [])
            if text_answers:
                values = [str(answer.get('value', '')).strip() for answer in text_answers if str(answer.get('value', '')).strip()]
                structured_answers.append({
                    'question_id': question_id,
                    'title': meta['title'],
                    'kind': meta['kind'],
                    'answer_type': 'text',
                    'values': values,
                })
                continue

            uploaded_files = answer_obj.get('fileUploadAnswers', {}).get('answers', [])
            if uploaded_files:
                files = []
                for uploaded in uploaded_files:
                    file_info = {
                        'file_id': uploaded.get('fileId', ''),
                        'file_name': uploaded.get('fileName', ''),
                        'mime_type': uploaded.get('mimeType', ''),
                    }
                    files.append(file_info)
                    file_uploads.append({
                        'question_id': question_id,
                        'question_title': meta['title'],
                        **file_info,
                    })

                structured_answers.append({
                    'question_id': question_id,
                    'title': meta['title'],
                    'kind': meta['kind'],
                    'answer_type': 'file_upload',
                    'files': files,
                })

        structured_responses.append({
            'response_id': response.get('responseId', ''),
            'create_time': response.get('createTime', ''),
            'submitted_at': response.get('lastSubmittedTime', ''),
            'respondent_email': response.get('respondentEmail', ''),
            'answers': structured_answers,
            'file_uploads': file_uploads,
            'raw_response': response,
        })

    structured_responses.sort(key=lambda item: item.get('submitted_at') or item.get('create_time') or '', reverse=True)
    return {
        'form_id': form_id,
        'form_title': (form_meta.get('info') or {}).get('title', ''),
        'questions': [
            {
                'question_id': question_id,
                **meta,
            }
            for question_id, meta in ordered_questions
        ],
        'responses': structured_responses,
    }


def download_drive_file(file_id: str) -> dict[str, Any]:
    try:
        drive_service = _build_drive_service()
        metadata = drive_service.files().get(
            fileId=file_id,
            fields='id,name,mimeType,webViewLink,thumbnailLink',
        ).execute()
        request = drive_service.files().get_media(fileId=file_id)
        output = BytesIO()
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except RefreshError as exc:
        raise RuntimeError(
            _refresh_error_message(
                f'Failed to download Google Drive file {file_id}',
                needs_drive_access=True,
            )
        ) from exc
    except HttpError as exc:
        raise RuntimeError(
            f'Failed to download Google Drive file {file_id}. '
            'If this is a permissions error, rerun authorize_google_forms.py with the same Gmail account that owns the form so the saved token includes Google Drive read access.'
        ) from exc

    return {
        'file_id': metadata.get('id', file_id),
        'file_name': metadata.get('name', ''),
        'mime_type': metadata.get('mimeType', 'application/octet-stream'),
        'web_view_link': metadata.get('webViewLink', ''),
        'thumbnail_link': metadata.get('thumbnailLink', ''),
        'bytes': output.getvalue(),
    }


def get_form_responses(form_id: str) -> list[dict[str, Any]]:
    """Return all submitted responses for a form as a list of flat dicts."""
    bundle = get_form_response_bundle(form_id)
    results = []
    for response in bundle.get('responses') or []:
        row: dict[str, Any] = {
            'response_id': response.get('response_id', ''),
            'submitted_at': response.get('submitted_at', ''),
            'respondent_email': response.get('respondent_email', ''),
        }
        for answer in response.get('answers') or []:
            if answer.get('answer_type') == 'text':
                row[answer.get('title', '')] = ', '.join(answer.get('values') or [])
            elif answer.get('answer_type') == 'file_upload':
                row[answer.get('title', '')] = ', '.join(
                    file_entry.get('file_name', '') for file_entry in (answer.get('files') or []) if file_entry.get('file_name')
                )
        results.append(row)
    return results
