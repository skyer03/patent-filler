from __future__ import annotations

import io
import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.dom_bridge import DomBridgeError, NativeMessageHost, TaskStore, build_dom_task
from app.m7 import M7Service, load_workflow_sources


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "m0" / "golden" / "001-ZL202010430096.0.json"


class DomTaskTests(unittest.TestCase):
    def test_reviewed_draft_builds_five_certificate_fields_and_manual_checkbox(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        task = build_dom_task(draft, {"joint_application": "否"})

        self.assertEqual(task["format"], "patent-dom-task-v1")
        self.assertEqual(task["status"], "ready_for_fill")
        fields = {item["field_id"]: item for item in task["fields"]}
        self.assertEqual(set(fields), {
            "patent_no", "application_title", "patent_type",
            "application_date", "grant_date", "joint_application",
        })
        self.assertEqual(fields["patent_no"]["value"], "2020104300960")
        self.assertEqual(fields["patent_type"]["value"], "发明")
        self.assertIs(fields["joint_application"]["value"], False)
        self.assertTrue(all(item["confirmed"] for item in fields.values()))
        self.assertTrue(all(item["overwrite_policy"] == "empty_or_same" for item in fields.values()))
        self.assertEqual(task["safety"]["save_submit_return_delete"], "manual_only")

    def test_pending_review_and_ambiguous_checkbox_are_rejected(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        draft.add_review("title")
        with self.assertRaises(DomBridgeError):
            build_dom_task(draft)

        reviewed = load_workflow_sources(GOLDEN)[0]
        with self.assertRaises(DomBridgeError):
            build_dom_task(reviewed, {"joint_application": "可能"})

    def test_allow_overwrite_requires_explicit_task_mode(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        task = build_dom_task(draft, allow_overwrite=True)

        self.assertTrue(task["safety"]["overwrite_existing"])
        self.assertTrue(all(item["overwrite_policy"] == "reviewed_value" for item in task["fields"]))

    def test_complex_controls_are_opt_in_and_come_from_reviewed_fields(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        task = build_dom_task(draft, include_complex=True)
        fields = {item["field_id"]: item for item in task["fields"]}

        self.assertEqual(task["profile_version"], "dom-poc-v3")
        self.assertEqual(fields["rights_holder_rows"]["value"], list(draft.current_patentees))
        self.assertEqual(fields["inventor_rows"]["value"], list(draft.inventors))
        self.assertEqual(fields["first_inventor_select"]["value"], draft.inventors[0])
        self.assertEqual(fields["patentee_merge"]["source"], "derived_from_reviewed_table")
        self.assertEqual(fields["patentee_merge"]["value"], ",".join(draft.current_patentees))
        self.assertEqual(fields["inventor_merge"]["value"], ",".join(draft.inventors))
        self.assertEqual(fields["inventor_merge"]["normalizer"], "trim")

    def test_service_publishes_task_without_a_bound_screen_window(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        with TemporaryDirectory() as directory:
            service = M7Service(
                queue_path=Path(directory) / "queue.json",
                dom_store=Path(directory) / "dom",
            )
            task = service.prepare_dom_task(draft)
            status = service.dom_task_status()

        self.assertEqual(task["status"], "ready_for_fill")
        self.assertEqual(status["task"]["field_count"], 5)
        self.assertIsNone(service.binding)


class NativeMessageTests(unittest.TestCase):
    def test_step_results_are_incremental_and_finish_requires_exact_verified_order(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory))
            task = store.prepare(draft)
            host = NativeMessageHost(store)
            for field in task["fields"]:
                response = host.handle({
                    "type": "report_step",
                    "task_id": task["task_id"],
                    "field_id": field["field_id"],
                    "status": "filled",
                    "before": "",
                    "after": field["value"],
                    "verified": True,
                })
                self.assertTrue(response["ok"], response)
            finished = host.handle({"type": "finish_task", "task_id": task["task_id"]})

            self.assertTrue(finished["ok"], finished)
            self.assertEqual(finished["payload"]["status"], "completed_waiting_for_manual_save")
            self.assertFalse(finished["payload"]["save_attempted"])
            self.assertEqual(len(finished["payload"]["steps"]), len(task["fields"]))
            self.assertNotIn(draft.title or "", json.dumps(finished["payload"], ensure_ascii=False))
            cancelled = host.handle({
                "type": "cancel_task",
                "task_id": task["task_id"],
                "reason_code": "user_stop",
            })
            self.assertFalse(cancelled["ok"])
            self.assertEqual(store.status()["status"], "completed_waiting_for_manual_save")
            with self.assertRaises(DomBridgeError):
                store.get_ready_task()

    def test_step_results_must_follow_the_reviewed_task_order(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory))
            task = store.prepare(draft)
            second = task["fields"][1]
            response = NativeMessageHost(store).handle({
                "type": "report_step",
                "task_id": task["task_id"],
                "field_id": second["field_id"],
                "status": "filled",
                "before": "",
                "after": second["value"],
                "verified": True,
            })

            self.assertFalse(response["ok"])
            self.assertEqual(store.status()["result"]["steps"], [])

    def test_conflict_is_never_reported_as_complete(self) -> None:
        draft = load_workflow_sources(GOLDEN)[0]
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory))
            task = store.prepare(draft)
            first = task["fields"][0]
            store.report_step({
                "task_id": task["task_id"],
                "field_id": first["field_id"],
                "status": "blocked",
                "before": "conflict",
                "after": "conflict",
                "verified": False,
                "error_code": "existing_value_conflict",
            })
            result = store.finish({"task_id": task["task_id"]})

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "incomplete_or_unverified_steps")
        self.assertFalse(result["save_attempted"])

    def test_native_host_uses_chromium_length_prefixed_json(self) -> None:
        with TemporaryDirectory() as directory:
            host = NativeMessageHost(TaskStore(Path(directory)))
            request = json.dumps({"type": "get_status"}).encode("utf-8")
            source = io.BytesIO(struct.pack("<I", len(request)) + request)
            target = io.BytesIO()

            self.assertEqual(host.serve(source, target), 0)
            target.seek(0)
            length = struct.unpack("<I", target.read(4))[0]
            response = json.loads(target.read(length).decode("utf-8"))

        self.assertTrue(response["ok"])
        self.assertEqual(response["payload"]["status"], "empty")


