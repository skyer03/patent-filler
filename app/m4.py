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
    trace: list[dict[str, object]] = field(default_factory=list)

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
            "trace": list(self.trace),
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


def _normalise_page_value(control_id: str, value: str) -> str:
    normalized = " ".join(str(value).strip().split())
    if control_id == "patent_no":
        return _web_patent_no(normalized)
    if control_id in {"application_date", "grant_date"}:
        return normalized.replace("/", "-").replace(".", "-")
    if control_id in {"patentee_merge", "inventor_merge"}:
        return normalized.replace(";", "；").replace(" ", "")
    return normalized


def _is_empty_page_value(value: str | None) -> bool:
    if value is None:
        return True
    normalized = "".join(str(value).strip().split())
    return not normalized or normalized in {"请选择", "未选择", "尚未选择"} or normalized.startswith("尚未选择（")


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
        values={
            "patent_no": "",
            "application_title": "",
            "application_date": "",
            "grant_date": "",
            "summary_text": "",
        },
        selected_options={"patent_type": ""},
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
        on_action: Callable[[int, int, Action], None] | None = None,
        verify_attachments: bool = True,
    ) -> None:
        self.profile = profile
        self.adapter_factory = adapter_factory
        self.binder = binder or WindowBinder()
        self.binding = binding
        self.max_retries = max_retries
        self.stop_requested = stop_requested or (lambda: False)
        self.focus_ok = focus_ok or (lambda: True)
        self.on_action = on_action
        self.verify_attachments = verify_attachments

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
            report.actions = self.plan_actions(draft, manual, before)
        except M4PlanningError as error:
            report.status = "blocked"
            report.reason = str(error)
            report.final_page = before
            return self._finish(report, diagnostics)

        report.skipped_manual_fields = sorted(MANUAL_FIELDS - set(manual.values))
        report.phase = "automation"
        action_index = 0

        def before_action(action: Action) -> None:
            nonlocal action_index
            action_index += 1
            if self.on_action is not None:
                self.on_action(action_index, len(report.actions), action)

        report.automation = AutomationEngine(
            self.profile,
            page,
            max_retries=self.max_retries,
            stop_requested=self.stop_requested,
            focus_ok=self.focus_ok,
            before_action=before_action,
        ).run(report.actions)
        trace_event = getattr(page, "trace_event", None)
        if callable(trace_event):
            for step in report.automation.steps:
                trace_event(
                    "automation.verify",
                    "passed" if step.verified else "failed",
                    control_id=step.action.control_id,
                    kind=step.action.kind,
                    attempts=step.attempts,
                    error_code=step.error_code,
                )
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

    def plan_actions(
        self,
        draft: CertificateDraft,
        supplements: Mapping[str, object] | ManualFields | None,
        before: PageSnapshot,
    ) -> list[Action]:
        """Build a conflict-aware public action plan from an observed page.

        Empty fields are filled, matching fields are skipped, and a non-empty
        different value blocks the run before input.  This is also what makes
        an interrupted run safe to start again.
        """

        review = review_draft(draft)
        if not review.approved:
            raise M4PlanningError("证书草稿仍有待复核字段，不能自动填报。")
        manual = supplements if isinstance(supplements, ManualFields) else ManualFields.from_mapping(supplements)
        return self._plan_actions(draft, manual, before)

    def _plan_actions(
        self,
        draft: CertificateDraft,
        manual: ManualFields,
        before: PageSnapshot,
    ) -> list[Action]:
        actions: list[Action] = []
        values = {
            "patent_no": _web_patent_no(draft.patent_no or ""),
            "application_title": draft.title or "",
            "patent_type": _web_patent_type(draft.patent_type or ""),
            "application_date": draft.application_date or "",
            "grant_date": draft.grant_publication_date or "",
        }
        basic_actions = self._difference_actions(before.values, before.selected_options, (
            ("patent_no", "fill", values["patent_no"]),
            ("application_title", "fill", values["application_title"]),
            ("patent_type", "select", values["patent_type"]),
            ("application_date", "date", values["application_date"]),
            ("grant_date", "date", values["grant_date"]),
        ))

        if "joint_application" in manual.values:
            kind = "check" if manual.get("joint_application").casefold() in {"true", "1", "yes", "是"} else "uncheck"
            desired = kind == "check"
            current = before.checked.get("joint_application")
            if current is None or current is not desired:
                basic_actions.append(Action("joint_application", kind, manual.get("joint_application")))
        if basic_actions:
            actions.append(Action("basic_info", "scroll"))
            actions.extend(basic_actions)

        party_actions = self._table_actions("rights_holder_rows", draft.current_patentees, before)
        party_actions.extend(self._table_actions("inventor_rows", draft.inventors, before))
        party_actions.extend(self._difference_actions(before.values, before.selected_options, (
            ("patentee_merge", "fill", "；".join(draft.current_patentees)),
            ("inventor_merge", "fill", "；".join(draft.inventors)),
        )))
        if before.selected_person is None:
            party_actions.append(Action("first_inventor_select", "person", draft.inventors[0]))
        elif _normalise_page_value("first_inventor_select", before.selected_person) != _normalise_page_value(
            "first_inventor_select", draft.inventors[0]
        ):
            raise M4PlanningError("第一发明人已有值与确认草稿不一致，拒绝覆盖。")

        party_manual = tuple(
            (name, "fill", manual.get(name))
            for name in ("first_inventor_id", "first_inventor_contact")
            if name in manual.values
        )
        party_actions.extend(self._difference_actions(before.values, before.selected_options, party_manual))
        if party_actions:
            actions.append(Action("parties", "scroll"))
            actions.extend(party_actions)

        summary_actions: list[Action] = []
        if "summary_text" in manual.values:
            fill_summary = self._difference_actions(
                before.values, before.selected_options, (("summary_text", "fill", manual.get("summary_text")),)
            )
            if fill_summary:
                summary_actions.extend((Action("summary_edit", "edit"), *fill_summary))
        for name in ("benefit_efficiency", "benefit_reliability", "benefit_energy"):
            if name in manual.values:
                summary_actions.extend(
                    self._difference_actions(before.values, before.selected_options, ((name, "fill", manual.get(name)),))
                )
        if summary_actions:
            actions.append(Action("technical_summary", "scroll"))
            actions.extend(summary_actions)

        origin_fields = (
            "tech_project_name",
            "tech_project_no",
            "tech_project_org",
            "engineering_project_name",
            "engineering_project_no",
            "other_origin",
        )
        origin_actions = self._difference_actions(
            before.values,
            before.selected_options,
            tuple((name, "fill", manual.get(name)) for name in origin_fields if name in manual.values),
        )
        if origin_actions:
            actions.append(Action("origin", "scroll"))
            actions.extend(origin_actions)

        operator_fields = ("pct_count", "operator_name", "operator_phone", "operator_email")
        operator_actions = self._difference_actions(
            before.values,
            before.selected_options,
            tuple(
                (name, "select" if name == "pct_count" else "fill", manual.get(name))
                for name in operator_fields
                if name in manual.values
            ),
        )
        if operator_actions:
            actions.append(Action("operator", "scroll"))
            actions.extend(operator_actions)

        if self.verify_attachments:
            actions.append(Action("attachments", "scroll"))
            actions.append(Action("attachments", "verify_attachments", manual.get("attachment_name")))
        return actions

    @staticmethod
    def _difference_actions(
        values: Mapping[str, str],
        selected: Mapping[str, str],
        requested: Sequence[tuple[str, str, str]],
    ) -> list[Action]:
        actions: list[Action] = []
        for control_id, kind, expected in requested:
            current = selected.get(control_id) if kind in {"select", "dropdown"} else values.get(control_id)
            if _is_empty_page_value(current):
                actions.append(Action(control_id, kind, expected))
                continue
            if _normalise_page_value(control_id, str(current)) == _normalise_page_value(control_id, expected):
                continue
            raise M4PlanningError(f"控件 {control_id} 已有非空值且与确认草稿不一致，拒绝覆盖。")
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
