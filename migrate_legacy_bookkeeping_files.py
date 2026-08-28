"""Import exported legacy bookkeeping files into the new Firebase bucket.

This script does not use any legacy Firebase credentials. Instead, it expects a
fresh export of the old object bytes on disk and uploads those bytes into the
new Kumbu-owned Firebase bucket while updating bookkeeping_documents rows in the
target database.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, storage
from sqlalchemy import create_engine, text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, help='Directory containing exported legacy files by object path.')
    parser.add_argument('--database-url', required=True, help='SQLAlchemy database URL for the target Kumbu database.')
    parser.add_argument('--firebase-service-account-json', required=True, help='Path to the new Firebase service account JSON file.')
    parser.add_argument('--firebase-bucket', required=True, help='Destination Firebase Storage bucket name.')
    parser.add_argument(
        '--legacy-bucket',
        default='kumbu-connect.firebasestorage.app',
        help='Legacy Firebase bucket name to migrate from in bookkeeping_documents rows.',
    )
    parser.add_argument('--dry-run', action='store_true', help='Report what would be uploaded without changing storage or the database.')
    return parser


def _firebase_app(service_account_json: str, firebase_bucket: str):
    try:
        return firebase_admin.get_app('legacy-bookkeeping-import')
    except ValueError:
        return firebase_admin.initialize_app(
            credentials.Certificate(service_account_json),
            {'storageBucket': firebase_bucket},
            name='legacy-bookkeeping-import',
        )


def main() -> int:
    args = _build_parser().parse_args()
    source_root = Path(args.source_root).expanduser().resolve()
    if not source_root.exists() or not source_root.is_dir():
        print(f'Source root does not exist or is not a directory: {source_root}', file=sys.stderr)
        return 1

    engine = create_engine(args.database_url)
    rows = []
    with engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT id, COALESCE(storage_object_path, stored_path) AS object_path, mime_type
                    FROM bookkeeping_documents
                    WHERE storage_backend = 'firebase' AND storage_bucket = :legacy_bucket
                    ORDER BY id
                    """
                ),
                {'legacy_bucket': args.legacy_bucket},
            ).mappings()
        )

    if not rows:
        print('No legacy Firebase-backed bookkeeping rows matched the requested bucket.')
        return 0

    bucket = None
    if not args.dry_run:
        app = _firebase_app(args.firebase_service_account_json, args.firebase_bucket)
        bucket = storage.bucket(name=args.firebase_bucket, app=app)

    uploaded = 0
    missing = []

    with engine.begin() as connection:
        for row in rows:
            object_path = str(row['object_path'] or '').strip()
            local_path = source_root / object_path
            if not local_path.exists():
                missing.append((row['id'], object_path))
                print(f'MISSING\t{row["id"]}\t{object_path}')
                continue

            if args.dry_run:
                print(f'DRY_RUN\t{row["id"]}\t{object_path}')
                continue

            blob = bucket.blob(object_path)
            blob.upload_from_filename(str(local_path), content_type=str(row['mime_type'] or 'application/octet-stream'))

            connection.execute(
                text(
                    """
                    UPDATE bookkeeping_documents
                    SET storage_bucket = :firebase_bucket,
                        storage_object_path = :object_path,
                        stored_path = :object_path,
                        storage_backend = 'firebase'
                    WHERE id = :document_id
                    """
                ),
                {
                    'firebase_bucket': args.firebase_bucket,
                    'object_path': object_path,
                    'document_id': row['id'],
                },
            )
            uploaded += 1
            print(f'UPLOADED\t{row["id"]}\t{object_path}')

    print(f'Uploaded {uploaded} legacy bookkeeping files into {args.firebase_bucket}.')
    if missing:
        print('Missing exported source files:')
        for document_id, object_path in missing:
            print(f'  {document_id}\t{object_path}')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())