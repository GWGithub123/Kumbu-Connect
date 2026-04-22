"""Helpers for turning uploaded bookkeeping files into page images."""
from io import BytesIO
import os
import subprocess
import tempfile

from PIL import Image, ImageOps
import pypdfium2 as pdfium

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - optional dependency until installed
    register_heif_opener = None

if register_heif_opener:
    register_heif_opener()


class DocumentIngestionError(RuntimeError):
    """Raised when an uploaded bookkeeping file cannot be ingested."""


_IMAGE_FORMAT_TO_MIME = {
    'JPEG': 'image/jpeg',
    'PNG': 'image/png',
    'WEBP': 'image/webp',
    'HEIC': 'image/heic',
    'HEIF': 'image/heif',
}


def prepare_uploaded_document(uploaded, max_pdf_pages: int) -> dict:
    raw_filename = uploaded.filename or 'bookkeeping-document'
    file_bytes = uploaded.read()
    if not file_bytes:
        raise DocumentIngestionError(f'{raw_filename}: file was empty.')

    mime_type = _resolve_upload_mime_type(raw_filename, uploaded.mimetype or '', file_bytes)
    filename = _normalize_upload_filename(raw_filename, mime_type)

    return prepare_document_bytes(filename, mime_type, file_bytes, max_pdf_pages=max_pdf_pages)


def prepare_document_bytes(filename: str, mime_type: str, file_bytes: bytes, max_pdf_pages: int) -> dict:
    if not file_bytes:
        raise DocumentIngestionError(f'{filename}: file was empty.')

    mime_type = _resolve_upload_mime_type(filename, mime_type, file_bytes)
    filename = _normalize_upload_filename(filename, mime_type)

    if mime_type == 'application/pdf' or filename.lower().endswith('.pdf'):
        return {
            'source_filename': filename,
            'source_mime_type': 'application/pdf',
            'source_bytes': file_bytes,
            'source_channel': 'pdf_upload',
            'pages': _render_pdf_pages(filename, file_bytes, max_pdf_pages),
        }

    if mime_type in {'image/heic', 'image/heif'} or filename.lower().endswith(('.heic', '.heif')):
        converted_bytes, converted_mime_type = _render_heif_as_png(filename, file_bytes)
        return {
            'source_filename': filename,
            'source_mime_type': mime_type,
            'source_bytes': file_bytes,
            'source_channel': 'web_upload',
            'pages': [{
                'filename': f'{os.path.splitext(filename)[0]}.png',
                'mime_type': converted_mime_type,
                'image_bytes': converted_bytes,
                'page_number': 1,
            }],
        }

    normalized_bytes, normalized_mime = _normalize_image_orientation(filename, file_bytes, mime_type)
    return {
        'source_filename': filename,
        'source_mime_type': mime_type,
        'source_bytes': file_bytes,
        'source_channel': 'web_upload',
        'pages': [{
            'filename': filename,
            'mime_type': normalized_mime,
            'image_bytes': normalized_bytes,
            'page_number': 1,
        }],
    }


def expand_uploaded_document(uploaded, max_pdf_pages: int) -> list[dict]:
    prepared = prepare_uploaded_document(uploaded, max_pdf_pages=max_pdf_pages)
    return prepared['pages']


def render_bookkeeping_preview(filename: str, mime_type: str, file_bytes: bytes) -> tuple[bytes, str]:
    if mime_type == 'application/pdf' or filename.lower().endswith('.pdf'):
        pages = _render_pdf_pages(filename, file_bytes, max_pdf_pages=1)
        first_page = pages[0]
        return first_page['image_bytes'], first_page['mime_type']

    return file_bytes, mime_type


def _render_pdf_pages(filename: str, file_bytes: bytes, max_pdf_pages: int) -> list[dict]:
    try:
        pdf = pdfium.PdfDocument(file_bytes)
    except Exception as exc:
        raise DocumentIngestionError(f'{filename}: could not open PDF.') from exc

    page_count = len(pdf)
    if page_count == 0:
        raise DocumentIngestionError(f'{filename}: PDF contained no pages.')
    if page_count > max_pdf_pages:
        raise DocumentIngestionError(f'{filename}: PDF exceeds the {max_pdf_pages}-page limit.')

    rendered_pages = []
    base_name = os.path.splitext(filename)[0]
    for index in range(page_count):
        page = pdf[index]
        pil_image = page.render(scale=2.2).to_pil()
        buffer = BytesIO()
        pil_image.save(buffer, format='PNG')
        rendered_pages.append({
            'filename': f'{base_name}-page-{index + 1}.png',
            'mime_type': 'image/png',
            'image_bytes': buffer.getvalue(),
            'page_number': index + 1,
        })
        page.close()

    pdf.close()
    return rendered_pages


