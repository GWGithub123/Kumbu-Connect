"""Cloud file storage helpers backed by Firebase Storage with local fallback."""
import os
import uuid
from datetime import datetime

from flask import current_app
from werkzeug.utils import secure_filename

try:
    import firebase_admin
    from firebase_admin import credentials, storage
except ImportError:
    firebase_admin = None
    credentials = None
    storage = None


def store_bookkeeping_image(cbo, filename: str, image_bytes: bytes, mime_type: str) -> dict:
    return _store_binary_file(
        cbo_id=cbo.id,
        filename=filename,
        file_bytes=image_bytes,
        mime_type=mime_type,
        object_prefix='bookkeeping_uploads',
        local_upload_dir=current_app.config.get('BOOKKEEPING_UPLOAD_DIR'),
    )


def store_supporting_file(
    cbo_id: int,
    filename: str,
    file_bytes: bytes,
    mime_type: str,
    *,
    object_prefix: str,
    local_upload_dir: str,
) -> dict:
    return _store_binary_file(
        cbo_id=cbo_id,
        filename=filename,
        file_bytes=file_bytes,
        mime_type=mime_type,
        object_prefix=object_prefix,
        local_upload_dir=local_upload_dir,
    )


def ensure_bookkeeping_image_in_storage(document) -> None:
    if getattr(document, 'storage_backend', '') == 'firebase' and getattr(document, 'storage_object_path', ''):
        return

    bucket_name = _resolve_bucket_name()
    if not bucket_name:
        return

    local_absolute = _local_absolute_path(document.stored_path)
    if not local_absolute or not os.path.exists(local_absolute):
        return

    with open(local_absolute, 'rb') as file_handle:
        image_bytes = file_handle.read()

    stored = store_bookkeeping_image(document.cbo, document.original_filename, image_bytes, document.mime_type)
    document.storage_backend = stored['storage_backend']
    document.storage_bucket = stored['storage_bucket']
    document.storage_object_path = stored['storage_object_path']
    document.stored_path = stored['stored_path']


def get_bookkeeping_image_bytes(document) -> tuple[bytes, str]:
    return get_stored_file_bytes(
        storage_backend=getattr(document, 'storage_backend', '') or 'local',
        stored_path=document.stored_path,
        mime_type=document.mime_type or 'image/jpeg',
        storage_bucket=getattr(document, 'storage_bucket', ''),
        storage_object_path=getattr(document, 'storage_object_path', ''),
    )


def get_stored_file_bytes(
    *,
    storage_backend: str,
    stored_path: str,
    mime_type: str,
    storage_bucket: str = '',
    storage_object_path: str = '',
) -> tuple[bytes, str]:
    backend = storage_backend or 'local'
    object_path = storage_object_path or stored_path
    if backend == 'firebase' and object_path:
        bucket_name = storage_bucket or _resolve_bucket_name()
        if not bucket_name:
            raise FileNotFoundError('Firebase Storage bucket is not configured.')
        app = _get_firebase_app(bucket_name)
        bucket = storage.bucket(name=bucket_name, app=app)
        blob = bucket.blob(object_path)
        if not blob.exists():
            raise FileNotFoundError('Stored file was not found in Firebase Storage.')
        return blob.download_as_bytes(), mime_type or 'application/octet-stream'

    local_absolute = _local_absolute_path(stored_path)
    if not local_absolute or not os.path.exists(local_absolute):
        raise FileNotFoundError('Stored file was not found on disk.')
    with open(local_absolute, 'rb') as file_handle:
        return file_handle.read(), mime_type or 'application/octet-stream'


def delete_bookkeeping_image(document) -> None:
    delete_stored_file(
        storage_backend=getattr(document, 'storage_backend', '') or 'local',
        stored_path=document.stored_path,
        storage_bucket=getattr(document, 'storage_bucket', ''),
        storage_object_path=getattr(document, 'storage_object_path', ''),
    )


def delete_stored_file(
    *,
    storage_backend: str,
    stored_path: str,
    storage_bucket: str = '',
    storage_object_path: str = '',
) -> None:
    backend = storage_backend or 'local'
    object_path = storage_object_path or stored_path
    if backend == 'firebase' and object_path:
        bucket_name = storage_bucket or _resolve_bucket_name()
        if not bucket_name:
            return
        app = _get_firebase_app(bucket_name)
        bucket = storage.bucket(name=bucket_name, app=app)
        blob = bucket.blob(object_path)
        if blob.exists():
            blob.delete()
        return

    local_absolute = _local_absolute_path(stored_path)
    if local_absolute and os.path.exists(local_absolute):
        os.remove(local_absolute)


