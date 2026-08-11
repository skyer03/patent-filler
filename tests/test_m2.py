from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

from app.automation import AnchorRecognizer, Mode, ModeRunner, WindowBinding, WindowBinder, WindowRect, load_profile
from app.automation.modes import Action
from app.automation.recognizer import BoundingBox, TemplateMatcher, TextObservation


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "PROJECT_PLAN_M2_PROFILE.json")


def observations_for_profile(include_states: bool = False) -> list[TextObservation]:
    observations = [
        TextObservation(anchor.text, BoundingBox(20, index * 32 + 20, 180, index * 32 + 44), 0.99)
        for index, anchor in enumerate(PROFILE.anchors)
        if include_states or anchor.kind != "state"
    ]
    return observations


class M2ProfileAndRecognitionTests(unittest.TestCase):
    def test_every_m2_control_has_a_relative_locator(self) -> None:
        self.assertGreaterEqual(len(PROFILE.controls), 30)
        self.assertEqual(set(PROFILE.coverage()), {control.id for control in PROFILE.controls})
        for control in PROFILE.controls:
            self.assertIn(control.locator.anchor, PROFILE.anchors_by_id)

    def test_all_targets_are_located_from_labeled_or_block_anchors(self) -> None:
        result = AnchorRecognizer(PROFILE).recognize_observations(observations_for_profile())

        self.assertTrue(result.safe_for_input)
        self.assertEqual(result.missing_anchors, [])
        self.assertEqual(result.missing_controls, [])
        self.assertEqual(set(result.controls), {control.id for control in PROFILE.controls})

    def test_error_page_blocks_input_even_when_page_anchors_exist(self) -> None:
        observations = observations_for_profile()
        observations.append(TextObservation("错误页面", BoundingBox(0, 0, 100, 30), 0.99))
        result = AnchorRecognizer(PROFILE).recognize_observations(observations)
        runner = ModeRunner(PROFILE, Mode.SIMULATION)

        execution = runner.run(result, [Action("patent_no", "fill", "2018106374980")])

        self.assertFalse(result.safe_for_input)
        self.assertEqual(execution.status, "blocked")
        self.assertEqual(execution.executed, [])

    def test_edit_only_control_requires_editing_state(self) -> None:
        result = AnchorRecognizer(PROFILE).recognize_observations(observations_for_profile())
        runner = ModeRunner(PROFILE, Mode.SIMULATION, executor=RecordingExecutor())

        execution = runner.run(result, [Action("summary_text", "fill", "人工摘要")])

        self.assertEqual(execution.status, "blocked")
        self.assertIn("summary_text", execution.blocked)

        observations = observations_for_profile()
        observations.append(TextObservation("编辑中", BoundingBox(20, 20, 80, 40), 0.99))
        editing_result = AnchorRecognizer(PROFILE).recognize_observations(observations)
        self.assertEqual(editing_result.edit_state, "editing")

    def test_recognition_only_never_calls_executor(self) -> None:
        executor = RecordingExecutor()
        result = AnchorRecognizer(PROFILE).recognize_observations(observations_for_profile())
        execution = ModeRunner(PROFILE, Mode.RECOGNITION_ONLY, executor=executor).run(
            result, [Action("patent_no", "fill", "2018106374980")]
        )
        self.assertEqual(execution.status, "planned")
        self.assertEqual(execution.executed, [])
        self.assertEqual(execution.planned, ["patent_no"])
        self.assertEqual(executor.calls, [])

    def test_simulation_executes_safe_action_and_step_can_pause(self) -> None:
        result = AnchorRecognizer(PROFILE).recognize_observations(observations_for_profile())
        executor = RecordingExecutor()
        simulation = ModeRunner(PROFILE, Mode.SIMULATION, executor=executor).run(
            result, [Action("patent_no", "fill", "2018106374980")]
        )
        self.assertEqual(simulation.status, "completed")
        self.assertEqual(executor.calls[0][0], "patent_no")

        step = ModeRunner(PROFILE, Mode.STEP, executor=executor, confirm=lambda _action: False).run(
            result, [Action("patent_no", "fill", "2018106374980")]
        )
        self.assertEqual(step.status, "paused")
        self.assertEqual(step.executed, [])

    def test_loading_validation_and_modal_states_are_blocked(self) -> None:
        for state in ("页面加载中", "校验失败", "弹窗遮挡"):
            observations = observations_for_profile()
            observations.append(TextObservation(state + "：请暂停", BoundingBox(0, 0, 200, 30), 0.99))
            result = AnchorRecognizer(PROFILE).recognize_observations(observations)
            self.assertFalse(result.safe_for_input, state)
            self.assertEqual(ModeRunner(PROFILE, Mode.SIMULATION).run(result, []).status, "blocked")


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def execute(self, action: Action, x: int, y: int) -> None:
        self.calls.append((action.control_id, x, y))


class M2UtilityTests(unittest.TestCase):
    def test_window_coordinate_is_relative_to_current_bound_rectangle(self) -> None:
        current = {"value": WindowRect(100, 200, 900, 800)}
        binder = WindowBinder(rect_provider=lambda _hwnd: current["value"])
        binding = WindowBinding(10, "mock", current["value"], "now")

        current["value"] = WindowRect(240, 360, 1040, 960)
        refreshed = binder.refresh(binding)

        self.assertEqual(WindowBinder.screen_point(refreshed, 25, 30), (265, 390))

    def test_window_title_matching_ignores_edge_zero_width_characters(self) -> None:
        actual = "专利信息库 - M2 离线仿真页 - 个人 - Microsoft\u200b Edge"

        self.assertTrue(WindowBinder.title_matches("Microsoft Edge", actual))
        self.assertTrue(WindowBinder.title_matches("  M2   离线仿真页 ", actual))
        self.assertTrue(WindowBinder.is_browser_title(actual))

    def test_template_matcher_returns_high_confidence_exact_crop(self) -> None:
        image = Image.new("RGB", (8, 8), "white")
        pixels = image.load()
        for x, y, color in ((3, 3, (0, 0, 0)), (4, 3, (255, 0, 0)), (3, 4, (0, 0, 255)), (4, 4, (0, 128, 0))):
            pixels[x, y] = color
        template = image.crop((3, 3, 5, 5))

        match = TemplateMatcher().locate(image, template, "marker")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.name, "marker")
        self.assertEqual(match.box, BoundingBox(3, 3, 5, 5))
        self.assertGreaterEqual(match.score, 0.99)

    def test_mock_page_contains_required_controls_and_states(self) -> None:
        html = (ROOT / "mock_site" / "index.html").read_text(encoding="utf-8")
        self.assertIn("html { width: 100%; max-width: 100%; overflow-x: hidden; }", html)
        self.assertIn(".toolbar > span { flex: 1 1 100%;", html)
        for text in (
            "科技项目管理系统 信息版",
            "专利信息库",
            "技术摘要",
            "新增权利人",
            "选择人员",
            "申请 PCT 专利数量在 5 件以上",
            "经办人邮箱",
            "错误页面",
            "页面加载中",
            "校验失败",
            "弹窗遮挡",
        ):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
