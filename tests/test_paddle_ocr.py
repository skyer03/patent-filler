from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from PIL import Image

from app.automation.recognizer import BoundingBox, PaddleTextDetector, RecognitionError


class _FakeResult:
    json = {
        "res": {
            "rec_texts": ["专利名称", "申请号"],
            "rec_scores": [0.98, 0.81],
            "rec_boxes": [[10, 20, 100, 40], [10, 60, 100, 80]],
        }
    }


class _FakeOcr:
    def predict(self, image):
        return [_FakeResult()]


class PaddleOcrAdapterTests(unittest.TestCase):
    def test_result_mapping_becomes_screen_observations(self) -> None:
        detector = PaddleTextDetector()
        detector._ocr = _FakeOcr()

        observations = detector.detect(Image.new("RGB", (120, 100), "white"))

        self.assertEqual([item.text for item in observations], ["专利名称", "申请号"])
        self.assertEqual(observations[0].box, BoundingBox(10, 20, 100, 40))
        self.assertAlmostEqual(observations[1].confidence, 0.81)

    def test_missing_local_model_stops_before_loading_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            detector = PaddleTextDetector(model_root=directory)

            with self.assertRaisesRegex(RecognitionError, "本地模型未找到"):
                detector.detect(Image.new("RGB", (20, 20), "white"))


if __name__ == "__main__":
    unittest.main()
