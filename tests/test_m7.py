from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app.automation import (
    AttachmentSnapshot,
    InMemoryPageAdapter,
    WindowBinding,
    WindowRect,
    auto_update_profile_issues,
    load_profile,
)
from app.m7 import M7Error, M7Mode, M7Service, load_workflow_sources
from app.m7_ui import M7ToolApp
from app.m7_package import build_m7_package


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "m0" / "golden"


class M7UnifiedWorkflowTests(unittest.TestCase):
    @staticmethod
    def _blank_adapter(draft):
        return InMemoryPageAdapter(
            values={
                "patent_no": "",
                "application_title": "",
                "application_date": "",
                "grant_date": "",
                "summary_text": "",
            },
            selected_options={"patent_type": ""},
            tables={"rights_holder_rows": [], "inventor_rows": []},
            people={name: "测试人员库" for name in draft.inventors},
            attachments=(AttachmentSnapshot("certificate.pdf", True, True),),
        )

    def test_unified_source_loader_and_simulation_label_the_executor(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        with TemporaryDirectory() as directory:
            service = M7Service(queue_path=Path(directory) / "queue.json")
            report = service.run_simulation(draft, diagnostics=Path(directory) / "diagnostics")

        self.assertEqual(report.mode, M7Mode.SIMULATION.value)
        self.assertEqual(report.status, "completed")
        self.assertIn("InMemoryPageAdapter", report.executor)
        self.assertTrue(report.verified, report.to_dict())
        self.assertFalse(report.save_attempted)
        self.assertEqual(report.to_dict()["format"], "m7-unified-report-v1")

    def test_controlled_batch_runs_all_fifty_reviewed_drafts_through_m6(self) -> None:
        with TemporaryDirectory() as directory:
            queue_path = Path(directory) / "queue.json"
            service = M7Service(queue_path=queue_path)
            service.enqueue(sorted(GOLDEN.glob("*.json")))
            report = service.run_controlled_batch(diagnostics=Path(directory) / "diagnostics")

            self.assertEqual(report.mode, M7Mode.CONTROLLED_BATCH.value)
            self.assertEqual(report.status, "completed")
            self.assertTrue(report.verified, report.to_dict())
            self.assertIn("M6 controlled offline batch", report.executor)
            payload = report.payload.to_dict()
            self.assertEqual((payload["processed"], payload["completed"]), (50, 50))
            self.assertEqual(payload["paused"], 0)
            self.assertEqual(payload["failed"], 0)

    def test_field_modes_require_an_explicit_capture_source(self) -> None:
        service = M7Service()
        with self.assertRaises(M7Error):
            service.run_recognition_only()
        with self.assertRaises(M7Error):
            service.run_step([])

    def test_one_click_auto_update_fills_and_verifies_without_save(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        adapter = self._blank_adapter(draft)
        progress: list[tuple[str, int, int, str]] = []
        service = M7Service()

        report = service.run_auto_update(draft, adapter=adapter, progress=lambda *item: progress.append(item))

        self.assertEqual(report.mode, M7Mode.AUTO_UPDATE.value)
        self.assertEqual(report.status, "completed", report.to_dict())
        self.assertTrue(report.verified)
        self.assertFalse(report.save_attempted)
        self.assertFalse(report.manual_readback_required)
        self.assertEqual(adapter.values["patent_no"], "2020104300960")
        self.assertEqual(tuple(adapter.tables["inventor_rows"]), tuple(draft.inventors))
        self.assertEqual(adapter.selected_person, draft.inventors[0])
        self.assertTrue(any(item[0] == "automation" for item in progress))

    def test_auto_update_emits_incremental_live_trace(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        with TemporaryDirectory() as directory:
            events: list[dict[str, object]] = []
            diagnostics = Path(directory) / "sample-001.json"
            report = M7Service().run_auto_update(
                draft,
                adapter=self._blank_adapter(draft),
                diagnostics=diagnostics,
                trace_callback=events.append,
            )

            live_log = Path(directory) / "sample-001.live.log"
            self.assertEqual(report.status, "completed", report.to_dict())
            self.assertTrue(live_log.exists())
            content = live_log.read_text(encoding="utf-8")
            self.assertIn("[m7.run] started", content)
            self.assertIn("[automation.action_plan] started", content)
            self.assertTrue(events)
            self.assertEqual(events[0]["step"], "m7.run")
            self.assertIn("live_log", events[0])

    def test_auto_update_stops_on_review_conflict_and_emergency_stop(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        conflict = InMemoryPageAdapter(
            tables={"rights_holder_rows": [], "inventor_rows": []},
            people={name: "测试人员库" for name in draft.inventors},
        )
        before = conflict.observe().to_dict()
        blocked = M7Service().run_auto_update(draft, adapter=conflict)
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(conflict.observe().to_dict(), before)

        pending = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        pending.add_review("inventors")
        pending_adapter = self._blank_adapter(pending)
        review_blocked = M7Service().run_auto_update(pending, adapter=pending_adapter)
        self.assertEqual(review_blocked.status, "blocked")
        self.assertFalse(review_blocked.payload.review.approved)

        stopped_adapter = self._blank_adapter(draft)
        stopped = M7Service(stop_requested=lambda: True).run_auto_update(draft, adapter=stopped_adapter)
        self.assertEqual(stopped.status, "paused")
        self.assertEqual(stopped_adapter.values["patent_no"], "")

    def test_old_profile_without_readback_cannot_start_real_auto_update(self) -> None:
        legacy = load_profile(ROOT / "PROJECT_PLAN_M2_PROFILE.json")
        issues = auto_update_profile_issues(legacy, {"patent_no", "inventor_rows"})
        self.assertIn("unsupported_readback:patent_no", issues)
        self.assertIn("unsupported_readback:inventor_rows", issues)

    def test_bound_title_selects_mock_or_actual_profile_and_gates_auto_update(self) -> None:
        service = M7Service()
        service.binding = WindowBinding(1, "科技项目管理系统 信创版", WindowRect(0, 0, 1280, 900), "now")
        actual = service._load_bound_profile()
        self.assertEqual(actual.id, "cnipa_intranet_actual")
        self.assertFalse(service.can_auto_update_bound())

        service.binding = WindowBinding(1, "专利信息库 - M2 离线仿真页", WindowRect(0, 0, 1280, 900), "now")
        mock = service._load_bound_profile()
        self.assertEqual(mock.id, "cnipa_intranet")
        self.assertTrue(service.can_auto_update_bound())

    def test_actual_probe_stops_before_input_until_control_is_calibrated(self) -> None:
        with TemporaryDirectory() as directory:
            service = M7Service()
            service.binding = WindowBinding(
                1, "科技项目管理系统 信创版", WindowRect(0, 0, 1280, 900), "now"
            )
            report = service.run_basic_probe(
                "patent_type",
                "发明",
                diagnostics=Path(directory) / "probe",
            )

            self.assertEqual(report.status, "blocked")
            payload = report.payload.to_dict()
            self.assertFalse(payload["action_sent"])
            self.assertEqual(payload["input_status"], "not_sent")
            self.assertEqual(payload["verification_status"], "not_run")
            self.assertTrue((Path(directory) / "probe" / "report.json").exists())

    def test_probe_defaults_use_certificate_draft_field_names(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        app = M7ToolApp.__new__(M7ToolApp)
        app.probe_control = Mock(get=lambda: "申请名称")
        app.probe_value = Mock()
        app.current_index = 0
        app.drafts = [draft]

        app._fill_probe_value()

        app.probe_value.set.assert_called_once_with(draft.title or "")

    def test_auto_update_report_redacts_sensitive_config_values(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        report = M7Service().run_auto_update(
            draft,
            {"operator_phone": "SECRET-PHONE"},
            adapter=self._blank_adapter(draft),
        )

        encoded = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertEqual(report.status, "completed")
        self.assertNotIn("SECRET-PHONE", encoded)
        self.assertIn("[REDACTED]", encoded)

    def test_queue_controls_are_explicit_and_recover_running_tasks(self) -> None:
        with TemporaryDirectory() as directory:
            service = M7Service(queue_path=Path(directory) / "queue.json")
            task = service.enqueue([GOLDEN / "001-ZL202010430096.0.json"])[0]
            queue = service.queue_path
            from app.m6 import TaskQueue

            loaded = TaskQueue(queue)
            loaded.claim_next()
            self.assertEqual(service.recover_queue(), 1)
            self.assertEqual(service.queue_snapshot()[0]["status"], "paused")
            retried = service.retry_task(task.task_id)
            self.assertEqual(retried.status, "queued")

    def test_profile_and_manual_config_versions_are_exposed_by_unified_service(self) -> None:
        with TemporaryDirectory() as directory:
            service = M7Service(queue_path=Path(directory) / "queue.json")
            version = service.install_profile(ROOT / "resources" / "web_profiles" / "intranet_v1.json", activate=True)
            self.assertEqual(service.configuration_snapshot()["profile"]["active_version"], version)
            service.save_manual_config("v1", {"operator_name": "经办人"}, activate=True)
            service.save_manual_config("v2", {"operator_name": "新经办人"}, activate=True)
            service.rollback_manual_config()
            snapshot = service.configuration_snapshot()
            self.assertEqual(snapshot["manual_fields"]["active_version"], "v1")

    def test_auto_update_uses_active_config_when_no_override_is_supplied(self) -> None:
        draft = load_workflow_sources(GOLDEN / "001-ZL202010430096.0.json")[0]
        with TemporaryDirectory() as directory:
            service = M7Service(queue_path=Path(directory) / "queue.json")
            service.save_manual_config("active", {"operator_name": "本地经办人"}, activate=True)
            adapter = self._blank_adapter(draft)

            report = service.run_auto_update(draft, adapter=adapter)

        self.assertEqual(report.status, "completed")
        self.assertEqual(adapter.values["operator_name"], "本地经办人")


class M7PackageTests(unittest.TestCase):
    def test_package_contains_unified_launcher_docs_and_fifty_golden_files(self) -> None:
        with TemporaryDirectory() as directory:
            target = build_m7_package(Path(directory) / "m7.zip", ROOT)
            import zipfile

            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("M7_PACKAGE_MANIFEST.json"))

        self.assertIn("app/m7.py", names)
        self.assertIn("app/dom_bridge.py", names)
        self.assertIn("app/m7_ui.py", names)
        self.assertIn("app/version.py", names)
        self.assertIn("app/automation/screen_adapter.py", names)
        self.assertIn("mock_site/index.html", names)
        self.assertIn("resources/web_profiles/intranet_actual_v1.json", names)
        self.assertIn("edge_extension/manifest.json", names)
        self.assertIn("edge_extension/content.js", names)
        self.assertIn("edge_extension/install/register_native_host.ps1", names)
        self.assertIn("EDGE_EXTENSION_OPERATIONS.md", names)
        self.assertIn("install/start_m7.cmd", names)
        self.assertIn("install/run_m7_golden.cmd", names)
        self.assertIn("M7_OPERATIONS.md", names)
        self.assertIn("M7_FIELD_ACCEPTANCE_RECORD.md", names)
        self.assertEqual(manifest["golden_samples"], 50)


if __name__ == "__main__":
    unittest.main()
