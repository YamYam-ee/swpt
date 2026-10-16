"""Textigenkänning (OCR) för skannade dokument med Windows inbyggda OCR-motor (Windows.Media.Ocr).

Körs i hjälpprocesser. Sidor i en PDF som redan har text används som de är; bara sidor utan
text (inskannade) och bildfiler tolkas.
"""
import asyncio
import os
import threading

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
MIN_PAGE_TEXT = 25          # färre tecken än så på en sida = troligen inskannad
PDF_DPI = 300
MAX_PAGES = 300

_local = threading.local()     # en OCR-motor per tråd


def candidate(path):
    """Filer som kan behöva OCR: PDF:er och bilder."""
    ext = os.path.splitext(path)[1].lower()
    return ext == ".pdf" or ext in IMAGE_EXT


def available():
    try:
        from winrt.windows.media.ocr import OcrEngine
        return bool(list(OcrEngine.available_recognizer_languages))
    except Exception:
        return False


def _get_engine():
    engine = getattr(_local, "engine", None)
    if engine is None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        engine = OcrEngine.try_create_from_language(Language("sv-SE")) \
            or OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            langs = list(OcrEngine.available_recognizer_languages)
            if langs:
                engine = OcrEngine.try_create_from_language(langs[0])
        _local.engine = engine
    return engine


async def _recognize_result(gray, width, height):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    writer = DataWriter()
    writer.write_bytes(gray)
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.GRAY8, width, height)
    return await _get_engine().recognize_async(bitmap)


async def _recognize(gray, width, height):
    result = await _recognize_result(gray, width, height)
    return "\n".join(line.text for line in result.lines)


def page_words(page, is_image=False):
    """OCR av en sida med ordens lägen: [(x0, y0, x1, y1, ord, radnummer)] i sidans koordinater."""
    if _get_engine() is None:
        return []
    gray, w, h = _page_image(page, is_image)
    zx, zy = w / max(page.rect.width, 1), h / max(page.rect.height, 1)
    result = asyncio.run(_recognize_result(gray, w, h))
    out = []
    for line_no, line in enumerate(result.lines):
        for word in line.words:
            r = word.bounding_rect
            out.append((r.x / zx, r.y / zy, (r.x + r.width) / zx, (r.y + r.height) / zy, word.text, line_no))
    return out


def needs_ocr(page, is_image=False):
    return is_image or len(page.get_text().strip()) < MIN_PAGE_TEXT


def _page_image(page, is_image):
    import pymupdf as fitz
    from winrt.windows.media.ocr import OcrEngine
    limit = OcrEngine.max_image_dimension
    rect = page.rect
    zoom = (1.0 if is_image else PDF_DPI / 72)
    if is_image and max(rect.width, rect.height) < 1600:      # små bilder: förstora för bättre tolkning
        zoom = 1600 / max(rect.width, rect.height, 1)
    zoom = min(zoom, limit / max(rect.width, rect.height, 1))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    return pix.samples, pix.width, pix.height


def ocr_file(path):
    """Returnerar dokumentets text (med OCR där det behövs), eller None om inget behövde tolkas."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    fitz.TOOLS.mupdf_display_errors(False)
    is_image = os.path.splitext(path)[1].lower() in IMAGE_EXT
    if _get_engine() is None:
        return None
    parts, did_ocr = [], False
    try:
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                if i >= MAX_PAGES:
                    break
                text = "" if is_image else page.get_text()
                if len(text.strip()) >= MIN_PAGE_TEXT:
                    parts.append(text)
                    continue
                gray, w, h = _page_image(page, is_image)
                parts.append(asyncio.run(_recognize(gray, w, h)))
                did_ocr = True
    except Exception:
        return None
    return "\n".join(parts) if did_ocr else None
