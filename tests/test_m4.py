from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.automation import AttachmentSnapshot, InMemoryPageAdapter, PageSnapshot, load_profile
from app.domain import CertificateDraft
from app.m4 import M4PlanningError, M4Workflow, ManualFields, run_m4_regression


ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "PROJECT_PLAN_M2_PROFILE.json")


def first_draft() -> CertificateDraft:
    data = json.loads((ROOT / "m0" / "golden" / "001-ZL202010430096.0.json").read_text(encoding="utf-8"))
    return CertificateDraft.from_dict(data)


class M4WorkflowTests(unittest.TestCase):
    def test_fifty_golden_samples_complete_without_save(self) -> None:
        report = run_m4_regression(PROFILE, ROOT / "m0" / "golden")

        self.assertTrue(report.verified, report.to_dict(include_steps=False))
        self.assertEqual((report.total, report.passed, report.failed), (50, 50, 0))
        self.assertTrue(all(not item.save_attempted for item in report.reports))

    def test_manual_fields_are_controlled_and_read_back(self) -> None:
        draft = first_draft()
        manual = {
            "summary_text": "人工确认的技术摘要。",
            "benefit_efficiency": "提高效率。",
            "benefit_reliability": "提高可靠性。",
            "benefit_energy": "降低能耗。",
            "first_inventor_id": "ID-REDACTED-TEST",
            "first_inventor_contact": "CONTACT-REDACTED-TEST",
            "joint_application": True,
            "tech_project_name": "技术项目 A",
            "tech_project_no": "TECH-001",
            "tech_project_org": "技术部门",
            "engineering_project_name": "工程项目 B",
            "engineering_project_no": "ENG-002",
            "other_origin": "人工确认",
            "pct_count": "否",
            "operator_name": "经办人",
            "operator_phone": "PHONE-REDACTED-TEST",
            "operator_email": "EMAIL-REDACTED-TEST",
            "attachment_name": "certificate.pdf",
        }

        report = M4Workflow(PROFILE).run(draft, manual)

        self.assertTrue(report.verified, report.to_dict(include_steps=False))
        self.assertEqual(report.final_page.values["summary_text"], manual["summary_text"])
        self.assertEqual(report.final_page.tables["inventor_rows"], tuple(draft.inventors))

    def test_design_type_uses_the_design_web_option(self) -> None:
        draft = first_draft()
        draft.patent_type = "design"

        report = M4Workflow(PROFILE).run(draft)

        self.assertTrue(report.verified, report.to_dict(include_steps=False))
        self.assertEqual(report.final_page.selected_options["patent_type"], "外观设计")

    def test_pending_review_and_existing_row_mismatch_stop_before_input(self) -> None:
        draft = first_draft()
        draft.add_review("inventors")
        blocked = M4Workflow(PROFILE).run(draft)
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.phase, "review")

        clean = first_draft()
        adapter = InMemoryPageAdapter(
            tables={"rights_holder_rows": ["不在确认草稿中的单位"], "inventor_rows": []},
            people={name: "mock" for name in clean.inventors},
        )
        before = adapter.observe().to_dict()
        mismatched = M4Workflow(PROFILE).run(clean, adapter=adapter)
        self.assertEqual(mismatched.status, "blocked")
        self.assertEqual(adapter.observe().to_dict(), before)

    def test_public_planner_skips_matching_values_and_rejects_conflicts(self) -> None:
        draft = first_draft()
        matching = PageSnapshot(
            page_state="ready",
            values={
                "patent_no": "2020104300960",
                "application_title": draft.title or "",
                "application_date": draft.application_date or "",
                "grant_date": draft.grant_publication_date or "",
                "patentee_merge": "；".join(draft.current_patentees),
                "inventor_merge": "；".join(draft.inventors),
            },
            selected_options={"patent_type": "发明"},
            tables={
                "rights_holder_rows": tuple(draft.current_patentees),
                "inventor_rows": tuple(draft.inventors),
            },
            selected_person=draft.inventors[0],
            attachments=(AttachmentSnapshot("certificate.pdf", True, True),),
        )

        actions = M4Workflow(PROFILE).plan_actions(draft, ManualFields({}), matching)

        self.assertEqual([item.kind for item in actions], ["scroll", "verify_attachments"])
        conflict = PageSnapshot(page_state="ready", values={"patent_no": "999999"})
        with self.assertRaises(M4PlanningError):
            M4Workflow(PROFILE).plan_actions(draft, ManualFields({}), conflict)

    def test_mock_first_pass_can_defer_attachment_verification(self) -> None:
        draft = first_draft()
        matching = PageSnapshot(
            page_state="ready",
            values={
                "patent_no": "2020104300960",
                "application_title": draft.title or "",
                "application_date": draft.application_date or "",
                "grant_date": draft.grant_publication_date or "",
                "patentee_merge": ";".join(draft.current_patentees),
                "inventor_merge": ";".join(draft.inventors),
            },
            selected_options={"patent_type": "发明"},
            tables={
                "rights_holder_rows": tuple(draft.current_patentees),
                "inventor_rows": tuple(draft.inventors),
            },
            selected_person=draft.inventors[0],
            attachments=(AttachmentSnapshot("certificate.pdf", True, True),),
        )

        actions = M4Workflow(PROFILE, verify_attachments=False).plan_actions(
            draft, ManualFields({}), matching
        )

        self.assertEqual(actions, [])

    def test_diagnostics_redact_sensitive_page_values(self) -> None:
        draft = first_draft()
        with TemporaryDirectory() as directory:
            target = Path(directory) / "diagnostics.json"
            report = M4Workflow(PROFILE).run(
                draft,
                {"first_inventor_id": "SECRET-ID", "operator_phone": "SECRET-PHONE"},
                diagnostics=target,
            )
            content = target.read_text(encoding="utf-8")

        self.assertTrue(report.verified)
        self.assertNotIn("SECRET-ID", content)
        self.assertNotIn("SECRET-PHONE", content)
        self.assertIn("m4-diagnostics-v1", content)


if __name__ == "__main__":
    unittest.main()
