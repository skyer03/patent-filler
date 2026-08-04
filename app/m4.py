"""M4 end-to-end workflow built on the verified M3 action engine.

The workflow is intentionally small: certificate data is the only automatic
source for certificate fields, while non-certificate fields are accepted only
from an explicit manual/configuration mapping.  The default adapter is an
in-memory model of the offline mock page, which makes the 50-sample M4 exit
criterion repeatable without opening a browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .automation import (
    Action,
    AttachmentSnapshot,
    AutomationEngine,
    AutomationReport,
    InMemoryPageAdapter,
    PageAdapter,
    PageProfile,
    PageSnapshot,
    WindowBinding,
    WindowBinder,
    WindowRect,
)
from .domain import REQUIRED_FIELDS, CertificateDraft


MANUAL_FIELDS = frozenset(
    {
        "summary_text",
        "benefit_efficiency",
        "benefit_reliability",
        "benefit_energy",
        "first_inventor_id",
        "first_inventor_contact",
        "joint_application",
        "tech_project_name",
        "tech_project_no",
        "tech_project_org",
        "engineering_project_name",
        "engineering_project_no",
        "other_origin",
        "pct_count",
        "operator_name",
        "operator_phone",
        "operator_email",
        "attachment_name",
    }
)

SENSITIVE_FIELDS = frozenset(
    {"first_inventor_id", "first_inventor_contact", "operator_phone", "operator_email"}
)


class M4PlanningError(ValueError):
    """Raised when the draft or current page cannot be safely planned."""


@dataclass(frozen=True)
class ManualFields:
    """Explicit values for fields that a certificate cannot provide."""

    values: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, source: Mapping[str, object] | None) -> "ManualFields":
        if not source:
            return cls({})
        raw = source.get("manual_fields", source)
        if not isinstance(raw, Mapping):
            raise M4PlanningError("manual_fields 必须是 JSON 对象。")
        aliases = {"technical_summary": "summary_text"}
        values: dict[str, str] = {}
        for key, value in raw.items():
            name = aliases.get(str(key), str(key))
            if name not in MANUAL_FIELDS:
                raise M4PlanningError(f"不允许的人工/配置字段：{key}")
            if value is None:
                continue
            if isinstance(value, bool):
                values[name] = "true" if value else "false"
            else:
                values[name] = str(value).strip()
        return cls(values)

    def get(self, field_name: str, default: str = "") -> str:
        return self.values.get(field_name, default)

    def to_dict(self) -> dict[str, str]:
        return dict(self.values)


@dataclass(frozen=True)
class ReviewResult:
    approved: bool
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"approved": self.approved, "issues": list(self.issues)}


def review_draft(draft: CertificateDraft) -> ReviewResult:
    """Require all certificate-driven fields to be present and confirmed."""

    issues: list[str] = []
    for field_name in REQUIRED_FIELDS:
        value = getattr(draft, field_name)
        if not value:
            issues.append(f"missing:{field_name}")
    for field_name in draft.needs_review:
        if f"needs_review:{field_name}" not in issues:
            issues.append(f"needs_review:{field_name}")
    for field_name in ("current_patentees", "inventors"):
        values = list(getattr(draft, field_name))
        if len(values) != len(set(values)):
            issues.append(f"duplicate:{field_name}")
    return ReviewResult(not issues, tuple(issues))


@dataclass
class M4Report:
    status: str
    phase: str
    source_file: str
    sample_index: int | None
    review: ReviewResult
    binding: WindowBinding | None = None
    actions: list[Action] = field(default_factory=list)
    skipped_manual_fields: list[str] = field(default_factory=list)
    automation: AutomationReport | None = None
    final_page: PageSnapshot | None = None
    certificate_match: bool = False
    mismatches: list[str] = field(default_factory=list)
    reason: str | None = None
    diagnostics_path: str | None = None
    save_attempted: bool = False

    @property
    def verified(self) -> bool:
        return (
            self.status == "completed"
            and self.certificate_match
            and self.automation is not None
            and self.automation.verified
            and not self.save_attempted
        )

    def to_dict(self, *, include_steps: bool = True) -> dict[str, object]:
        automation = self.automation.to_dict() if self.automation else None
        if automation is not None and not include_steps:
            automation = {
                key: value for key, value in automation.items() if key != "steps"
            }
        return {
            "status": self.status,
            "phase": self.phase,
            "verified": self.verified,
            "source_file": self.source_file,
            "sample_index": self.sample_index,
            "review": self.review.to_dict(),
            "binding": self.binding.to_dict() if self.binding else None,
            "actions": [
                {"control_id": item.control_id, "kind": item.kind, "value": item.value}
                for item in self.actions
            ],
            "skipped_manual_fields": list(self.skipped_manual_fields),
            "automation": automation,
            "final_page": self.final_page.to_dict() if self.final_page else None,
            "certificate_match": self.certificate_match,
            "mismatches": list(self.mismatches),
            "reason": self.reason,
            "diagnostics_path": self.diagnostics_path,
            "save_attempted": self.save_attempted,
        }


@dataclass
class M4RegressionReport:
    reports: list[M4Report]

    @property
    def total(self) -> int:
        return len(self.reports)

    @property
    def passed(self) -> int:
        return sum(report.verified for report in self.reports)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def verified(self) -> bool:
        return self.total > 0 and self.failed == 0

    @property
    def status(self) -> str:
        return "completed" if self.verified else "failed"

    def to_dict(self, *, include_steps: bool = True) -> dict[str, object]:
        return {
            "status": self.status,
            "verified": self.verified,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "save_attempted": any(report.save_attempted for report in self.reports),
            "reports": [report.to_dict(include_steps=include_steps) for report in self.reports],
        }


def _web_patent_no(value: str) -> str:
    normalized = value.strip().upper()
    if normalized.startswith("ZL"):
        normalized = normalized[2:]
    return normalized.replace(".", "")


def _web_patent_type(value: str) -> str:
    return {"invention": "发明", "utility_model": "实用新型"}.get(value, value)


def _simulation_binding() -> WindowBinding:
    return WindowBinding(
        0,
        "offline-mock-site",
        WindowRect(0, 0, 1280, 900),
        datetime.now(timezone.utc).isoformat(),
    )


def _default_adapter(draft: CertificateDraft, manual: ManualFields) -> InMemoryPageAdapter:
    attachment_name = manual.get("attachment_name") or Path(draft.source_file).name or "证书样本.pdf"
    people = {
        name: f"模拟人员库 / {index:04d}"
        for index, name in enumerate(draft.inventors, start=1)
    }
    return InMemoryPageAdapter(
        tables={"rights_holder_rows": [], "inventor_rows": []},
        people=people,
        attachments=(AttachmentSnapshot(attachment_name, True, True),),
    )


AdapterFactory = Callable[[CertificateDraft, ManualFields], PageAdapter]


class M4Workflow:
    """Run one reviewed certificate through the safe M4 pipeline."""

    def __init__(
        self,
        profile: PageProfile,
        *,
        adapter_factory: AdapterFactory = _default_adapter,
        binder: WindowBinder | None = None,
        binding: WindowBinding | None = None,
        max_retries: int = 1,
        stop_requested: Callable[[], bool] | None = None,
        focus_ok: Callable[[], bool] | None = None,
    ) -> None:
        self.profile = profile
        self.adapter_factory = adapter_factory
        self.binder = binder or WindowBinder()
        self.binding = binding
        self.max_retries = max_retries
        self.stop_requested = stop_requested or (lambda: False)
        self.focus_ok = focus_ok or (lambda: True)

    def bind_window(self, title: str) -> WindowBinding:
        self.binding = self.binder.bind_by_title(title)
        return self.binding

    def run(
        self,
        draft: CertificateDraft,
        supplements: Mapping[str, object] | ManualFields | None = None,
        *,
        adapter: PageAdapter | None = None,
        diagnostics: str | Path | None = None,
    ) -> M4Report:
        manual = supplements if isinstance(supplements, ManualFields) else ManualFields.from_mapping(supplements)
        review = review_draft(draft)
        report = M4Report(
            "ready",
            "review",
            draft.source_file,
            draft.sample_index,
            review,
            binding=self.binding or _simulation_binding(),
        )
        if not review.approved:
            report.status = "blocked"
            report.reason = "证书草稿仍有待复核字段，不能自动填报。"
            return self._finish(report, diagnostics)

        report.phase = "bind"
        page = adapter or self.adapter_factory(draft, manual)
        before = page.observe()
        report.phase = "preflight"
        if before.page_state != "ready":
            report.status = "blocked"
            report.reason = f"页面状态不是 ready：{before.page_state}。"
            report.final_page = before
            return self._finish(report, diagnostics)

        try:
            report.actions = self._plan_actions(draft, manual, before)
        except M4PlanningError as error:
            report.status = "blocked"
            report.reason = str(error)
            report.final_page = before
            return self._finish(report, diagnostics)

        report.skipped_manual_fields = sorted(MANUAL_FIELDS - set(manual.values))
        report.phase = "automation"
        report.automation = AutomationEngine(
            self.profile,
            page,
            max_retries=self.max_retries,
            stop_requested=self.stop_requested,
            focus_ok=self.focus_ok,
        ).run(report.actions)
        report.final_page = page.observe()
        if not report.automation.verified:
            report.status = report.automation.status
            report.reason = report.automation.reason
            return self._finish(report, diagnostics)

        report.phase = "verify"
        report.mismatches = compare_page_to_draft(draft, report.final_page, manual)
        report.certificate_match = not report.mismatches
        if not report.certificate_match:
            report.status = "failed"
            report.reason = "网页最终值与填报草稿不一致。"
        else:
            report.status = "completed"
            report.phase = "report"
            report.reason = "已完成填报草稿并停止在最终保存前。"
        return self._finish(report, diagnostics)

    def retry(
        self,
        draft: CertificateDraft,
        supplements: Mapping[str, object] | ManualFields | None = None,
        *,
        adapter: PageAdapter | None = None,
        diagnostics: str | Path | None = None,
    ) -> M4Report:
        """Retry a failed/paused run using the same guarded planning path."""

        return self.run(draft, supplements, adapter=adapter, diagnostics=diagnostics)

    def _plan_actions(
        self,
        draft: CertificateDraft,
        manual: ManualFields,
        before: PageSnapshot,
    ) -> list[Action]:
        actions: list[Action] = [Action("basic_info", "scroll")]
        values = {
            "patent_no": _web_patent_no(draft.patent_no or ""),
            "application_title": draft.title or "",
            "patent_type": _web_patent_type(draft.patent_type or ""),
            "application_date": draft.application_date or "",
            "grant_date": draft.grant_publication_date or "",
        }
        actions.extend(Action(control_id, kind, value) for control_id, kind, value in (
            ("patent_no", "fill", values["patent_no"]),
            ("application_title", "fill", values["application_title"]),
            ("patent_type", "select", values["patent_type"]),
            ("application_date", "date", values["application_date"]),
            ("grant_date", "date", values["grant_date"]),
        ))

        if "joint_application" in manual.values:
            kind = "check" if manual.get("joint_application").casefold() in {"true", "1", "yes", "是"} else "uncheck"
            actions.append(Action("joint_application", kind, manual.get("joint_application")))

        actions.append(Action("parties", "scroll"))
        actions.extend(self._table_actions("rights_holder_rows", draft.current_patentees, before))
        actions.extend(self._table_actions("inventor_rows", draft.inventors, before))
        actions.append(Action("patentee_merge", "fill", "；".join(draft.current_patentees)))
        actions.append(Action("inventor_merge", "fill", "；".join(draft.inventors)))
        if before.selected_person != draft.inventors[0]:
            actions.append(Action("first_inventor_select", "person", draft.inventors[0]))

        if any(name in manual.values for name in ("summary_text", "benefit_efficiency", "benefit_reliability", "benefit_energy")):
            actions.append(Action("technical_summary", "scroll"))
        if "summary_text" in manual.values:
            actions.extend((
                Action("summary_edit", "edit"),
                Action("summary_text", "fill", manual.get("summary_text")),
            ))
        for name in ("benefit_efficiency", "benefit_reliability", "benefit_energy"):
            if name in manual.values:
                actions.append(Action(name, "fill", manual.get(name)))

        origin_fields = (
            "tech_project_name",
            "tech_project_no",
            "tech_project_org",
            "engineering_project_name",
            "engineering_project_no",
            "other_origin",
        )
        if any(name in manual.values for name in origin_fields):
            actions.append(Action("origin", "scroll"))
            actions.extend(Action(name, "fill", manual.get(name)) for name in origin_fields if name in manual.values)

        operator_fields = ("pct_count", "operator_name", "operator_phone", "operator_email")
        if any(name in manual.values for name in operator_fields):
            actions.append(Action("operator", "scroll"))
            if "pct_count" in manual.values:
                actions.append(Action("pct_count", "select", manual.get("pct_count")))
            actions.extend(Action(name, "fill", manual.get(name)) for name in operator_fields if name in manual.values and name != "pct_count")
        if "first_inventor_id" in manual.values or "first_inventor_contact" in manual.values:
            actions.extend(
                Action(name, "fill", manual.get(name))
                for name in ("first_inventor_id", "first_inventor_contact")
                if name in manual.values
            )

        actions.append(Action("attachments", "scroll"))
        actions.append(Action("attachments", "verify_attachments", manual.get("attachment_name")))
        return actions

    @staticmethod
    def _table_actions(table_id: str, expected: Sequence[str], before: PageSnapshot) -> list[Action]:
        existing = tuple(before.tables.get(table_id, ()))
        expected_tuple = tuple(expected)
        if len(existing) > len(expected_tuple) or existing != expected_tuple[: len(existing)]:
            raise M4PlanningError(f"动态表格 {table_id} 的已有行与确认草稿不一致，拒绝删除或重排。")
        return [Action(table_id, "add_row", value) for value in expected_tuple[len(existing) :]]

    def _finish(self, report: M4Report, diagnostics: str | Path | None) -> M4Report:
        if diagnostics is not None:
            report.diagnostics_path = write_diagnostics(diagnostics, report)
        return report


def compare_page_to_draft(
    draft: CertificateDraft,
    page: PageSnapshot | None,
    manual: ManualFields | None = None,
) -> list[str]:
    if page is None:
        return ["page_snapshot_missing"]
    manual = manual or ManualFields({})
    expected_values = {
        "patent_no": _web_patent_no(draft.patent_no or ""),
        "application_title": draft.title or "",
        "application_date": draft.application_date or "",
        "grant_date": draft.grant_publication_date or "",
        "patentee_merge": "；".join(draft.current_patentees),
        "inventor_merge": "；".join(draft.inventors),
    }
    mismatches = [
        f"{name}: {page.values.get(name)!r} != {expected!r}"
        for name, expected in expected_values.items()
        if page.values.get(name) != expected
    ]
    expected_type = _web_patent_type(draft.patent_type or "")
    if page.selected_options.get("patent_type") != expected_type:
        mismatches.append(
            f"patent_type: {page.selected_options.get('patent_type')!r} != {expected_type!r}"
        )
    if tuple(page.tables.get("rights_holder_rows", ())) != tuple(draft.current_patentees):
        mismatches.append("rights_holder_rows: final rows differ from draft")
    if tuple(page.tables.get("inventor_rows", ())) != tuple(draft.inventors):
        mismatches.append("inventor_rows: final rows differ from draft")
    if page.selected_person != draft.inventors[0]:
        mismatches.append(f"selected_person: {page.selected_person!r} != {draft.inventors[0]!r}")

    for name, value in manual.values.items():
        if name == "attachment_name":
            if not any(item.name == value for item in page.attachments):
                mismatches.append(f"attachment_name: {value!r} not found")
        elif name == "joint_application":
            expected = value.casefold() in {"true", "1", "yes", "是"}
            if page.checked.get(name) is not expected:
                mismatches.append(f"{name}: {page.checked.get(name)!r} != {expected!r}")
        elif name == "pct_count":
            if page.selected_options.get(name) != value:
                mismatches.append(f"{name}: {page.selected_options.get(name)!r} != {value!r}")
        elif name not in {"technical_summary"} and page.values.get(name) != value:
            mismatches.append(f"{name}: {page.values.get(name)!r} != {value!r}")
    return mismatches


def write_diagnostics(path: str | Path, report: M4Report) -> str:
    """Write a local JSON diagnostic package with sensitive values redacted."""

    target = Path(path)
    if target.suffix.casefold() != ".json":
        target.mkdir(parents=True, exist_ok=True)
        target = target / "diagnostics.json"
    payload = report.to_dict()
    _redact_diagnostics(payload)
    package = {
        "format": "m4-diagnostics-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": report.status,
        "error_code": report.reason,
        "report": payload,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(target)


def _redact_diagnostics(value: object, key: str = "") -> None:
    if isinstance(value, dict):
        control_id = value.get("control_id")
        for child_key, child in value.items():
            if child_key in SENSITIVE_FIELDS or (
                child_key == "value" and (key in SENSITIVE_FIELDS or control_id in SENSITIVE_FIELDS)
            ):
                value[child_key] = "[REDACTED]"
            else:
                _redact_diagnostics(child, child_key)
    elif isinstance(value, list):
        for child in value:
            _redact_diagnostics(child, key)


def run_m4_e2e(
    profile: PageProfile,
    draft: CertificateDraft,
    *,
    supplements: Mapping[str, object] | ManualFields | None = None,
    diagnostics: str | Path | None = None,
) -> M4Report:
    return M4Workflow(profile).run(draft, supplements, diagnostics=diagnostics)


def run_m4_regression(
    profile: PageProfile,
    golden_dir: str | Path,
    *,
    supplements: Mapping[str, object] | ManualFields | None = None,
    diagnostics_dir: str | Path | None = None,
    binding: WindowBinding | None = None,
    max_retries: int = 1,
) -> M4RegressionReport:
    reports: list[M4Report] = []
    golden_paths = sorted(Path(golden_dir).glob("*.json"), key=lambda path: path.name)
    for path in golden_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        draft = CertificateDraft.from_dict(data)
        if draft.sample_index is None:
            try:
                draft.sample_index = int(path.name.split("-", 1)[0])
            except (ValueError, IndexError):
                pass
        diagnostic_path = None
        if diagnostics_dir is not None:
            diagnostic_path = Path(diagnostics_dir) / f"{path.stem}.json"
        reports.append(
            M4Workflow(profile, binding=binding, max_retries=max_retries).run(
                draft, supplements, diagnostics=diagnostic_path
            )
        )
    return M4RegressionReport(reports)


__all__ = [
    "MANUAL_FIELDS",
    "M4PlanningError",
    "ManualFields",
    "ReviewResult",
    "M4Report",
    "M4RegressionReport",
    "M4Workflow",
    "compare_page_to_draft",
    "review_draft",
    "run_m4_e2e",
    "run_m4_regression",
    "write_diagnostics",
]
