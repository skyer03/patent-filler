from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from app.automation import load_profile
from app.m6 import (
    BackupManager,
    M6BatchRunner,
    M6SafetyStop,
    ProfileRegistry,
    TaskQueue,
    TaskRunResult,
    VersionedConfigStore,
    check_profile_compatibility,
    redact_payload,
    run_m4_queue,
    validate_safety_state,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "PROJECT_PLAN_M2_PROFILE.json")


class M6QueueTests(unittest.TestCase):
    def test_checkpoint_survives_reload_and_orphan_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            queue_path = Path(directory) / "queue.json"
            queue = TaskQueue(queue_path)
            task = queue.enqueue("certificate.pdf", task_id="task-one")
            claimed = queue.claim_next()
            self.assertIs(claimed, task)
            queue.save_checkpoint(task.task_id, {"sample_index": 1}, phase="automation", step="patent_no")

            reloaded = TaskQueue(queue_path)
            self.assertEqual(reloaded.get(task.task_id).status, "running")
            self.assertEqual(reloaded.get(task.task_id).checkpoint["step"], "patent_no")
            self.assertEqual(reloaded.recover_orphaned(), 1)
            self.assertEqual(reloaded.get(task.task_id).status, "paused")
            reloaded.retry(task.task_id)
            self.assertEqual(reloaded.claim_next().attempts, 2)

    def test_safety_stop_leaves_following_tasks_queued_and_retry_is_explicit(self) -> None:
        with TemporaryDirectory() as directory:
            queue = TaskQueue(Path(directory) / "queue.json")
            first = queue.enqueue("first.pdf", task_id="first")
            second = queue.enqueue("second.pdf", task_id="second")

            def handler(task, context):
                context.save_checkpoint({}, phase="recognition")
                if task.task_id == first.task_id:
                    raise M6SafetyStop("low_confidence", "需要人工复核。", {"step": "recognition"})
                return TaskRunResult()

            report = M6BatchRunner(queue, handler).run()
            self.assertEqual(report.status, "paused")
            self.assertEqual(report.paused, 1)
            self.assertEqual(queue.get(second.task_id).status, "queued")
            queue.retry(first.task_id)
            retry_report = M6BatchRunner(queue, lambda _task, _context: TaskRunResult()).run()
            self.assertTrue(retry_report.verified)

    def test_redaction_and_versioned_config_rollback(self) -> None:
        payload = {"operator_phone": "secret", "nested": {"email": "hidden", "title": "kept"}}
        redacted = redact_payload(payload)
        self.assertEqual(redacted["operator_phone"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["title"], "kept")

        with TemporaryDirectory() as directory:
            store = VersionedConfigStore(Path(directory) / "config")
            store.save("v1", payload, activate=True)
            store.save("v2", {"operator_phone": "new"}, activate=True)
            self.assertEqual(store.active_version, "v2")
            store.rollback()
            self.assertEqual(store.active_version, "v1")
            review = Path(directory) / "review.json"
            store.export_redacted(review)
            self.assertNotIn("secret", review.read_text(encoding="utf-8"))


class M6ProfileAndBackupTests(unittest.TestCase):
    def test_profile_registry_requires_minimum_anchors_and_can_rollback(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "profile.json"
            data = json.loads((ROOT / "resources" / "web_profiles" / "intranet_v1.json").read_text(encoding="utf-8"))
            data["version"] = "m6-test-v1"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            registry = ProfileRegistry(Path(directory) / "profiles")
            registry.install(source, activate=True)
            data["version"] = "m6-test-v2"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            registry.install(source, activate=True)
            self.assertEqual(registry.active_version, "m6-test-v2")
            check = registry.check_compatibility({"system_title", "module_title"})
            self.assertFalse(check.compatible)
            self.assertIn("basic_info", check.missing_anchors)
            registry.rollback()
            self.assertEqual(registry.active_version, "m6-test-v1")

    def test_backup_redacts_json_and_restore_rejects_no_path_escape(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "state"
            source.mkdir()
            (source / "config.json").write_text(
                json.dumps({"operator_email": "secret@example.com"}), encoding="utf-8"
            )
            archive_path = Path(directory) / "backup.zip"
            BackupManager.create(source, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                content = archive.read("state/config.json").decode("utf-8")
            self.assertNotIn("secret@example.com", content)
            restored = Path(directory) / "restored"
            BackupManager.restore(archive_path, restored)
            self.assertTrue((restored / "state" / "config.json").exists())

    def test_safety_gate_covers_m6_exit_conditions(self) -> None:
        with self.assertRaises(M6SafetyStop) as page_error:
            validate_safety_state(page_state="loading")
        self.assertEqual(page_error.exception.error_code, "page_not_ready")
        with self.assertRaises(M6SafetyStop) as confidence_error:
            validate_safety_state(page_state="ready", confidence=0.2)
        self.assertEqual(confidence_error.exception.error_code, "low_confidence")
        with self.assertRaises(M6SafetyStop) as table_error:
            validate_safety_state(page_state="ready", dynamic_table_ok=False)
        self.assertEqual(table_error.exception.error_code, "dynamic_table_abnormal")


class M6M4IntegrationTests(unittest.TestCase):
    def test_one_reviewed_draft_runs_through_persistent_m6_queue(self) -> None:
        with TemporaryDirectory() as directory:
            source = ROOT / "m0" / "golden" / "001-ZL202010430096.0.json"
            queue = TaskQueue(Path(directory) / "queue.json")
            queue.enqueue(source, task_id="sample-001")
            report = run_m4_queue(queue, PROFILE, diagnostics_dir=Path(directory) / "diagnostics")
            self.assertTrue(report.verified, report.to_dict())
            self.assertEqual(queue.get("sample-001").status, "completed")

    def test_fifty_reviewed_drafts_run_continuously_without_save(self) -> None:
        with TemporaryDirectory() as directory:
            queue = TaskQueue(Path(directory) / "queue.json")
            queue.enqueue_many(sorted((ROOT / "m0" / "golden").glob("*.json")))
            report = run_m4_queue(
                queue,
                PROFILE,
                diagnostics_dir=Path(directory) / "diagnostics",
                limit=50,
            )
            self.assertTrue(report.verified, report.to_dict())
            self.assertEqual((report.processed, report.completed), (50, 50))


if __name__ == "__main__":
    unittest.main()
