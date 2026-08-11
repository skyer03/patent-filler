"""Local PaddleOCR fallback for scanned certificates."""

from __future__ import annotations

import io

import fitz

from .automation.recognizer import PaddleTextDetector, RecognitionError


class OcrUnavailableError(RuntimeError):
    pass


def extract_document_text(document: fitz.Document, dpi: int = 250) -> str:
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - depends on local install
        raise OcrUnavailableError(
            "扫描证书需要本地 OCR：请使用随项目提供的运行时，并确认 Pillow 已安装。"
        ) from error

    detector = PaddleTextDetector()
    pages: list[str] = []
    scale = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    for page in document:
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        try:
            observations = detector.detect(image)
        except RecognitionError as error:
            raise OcrUnavailableError(str(error)) from error
        observations.sort(key=lambda item: (item.box.top, item.box.left))
        pages.append("\n".join(item.text for item in observations))
    return "\n".join(pages)
