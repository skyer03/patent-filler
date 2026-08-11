from __future__ import annotations

import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.automation import (
    AnchorRecognizer,
    BoundingBox,
    AutomationError,
    ProfileScreenReadback,
    RecognitionResult,
    ScreenPageAdapter,
    TextObservation,
    WindowBinding,
    WindowRect,
    load_profile,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "resources" / "web_profiles" / "intranet_v1.json")


def ready_result():
    observations = [
        TextObservation(anchor.text, BoundingBox(20, index * 26 + 10, 190, index * 26 + 30), 0.99)
        for index, anchor in enumerate(PROFILE.anchors)
        if anchor.kind != "state"
    ]
    return AnchorRecognizer(PROFILE).recognize_observations(observations)


class FakeClipboard:
    def __init__(self, value: str = "ORIGINAL") -> None:
        self.value = value

    def get_text(self) -> str:
        return self.value

    def set_text(self, value: str) -> None:
        self.value = value


class FakeBackend:
    def __init__(self, clipboard: FakeClipboard, copied: list[str]) -> None:
        self.clipboard = clipboard
        self.copied = list(copied)
        self.clicks: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def write(self, _text: str) -> None:
        return

    def key(self, name: str) -> None:
        if name == "ctrl+c" and self.copied:
            self.clipboard.value = self.copied.pop(0)

    def scroll(self, _amount: int) -> None:
        return


class FakeBinder:
    @staticmethod
    def screen_point(_binding, x: int, y: int) -> tuple[int, int]:
        return x, y


class RecordingInputBackend:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[str] = []

    def click(self, _x: int, _y: int) -> None:
        self.clicks.append((_x, _y))

    def write(self, _text: str) -> None:
        return

    def key(self, _name: str) -> None:
        self.keys.append(_name)

    def scroll(self, _amount: int) -> None:
        return


class RecordingWindowBinder(FakeBinder):
    def __init__(self) -> None:
        self.foreground = False
        self.activations = 0
        self.capture_activate_flags: list[bool] = []

    def activate(self, _binding) -> None:
        self.activations += 1
        self.foreground = True

    def is_foreground(self, _binding) -> bool:
        return self.foreground

    def capture(self, _binding, *, activate: bool = True):
        self.capture_activate_flags.append(activate)
        if activate:
            self.activate(_binding)
        return Image.new("RGB", (1600, 2000), "white")


class StaticRecognizer:
    def __init__(self, result) -> None:
        self.result = result
        self.ocr = FakeOcr()

    def recognize_image(self, _image):
        return self.result


class SequenceRecognizer:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.ocr = FakeOcr()
        self.index = 0

    def recognize_image(self, _image):
        result = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        return result


class SlowRecognizer(StaticRecognizer):
    def recognize_image(self, _image):
        time.sleep(0.05)
        return self.result


class FakeOcr:
    def __init__(self, observations=None) -> None:
        self.observations = list(observations or [])

    def detect(self, _image):
        return list(self.observations)


