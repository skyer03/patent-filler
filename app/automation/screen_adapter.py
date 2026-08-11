"""Profile-driven, screenshot-only PageAdapter for guarded real-page runs.

The adapter deliberately does not inspect the browser DOM.  It combines the
existing OCR locators with exact clipboard readback for editable controls and
small profile-declared visual/OCR readers for choices, tables, people and
attachments.  Every public action is followed by ``observe`` by
``AutomationEngine``.
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Callable, Protocol

from PIL import Image, ImageStat

from .engine import (
    AttachmentSnapshot,
    AutomationError,
    InputBackend,
    PageSnapshot,
    ScreenActionExecutor,
    Win32InputBackend,
)
from .modes import Action
from .profile import ControlSpec, PageProfile
from .recognizer import AnchorRecognizer, BoundingBox, RecognitionResult, TextDetector, TextObservation
from .window import WindowBinding, WindowBinder, WindowBindingError


SUPPORTED_READBACK_METHODS = frozenset(
    {
        "clipboard",
        "visual_choice",
        "visual_checkbox",
        "table_clipboard",
        "selected_person_ocr",
        "attachment_ocr",
        "ocr_text",
    }
)

_PAGE_TEXT_MARKERS = (
    "基本信息",
    "申请名称",
    "申请类型",
    "专利状态",
    "保存",
    "返回",
    "补充附件",
)


class Clipboard(Protocol):
    def get_text(self) -> str:
        ...

    def set_text(self, value: str) -> None:
        ...


class Win32Clipboard:
    """Minimal Unicode clipboard surface that preserves existing text."""

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    @staticmethod
    def _require_windows():
        if os.name != "nt":
            raise AutomationError("真实页面剪贴板回读仅支持 Windows。")
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        return user32, kernel32

    def get_text(self) -> str:
        user32, kernel32 = self._require_windows()
        if not user32.OpenClipboard(None):
            raise AutomationError("无法打开剪贴板进行字段回读。")
        try:
            handle = user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_text(self, value: str) -> None:
        user32, kernel32 = self._require_windows()
        encoded = (value + "\0").encode("utf-16-le")
        if not user32.OpenClipboard(None):
            raise AutomationError("无法打开剪贴板恢复原内容。")
        handle = None
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(encoded))
            if not handle:
                raise AutomationError("无法分配剪贴板内存。")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise AutomationError("无法锁定剪贴板内存。")
            try:
                ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                raise AutomationError("无法写入剪贴板。")
            handle = None  # ownership transferred to the system
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()


def _normalise(value: str, kind: str) -> str:
    text = " ".join(str(value).strip().split())
    if kind == "patent_no":
        text = text.upper()
        if text.startswith("ZL"):
            text = text[2:]
        return text.replace(".", "").replace(" ", "")
    if kind == "date":
        return text.replace("/", "-").replace(".", "-")
    if kind == "merged_names":
        return text.replace(";", "；").replace(" ", "")
    return text


def _looks_like_page_text(value: str) -> bool:
    compact = "".join(str(value).split())
    return sum(marker in compact for marker in _PAGE_TEXT_MARKERS) >= 2


def _box_point(box: BoundingBox, x: float, y: float) -> tuple[int, int]:
    return box.left + int(x), box.top + int(y)


def _intersects_image(box: BoundingBox, image: Image.Image) -> bool:
    return box.right > 0 and box.bottom > 0 and box.left < image.width and box.top < image.height


class ProfileScreenReadback:
    """Execute the small, audited readback methods declared by a profile."""

    def __init__(
        self,
        binder: WindowBinder,
        binding: WindowBinding,
        *,
        backend: InputBackend | None = None,
        clipboard: Clipboard | None = None,
        ocr: TextDetector,
        expected_tables: Mapping[str, Sequence[str]] | None = None,
        settle_seconds: float = 0.08,
    ) -> None:
        self.binder = binder
        self.binding = binding
        self.backend = backend or Win32InputBackend()
        self.clipboard = clipboard or Win32Clipboard()
        self.ocr = ocr
        self.expected_tables = {
            key: tuple(values) for key, values in (expected_tables or {}).items()
        }
        self.settle_seconds = settle_seconds

    def read(self, control: ControlSpec, image: Image.Image, result: RecognitionResult):
        located = result.controls.get(control.id)
        if located is None:
            raise AutomationError(f"回读时未识别到控件：{control.id}")
        method = control.readback.method
        options = control.readback.options
        if method == "clipboard":
            focus_box = located.box
            if options.get("focus") == "label":
                label = result.anchors.get(control.locator.anchor)
                if label is None:
                    raise AutomationError(f"控件 {control.id} 缺少标签焦点锚点。")
                focus_box = label.box
            raw_value = self.copy_at(focus_box)
            if _looks_like_page_text(raw_value):
                raise AutomationError(f"控件 {control.id} 回读疑似整页文本，未获得输入框焦点。")
            return _normalise(raw_value, control.readback.normalizer)
        if method == "visual_choice":
            return self._visual_choice(image, located.box, options)
        if method == "visual_checkbox":
            return self._visual_checkbox(image, located.box, options)
        if method == "table_clipboard":
            return self._table_rows(image, located.box, control, options)
        if method == "selected_person_ocr":
            return self._selected_person(image, located.box, options)
        if method == "attachment_ocr":
            return self._attachments(image, located.box, options)
        if method == "ocr_text":
            return self._ocr_text(image, located.box, options)
        raise AutomationError(f"控件 {control.id} 没有可用的一键回读配置。")

    def copy_at(self, box: BoundingBox, *, local_point: tuple[int, int] | None = None) -> str:
        local_x, local_y = local_point or (box.left + box.width // 2, box.top + box.height // 2)
        screen_x, screen_y = self.binder.screen_point(self.binding, local_x, local_y)
        self.backend.click(screen_x, screen_y)
        time.sleep(self.settle_seconds)
        return self.copy_focused()

    def copy_focused(self) -> str:
        previous = self.clipboard.get_text()
        marker = f"__M7_READBACK_{time.monotonic_ns()}__"
        self.clipboard.set_text(marker)
        try:
            self.backend.key("ctrl+a")
            self.backend.key("ctrl+c")
            time.sleep(self.settle_seconds)
            value = self.clipboard.get_text()
            if value == marker:
                # Chromium leaves the clipboard unchanged when a focused
                # input is genuinely empty.  The click target is already
                # guarded by the calibrated profile and current page state.
                return ""
            return value.strip()
        finally:
            self.clipboard.set_text(previous)

    def observations(self, image: Image.Image) -> list[TextObservation]:
        return self.ocr.detect(image)

    def find_text(self, image: Image.Image, expected: str) -> list[TextObservation]:
        target = re.sub(r"\s+", "", expected).casefold()
        return [
            item
            for item in self.observations(image)
            if target in re.sub(r"\s+", "", item.text).casefold()
        ]

    @staticmethod
    def _sample_luminance(image: Image.Image, x: int, y: int, radius: int = 2) -> float:
        left, top = max(0, x - radius), max(0, y - radius)
        right, bottom = min(image.width, x + radius + 1), min(image.height, y + radius + 1)
        if right <= left or bottom <= top:
            return 255.0
        return float(ImageStat.Stat(image.crop((left, top, right, bottom)).convert("L")).mean[0])

    @staticmethod
    def _indicator_score(image: Image.Image, box: BoundingBox, region: object) -> float:
        if not isinstance(region, Sequence) or len(region) != 4:
            raise AutomationError("视觉选框指示器区域格式无效。")
        left, top = _box_point(box, float(region[0]), float(region[1]))
        right, bottom = _box_point(box, float(region[2]), float(region[3]))
        left, top = max(0, left), max(0, top)
        right, bottom = min(image.width, right), min(image.height, bottom)
        if right <= left or bottom <= top:
            raise AutomationError("视觉选框指示器区域超出截图。")
        crop = image.crop((left, top, right, bottom)).convert("RGB")
        # Ignore the native control border.  Text beside the control is outside
        # this inner patch, so an empty option cannot be selected by label ink.
        inset_x = max(1, crop.width // 4)
        inset_y = max(1, crop.height // 4)
        inner = crop.crop((inset_x, inset_y, crop.width - inset_x, crop.height - inset_y))
        pixels = list(inner.get_flattened_data())
        if not pixels:
            return 0.0
        marked = 0
        for red, green, blue in pixels:
            dark = (red + green + blue) / 3 < 150
            colored = max(red, green, blue) - min(red, green, blue) > 45
            if dark or colored:
                marked += 1
        return marked / len(pixels)

    @staticmethod
    def _visual_thresholds(options: Mapping[str, object]) -> tuple[float, float]:
        selected = float(options.get("selected_threshold", 0.12))
        blank = float(options.get("blank_threshold", 0.04))
        if not 0 <= blank < selected <= 1:
            raise AutomationError("视觉选框阈值配置无效。")
        return selected, blank

    def _visual_choice(self, image: Image.Image, box: BoundingBox, options: Mapping[str, object]) -> str:
        raw_regions = options.get("indicators")
        if not isinstance(raw_regions, Mapping) or not raw_regions:
            raise AutomationError("visual_choice 缺少 indicators 区域配置。")
        selected_threshold, blank_threshold = self._visual_thresholds(options)
        selected: list[str] = []
        for value, region in raw_regions.items():
            score = self._indicator_score(image, box, region)
            if score >= selected_threshold:
                selected.append(str(value))
            elif score > blank_threshold:
                raise AutomationError(f"申请类型选框状态不确定：{value}")
        if len(selected) > 1:
            raise AutomationError("申请类型选框同时处于多个选中状态。")
        return selected[0] if selected else ""

    def _visual_checkbox(self, image: Image.Image, box: BoundingBox, options: Mapping[str, object]) -> bool:
        region = options.get("indicator")
        if region is None:
            raise AutomationError("visual_checkbox 缺少 indicator 区域配置。")
        selected_threshold, blank_threshold = self._visual_thresholds(options)
        score = self._indicator_score(image, box, region)
        if score >= selected_threshold:
            return True
        if score <= blank_threshold:
            return False
        raise AutomationError("联合申请复选框状态不确定。")

    def _table_rows(
        self,
        image: Image.Image,
        box: BoundingBox,
        control: ControlSpec,
        options: Mapping[str, object],
    ) -> tuple[str, ...]:
        value_x = float(options.get("value_x", min(260, box.width / 2)))
        first_y = float(options.get("first_row_y", 66))
        row_height = float(options.get("row_height", 50))
        max_rows = int(options.get("max_rows", 20))
        observations = [
            item
            for item in self.observations(image)
            if box.left <= item.box.left <= min(box.right, image.width)
            and box.top <= item.box.top <= min(box.bottom, image.height)
        ]
        empty_text = str(options.get("empty_text", "")).strip()
        joined = "".join(item.text for item in observations).replace(" ", "")
        if empty_text and empty_text.replace(" ", "") in joined:
            return ()
        expected = self.expected_tables.get(control.id)
        if expected is not None:
            rows: list[str] = []
            tab_stride = int(options.get("tab_stride", 2))
            for index in range(min(max_rows, len(expected))):
                if index == 0:
                    local = _box_point(box, value_x, first_y)
                    value = self.copy_at(box, local_point=local)
                else:
                    for _ in range(tab_stride):
                        self.backend.key("tab")
                    value = self.copy_focused()
                value = _normalise(value, control.readback.normalizer)
                if not value or value in {"待填写权利人", "待填写发明人"}:
                    break
                rows.append(value)
                if value != _normalise(expected[index], control.readback.normalizer):
                    break
            return tuple(rows)
        number_width = int(options.get("number_column_width", 100))
        row_numbers = {
            int(item.text.strip())
            for item in observations
            if item.box.left <= box.left + number_width and item.text.strip().isdigit()
        }
        visible_rows = 0
        while visible_rows + 1 in row_numbers:
            visible_rows += 1
        if visible_rows == 0:
            raise AutomationError(f"无法确认表格 {control.id} 为空或读取其现有行数。")
        rows: list[str] = []
        for index in range(min(max_rows, visible_rows)):
            local = _box_point(box, value_x, first_y + index * row_height)
            if local[1] >= image.height - 4:
                raise AutomationError(f"表格 {control.id} 的现有行超出可回读区域。")
            try:
                value = self.copy_at(box, local_point=local)
            except AutomationError:
                raise AutomationError(f"表格 {control.id} 第 {index + 1} 行回读失败。") from None
            value = _normalise(value, control.readback.normalizer)
            if not value or value in {"待填写权利人", "待填写发明人"}:
                raise AutomationError(f"表格 {control.id} 第 {index + 1} 行为空或仍是占位值。")
            rows.append(value)
        return tuple(rows)

    def _crop_box(self, box: BoundingBox, options: Mapping[str, object], image: Image.Image) -> BoundingBox:
        region = options.get("region")
        if isinstance(region, Sequence) and len(region) == 4:
            left, top = _box_point(box, float(region[0]), float(region[1]))
            right, bottom = _box_point(box, float(region[2]), float(region[3]))
        else:
            left, top, right, bottom = box.left, box.top, box.right, box.bottom
        return BoundingBox(max(0, left), max(0, top), min(image.width, right), min(image.height, bottom))

    def _ocr_text(self, image: Image.Image, box: BoundingBox, options: Mapping[str, object]) -> str:
        crop_box = self._crop_box(box, options, image)
        crop = image.crop((crop_box.left, crop_box.top, crop_box.right, crop_box.bottom))
        items = sorted(self.observations(crop), key=lambda item: (item.box.top, item.box.left))
        return " ".join(item.text for item in items).strip()

    def _selected_person(self, image: Image.Image, box: BoundingBox, options: Mapping[str, object]) -> str | None:
        text = self._ocr_text(image, box, options)
        if not text or "尚未选择" in text:
            return None
        match = re.search(r"已选择\s*[:：]\s*([^（(\s]+)", text)
        return match.group(1).strip() if match else None

    def _attachments(
        self, image: Image.Image, box: BoundingBox, options: Mapping[str, object]
    ) -> tuple[AttachmentSnapshot, ...]:
        text = self._ocr_text(image, box, options)
        if not text:
            return ()
        can_preview = "预览" in text
        can_download = "下载" in text
        name_match = re.search(r"([^\s]+\.(?:pdf|docx?|xlsx?|zip))", text, re.IGNORECASE)
        name = name_match.group(1) if name_match else str(options.get("default_name", "既有附件"))
        return (AttachmentSnapshot(name, can_preview, can_download),)


def auto_update_profile_issues(profile: PageProfile, required_controls: Iterable[str]) -> list[str]:
    issues: list[str] = []
    for control_id in sorted(set(required_controls)):
        control = profile.controls_by_id.get(control_id)
        if control is None:
            issues.append(f"missing_control:{control_id}")
        elif control.readback.method not in SUPPORTED_READBACK_METHODS:
            issues.append(f"unsupported_readback:{control_id}")
        elif control.readback.method == "visual_choice" and not isinstance(
            control.readback.options.get("indicators"), Mapping
        ):
            issues.append(f"uncalibrated_visual_readback:{control_id}")
        elif control.readback.method == "visual_checkbox" and not isinstance(
            control.readback.options.get("indicator"), Sequence
        ):
            issues.append(f"uncalibrated_visual_readback:{control_id}")
    return issues


class ScreenPageAdapter:
    """A real foreground-window implementation of the M3 PageAdapter."""

    SECTION_ORDER = (
        "basic_info",
        "technical_summary",
        "expected_benefits",
        "parties",
        "origin",
        "operator",
        "attachments",
    )

    def __init__(
        self,
        profile: PageProfile,
        binder: WindowBinder,
        binding: WindowBinding,
        *,
        required_controls: Iterable[str],
        expected_tables: Mapping[str, Sequence[str]] | None = None,
        backend: InputBackend | None = None,
        recognizer: AnchorRecognizer | None = None,
        readback: ProfileScreenReadback | None = None,
        stop_requested=lambda: False,
        settle_seconds: float = 0.12,
        trace: list[dict[str, object]] | None = None,
        trace_callback: Callable[[dict[str, object]], None] | None = None,
        progress: Callable[[str, int, int, str], None] | None = None,
        ocr_timeout_seconds: float = 45.0,
    ) -> None:
        self.profile = profile
        self.binder = binder
        self.binding = binding
        self.backend = backend or Win32InputBackend()
        self.recognizer = recognizer or AnchorRecognizer(profile)
        self.required_controls = set(required_controls)
        self.expected_tables = {
            key: tuple(values) for key, values in (expected_tables or {}).items()
        }
        issues = auto_update_profile_issues(profile, self.required_controls)
        if issues:
            raise AutomationError("当前页面 Profile 不支持一键回读：" + ", ".join(issues))
        self.readback = readback or ProfileScreenReadback(
            binder,
            binding,
            backend=self.backend,
            ocr=self.recognizer.ocr,
            expected_tables=self.expected_tables,
        )
        self.stop_requested = stop_requested
        self.settle_seconds = settle_seconds
        if ocr_timeout_seconds <= 0:
            raise ValueError("ocr_timeout_seconds must be positive")
        self.ocr_timeout_seconds = ocr_timeout_seconds
        self.progress = progress
        self.values: dict[str, str] = {}
        self.selected_options: dict[str, str] = {}
        self.checked: dict[str, bool] = {}
        self.tables: dict[str, tuple[str, ...]] = {}
        self.selected_person: str | None = None
        self.person_candidates: tuple[str, ...] = ()
        self.visible_anchor: str | None = None
        self.attachments: tuple[AttachmentSnapshot, ...] = ()
        self.last_result: RecognitionResult | None = None
        self.last_image: Image.Image | None = None
        self.operation = "preflight"
        self.preflight_active = False
        self.focus_recovery_operations: set[str] = set()
        self.trace = trace if trace is not None else []
        self.trace_callback = trace_callback
        self._cached_capture: tuple[Image.Image, RecognitionResult] | None = None
        self._cached_snapshot: PageSnapshot | None = None

    def trace_event(self, step: str, status: str, **details: object) -> None:
        """Append a bounded, value-redacted event to the run trace."""

        if len(self.trace) >= 1200:
            return
        event: dict[str, object] = {
            "seq": len(self.trace) + 1,
            "at": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "status": status,
        }
        event.update(details)
        self.trace.append(event)
        if self.trace_callback is not None:
            try:
                self.trace_callback(event)
            except Exception:
                # A live log/UI sink must never change the automation result.
                pass

    @staticmethod
    def value_meta(value: object) -> dict[str, object]:
        text = "" if value is None else str(value)
        return {"has_value": bool(text), "length": len(text)}

    def scan_sections(self, sections: Iterable[str]) -> PageSnapshot:
        sections = list(sections)
        self.trace_event("preflight.scan", "started", sections=sections)
        self.preflight_active = True
        snapshot: PageSnapshot | None = None
        try:
            self._activate_for_preflight()
            for section in sections:
                self.operation = f"preflight.scroll:{section}"
                if self.stop_requested():
                    raise AutomationError("emergency_stop")
                self.scroll_to(section)
                snapshot = self.observe()
                if snapshot.page_state != "ready":
                    raise AutomationError(f"预检时页面状态不是 ready：{snapshot.page_state}")
            if not sections or sections[-1] != "basic_info":
                self.scroll_to("basic_info")
                snapshot = self.observe()
            if snapshot is None:
                raise AutomationError("预检未获得页面快照。")
            self._cached_snapshot = snapshot
            self.trace_event("preflight.scan", "completed", page_state=snapshot.page_state)
            return snapshot
        finally:
            self.preflight_active = False

    def observe(self) -> PageSnapshot:
        self.operation = "observe"
        self.trace_event(
            "page.observe",
            "started",
            required_controls=len(self.required_controls),
            visible_anchor=self.visible_anchor,
        )
        if self._cached_snapshot is not None:
            snapshot = self._cached_snapshot
            self._cached_snapshot = None
            self.trace_event("page.observe", "reused", source="preflight")
            return snapshot
        if self._cached_capture is not None:
            image, result = self._cached_capture
            self._cached_capture = None
            self.trace_event("screen.capture", "reused", operation="observe")
        else:
            image, result = self._capture_recognition()
        self.last_result = result
        if self.preflight_active:
            current_section = self.visible_anchor
            if current_section is None:
                current_section = next(
                    (section for section in self.SECTION_ORDER if section in result.anchors),
                    None,
                )
            if current_section is not None:
                required_here = {
                    control_id
                    for control_id in self.required_controls
                    if self.profile.controls_by_id.get(control_id)
                    and self.profile.controls_by_id[control_id].section == current_section
                }
                missing_here = sorted(required_here - set(result.controls))
                if missing_here:
                    self.trace_event(
                        "page.observe",
                        "blocked",
                        reason="required_controls_not_visible",
                        section=current_section,
                        missing_controls=missing_here,
                    )
                    raise AutomationError(
                        f"预检未完整识别当前区块控件：{current_section}，缺少 {', '.join(missing_here)}"
                    )
        if result.page_state in {"ready", "modal"}:
            for control_id in sorted(self.required_controls):
                control = self.profile.controls_by_id.get(control_id)
                located = result.controls.get(control_id)
                if control is None or located is None or not _intersects_image(located.box, image):
                    self.trace_event(
                        "page.readback",
                        "skipped",
                        control_id=control_id,
                        reason="not_visible_or_not_located",
                    )
                    continue
                method = control.readback.method
                focus_box = located.box
                if method == "clipboard" and control.readback.options.get("focus") == "label":
                    label = result.anchors.get(control.locator.anchor)
                    if label is not None:
                        focus_box = label.box
                self.trace_event(
                    "page.readback",
                    "started",
                    control_id=control_id,
                    method=method,
                    box=located.box.to_dict(),
                    focus_box=focus_box.to_dict(),
                    confidence=located.confidence,
                )
                try:
                    # OCR can take long enough for the foreground window to
                    # change after capture.  Recheck immediately before the
                    # clipboard click so Ctrl+A/C cannot land in another
                    # window or on the document body.
                    self._ensure_foreground()
                    value = self.readback.read(control, image, result)
                except Exception as error:
                    self.trace_event(
                        "page.readback",
                        "failed",
                        control_id=control_id,
                        method=method,
                        error=str(error),
                    )
                    raise
                if method in {"clipboard", "ocr_text"}:
                    if control.kind == "select":
                        self.selected_options[control_id] = str(value)
                    else:
                        self.values[control_id] = str(value)
                elif method == "visual_choice":
                    self.selected_options[control_id] = str(value)
                elif method == "visual_checkbox":
                    self.checked[control_id] = bool(value)
                elif method == "table_clipboard":
                    self.tables[control_id] = tuple(value)
                elif method == "selected_person_ocr":
                    self.selected_person = str(value) if value else None
                elif method == "attachment_ocr":
                    self.attachments = tuple(value)
                self.trace_event(
                    "page.readback",
                    "completed",
                    control_id=control_id,
                    method=method,
                    value=self.value_meta(value),
                )
        # Keep the completed observation available for the immediately
        # following action.  AutomationEngine already performed the readback;
        # repeating OCR before every click only adds latency and can produce a
        # different layout while the page is otherwise unchanged.
        self._cached_capture = (image, result)
        if self.visible_anchor not in result.anchors:
            for section in self.SECTION_ORDER:
                if section in result.anchors:
                    self.visible_anchor = section
                    break
        snapshot = PageSnapshot(
            page_state=result.page_state,
            edit_state=result.edit_state,
            values=dict(self.values),
            selected_options=dict(self.selected_options),
            checked=dict(self.checked),
            tables=dict(self.tables),
            selected_person=self.selected_person,
            person_candidates=self.person_candidates,
            visible_anchor=self.visible_anchor,
            attachments=self.attachments,
        )
        self.trace_event(
            "page.observe",
            "completed",
            page_state=snapshot.page_state,
            edit_state=snapshot.edit_state,
            controls=len(result.controls),
            anchors=len(result.anchors),
            visible_anchor=self.visible_anchor,
        )
        return snapshot

    def capture_evidence(self, *, require_ready: bool = True) -> tuple[Image.Image, RecognitionResult]:
        """Capture one fresh screenshot and recognition result for UI probes."""

        return self._capture_recognition(require_ready=require_ready)

    def click(self, control_id: str) -> None:
        self.trace_event("action.click", "started", control_id=control_id)
        result = self._result_for_control(control_id)
        if result.page_state != "ready":
            raise AutomationError(f"点击前页面状态不是 ready：{result.page_state}")
        self._execute(Action(control_id, "click"), result)
        time.sleep(self.settle_seconds)
        self.trace_event("action.click", "completed", control_id=control_id)

    def fill(self, control_id: str, value: str) -> None:
        self.trace_event(
            "action.fill",
            "started",
            control_id=control_id,
            value=self.value_meta(value),
        )
        control = self.profile.controls_by_id[control_id]
        current = self.values.get(control_id, "")
        if current and _normalise(current, control.readback.normalizer) != _normalise(
            value, control.readback.normalizer
        ):
            self.trace_event("action.fill", "blocked_conflict", control_id=control_id)
            raise AutomationError(f"控件 {control_id} 已有冲突值，拒绝覆盖。")
        if current and _normalise(current, control.readback.normalizer) == _normalise(
            value, control.readback.normalizer
        ):
            self.trace_event("action.fill", "skipped_same_value", control_id=control_id)
            return
        result = self._result_for_control(control_id)
        self._execute(Action(control_id, "fill", value), result)
        time.sleep(self.settle_seconds)
        self.trace_event("action.fill", "completed", control_id=control_id)

    def select(self, control_id: str, visible_text: str) -> None:
        self.trace_event(
            "action.select",
            "started",
            control_id=control_id,
            value=self.value_meta(visible_text),
        )
        control = self.profile.controls_by_id[control_id]
        current = self.selected_options.get(control_id, "")
        if current and current != "请选择" and _normalise(current, control.readback.normalizer) != _normalise(
            visible_text, control.readback.normalizer
        ):
            self.trace_event("action.select", "blocked_conflict", control_id=control_id)
            raise AutomationError(f"控件 {control_id} 已有冲突选项，拒绝覆盖。")
        if _normalise(current, control.readback.normalizer) == _normalise(
            visible_text, control.readback.normalizer
        ):
            self.trace_event("action.select", "skipped_same_value", control_id=control_id)
            return
        result = self._result_for_control(control_id)
        if control.readback.method == "visual_choice":
            located = result.controls.get(control_id)
            clicks = control.readback.options.get("clicks", {})
            point = clicks.get(visible_text) if isinstance(clicks, Mapping) else None
            if located is None or not isinstance(point, Sequence) or len(point) != 2:
                raise AutomationError(f"控件 {control_id} 缺少选项点击坐标：{visible_text}")
            local_x, local_y = _box_point(located.box, float(point[0]), float(point[1]))
            screen_x, screen_y = self.binder.screen_point(self.binding, local_x, local_y)
            self.backend.click(screen_x, screen_y)
            self.trace_event(
                "action.input",
                "sent",
                control_id=control_id,
                kind="select",
                route="visual_click",
            )
        else:
            self._execute(Action(control_id, "select", visible_text), result)
        time.sleep(self.settle_seconds)
        self.trace_event("action.select", "completed", control_id=control_id)

    def set_checked(self, control_id: str, checked: bool) -> None:
        self.trace_event("action.checkbox", "started", control_id=control_id, checked=checked)
        if self.checked.get(control_id) is checked:
            self.trace_event("action.checkbox", "skipped_same_value", control_id=control_id)
            return
        result = self._result_for_control(control_id)
        self._execute(Action(control_id, "check" if checked else "uncheck"), result)
        time.sleep(self.settle_seconds)
        self.trace_event("action.checkbox", "completed", control_id=control_id, checked=checked)

    def add_row(self, table_id: str, value: str) -> None:
        self.trace_event(
            "action.table_add",
            "started",
            control_id=table_id,
            value=self.value_meta(value),
        )
        control = self.profile.controls_by_id[table_id]
        options = control.readback.options
        add_control = str(options.get("add_control", ""))
        if not add_control:
            raise AutomationError(f"表格 {table_id} 未配置新增按钮。")
        current = tuple(self.tables.get(table_id, ()))
        self.scroll_to(control.section)
        _image, result = self._capture_recognition()
        self._execute(Action(add_control, "click"), result)
        time.sleep(self.settle_seconds)
        index = len(current)
        tab_count = int(options.get("first_tab", 1)) + index * int(options.get("tab_stride", 2))
        for _ in range(tab_count):
            self.backend.key("tab")
        self.backend.key("ctrl+a")
        self.backend.write(value)
        self.trace_event(
            "action.table_write",
            "sent",
            control_id=table_id,
            row_index=index,
            tab_count=tab_count,
            value=self.value_meta(value),
        )
        time.sleep(self.settle_seconds)
        actual = _normalise(self.readback.copy_focused(), control.readback.normalizer)
        expected = _normalise(value, control.readback.normalizer)
        if actual != expected:
            self.trace_event("action.table_write", "verification_failed", control_id=table_id)
            raise AutomationError(f"表格 {table_id} 新增行回读不一致。")
        self.tables[table_id] = current + (value,)
        self.trace_event("action.table_add", "completed", control_id=table_id, row_index=index)

    def search_person(self, query: str) -> None:
        self.trace_event(
            "action.person_search",
            "started",
            control_id="first_inventor_select",
            value=self.value_meta(query),
        )
        image, result = self._capture_recognition(require_ready=False)
        if result.page_state != "modal":
            raise AutomationError("人员选择器未进入弹窗状态。")
        selector = self.profile.controls_by_id["first_inventor_select"]
        search_text = str(selector.readback.options.get("search_text", "搜索姓名"))
        matches = self.readback.find_text(image, search_text)
        if len(matches) != 1:
            raise AutomationError("人员搜索框定位不唯一。")
        box = matches[0].box
        screen_x, screen_y = self.binder.screen_point(
            self.binding, box.left + box.width // 2, box.top + box.height // 2
        )
        self.backend.click(screen_x, screen_y)
        self.backend.key("ctrl+a")
        self.backend.write(query)
        time.sleep(self.settle_seconds)
        image, _ = self._capture_recognition(require_ready=False)
        matches = self.readback.find_text(image, query)
        self.person_candidates = (query,) if len(matches) == 1 else ()
        self.trace_event(
            "action.person_search",
            "completed" if self.person_candidates else "blocked_non_unique",
            control_id="first_inventor_select",
            candidate_count=len(matches),
        )

    def choose_person(self, name: str) -> None:
        self.trace_event(
            "action.person_choose",
            "started",
            control_id="first_inventor_select",
            value=self.value_meta(name),
        )
        if self.person_candidates != (name,):
            raise AutomationError(f"人员匹配不唯一：{name}")
        image, _ = self._capture_recognition(require_ready=False)
        matches = self.readback.find_text(image, name)
        if len(matches) != 1:
            raise AutomationError(f"人员结果定位不唯一：{name}")
        selector = self.profile.controls_by_id["first_inventor_select"]
        dx = int(selector.readback.options.get("choose_dx", 260))
        local_x = min(image.width - 8, matches[0].box.right + dx)
        local_y = matches[0].box.top + matches[0].box.height // 2
        screen_x, screen_y = self.binder.screen_point(self.binding, local_x, local_y)
        self.backend.click(screen_x, screen_y)
        self.person_candidates = ()
        time.sleep(self.settle_seconds)
        self.trace_event("action.person_choose", "completed", control_id="first_inventor_select")

    def scroll_to(self, anchor_id: str) -> None:
        self.operation = f"scroll:{anchor_id}"
        self._cached_snapshot = None
        self.trace_event("scroll", "started", anchor_id=anchor_id)
        if anchor_id not in self.profile.anchors_by_id:
            raise AutomationError(f"未知滚动锚点：{anchor_id}")
        initial_image, initial_result = self._capture_recognition(require_ready=False)
        if initial_result.page_state != "ready":
            raise AutomationError(self._page_state_error("滚动前", initial_result))
        if anchor_id in initial_result.anchors:
            self.visible_anchor = anchor_id
            self._cached_capture = (initial_image, initial_result)
            self.trace_event(
                "scroll.check",
                "anchor_found",
                anchor_id=anchor_id,
                iteration=0,
                reused_initial=True,
            )
            self.trace_event("scroll", "completed", anchor_id=anchor_id, reused_initial=True)
            return
        self._ensure_foreground()
        center_x = self.binding.rect.width // 2
        center_y = min(220, self.binding.rect.height // 2)
        screen_x, screen_y = self.binder.screen_point(self.binding, center_x, center_y)
        self.backend.click(screen_x, screen_y)
        section_ids = [anchor.id for anchor in self.profile.anchors if anchor.kind == "section"]
        at_page_end = bool(section_ids) and anchor_id == section_ids[-1]
        navigation_key = "ctrl+end" if at_page_end else "ctrl+home"
        self.backend.key(navigation_key)
        self.trace_event(
            "scroll.position",
            "sent",
            anchor_id=anchor_id,
            key=navigation_key,
            page_end=at_page_end,
        )
        time.sleep(self.settle_seconds)
        max_iterations = 4 if at_page_end else 14
        for iteration in range(max_iterations):
            if self.stop_requested():
                raise AutomationError("emergency_stop")
            image, result = self._capture_recognition(require_ready=False)
            if result.page_state != "ready":
                raise AutomationError(self._page_state_error("滚动时", result))
            if anchor_id in result.anchors:
                self.visible_anchor = anchor_id
                self._cached_capture = (image, result)
                self.trace_event(
                    "scroll.check",
                    "anchor_found",
                    anchor_id=anchor_id,
                    iteration=iteration + 1,
                )
                self.trace_event("scroll", "completed", anchor_id=anchor_id)
                return
            self.trace_event(
                "scroll.check",
                "not_found",
                anchor_id=anchor_id,
                iteration=iteration + 1,
                anchor_ids=sorted(result.anchors),
                control_ids=sorted(result.controls),
            )
            self.backend.scroll(-620)
            self.trace_event(
                "scroll.input",
                "sent",
                anchor_id=anchor_id,
                amount=-620,
                iteration=iteration + 1,
            )
            time.sleep(self.settle_seconds)
        self.trace_event(
            "scroll",
            "failed",
            anchor_id=anchor_id,
            attempts=max_iterations,
            last_visible_anchor_ids=sorted(result.anchors),
            last_visible_control_ids=sorted(result.controls),
        )
        raise AutomationError(f"滚动后仍未识别到目标区块：{anchor_id}")

    def _execute(self, action: Action, result: RecognitionResult) -> None:
        if not result.safe_for_input:
            raise AutomationError("页面锚点或状态不满足输入条件。")
        self.trace_event(
            "action.input",
            "started",
            control_id=action.control_id,
            kind=action.kind,
            value=self.value_meta(action.value),
        )
        ScreenActionExecutor(
            self.binding,
            result,
            backend=self.backend,
            binder=type(self.binder),
        ).execute(action)
        self.trace_event(
            "action.input",
            "sent",
            control_id=action.control_id,
            kind=action.kind,
        )

    def _result_for_control(self, control_id: str) -> RecognitionResult:
        if self._cached_capture is not None:
            _image, result = self._cached_capture
            self._cached_capture = None
            self.trace_event("screen.capture", "reused", operation=f"control:{control_id}")
        else:
            _image, result = self._capture_recognition()
        if control_id in result.controls:
            return result
        control = self.profile.controls_by_id.get(control_id)
        if control is None:
            raise AutomationError(f"profile 中没有控件：{control_id}")
        self.scroll_to(control.section)
        if self._cached_capture is not None:
            _image, result = self._cached_capture
            self._cached_capture = None
            self.trace_event("screen.capture", "reused", operation=f"control:{control_id}")
        else:
            _image, result = self._capture_recognition()
        if control_id not in result.controls:
            raise AutomationError(f"滚动后仍未识别到控件：{control_id}")
        return result

    def _ensure_foreground(self) -> None:
        if os.name == "nt":
            state_reader = getattr(self.binder, "foreground_state", None)
            state = (
                state_reader(self.binding)
                if callable(state_reader)
                else {
                    "is_foreground": self.binder.is_foreground(self.binding),
                    "target_hwnd": int(self.binding.hwnd),
                    "foreground_hwnd": 0,
                    "foreground_root": 0,
                    "foreground_title": "",
                    }
                )
            self.trace_event(
                "focus.check",
                "passed" if state["is_foreground"] else "failed",
                operation=self.operation,
                target_hwnd=state["target_hwnd"],
                target_valid=state.get("target_valid"),
                foreground_hwnd=state["foreground_hwnd"],
                foreground_root=state["foreground_root"],
                foreground_title=state["foreground_title"] or "",
            )
            if (
                not state["is_foreground"]
                and self.preflight_active
                and self.operation not in self.focus_recovery_operations
            ):
                self.focus_recovery_operations.add(self.operation)
                self.trace_event(
                    "focus.recovery",
                    "started",
                    operation=self.operation,
                    target_hwnd=state["target_hwnd"],
                    previous_foreground_hwnd=state["foreground_hwnd"],
                    previous_foreground_title=state["foreground_title"] or "",
                )
                try:
                    self.binder.activate(self.binding)
                except WindowBindingError as error:
                    self.trace_event("focus.recovery", "failed", error=str(error))
                state = (
                    state_reader(self.binding)
                    if callable(state_reader)
                    else {
                        "is_foreground": self.binder.is_foreground(self.binding),
                        "target_hwnd": int(self.binding.hwnd),
                        "foreground_hwnd": 0,
                        "foreground_root": 0,
                        "foreground_title": "",
                    }
                )
                self.trace_event(
                    "focus.recovery",
                    "completed" if state["is_foreground"] else "blocked",
                    foreground_hwnd=state["foreground_hwnd"],
                    foreground_root=state["foreground_root"],
                    foreground_title=state["foreground_title"] or "",
                )
            if not state["is_foreground"]:
                raise AutomationError(
                    "目标浏览器窗口失去前台，已停止。"
                    f"（步骤={self.operation}，目标HWND={state['target_hwnd']}，"
                    f"当前前台HWND={state['foreground_hwnd']}，"
                    f"前台根HWND={state['foreground_root']}，"
                    f"前台标题={state['foreground_title'] or '无'}）"
                )

    def _activate_for_preflight(self) -> None:
        self.trace_event("focus.activate", "started", target_hwnd=int(self.binding.hwnd))
        # The UI button callback activates Edge before the worker starts.
        # Avoid a second foreground request from the worker thread: Windows
        # may reject it even though Edge is already the active window.
        if os.name == "nt" and self.binder.is_foreground(self.binding):
            self.trace_event("focus.activate", "already_foreground", target_hwnd=int(self.binding.hwnd))
            return
        try:
            self.binder.activate(self.binding)
        except WindowBindingError as error:
            self.trace_event("focus.activate", "failed", error=str(error))
            raise AutomationError(str(error)) from error
        self._ensure_foreground()
        self.trace_event("focus.activate", "completed", target_hwnd=int(self.binding.hwnd))

    def _capture_recognition(self, *, require_ready: bool = True) -> tuple[Image.Image, RecognitionResult]:
        self.trace_event(
            "screen.capture",
            "started",
            operation=self.operation,
            require_ready=require_ready,
        )
        self._ensure_foreground()
        try:
            image = self.binder.capture(self.binding, activate=False).convert("RGB")
        except WindowBindingError as error:
            self.trace_event("screen.capture", "failed", error=str(error))
            raise AutomationError(str(error)) from error
        self.last_image = image
        self.trace_event("screen.capture", "completed", width=image.width, height=image.height)
        self.trace_event("ocr.recognize", "started", operation=self.operation)
        if self.progress is not None:
            self.progress("ocr", 0, 0, f"正在识别当前页面（单次最多 {int(self.ocr_timeout_seconds)} 秒）")
        result = self._recognize_with_timeout(image)
        self.last_result = result
        self.trace_event(
            "ocr.recognize",
            "completed",
            page_state=result.page_state,
            safe_for_input=result.safe_for_input,
            anchors=len(result.anchors),
            controls=len(result.controls),
            anchor_ids=sorted(result.anchors),
            control_ids=sorted(result.controls),
            missing_anchors=sorted(result.missing_anchors),
            missing_controls=sorted(result.missing_controls),
            methods=result.methods,
            issues=list(result.issues),
        )
        if require_ready and not result.safe_for_input:
            reason = "; ".join(result.issues or result.missing_anchors or [result.page_state])
            raise AutomationError(f"页面识别不满足输入条件：{reason}")
        return image, result

    def _recognize_with_timeout(self, image: Image.Image) -> RecognitionResult:
        result_box: dict[str, RecognitionResult] = {}
        error_box: dict[str, Exception] = {}

        def recognize() -> None:
            try:
                result_box["result"] = self.recognizer.recognize_image(image)
            except Exception as error:  # recognizer boundary
                error_box["error"] = error

        worker = threading.Thread(target=recognize, name="m7-ocr", daemon=True)
        worker.start()
        worker.join(self.ocr_timeout_seconds)
        if worker.is_alive():
            self.trace_event(
                "ocr.recognize",
                "timeout",
                timeout_seconds=self.ocr_timeout_seconds,
            )
            raise AutomationError(
                f"OCR 单次识别超过 {int(self.ocr_timeout_seconds)} 秒，已安全停止；未发送输入。"
            )
        if "error" in error_box:
            raise error_box["error"]
        result = result_box.get("result")
        if result is None:
            raise AutomationError("OCR 未返回识别结果，已安全停止；未发送输入。")
        return result

    @staticmethod
    def _page_state_error(prefix: str, result: RecognitionResult) -> str:
        detail = "; ".join(result.issues or result.missing_anchors or [result.page_state])
        return f"{prefix}页面状态不是 ready：{result.page_state}（{detail}）"


__all__ = [
    "ProfileScreenReadback",
    "SUPPORTED_READBACK_METHODS",
    "ScreenPageAdapter",
    "Win32Clipboard",
    "auto_update_profile_issues",
]