class EdgeExtensionPackageTests(unittest.TestCase):
    def test_manifest_uses_minimal_permissions_and_profile_has_versioned_fields(self) -> None:
        manifest = json.loads((ROOT / "edge_extension" / "manifest.json").read_text(encoding="utf-8"))
        profile = json.loads((ROOT / "edge_extension" / "profiles" / "dom_profile.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(set(manifest["permissions"]), {"activeTab", "nativeMessaging", "scripting", "storage"})
        self.assertNotIn("host_permissions", manifest)
        self.assertTrue({"cookies", "history", "webRequest"}.isdisjoint(manifest["permissions"]))
        self.assertEqual(profile["version"], "dom-poc-v3")
        self.assertTrue({
            "patent_no", "application_title", "patent_type", "application_date",
            "grant_date", "joint_application", "rights_holder_rows", "inventor_rows",
            "first_inventor_select", "patentee_merge", "inventor_merge",
        }.issubset(profile["fields"]))
        self.assertEqual(profile["fields"]["first_inventor_select"]["person"], {"mode": "direct"})
        self.assertEqual(profile["fields"]["rights_holder_rows"]["table"]["identity_selector"], ".x-grid-cell-ZLQR_NAME")
        self.assertEqual(profile["fields"]["inventor_rows"]["table"]["identity_selector"], ".x-grid-cell-USERNAME")
        self.assertEqual(profile["fields"]["rights_holder_rows"]["table"]["identity_texts"], ["单位名称"])
        self.assertEqual(profile["fields"]["inventor_rows"]["table"]["identity_texts"], ["姓名", "身份证号"])
        self.assertEqual(profile["fields"]["patentee_merge"]["selectors"], ["input[name='ZL_ZLQRHB']"])
        self.assertEqual(profile["fields"]["inventor_merge"]["selectors"], ["input[name='ZL_FMRHB']"])
        self.assertIn("保存", profile["blocked_action_labels"])

    def test_content_script_contains_no_destructive_click_path(self) -> None:
        content = (ROOT / "edge_extension" / "content.js").read_text(encoding="utf-8")
        worker = (ROOT / "edge_extension" / "service_worker.js").read_text(encoding="utf-8")
        installer = (ROOT / "edge_extension" / "install" / "configure_enterprise_extension.ps1").read_text(encoding="utf-8")

        self.assertIn("empty_or_same", content)
        self.assertIn("existing_value_conflict", content)
        self.assertIn("readback_mismatch", content)
        self.assertNotIn("<all_urls>", content + worker)
        self.assertIn('$_ -ne "activeTab"', installer)

    def test_hidden_extjs_table_editor_is_not_treated_as_active(self) -> None:
        content = (ROOT / "edge_extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn("function elementVisible(element)", content)
        self.assertGreaterEqual(
            content.count("querySelectorAll(tableProfile.new_input_selector)]).filter(elementVisible)"),
            3,
        )
        self.assertIn('new PointerEvent("pointerdown"', content)
        self.assertIn('new MouseEvent("click", { ...base, buttons: 0, detail: 1 })', content)
        self.assertIn("clickLikeUser(addButtons[0]);", content)
        self.assertIn("const values = [...new Set(row.querySelectorAll(table.value_selector))];", content)
        self.assertIn('new KeyboardEvent("keydown", options)', content)
        self.assertIn("commitTableEditor(newInput);", content)
        self.assertIn("async function overwriteExistingTableRows", content)
        self.assertIn('throw new Error("table_overwrite_requires_delete")', content)
        self.assertNotIn('allowOverwrite ? "overwrite_not_supported_table"', content)
        self.assertIn("`write_failed:${field.field_id}:${errorCode}`", content)


if __name__ == "__main__":
    unittest.main()
