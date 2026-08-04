from __future__ import annotations

import unittest
from pathlib import Path

from app.automation import (
    Action,
    AnchorRecognizer,
    AutomationEngine,
    InMemoryPageAdapter,
    ScreenActionExecutor,
    TextObservation,
    BoundingBox,
    load_profile,
    run_m3_poc,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "PROJECT_PLAN_M2_PROFILE.json")


class M3EngineTests(unittest.TestCase):
    def test_offline_poc_covers_controls_and_verifies_every_step(self) -> None:
        report = run_m3_poc(PROFILE)

        self.assertTrue(report.verified, report.to_dict())
        self.assertEqual(report.status, "completed")
        self.assertEqual(len(report.steps), 11)
        self.assertEqual({step.status.value for step in report.steps}, {"verified"})

    def test_save_return_and_delete_are_blocked_without_touching_page(self) -> None:
        for action in (
            Action("save", "click"),
            Action("return", "click"),
            Action("rights_holder_rows", "delete", "0"),
        ):
            adapter = InMemoryPageAdapter()
            before = adapter.observe()
            report = AutomationEngine(PROFILE, adapter).run([action])

            self.assertEqual(report.status, "blocked")
            self.assertEqual(report.steps[0].error_code, "destructive_action")
            self.assertEqual(adapter.observe().to_dict(), before.to_dict())

    def test_non_ready_page_is_paused_before_input(self) -> None:
        adapter = InMemoryPageAdapter()
        adapter.page_state = "loading"
        report = AutomationEngine(PROFILE, adapter).run([Action("patent_no", "fill", "新值")])

        self.assertEqual(report.status, "blocked")
        self.assertEqual(report.steps[0].error_code, "page_not_ready")
        self.assertEqual(adapter.values["patent_no"], "2018106374980")

    def test_person_picker_requires_exactly_one_candidate(self) -> None:
        adapter = InMemoryPageAdapter()
        adapter.people["张三丰"] = "技术部 / 1003"
        report = AutomationEngine(PROFILE, adapter).run(
            [Action("first_inventor_select", "person", "张")]
        )

        self.assertEqual(report.status, "failed")
        self.assertEqual(report.steps[0].error_code, "action_error")
        self.assertIsNone(adapter.selected_person)

    def test_stop_and_focus_guards_run_before_next_action(self) -> None:
        adapter = InMemoryPageAdapter()
        focus = {"ok": True}
        report = AutomationEngine(PROFILE, adapter, focus_ok=lambda: focus["ok"]).run(
            [Action("patent_no", "fill", "第一步"), Action("application_title", "fill", "第二步")]
        )

        self.assertTrue(report.verified)
        focus["ok"] = False
        paused = AutomationEngine(PROFILE, adapter, focus_ok=lambda: focus["ok"]).run(
            [Action("application_title", "fill", "不会写入")]
        )
        self.assertEqual(paused.status, "paused")
        self.assertEqual(paused.reason, "focus_lost")

    def test_screen_executor_uses_current_window_origin_and_visible_text(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def click(self, x: int, y: int) -> None:
                self.calls.append(("click", x, y))

            def write(self, text: str) -> None:
                self.calls.append(("write", text))

            def key(self, name: str) -> None:
                self.calls.append(("key", name))

            def scroll(self, amount: int) -> None:
                self.calls.append(("scroll", amount))

        observations = [
            TextObservation(anchor.text, BoundingBox(10, index * 30 + 10, 120, index * 30 + 28), 0.99)
            for index, anchor in enumerate(PROFILE.anchors)
            if anchor.kind != "state"
        ]
        recognition = AnchorRecognizer(PROFILE).recognize_observations(observations)
        from app.automation import WindowBinding, WindowRect

        backend = RecordingBackend()
        executor = ScreenActionExecutor(
            WindowBinding(1, "mock", WindowRect(100, 200, 900, 800), "now"),
            recognition,
            backend=backend,
        )
        executor.execute(Action("patent_no", "fill", "2018104300960"))
        executor.execute(Action("pct_count", "select", "否"))

        self.assertEqual(backend.calls[0], ("click", 378, 523))
        self.assertEqual(backend.calls[1:], [("key", "ctrl+a"), ("write", "2018104300960"), ("click", 328, 1273), ("write", "否"), ("key", "enter")])


if __name__ == "__main__":
    unittest.main()