def is_stored_file_available(
    *,
    storage_backend: str,
    stored_path: str,
    storage_bucket: str = '',
    storage_object_path: str = '',
) -> bool:
    backend = storage_backend or 'local'
    object_path = storage_object_path or stored_path
    if backend == 'firebase' and object_path:
        bucket_name = storage_bucket or _resolve_bucket_name()
        if not bucket_name:
            return False
        try:
            app = _get_firebase_app(bucket_name)
            bucket = storage.bucket(name=bucket_name, app=app)
            return bucket.blob(object_path).exists()
        except Exception:
            return False

    local_absolute = _local_absolute_path(stored_path)
    return bool(local_absolute and os.path.exists(local_absolute))


def _get_firebase_app(bucket_name: str):
    if not firebase_admin:
        raise RuntimeError('firebase-admin is not installed.')

    service_account_path = current_app.config.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not service_account_path:
        raise RuntimeError('FIREBASE_SERVICE_ACCOUNT_JSON is not configured.')

    app_name = 'kumbu-connect-firestore'
    try:
        return firebase_admin.get_app(app_name)
    except ValueError:
        options = {'storageBucket': bucket_name}
        project_id = current_app.config.get('FIREBASE_PROJECT_ID')
        if project_id:
            options['projectId'] = project_id
        return firebase_admin.initialize_app(
            credentials.Certificate(service_account_path),
            options=options,
            name=app_name,
        )


def _resolve_bucket_name() -> str:
    if not firebase_admin or not storage:
        return ''

    configured = current_app.config.get('FIREBASE_STORAGE_BUCKET', '').strip()
    candidates = [configured] if configured else []
    project_id = current_app.config.get('FIREBASE_PROJECT_ID', '').strip()
    if project_id:
        candidates.extend([
            f'{project_id}.firebasestorage.app',
            f'{project_id}.appspot.com',
        ])

    seen = set()
    for bucket_name in candidates:
        if not bucket_name or bucket_name in seen:
            continue
        seen.add(bucket_name)
        try:
            app = _get_firebase_app(bucket_name)
            bucket = storage.bucket(name=bucket_name, app=app)
            if bucket.exists():
                return bucket_name
        except Exception:
            continue
    return ''


def _store_binary_file(
    *,
    cbo_id: int,
    filename: str,
    file_bytes: bytes,
    mime_type: str,
    object_prefix: str,
    local_upload_dir: str,
) -> dict:
    bucket_name = _resolve_bucket_name()
    stored_name = _build_storage_filename(filename, mime_type)
    object_path = f'{object_prefix.rstrip("/")}/cbo-{cbo_id}/{stored_name}'

    if bucket_name:
        try:
            app = _get_firebase_app(bucket_name)
            bucket = storage.bucket(name=bucket_name, app=app)
            blob = bucket.blob(object_path)
            blob.upload_from_string(file_bytes, content_type=mime_type)
            return {
                'storage_backend': 'firebase',
                'stored_path': object_path,
                'storage_bucket': bucket_name,
                'storage_object_path': object_path,
            }
        except Exception:
            current_app.logger.exception(
                'Failed to store uploaded file %s for CBO %s in Firebase Storage; falling back to local storage',
                filename,
                cbo_id,
            )

    if not current_app.config.get('ALLOW_LOCAL_FILE_STORAGE_FALLBACK', True):
        raise RuntimeError(
            'Remote file storage is required, but Firebase Storage is not configured for this deployment.'
        )

    local_path = _store_locally(local_upload_dir, cbo_id, stored_name, file_bytes)
    return {
        'storage_backend': 'local',
        'stored_path': local_path,
        'storage_bucket': '',
        'storage_object_path': '',
    }


def _store_locally(upload_dir: str, cbo_id: int, filename: str, file_bytes: bytes) -> str:
    target_dir = os.path.join(upload_dir, f'cbo-{cbo_id}')
    os.makedirs(target_dir, exist_ok=True)
    absolute_path = os.path.join(target_dir, filename)
    with open(absolute_path, 'wb') as file_handle:
        file_handle.write(file_bytes)
    return os.path.relpath(absolute_path, current_app.root_path)


def _local_absolute_path(stored_path: str) -> str:
    if not stored_path:
        return ''
    if os.path.isabs(stored_path):
        return stored_path
    return os.path.join(current_app.root_path, stored_path)


def _build_storage_filename(filename: str, mime_type: str) -> str:
    safe_name = secure_filename(filename or '')
    stem = os.path.splitext(safe_name)[0].strip('-_.')
    extension = _normalize_extension(safe_name, mime_type)
    suffix = f'-{stem}' if stem else ''
    return f'{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex}{suffix}{extension}'


def _normalize_extension(filename: str, mime_type: str) -> str:
    extension = os.path.splitext(filename or '')[1].lower().strip()
    if extension:
        return extension
    return {
        'application/pdf': '.pdf',
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/heic': '.heic',
        'image/heif': '.heif',
    }.get((mime_type or '').lower(), '.bin')