def _normalize_image_orientation(filename: str, file_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Apply EXIF transpose so pixel data matches display orientation, then re-encode.

    Mobile cameras store pixels in the sensor's native (landscape) layout and
    rely on an EXIF Orientation tag to tell viewers how to rotate for display.
    Desktop files may already have baked-in orientation with no EXIF tag.
    Normalizing here ensures OpenAI Vision always receives upright pixels
    regardless of where the photo came from.
    """
    canonical_mime = _resolve_upload_mime_type(filename, mime_type, file_bytes)
    if not canonical_mime.startswith('image/'):
        return file_bytes, canonical_mime
    try:
        with Image.open(BytesIO(file_bytes)) as image:
            transposed = ImageOps.exif_transpose(image)
            if transposed is image:
                # No EXIF rotation was needed — return the original bytes unchanged.
                # Keep the sniffed canonical MIME so generic mobile camera uploads like
                # application/octet-stream still flow through the image pipeline.
                return file_bytes, canonical_mime

            output_format = _output_format_for_mime(canonical_mime)
            if output_format == 'JPEG' and transposed.mode != 'RGB':
                transposed = transposed.convert('RGB')
            elif output_format in {'PNG', 'WEBP'} and transposed.mode not in ('RGB', 'RGBA'):
                transposed = transposed.convert('RGB')

            buffer = BytesIO()
            save_kwargs = {'quality': 95} if output_format in {'JPEG', 'WEBP'} else {}
            transposed.save(buffer, format=output_format, **save_kwargs)
            out_mime = {
                'JPEG': 'image/jpeg',
                'PNG': 'image/png',
                'WEBP': 'image/webp',
            }[output_format]
            return buffer.getvalue(), out_mime
    except Exception:
        return file_bytes, canonical_mime


def _render_heif_as_png(filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    if register_heif_opener:
        try:
            with Image.open(BytesIO(file_bytes)) as image:
                normalized = ImageOps.exif_transpose(image)
                if normalized.mode not in ('RGB', 'RGBA'):
                    normalized = normalized.convert('RGB')
                buffer = BytesIO()
                normalized.save(buffer, format='PNG')
                return buffer.getvalue(), 'image/png'
        except Exception:
            pass

    sips_path = _find_sips()
    if sips_path:
        try:
            return _render_heif_as_png_via_sips(filename, file_bytes, sips_path)
        except Exception as exc:
            raise DocumentIngestionError(f'{filename}: could not decode HEIC/HEIF image.') from exc

    raise DocumentIngestionError(
        f'{filename}: HEIC/HEIF support is not available in the running server process. Restart the app after installing pillow-heif, or upload JPG/PNG instead.'
    )


def _render_heif_as_png_via_sips(filename: str, file_bytes: bytes, sips_path: str) -> tuple[bytes, str]:
    source_suffix = os.path.splitext(filename)[1] or '.heic'
    with tempfile.TemporaryDirectory(prefix='bookkeeping-heif-') as temp_dir:
        source_path = os.path.join(temp_dir, f'source{source_suffix}')
        target_path = os.path.join(temp_dir, 'converted.png')
        with open(source_path, 'wb') as source_file:
            source_file.write(file_bytes)

        completed = subprocess.run(
            [sips_path, '-s', 'format', 'png', source_path, '--out', target_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not os.path.exists(target_path):
            stderr = (completed.stderr or completed.stdout or '').strip()
            raise RuntimeError(stderr or 'sips conversion failed')

        with open(target_path, 'rb') as converted_file:
            return converted_file.read(), 'image/png'


def _find_sips() -> str:
    for candidate in ('/usr/bin/sips', 'sips'):
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
    return ''


def _normalize_upload_filename(filename: str, mime_type: str) -> str:
    clean = os.path.basename(filename).strip() or 'bookkeeping-document'
    extension = os.path.splitext(clean)[1].lower()
    if extension:
        return clean
    return clean + {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/heic': '.heic',
        'image/heif': '.heif',
        'application/pdf': '.pdf',
    }.get(mime_type.lower(), '.jpg')


def _guess_mime_type(filename: str) -> str:
    extension = os.path.splitext(filename.lower())[1]
    return {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.heic': 'image/heic',
        '.heif': 'image/heif',
        '.pdf': 'application/pdf',
    }.get(extension, 'application/octet-stream')


def _resolve_upload_mime_type(filename: str, mime_type: str, file_bytes: bytes) -> str:
    declared = str(mime_type or '').strip().lower()
    if declared == 'image/jpg':
        declared = 'image/jpeg'

    guessed = _guess_mime_type(filename)
    sniffed = _sniff_mime_type_from_bytes(file_bytes)

    if file_bytes[:4] == b'%PDF':
        return 'application/pdf'
    if declared == 'application/pdf' or guessed == 'application/pdf':
        return 'application/pdf'

    if sniffed.startswith('image/'):
        if not declared or declared == 'application/octet-stream':
            return sniffed
        if declared.startswith('image/'):
            return sniffed

    if declared.startswith('image/'):
        return declared
    if guessed.startswith('image/'):
        return guessed
    return declared or guessed


def _sniff_mime_type_from_bytes(file_bytes: bytes) -> str:
    if not file_bytes:
        return ''
    try:
        with Image.open(BytesIO(file_bytes)) as image:
            return _IMAGE_FORMAT_TO_MIME.get(str(image.format or '').upper(), '')
    except Exception:
        return ''


def _output_format_for_mime(mime_type: str) -> str:
    return {
        'image/jpeg': 'JPEG',
        'image/png': 'PNG',
        'image/webp': 'WEBP',
    }.get(mime_type, 'PNG')