class ScreenReadbackTests(unittest.TestCase):
    def test_split_chinese_heading_is_joined_for_anchor_matching(self) -> None:
        observations = [
            TextObservation(anchor.text, BoundingBox(20, index * 26 + 10, 190, index * 26 + 30), 0.99)
            for index, anchor in enumerate(PROFILE.anchors)
            if anchor.id != "attachments"
        ]
        observations.extend(
            [
                TextObservation("补充", BoundingBox(20, 900, 65, 930), 0.99),
                TextObservation("附件", BoundingBox(70, 900, 115, 930), 0.99),
            ]
        )

        result = AnchorRecognizer(PROFILE).recognize_observations(observations)

        self.assertIn("attachments", result.anchors)

    def test_mock_patent_no_accepts_a_lower_confidence_exact_label(self) -> None:
        observations = [
            TextObservation(anchor.text, BoundingBox(20, index * 26 + 10, 190, index * 26 + 30), 0.99)
            for index, anchor in enumerate(PROFILE.anchors)
            if anchor.id != "patent_no"
        ]
        observations.append(TextObservation("专利号", BoundingBox(220, 40, 280, 60), 0.50))

        result = AnchorRecognizer(PROFILE).recognize_observations(observations)

        self.assertIn("patent_no", result.anchors)
        self.assertIn("patent_no", result.controls)
        self.assertEqual(result.controls["patent_no"].method, "ocr")

    def test_last_section_uses_page_end_navigation(self) -> None:
        binder = RecordingWindowBinder()
        binder.foreground = True
        backend = RecordingInputBackend()
        initial = ready_result()
        initial.anchors.pop("attachments", None)
        initial.controls.pop("attachments", None)
        mid = ready_result()
        mid.anchors.pop("basic_info", None)
        mid.controls.pop("patent_status", None)
        mid.controls.pop("application_title", None)
        adapter = ScreenPageAdapter(
            PROFILE,
            binder,
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            required_controls=(),
            backend=backend,
            recognizer=SequenceRecognizer([initial, ready_result(), mid, ready_result()]),
            settle_seconds=0,
        )

        adapter.scan_sections(["attachments"])

        self.assertIn("ctrl+end", backend.keys)
        self.assertIn("ctrl+home", backend.keys)
    def _reader(self, copied: list[str], observations=None):
        clipboard = FakeClipboard()
        backend = FakeBackend(clipboard, copied)
        reader = ProfileScreenReadback(
            FakeBinder(),
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            backend=backend,
            clipboard=clipboard,
            ocr=FakeOcr(observations),
            settle_seconds=0,
        )
        return reader, clipboard, backend

    def test_clipboard_value_is_normalized_and_original_text_is_restored(self) -> None:
        reader, clipboard, backend = self._reader(["ZL 2020 1 0430096.0"])

        value = reader.read(
            PROFILE.controls_by_id["patent_no"],
            Image.new("RGB", (1600, 2000), "white"),
            ready_result(),
        )

        self.assertEqual(value, "2020104300960")
        self.assertEqual(clipboard.value, "ORIGINAL")
        self.assertEqual(len(backend.clicks), 1)

    def test_clipboard_readback_rejects_full_page_text(self) -> None:
        reader, _clipboard, _backend = self._reader(["基本信息 申请名称 申请类型 专利状态 保存 返回"])

        with self.assertRaises(AutomationError):
            reader.read(
                PROFILE.controls_by_id["patent_no"],
                Image.new("RGB", (1600, 2000), "white"),
                ready_result(),
            )

    def test_mock_clipboard_readback_focuses_the_control_box(self) -> None:
        reader, _clipboard, backend = self._reader([""])
        result = ready_result()
        control = PROFILE.controls_by_id["patent_no"]
        located = result.controls[control.id].box

        reader.read(control, Image.new("RGB", (1600, 2000), "white"), result)

        self.assertEqual(
            backend.clicks[-1],
            (located.left + located.width // 2, located.top + located.height // 2),
        )

    def test_visual_choice_blank_group_ignores_adjacent_dark_text(self) -> None:
        reader, _clipboard, _backend = self._reader([])
        result = ready_result()
        box = result.controls["patent_type"].box
        image = Image.new("RGB", (1600, 2000), "white")
        draw = ImageDraw.Draw(image)
        draw.text((box.left + 25, box.top + 4), "发明", fill="black")

        value = reader.read(PROFILE.controls_by_id["patent_type"], image, result)

        self.assertEqual(value, "")

    def test_visual_choice_reads_unique_selected_option(self) -> None:
        reader, _clipboard, _backend = self._reader([])
        result = ready_result()
        box = result.controls["patent_type"].box
        image = Image.new("RGB", (1600, 2000), "white")
        draw = ImageDraw.Draw(image)
        left = box.left + 98 + 6
        top = box.top + 6
        draw.rectangle((left, top, left + 8, top + 16), fill="black")

        value = reader.read(PROFILE.controls_by_id["patent_type"], image, result)

        self.assertEqual(value, "发明")

    def test_visual_choice_ambiguous_state_stops(self) -> None:
        reader, _clipboard, _backend = self._reader([])
        result = ready_result()
        box = result.controls["patent_type"].box
        image = Image.new("RGB", (1600, 2000), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((box.left + 6, box.top + 6, box.left + 7, box.top + 10), fill="black")

        with self.assertRaises(AutomationError):
            reader.read(PROFILE.controls_by_id["patent_type"], image, result)

    def test_visual_checkbox_distinguishes_blank_selected_and_ambiguous(self) -> None:
        reader, _clipboard, _backend = self._reader([])
        result = ready_result()
        box = result.controls["joint_application"].box
        control = PROFILE.controls_by_id["joint_application"]
        origin_x = max(0, box.left)

        blank = Image.new("RGB", (1600, 2000), "white")
        self.assertFalse(reader.read(control, blank, result))

        selected = Image.new("RGB", (1600, 2000), "white")
        ImageDraw.Draw(selected).rectangle(
            (origin_x + 4, box.top + 4, origin_x + 15, box.top + 20), fill="black"
        )
        self.assertTrue(reader.read(control, selected, result))

        ambiguous = Image.new("RGB", (1600, 2000), "white")
        ImageDraw.Draw(ambiguous).rectangle(
            (origin_x + 4, box.top + 4, origin_x + 5, box.top + 11), fill="black"
        )
        with self.assertRaises(AutomationError):
            reader.read(control, ambiguous, result)

    def test_mock_css_keeps_native_choice_dimensions(self) -> None:
        html = (ROOT / "mock_site" / "index.html").read_text(encoding="utf-8")
        self.assertIn('.radio-row input[type="radio"]', html)
        self.assertIn('.field > label input[type="checkbox"]', html)
        self.assertIn("min-width: 16px", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", html)

    def test_table_clipboard_stops_at_first_empty_row(self) -> None:
        result = ready_result()
        box = result.controls["rights_holder_rows"].box
        observations = [
            TextObservation("1", BoundingBox(box.left + 5, box.top + 50, box.left + 15, box.top + 65), 0.99),
            TextObservation("2", BoundingBox(box.left + 5, box.top + 100, box.left + 15, box.top + 115), 0.99),
        ]
        reader, clipboard, _backend = self._reader(["甲公司", "乙公司"], observations)

        rows = reader.read(
            PROFILE.controls_by_id["rights_holder_rows"],
            Image.new("RGB", (1600, 2000), "white"),
            result,
        )

        self.assertEqual(rows, ("甲公司", "乙公司"))
        self.assertEqual(clipboard.value, "ORIGINAL")

    def test_screen_adapter_activates_once_before_preflight_capture(self) -> None:
        binder = RecordingWindowBinder()
        adapter = ScreenPageAdapter(
            PROFILE,
            binder,
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            required_controls=(),
            backend=RecordingInputBackend(),
            recognizer=StaticRecognizer(ready_result()),
            settle_seconds=0,
        )

        adapter.scan_sections(["basic_info"])

        self.assertEqual(binder.activations, 1)
        self.assertTrue(binder.capture_activate_flags)
        self.assertTrue(all(flag is False for flag in binder.capture_activate_flags))
        trace_steps = [event["step"] for event in adapter.trace]
        self.assertIn("focus.activate", trace_steps)
        self.assertIn("focus.check", trace_steps)
        self.assertIn("screen.capture", trace_steps)
        self.assertIn("ocr.recognize", trace_steps)
        self.assertIn("page.observe", trace_steps)
        self.assertIn("scroll", trace_steps)

    def test_screen_adapter_reports_ocr_failure_before_scroll_input(self) -> None:
        binder = RecordingWindowBinder()
        backend = RecordingInputBackend()
        adapter = ScreenPageAdapter(
            PROFILE,
            binder,
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            required_controls=(),
            backend=backend,
            recognizer=StaticRecognizer(
                RecognitionResult(PROFILE.id, page_state="unknown", issues=["OCR 不可用", "ocr_unavailable"])
            ),
            settle_seconds=0,
        )

        with self.assertRaisesRegex(AutomationError, "OCR 不可用"):
            adapter.scan_sections(["basic_info"])

        self.assertEqual(backend.clicks, [])

    def test_preflight_blocks_when_a_required_visible_control_is_missing(self) -> None:
        binder = RecordingWindowBinder()
        missing = ready_result()
        missing.anchors.pop("patent_no", None)
        missing.controls.pop("patent_no", None)
        adapter = ScreenPageAdapter(
            PROFILE,
            binder,
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            required_controls={"patent_no"},
            backend=RecordingInputBackend(),
            recognizer=StaticRecognizer(missing),
            settle_seconds=0,
        )

        with self.assertRaisesRegex(AutomationError, "required_controls_not_visible|patent_no"):
            adapter.scan_sections(["basic_info"])

    def test_screen_adapter_times_out_a_stuck_ocr_call(self) -> None:
        binder = RecordingWindowBinder()
        binder.foreground = True
        adapter = ScreenPageAdapter(
            PROFILE,
            binder,
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            required_controls=(),
            backend=RecordingInputBackend(),
            recognizer=SlowRecognizer(ready_result()),
            settle_seconds=0,
            ocr_timeout_seconds=0.01,
        )

        with self.assertRaisesRegex(AutomationError, "OCR 单次识别超过"):
            adapter.capture_evidence(require_ready=False)

    def test_empty_patent_type_generates_click_and_conflict_sends_no_input(self) -> None:
        binder = RecordingWindowBinder()
        binder.foreground = True
        backend = RecordingInputBackend()
        adapter = ScreenPageAdapter(
            PROFILE,
            binder,
            WindowBinding(1, "mock", WindowRect(0, 0, 1600, 2000), "now"),
            required_controls=(),
            backend=backend,
            recognizer=StaticRecognizer(ready_result()),
            settle_seconds=0,
        )

        adapter.selected_options["patent_type"] = ""
        adapter.select("patent_type", "发明")
        self.assertEqual(len(backend.clicks), 1)

        backend.clicks.clear()
        adapter.selected_options["patent_type"] = "实用新型"
        with self.assertRaises(AutomationError):
            adapter.select("patent_type", "发明")
        self.assertEqual(backend.clicks, [])


if __name__ == "__main__":
    unittest.main()
