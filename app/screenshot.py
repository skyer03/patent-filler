"""Screenshot viewer helpers shared by the CLI and Tk UI."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .automation.recognizer import RecognitionResult, TemplateMatch, annotate_image


def load_screenshot(path: str | Path) -> Image.Image:
    try:
        return Image.open(path).convert("RGB")
    except OSError as error:
        raise ValueError(f"无法打开截图：{path}") from error


def save_annotated_screenshot(
    source: str | Path,
    target: str | Path,
    result: RecognitionResult,
    template_matches: list[TemplateMatch] | None = None,
) -> None:
    image = load_screenshot(source)
    annotate_image(image, result, template_matches or []).save(target)
