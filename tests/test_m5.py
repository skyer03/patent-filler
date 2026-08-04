from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from app.automation import AnchorRecognizer, Action, BoundingBox, TextObservation, load_profile
from app.m5 import FileCapture, M5FieldRunner
from app.m5_package import build_m5_package


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "resources" / "web_profiles" / "intranet_v1.json")


def ready_recognition():
    observations = [
        TextObservation(anchor.text, BoundingBox(20, index * 24 + 10, 180, index * 24 + 28), 0.99)
        for index, anchor in enumerate(PROFILE.anchors)
        if anchor.kind != "state"
    ]
    return AnchorRecognizer(PROFILE).recognize_observations(observations)


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[Action] = []

    def execute(self, action: Action, _x: int, _y: int) -> None:
        self.calls.append(action)


class StaticRecognizer:
    def __init__(self) -> None:
        self.result = ready_recognition()

    def recognize_image(self, _image):
        return self.result


class M5FieldTests(unittest.TestCase):
    def test_recognition_only_never_sends_input_and_exports_profile_version(self) -> None:
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "field.png"
            report_path = Path(directory) / "field.json"
            Image.new("RGB", (32, 32), "white").save(image_path)
            runner = M5FieldRunner(
                PROFILE,
                FileCapture(image_path),
                recognizer=StaticRecognizer(),
            )
            report = runner.recognize_only(report_path=report_path)

            self.assertEqual(report.status, "recognized")
            self.assertFalse(report.input_executed)
            self.assertEqual(report.profile_version, PROFILE.version)
            exported = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(exported["input_executed"])

    def test_step_recaptures_and_marks_business_value_for_manual_readback(self) -> None:
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "field.png"
            Image.new("RGB", (32, 32), "white").save(image_path)
            executor = RecordingExecutor()
            runner = M5FieldRunner(
                PROFILE,
                FileCapture(image_path),
                recognizer=StaticRecognizer(),
                focus_ok=lambda: True,
                diagnostics_dir=Path(directory) / "diagnostics",
            )
            report = runner.run_step(
                [Action("patent_no", "fill", "2020104300960")],
                confirm=lambda _action: True,
                executor_factory=lambda _recognition: executor,
            )

            self.assertEqual(report.status, "completed")
            self.assertTrue(report.input_executed)
            self.assertEqual([item.control_id for item in executor.calls], ["patent_no"])
            self.assertEqual(report.steps[0].status, "dispatched")
            self.assertTrue(report.steps[0].manual_readback_required)
            diagnostics = Path(directory) / "diagnostics"
            self.assertTrue((diagnostics / "manifest.json").exists())
            self.assertTrue((diagnostics / "environment.json").exists())
            self.assertTrue((diagnostics / "execution.log").read_text(encoding="utf-8").strip())
            self.assertTrue((diagnostics / "steps" / "001_detect.json").exists())
            self.assertTrue((diagnostics / "steps" / "001_result.json").exists())

    def test_destructive_action_is_blocked_before_executor(self) -> None:
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "field.png"
            Image.new("RGB", (32, 32), "white").save(image_path)
            executor = RecordingExecutor()
            runner = M5FieldRunner(PROFILE, FileCapture(image_path), recognizer=StaticRecognizer())
            report = runner.run_step(
                [Action("save", "click")],
                confirm=lambda _action: True,
                executor_factory=lambda _recognition: executor,
            )

            self.assertEqual(report.status, "blocked")
            self.assertFalse(report.input_executed)
            self.assertEqual(executor.calls, [])
            self.assertEqual(report.steps[0].error_code, "destructive_action")

    def test_sensitive_step_values_are_redacted_in_report(self) -> None:
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "field.png"
            Image.new("RGB", (32, 32), "white").save(image_path)
            runner = M5FieldRunner(
                PROFILE,
                FileCapture(image_path),
                recognizer=StaticRecognizer(),
            )
            report = runner.run_step(
                [Action("operator_phone", "fill", "SECRET-PHONE")],
                confirm=lambda _action: False,
            )

            encoded = json.dumps(report.to_dict(), ensure_ascii=False)
            self.assertNotIn("SECRET-PHONE", encoded)
            self.assertIn("[REDACTED]", encoded)


class M5PackageTests(unittest.TestCase):
    def test_package_contains_field_profile_and_launcher(self) -> None:
        with TemporaryDirectory() as directory:
            target = build_m5_package(Path(directory) / "m5.zip", ROOT)
            import zipfile

            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                self.assertIn("M5_PACKAGE_MANIFEST.json", names)
                self.assertIn("resources/web_profiles/intranet_v1.json", names)
                self.assertIn("resources/image_templates/intranet_v1/manifest.json", names)
                self.assertIn("install/run_recognition.cmd", names)


if __name__ == "__main__":
    unittest.main()
