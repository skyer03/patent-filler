"""OCR and template based page recognition for M2.

Recognition is intentionally conservative: missing page anchors, unknown page
state, or unavailable OCR produce a result that cannot be used for input.
Coordinates are local to the current screenshot.  The window binding layer is
responsible for translating them to screen coordinates after every capture.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from PIL import Image, ImageOps

from .profile import LocatorSpec, PageProfile


class RecognitionError(RuntimeError):
    """Raised when an image cannot be inspected."""


@dataclass(frozen=True)
class BoundingBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def translated(self, dx: int, dy: int) -> "BoundingBox":
        return BoundingBox(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }


@dataclass(frozen=True)
class TextObservation:
    text: str
    box: BoundingBox
    confidence: float = 1.0


@dataclass(frozen=True)
class LocatedAnchor:
    id: str
    text: str
    box: BoundingBox
    confidence: float
    method: str = "ocr"


@dataclass(frozen=True)
class LocatedControl:
    id: str
    label: str
    box: BoundingBox
    confidence: float
    method: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "box": self.box.to_dict(),
            "confidence": self.confidence,
            "method": self.method,
        }


@dataclass
class RecognitionResult:
    profile_id: str
    anchors: dict[str, LocatedAnchor] = field(default_factory=dict)
    controls: dict[str, LocatedControl] = field(default_factory=dict)
    missing_anchors: list[str] = field(default_factory=list)
    missing_controls: list[str] = field(default_factory=list)
    page_state: str = "unknown"
    edit_state: str = "unknown"
    methods: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def safe_for_input(self) -> bool:
        return (
            not self.missing_anchors
            and self.page_state == "ready"
            and not self.issues
        )

    def has_control(self, control_id: str) -> bool:
        return control_id in self.controls

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "anchors": {
                key: {
                    "id": value.id,
                    "text": value.text,
                    "box": value.box.to_dict(),
                    "confidence": value.confidence,
                    "method": value.method,
                }
                for key, value in self.anchors.items()
            },
            "controls": {key: value.to_dict() for key, value in self.controls.items()},
            "missing_anchors": self.missing_anchors,
            "missing_controls": self.missing_controls,
            "page_state": self.page_state,
            "edit_state": self.edit_state,
            "methods": self.methods,
            "issues": self.issues,
            "safe_for_input": self.safe_for_input,
        }


class TextDetector(Protocol):
    def detect(self, image: Image.Image) -> list[TextObservation]:
        ...


class TesseractTextDetector:
    """Optional local OCR adapter.  It never treats OCR unavailability as success."""

    def __init__(self, language: str = "chi_sim+eng") -> None:
        self.language = language

    def detect(self, image: Image.Image) -> list[TextObservation]:
        try:
            import pytesseract
        except ImportError as error:  # pragma: no cover - environment dependent
            raise RecognitionError(
                "OCR 不可用：请安装本机 Tesseract、chi_sim 语言包和 pytesseract。"
            ) from error

        self._configure_executable(pytesseract)
        try:
            data = pytesseract.image_to_data(
                image, lang=self.language, output_type=pytesseract.Output.DICT
            )
        except (pytesseract.TesseractError, pytesseract.TesseractNotFoundError) as error:
            raise RecognitionError(
                "OCR 不可用：请确认 Tesseract 已安装并包含 chi_sim 中文语言包。"
            ) from error
        observations: list[TextObservation] = []
        for index, raw_text in enumerate(data.get("text", [])):
            text = str(raw_text).strip()
            if not text:
                continue
            try:
                confidence = max(0.0, min(1.0, float(data["conf"][index]) / 100))
                box = BoundingBox(
                    int(data["left"][index]),
                    int(data["top"][index]),
                    int(data["left"][index]) + int(data["width"][index]),
                    int(data["top"][index]) + int(data["height"][index]),
                )
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            observations.append(TextObservation(text, box, confidence))
        return observations

    @staticmethod
    def _configure_executable(pytesseract) -> None:
        """Find the usual Windows installer path when Tesseract is not on PATH."""

        configured = str(getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract"))
        if configured != "tesseract":
            return
        candidates = [
            shutil.which("tesseract"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                pytesseract.pytesseract.tesseract_cmd = candidate
                return


def _normalise(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":").casefold()


def _matches(observed: str, expected: str) -> bool:
    actual = _normalise(observed)
    target = _normalise(expected)
    return actual == target or target in actual or actual in target


def _best_observation(observations: Iterable[TextObservation], text: str) -> TextObservation | None:
    candidates = [item for item in observations if _matches(item.text, text)]
    return max(candidates, key=lambda item: item.confidence, default=None)


def _exact_observation(observations: Iterable[TextObservation], text: str) -> TextObservation | None:
    target = _normalise(text)
    candidates = [item for item in observations if _normalise(item.text) == target]
    return max(candidates, key=lambda item: item.confidence, default=None)


def _locate_from_anchor(anchor: BoundingBox, locator: LocatorSpec) -> BoundingBox:
    if locator.placement == "within":
        left = anchor.left + locator.dx
        top = anchor.top + locator.dy
    elif locator.placement == "below":
        left = anchor.left + locator.dx
        top = anchor.bottom + locator.dy
    else:
        left = anchor.right + locator.dx
        top = anchor.top + locator.dy
    return BoundingBox(left, top, left + locator.width, top + locator.height)


class AnchorRecognizer:
    """Match profile labels and derive control boxes from relative anchors."""

    def __init__(self, profile: PageProfile, ocr: TextDetector | None = None, threshold: float = 0.72) -> None:
        self.profile = profile
        self.ocr = ocr or TesseractTextDetector()
        self.threshold = threshold

    def recognize_image(self, image: Image.Image) -> RecognitionResult:
        try:
            observations = self.ocr.detect(image)
        except RecognitionError as error:
            return RecognitionResult(
                self.profile.id,
                page_state="unknown",
                issues=[str(error), "ocr_unavailable"],
            )
        result = self.recognize_observations(observations)
        if "ocr" not in result.methods:
            result.methods.append("ocr")
        return result

    def recognize_observations(
        self, observations: Iterable[TextObservation], method: str = "ocr"
    ) -> RecognitionResult:
        items = list(observations)
        result = RecognitionResult(self.profile.id, methods=[method])
        for anchor_spec in self.profile.anchors:
            match = _best_observation(items, anchor_spec.text)
            if match is None or match.confidence < self.threshold:
                if anchor_spec.required or anchor_spec.id in self.profile.minimum_anchors:
                    result.missing_anchors.append(anchor_spec.id)
                continue
            result.anchors[anchor_spec.id] = LocatedAnchor(
                anchor_spec.id, anchor_spec.text, match.box, match.confidence, method
            )

        state_keywords = {
            "错误页面": "error",
            "页面加载中": "loading",
            "校验失败": "validation_failed",
            "弹窗遮挡": "modal",
        }
        state_match = next(
            (
                item
                for item in items
                if any(_normalise(item.text).startswith(_normalise(keyword)) for keyword in state_keywords)
            ),
            None,
        )
        if state_match is not None:
            state_text = _normalise(state_match.text)
            for keyword, state in state_keywords.items():
                if state_text.startswith(_normalise(keyword)):
                    result.page_state = state
                    break
            result.issues.append(f"page_state:{result.page_state}")
        elif not result.missing_anchors:
            result.page_state = "ready"

        if _exact_observation(items, "编辑中") is not None:
            result.edit_state = "editing"
        elif _exact_observation(items, "编辑") is not None:
            result.edit_state = "read_only"

        for control in self.profile.controls:
            anchor = result.anchors.get(control.locator.anchor)
            if anchor is None:
                result.missing_controls.append(control.id)
                continue
            control_match = _best_observation(items, control.label)
            confidence = min(anchor.confidence, control_match.confidence) if control_match else anchor.confidence
            # A block anchor is sufficient for controls whose geometry is defined
            # within that block; a label match gives a stronger result.
            if control_match is not None and control.locator.anchor == control.id:
                anchor_box = control_match.box
                method_name = method
            else:
                anchor_box = anchor.box
                method_name = f"{method}_anchor"
            result.controls[control.id] = LocatedControl(
                control.id,
                control.label,
                _locate_from_anchor(anchor_box, control.locator),
                confidence,
                method_name,
            )
        return result


@dataclass(frozen=True)
class TemplateMatch:
    name: str
    box: BoundingBox
    score: float


class TemplateMatcher:
    """Small dependency-free exact/near-exact template matcher.

    M2 templates are expected to be small crops of stable icons or buttons.
    This matcher is intentionally conservative and returns no match below the
    threshold; it is a fallback/confirmation signal, not a source of guessed
    input coordinates.
    """

    def __init__(self, threshold: float = 0.92, sample_limit: int = 32) -> None:
        self.threshold = threshold
        self.sample_limit = sample_limit

    def _sample_points(self, width: int, height: int) -> list[tuple[int, int]]:
        points = {
            (0, 0),
            (max(0, width - 1), 0),
            (0, max(0, height - 1)),
            (max(0, width - 1), max(0, height - 1)),
            (width // 2, height // 2),
        }
        stride_x = max(1, width // 6)
        stride_y = max(1, height // 6)
        for y in range(0, height, stride_y):
            for x in range(0, width, stride_x):
                points.add((x, y))
        return list(points)[: self.sample_limit]

    def locate(self, image: Image.Image, template: Image.Image, name: str = "template") -> TemplateMatch | None:
        source = ImageOps.grayscale(image).convert("L")
        target = ImageOps.grayscale(template).convert("L")
        if target.width == 0 or target.height == 0 or source.width < target.width or source.height < target.height:
            return None
        points = self._sample_points(target.width, target.height)
        target_pixels = target.load()
        source_pixels = source.load()
        best_score = 0.0
        best_xy: tuple[int, int] | None = None
        for top in range(source.height - target.height + 1):
            for left in range(source.width - target.width + 1):
                error = sum(
                    abs(source_pixels[left + x, top + y] - target_pixels[x, y])
                    for x, y in points
                ) / (len(points) * 255)
                score = 1.0 - error
                if score > best_score:
                    best_score = score
                    best_xy = (left, top)
        if best_xy is None or best_score < self.threshold:
            return None
        left, top = best_xy
        return TemplateMatch(
            name,
            BoundingBox(left, top, left + target.width, top + target.height),
            best_score,
        )

    def locate_directory(self, image: Image.Image, directory: str | Path) -> list[TemplateMatch]:
        matches: list[TemplateMatch] = []
        for path in sorted(Path(directory).glob("*")):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
                continue
            try:
                template = Image.open(path)
            except OSError:
                continue
            match = self.locate(image, template, path.stem)
            if match is not None:
                matches.append(match)
        return matches


def annotate_image(
    image: Image.Image,
    result: RecognitionResult,
    template_matches: Iterable[TemplateMatch] = (),
) -> Image.Image:
    """Return a copy with anchor/control boxes for the screenshot viewer."""

    from PIL import ImageDraw

    annotated = image.convert("RGB").copy()
    draw = ImageDraw.Draw(annotated)
    for anchor in result.anchors.values():
        draw.rectangle(anchor.box.to_dict().values(), outline="#228be6", width=2)
        draw.text((anchor.box.left, max(0, anchor.box.top - 14)), anchor.id, fill="#228be6")
    for control in result.controls.values():
        draw.rectangle(control.box.to_dict().values(), outline="#e03131", width=2)
        draw.text((control.box.left, control.box.top), control.id, fill="#e03131")
    for match in template_matches:
        draw.rectangle(match.box.to_dict().values(), outline="#37b24d", width=2)
        draw.text((match.box.left, match.box.bottom), f"template:{match.name}", fill="#37b24d")
    return annotated
