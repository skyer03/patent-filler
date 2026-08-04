"""Local OCR fallback for scanned certificates.

Tesseract is intentionally optional: it keeps the text-layer path lightweight,
while scanned PDFs fail safely with a precise installation message when no local
OCR engine is configured.
"""

from __future__ import annotations

import io

import fitz


class OcrUnavailableError(RuntimeError):
    pass


def extract_document_text(document: fitz.Document, dpi: int = 250) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as error:  # pragma: no cover - depends on local install
        raise OcrUnavailableError(
            "扫描证书需要本机 OCR：请安装 Tesseract，并执行 `pip install pytesseract Pillow`。"
        ) from error

    pages: list[str] = []
    scale = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    for page in document:
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        pages.append(pytesseract.image_to_string(image, lang="chi_sim+eng"))
    return "\n".join(pages)
