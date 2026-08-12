"""M7 product orchestration for the unified desktop entry point.

The M7 layer deliberately contains no Tk code.  It composes the already
verified M4, M5 and M6 primitives and adds a single report shape that makes
the execution boundary explicit: offline simulation uses
``InMemoryPageAdapter``; real-page recognition and input use the M5
foreground-window runner.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .automation import (
    Action,
    AutomationEngine,
    AutomationError,
    PageAdapter,
    ScreenPageAdapter,
    WindowBinder,
    WindowBinding,
    auto_update_profile_issues,
    load_profile,
)
from .certificate import CertificateParser
from .domain import CertificateDraft
from .dom_bridge import DEFAULT_PROFILE_VERSION as DEFAULT_DOM_PROFILE_VERSION
from .dom_bridge import TaskStore as DomTaskStore
from .jsonio import import_drafts
from .m4 import M4Report, ManualFields, M4Workflow, review_draft, write_diagnostics
from .m5 import BoundWindowCapture, FileCapture, M5FieldRunner, M5RunReport
from .automation.recognizer import annotate_image
from .m6 import (
    M6BatchReport,
    ProfileRegistry,
    QueueTask,
    TaskQueue,
    VersionedConfigStore,
    run_m4_queue,
)
from .version import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIMULATION_PROFILE = ROOT / "PROJECT_PLAN_M2_PROFILE.json"
DEFAULT_FIELD_PROFILE = ROOT / "resources" / "web_profiles" / "intranet_actual_v1.json"
DEFAULT_MOCK_SCREEN_PROFILE = ROOT / "resources" / "web_profiles" / "intranet_v1.json"
DEFAULT_TEMPLATES = ROOT / "resources" / "image_templates" / "intranet_v1"
DEFAULT_GOLDEN = ROOT / "m0" / "golden"
DEFAULT_QUEUE = Path(".m6") / "queue.json"


class M7Error(RuntimeError):
    """Raised when the unified workflow cannot be started safely."""


class M7Mode(str, Enum):
    AUTO_UPDATE = "auto_update"
    SIMULATION = "simulation"
    RECOGNITION_ONLY = "recognition_only"
    STEP = "step"
    CONTROLLED_BATCH = "controlled_batch"


@dataclass(frozen=True)
class M7SafetyReason:
    code: str
    message: str
    phase: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "phase": self.phase}


@dataclass
class M7RunReport:
    """Common report envelope for simulation, field and queue runs."""

    mode: str
    status: str
    phase: str
    executor: str
    payload: Any
    source_file: str | None = None
    safety_reasons: list[M7SafetyReason] = field(default_factory=list)
    save_attempted: bool = False
    manual_readback_required: bool = True

    @property
    def ready_for_review(self) -> bool:
        return self.status in {"completed", "recognized", "paused", "blocked", "failed"}

    @property
    def verified(self) -> bool:
        if self.save_attempted:
            return False
        payload_verified = getattr(self.payload, "verified", None)
        if payload_verified is not None:
            return bool(payload_verified)
        payload_safe = getattr(self.payload, "safe", None)
        if payload_safe is not None:
            return bool(payload_safe)
        return self.status in {"completed", "recognized"}

    def to_dict(self) -> dict[str, object]:
        payload = self.payload.to_dict() if hasattr(self.payload, "to_dict") else self.payload
        payload = copy.deepcopy(payload)
        _redact_report_payload(payload)
        return {
            "format": "m7-unified-report-v1",
            "app_version": APP_VERSION,
            "mode": self.mode,
            "status": self.status,
            "verified": self.verified,
            "ready_for_review": self.ready_for_review,
            "phase": self.phase,
            "executor": self.executor,
            "source_file": self.source_file,
            "safety_reasons": [item.to_dict() for item in self.safety_reasons],
            "save_attempted": self.save_attempted,
            "manual_readback_required": self.manual_readback_required,
            "payload": payload,
        }


@dataclass
class ControlProbeReport:
    """One-field screen probe with explicit input and readback evidence."""

    control_id: str
    status: str
    profile_id: str
    profile_version: str
    action_kind: str | None = None
    action_value: str | None = None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None
    action_sent: bool = False
    verified: bool = False
    stop_reason: str | None = None
    diagnostics_path: str | None = None
    trace: list[dict[str, object]] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return self.verified

    def to_dict(self) -> dict[str, object]:
        blocked_before_input = any(
            item.get("status") == "blocked_conflict"
            for item in self.trace
        ) or bool(self.stop_reason and "冲突" in self.stop_reason)
        input_status = "sent" if self.action_sent else (
            "blocked_before_input" if blocked_before_input else "not_sent"
        )
        verification_status = "success" if self.verified else (
            "failed" if self.status == "failed" else "not_run"
        )
        return {
            "format": "m7-basic-control-probe-v1",
            "control_id": self.control_id,
            "status": self.status,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "action_kind": self.action_kind,
            "action_value": self.action_value,
            "before": self.before,
            "after": self.after,
            "action_sent": self.action_sent,
            "input_status": input_status,
            "verified": self.verified,
            "verification_status": verification_status,
            "stop_reason": self.stop_reason,
            "reason": self.stop_reason,
            "diagnostics_path": self.diagnostics_path,
            "trace": self.trace,
        }


def _payload_dict(payload: Any) -> dict[str, Any]:
    value = payload.to_dict() if hasattr(payload, "to_dict") else payload
    return value if isinstance(value, dict) else {}


_SENSITIVE_REPORT_FIELDS = frozenset(
    {"first_inventor_id", "first_inventor_contact", "operator_phone", "operator_email"}
)


def _redact_report_payload(value: object, key: str = "") -> None:
    if isinstance(value, dict):
        control_id = value.get("control_id")
        for child_key, child in value.items():
            if child_key in _SENSITIVE_REPORT_FIELDS or (
                child_key == "value" and (key in _SENSITIVE_REPORT_FIELDS or control_id in _SENSITIVE_REPORT_FIELDS)
            ):
                value[child_key] = "[REDACTED]"
            else:
                _redact_report_payload(child, child_key)
    elif isinstance(value, list):
        for child in value:
            _redact_report_payload(child, key)


def _safety_reasons(payload: Any, phase: str) -> list[M7SafetyReason]:
    """Normalize safety stops from M4, M5 and M6 into one user-facing list."""

    data = _payload_dict(payload)
    reasons: list[M7SafetyReason] = []
    reason = data.get("reason")
    if reason and data.get("status") not in {"completed", "recognized"}:
        reasons.append(M7SafetyReason(str(data.get("error_code") or reason), str(reason), phase))

    for step in data.get("steps", []):
        if not isinstance(step, dict):
            continue
        if step.get("status") in {"blocked", "paused", "failed"}:
            code = str(step.get("error_code") or step.get("status"))
            message = str(step.get("message") or code)
            reasons.append(M7SafetyReason(code, message, "field_step"))

    for task in data.get("tasks", []):
        if not isinstance(task, dict) or task.get("status") not in {"paused", "failed"}:
            continue
        error = task.get("last_error") or {}
        if isinstance(error, dict):
            code = str(error.get("error_code") or task.get("status"))
            message = str(error.get("reason") or code)
        else:
            code, message = str(task.get("status")), str(error)
        reasons.append(M7SafetyReason(code, message, "queue"))
    return reasons


def _manual_values(value: Mapping[str, object] | ManualFields | None) -> ManualFields:
    return value if isinstance(value, ManualFields) else ManualFields.from_mapping(value)


TraceCallback = Callable[[dict[str, object]], None]


def _append_trace(
    trace: list[dict[str, object]],
    step: str,
    status: str,
    *,
    trace_callback: TraceCallback | None = None,
    **details: object,
) -> None:
    if len(trace) >= 1200:
        return
    event: dict[str, object] = {
        "seq": len(trace) + 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "step": step,
        "status": status,
    }
    event.update(details)
    trace.append(event)
    if trace_callback is not None:
        try:
            trace_callback(event)
        except Exception:
            # Diagnostics and UI log sinks must never interrupt the guarded
            # automation path.
            pass


def _live_log_path(diagnostics: str | Path | None) -> Path | None:
    if diagnostics is None:
        return None
    target = Path(diagnostics)
    if target.suffix.casefold() == ".json":
        return target.with_name(f"{target.stem}.live.log")
    return target / "live.log"


def _write_live_log_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# M7 live trace; values are intentionally redacted\n"
        f"# started_at={datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )


def _write_live_log_event(path: Path, event: Mapping[str, object]) -> None:
    details = {
        key: value
        for key, value in event.items()
        if key not in {"seq", "at", "step", "status", "text", "raw_value"}
    }
    line = f"{event.get('at', '')} [{event.get('step', '')}] {event.get('status', '')}"
    if details:
        line += " " + json.dumps(details, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def load_workflow_sources(source: str | Path) -> list[CertificateDraft]:
    """Import PDF/JSON sources using the same source rules as the M4 CLI."""

    path = Path(source)
    parser = CertificateParser()
    if path.is_dir():
        candidates = sorted(
            [*path.glob("*.pdf"), *path.glob("*.json")],
            key=lambda item: item.name.casefold(),
        )
        if not candidates:
            raise M7Error(f"目录中没有 PDF 或 JSON 草稿：{path}")
        drafts: list[CertificateDraft] = []
        for candidate in candidates:
            if candidate.suffix.casefold() == ".json":
                drafts.extend(import_drafts(candidate))
            else:
                drafts.append(parser.parse_file(candidate))
    elif path.suffix.casefold() == ".json":
        drafts = import_drafts(path)
    elif path.suffix.casefold() == ".pdf":
        drafts = [parser.parse_file(path)]
    else:
        raise M7Error(f"统一入口只接受 PDF、JSON 草稿或其目录：{path}")

    for index, draft in enumerate(drafts, start=1):
        if draft.sample_index is None:
            draft.sample_index = index
    return drafts


class M7Service:
    """Coordinate the four product modes and M6 queue controls."""

    def __init__(
        self,
        *,
        simulation_profile: str | Path = DEFAULT_SIMULATION_PROFILE,
        field_profile: str | Path = DEFAULT_FIELD_PROFILE,
        templates: str | Path = DEFAULT_TEMPLATES,
        queue_path: str | Path = DEFAULT_QUEUE,
        dom_store: str | Path | None = None,
        golden_path: str | Path = DEFAULT_GOLDEN,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.simulation_profile_path = Path(simulation_profile)
        self.field_profile_path = Path(field_profile)
        self.templates_path = Path(templates)
        self.queue_path = Path(queue_path)
        self.golden_path = Path(golden_path)
        self.stop_requested = stop_requested or (lambda: False)
        self.binder = WindowBinder()
        self.binding: WindowBinding | None = None
        state_root = self.queue_path.parent
        self.dom_store = DomTaskStore(Path(dom_store) if dom_store is not None else state_root / "dom-bridge")
        self.profile_registry = ProfileRegistry(state_root / "profiles")
        self.config_store = VersionedConfigStore(state_root / "manual-fields")

    def bind_window(self, title: str) -> WindowBinding:
        self.binding = self.binder.bind_by_title(title)
        return self.binding

    def prepare_dom_task(
        self,
        draft: CertificateDraft,
        manual: Mapping[str, object] | ManualFields | None = None,
        *,
        profile_version: str = DEFAULT_DOM_PROFILE_VERSION,
        include_complex: bool = False,
        allow_overwrite: bool = False,
    ) -> dict[str, object]:
        """Publish one reviewed draft for the local-only Edge extension."""

        supplements = self._resolve_manual(manual)
        return self.dom_store.prepare(
            draft,
            supplements,
            profile_version=profile_version,
            include_complex=include_complex,
            allow_overwrite=allow_overwrite,
        )

    def dom_task_status(self) -> dict[str, object]:
        return self.dom_store.status()

    def cancel_dom_task(self) -> dict[str, object] | None:
        status = self.dom_store.status()
        task = status.get("task")
        if not isinstance(task, Mapping) or not task.get("task_id"):
            return None
        return self.dom_store.cancel(
            {"task_id": task["task_id"], "reason_code": "desktop_stop"}
        )

    def run_simulation(
        self,
        draft: CertificateDraft,
        manual: Mapping[str, object] | ManualFields | None = None,
        *,
        diagnostics: str | Path | None = None,
        source_file: str | None = None,
    ) -> M7RunReport:
        supplements = self._resolve_manual(manual)
        profile = load_profile(self.simulation_profile_path)
        report = M4Workflow(
            profile,
            stop_requested=self.stop_requested,
        ).run(draft, supplements, diagnostics=diagnostics)
        return self._wrap(
            M7Mode.SIMULATION,
            "InMemoryPageAdapter (offline simulation)",
            report,
            report.phase,
            source_file or draft.source_file,
        )

    def run_auto_update(
        self,
        draft: CertificateDraft,
        manual: Mapping[str, object] | ManualFields | None = None,
        *,
        diagnostics: str | Path | None = None,
        adapter: PageAdapter | None = None,
        progress: Callable[[str, int, int, str], None] | None = None,
        trace_callback: TraceCallback | None = None,
    ) -> M7RunReport:
        """Fill one bound page and stop before save, with per-action readback.

        ``adapter`` is injectable for repeatable integration tests.  Normal
        product use creates the profile-driven real-screen adapter and always
        requires a bound foreground browser window.
        """

        supplements = self._resolve_manual(manual)
        profile = self._load_bound_profile() if adapter is None else load_profile(self.simulation_profile_path)
        report_binding = self.binding
        executor = "M4Workflow + injected verified PageAdapter"
        trace: list[dict[str, object]] = []
        screen_adapter: ScreenPageAdapter | None = None
        verify_attachments = True
        diagnostics_target = diagnostics
        if diagnostics_target is None and adapter is None:
            diagnostics_target = (
                Path(".m6")
                / "diagnostics"
                / "basic-info"
                / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            )
        live_path = _live_log_path(diagnostics_target)
        if live_path is not None:
            try:
                _write_live_log_header(live_path)
            except OSError:
                live_path = None

        def emit_trace(event: dict[str, object]) -> None:
            if live_path is not None:
                try:
                    _write_live_log_event(live_path, event)
                except OSError:
                    pass
            if trace_callback is not None:
                try:
                    trace_callback(event)
                except Exception:
                    pass

        _append_trace(
            trace,
            "m7.run",
            "started",
            trace_callback=emit_trace,
            mode=M7Mode.AUTO_UPDATE.value,
            live_log=str(live_path) if live_path is not None else None,
        )
        if progress is not None:
            progress("preflight", 0, 0, "正在检查证书和绑定页面")
        try:
            if adapter is None:
                if self.binding is None:
                    raise M7Error("请先绑定当前专利信息网页，再开始更新信息。")
                required_controls = self._auto_required_controls(supplements)
                verify_attachments = profile.id != "cnipa_intranet"
                if not verify_attachments:
                    required_controls.discard("attachments")
                    _append_trace(
                        trace,
                        "preflight.optional_attachments",
                        "skipped",
                        trace_callback=emit_trace,
                        reason="mock_page_attachment_anchor_not_calibrated",
                    )
                _append_trace(
                    trace,
                    "preflight.plan_input_scope",
                    "completed",
                    trace_callback=emit_trace,
                    required_controls=len(required_controls),
                )
                screen_adapter = ScreenPageAdapter(
                    profile,
                    self.binder,
                    self.binding,
                    required_controls=required_controls,
                    expected_tables={
                        "rights_holder_rows": draft.current_patentees,
                        "inventor_rows": draft.inventors,
                    },
                    stop_requested=self.stop_requested,
                    trace=trace,
                    trace_callback=emit_trace,
                    progress=progress,
                )
                executor = "M4Workflow + M7 ScreenPageAdapter (real foreground screen)"
                if profile.id == "cnipa_intranet":
                    # The mock page is a long single document.  A full
                    # preflight would OCR every section before the first
                    # action; basic fields are enough to build the guarded
                    # plan, while later actions re-check their own section.
                    sections = ["basic_info"]
                    _append_trace(
                        trace,
                        "preflight.scope",
                        "limited",
                        trace_callback=emit_trace,
                        reason="mock_basic_first",
                    )
                else:
                    sections = [
                        section
                        for section in ScreenPageAdapter.SECTION_ORDER
                        if section in {profile.controls_by_id[item].section for item in required_controls}
                    ]
                _append_trace(
                    trace,
                    "preflight.sections",
                    "started",
                    trace_callback=emit_trace,
                    sections=sections,
                )
                screen_adapter.scan_sections(sections)
                _append_trace(
                    trace,
                    "preflight.sections",
                    "completed",
                    trace_callback=emit_trace,
                    sections=sections,
                )
                adapter = screen_adapter

            def on_action(index: int, total: int, action: Action) -> None:
                _append_trace(
                    trace,
                    "automation.action_plan",
                    "started",
                    trace_callback=emit_trace,
                    index=index,
                    total=total,
                    control_id=action.control_id,
                    kind=action.kind,
                    value={"has_value": bool(action.value), "length": len(action.value or "")},
                )
                if progress is not None:
                    progress("automation", index, total, f"正在处理：{action.control_id}")

            payload = M4Workflow(
                profile,
                binder=self.binder,
                binding=report_binding,
                stop_requested=self.stop_requested,
                focus_ok=(
                    (lambda: self.binder.is_foreground(self.binding))
                    if self.binding is not None and isinstance(adapter, ScreenPageAdapter)
                    else (lambda: True)
                ),
                on_action=on_action,
                verify_attachments=verify_attachments,
            ).run(draft, supplements, adapter=adapter, diagnostics=diagnostics_target)
            payload.trace = trace
            _append_trace(
                trace,
                "m7.run",
                "completed",
                trace_callback=emit_trace,
                run_status=payload.status,
                phase=payload.phase,
            )
        except (M7Error, AutomationError, OSError, ValueError) as error:
            if screen_adapter is not None:
                screen_adapter.trace_event("m7.run", "failed", error=str(error))
            else:
                _append_trace(trace, "m7.run", "failed", trace_callback=emit_trace, error=str(error))
            payload = M4Report(
                "blocked",
                "preflight",
                draft.source_file,
                draft.sample_index,
                review_draft(draft),
                binding=report_binding,
                reason=str(error),
                trace=trace,
            )
            if diagnostics_target is not None:
                payload.diagnostics_path = write_diagnostics(diagnostics_target, payload)
                if screen_adapter is not None:
                    self._write_screen_evidence(screen_adapter, diagnostics_target)
        if progress is not None:
            progress("finished", 1, 1, payload.reason or payload.status)
        return self._wrap(M7Mode.AUTO_UPDATE, executor, payload, payload.phase, draft.source_file)

    BASIC_PROBE_CONTROLS = frozenset(
        {
            "patent_no",
            "application_title",
            "patent_type",
            "application_date",
            "grant_date",
            "joint_application",
        }
    )

    def run_basic_probe(
        self,
        control_id: str,
        value: str,
        *,
        diagnostics: str | Path | None = None,
    ) -> M7RunReport:
        """Read or execute exactly one approved basic-information control."""

        if self.binding is None:
            raise M7Error("请先绑定当前专利信息网页。")
        if control_id not in self.BASIC_PROBE_CONTROLS:
            raise M7Error(f"基本信息逐项测试不允许控件：{control_id}")
        profile = self._load_bound_profile()
        control = profile.controls_by_id.get(control_id)
        if control is None:
            raise M7Error(f"当前 Profile 尚未配置控件：{control_id}")
        profile_issues = auto_update_profile_issues(profile, {control_id})
        if profile_issues:
            payload = ControlProbeReport(
                control_id,
                "blocked",
                profile.id,
                profile.version,
                stop_reason="当前控件 Profile 尚未完成校准（"
                + ", ".join(profile_issues)
                + "）；请先运行只识别定位并更新 Profile。",
            )
            self._write_probe_report(payload, diagnostics)
            return M7RunReport(
                M7Mode.STEP.value,
                "blocked",
                "basic_info_probe",
                "M7 basic control probe",
                payload,
                self.binding.title,
                [M7SafetyReason("profile_calibration_required", payload.stop_reason or "profile_calibration_required", "basic_info_probe")],
                False,
                True,
            )

        return self._run_basic_probe_action(control_id, value, profile, control, diagnostics)

    def inspect_basic_control(
        self,
        control_id: str,
        *,
        diagnostics: str | Path | None = None,
    ) -> M7RunReport:
        """Capture one approved basic control without sending input."""

        if self.binding is None:
            raise M7Error("请先绑定当前专利信息网页。")
        if control_id not in self.BASIC_PROBE_CONTROLS:
            raise M7Error(f"基本信息逐项测试不允许控件：{control_id}")
        profile = self._load_bound_profile()
        control = profile.controls_by_id.get(control_id)
        if control is None:
            raise M7Error(f"当前 Profile 尚未配置控件：{control_id}")
        profile_issues = auto_update_profile_issues(profile, {control_id})
        if profile_issues:
            payload = ControlProbeReport(
                control_id,
                "blocked",
                profile.id,
                profile.version,
                stop_reason="当前控件 Profile 尚未完成校准（"
                + ", ".join(profile_issues)
                + "）；只识别定位不会读取或猜测其值。",
            )
            self._write_probe_report(payload, diagnostics)
            return M7RunReport(
                M7Mode.RECOGNITION_ONLY.value,
                "blocked",
                "basic_info_probe",
                "M7 basic control probe",
                payload,
                self.binding.title,
                [M7SafetyReason("profile_calibration_required", payload.stop_reason, "basic_info_probe")],
                False,
                True,
            )
        trace: list[dict[str, object]] = []
        adapter = ScreenPageAdapter(
            profile,
            self.binder,
            self.binding,
            required_controls={control_id},
            stop_requested=self.stop_requested,
            trace=trace,
        )
        before_image = before_result = None
        try:
            before = adapter.observe()
            before_image, before_result = adapter.capture_evidence(require_ready=False)
            payload = ControlProbeReport(
                control_id,
                "recognized",
                profile.id,
                profile.version,
                before=before.to_dict(),
                action_sent=False,
                verified=True,
                trace=trace,
            )
            self._write_probe_report(payload, diagnostics, before_image, before_result)
            return M7RunReport(
                M7Mode.RECOGNITION_ONLY.value,
                "recognized",
                "basic_info_probe",
                "M7 basic control probe",
                payload,
                self.binding.title,
                [],
                False,
                True,
            )
        except (AutomationError, OSError, ValueError) as error:
            payload = ControlProbeReport(
                control_id,
                "failed",
                profile.id,
                profile.version,
                before=before.to_dict() if "before" in locals() else None,
                action_sent=False,
                verified=False,
                stop_reason=str(error),
                trace=trace,
            )
            self._write_probe_report(payload, diagnostics, before_image, before_result)
            return M7RunReport(
                M7Mode.RECOGNITION_ONLY.value,
                "failed",
                "basic_info_probe",
                "M7 basic control probe",
                payload,
                self.binding.title,
                [M7SafetyReason("probe_failed", str(error), "basic_info_probe")],
                False,
                True,
            )

    def _run_basic_probe_action(
        self,
        control_id: str,
        value: str,
        profile,
        control,
        diagnostics: str | Path | None,
    ) -> M7RunReport:
        if self.binding is None:
            raise M7Error("请先绑定当前专利信息网页。")

        if control_id == "joint_application":
            normalized = value.casefold().strip()
            action = Action(control_id, "check" if normalized in {"true", "1", "yes", "是"} else "uncheck", value)
        elif control_id == "patent_type":
            action = Action(control_id, "select", value)
        else:
            action = Action(control_id, "date" if control.kind == "date" else "fill", value)

        trace: list[dict[str, object]] = []
        adapter = ScreenPageAdapter(
            profile,
            self.binder,
            self.binding,
            required_controls={control_id},
            stop_requested=self.stop_requested,
            trace=trace,
        )
        before_snapshot = None
        after_snapshot = None
        before_image = None
        after_image = None
        before_result = None
        after_result = None
        try:
            before_snapshot = adapter.observe()
            before_image, before_result = adapter.capture_evidence(require_ready=False)
            automation = AutomationEngine(
                profile,
                adapter,
                stop_requested=self.stop_requested,
                focus_ok=lambda: self.binder.is_foreground(self.binding),
            ).run([action])
            try:
                after_snapshot = adapter.observe()
            except Exception as error:
                trace.append({"step": "probe.after_observe", "status": "failed", "error": str(error)})
            try:
                after_image, after_result = adapter.capture_evidence(require_ready=False)
            except Exception as error:
                trace.append({"step": "probe.after_capture", "status": "failed", "error": str(error)})
            action_sent = any(
                item.get("step") == "action.input" and item.get("status") == "sent"
                for item in trace
            )
            payload = ControlProbeReport(
                control_id,
                automation.status,
                profile.id,
                profile.version,
                action.kind,
                action.value,
                before_snapshot.to_dict() if before_snapshot else None,
                after_snapshot.to_dict() if after_snapshot else None,
                action_sent,
                automation.verified,
                automation.reason,
                trace=trace,
            )
            self._write_probe_report(payload, diagnostics, before_image, before_result, after_image, after_result)
            return M7RunReport(
                M7Mode.STEP.value,
                payload.status,
                "basic_info_probe",
                "M7 basic control probe",
                payload,
                self.binding.title,
                _safety_reasons(payload, "basic_info_probe"),
                False,
                True,
            )
        except (AutomationError, OSError, ValueError) as error:
            payload = ControlProbeReport(
                control_id,
                "failed",
                profile.id,
                profile.version,
                action.kind,
                action.value,
                before_snapshot.to_dict() if before_snapshot else None,
                after_snapshot.to_dict() if after_snapshot else None,
                any(item.get("step") == "action.input" and item.get("status") == "sent" for item in trace),
                False,
                str(error),
                trace=trace,
            )
            self._write_probe_report(payload, diagnostics, before_image, before_result, after_image, after_result)
            return M7RunReport(
                M7Mode.STEP.value,
                "failed",
                "basic_info_probe",
                "M7 basic control probe",
                payload,
                self.binding.title,
                [M7SafetyReason("probe_failed", str(error), "basic_info_probe")],
                False,
                True,
            )

    def run_recognition_only(
        self,
        *,
        image: str | Path | None = None,
        annotated: str | Path | None = None,
        report_path: str | Path | None = None,
        diagnostics: str | Path | None = None,
    ) -> M7RunReport:
        runner, executor = self._field_runner(image=image, diagnostics=diagnostics)
        report = runner.recognize_only(annotated=annotated, report_path=report_path)
        return self._wrap(M7Mode.RECOGNITION_ONLY, executor, report, "recognition", None)

    def run_step(
        self,
        actions: Sequence[Action],
        *,
        image: str | Path | None = None,
        confirm: Callable[[Action], bool] | None = None,
        report_path: str | Path | None = None,
        diagnostics: str | Path | None = None,
    ) -> M7RunReport:
        if not actions:
            raise M7Error("单步模式至少需要一个动作；保存、提交、返回和删除不能加入动作文件。")
        runner, executor = self._field_runner(image=image, diagnostics=diagnostics)
        report = runner.run_step(actions, confirm=confirm, report_path=report_path)
        return self._wrap(M7Mode.STEP, executor, report, "field_step", None)

    def enqueue(self, sources: Sequence[str | Path], *, max_attempts: int = 2) -> list[QueueTask]:
        queue = TaskQueue(self.queue_path)
        return queue.enqueue_many(sources, max_attempts=max_attempts)

    def install_profile(self, profile_path: str | Path, *, activate: bool = False) -> str:
        return self.profile_registry.install(profile_path, activate=activate)

    def rollback_profile(self, version: str | None = None) -> str:
        return self.profile_registry.rollback(version).version

    def save_manual_config(
        self,
        version: str,
        values: Mapping[str, object] | ManualFields,
        *,
        activate: bool = False,
    ) -> Path:
        data = values.to_dict() if isinstance(values, ManualFields) else dict(values)
        return self.config_store.save(version, data, activate=activate)

    def rollback_manual_config(self, version: str | None = None) -> Mapping[str, Any]:
        return self.config_store.rollback(version)

    def configuration_snapshot(self) -> dict[str, Any]:
        return {
            "profile": {
                "active_version": self.profile_registry.active_version,
                "versions": self.profile_registry.versions(),
            },
            "manual_fields": {
                "active_version": self.config_store.active_version,
                "versions": self.config_store.versions(),
            },
        }

    def queue_snapshot(self) -> list[dict[str, Any]]:
        return [task.to_dict() for task in TaskQueue(self.queue_path).tasks]

    def recover_queue(self) -> int:
        return TaskQueue(self.queue_path).recover_orphaned()

    def retry_task(self, task_id: str, *, force: bool = False) -> QueueTask:
        return TaskQueue(self.queue_path).retry(task_id, force=force)

    def pause_task(self, task_id: str, reason: str = "用户请求暂停，等待人工检查。") -> QueueTask:
        queue = TaskQueue(self.queue_path)
        return queue.pause(task_id, error_code="user_paused", reason=reason)

    def run_controlled_batch(
        self,
        manual: Mapping[str, object] | ManualFields | None = None,
        *,
        diagnostics: str | Path | None = None,
        max_retries: int = 1,
        limit: int | None = None,
    ) -> M7RunReport:
        queue = TaskQueue(self.queue_path)
        profile = load_profile(self.simulation_profile_path)
        report = run_m4_queue(
            queue,
            profile,
            supplements=self._resolve_manual(manual).to_dict(),
            diagnostics_dir=diagnostics,
            max_retries=max_retries,
            stop_on_failure=True,
            limit=limit,
            stop_requested=self.stop_requested,
        )
        return self._wrap(
            M7Mode.CONTROLLED_BATCH,
            "InMemoryPageAdapter (M6 controlled offline batch)",
            report,
            "queue",
            None,
        )

    def _field_runner(
        self,
        *,
        image: str | Path | None,
        diagnostics: str | Path | None,
    ) -> tuple[M5FieldRunner, str]:
        profile = self._load_field_profile() if image is not None else self._load_bound_profile()
        if image is not None and self.binding is not None:
            raise M7Error("现场模式不能同时使用截图和已绑定浏览器窗口。")
        if image is None and self.binding is None:
            raise M7Error("真实网页模式必须先绑定 Edge/Chrome 窗口，或明确提供现场截图做只识别。")
        if image is not None:
            capture = FileCapture(image)
            executor = "M5FieldRunner + FileCapture (offline screenshot evidence)"
            binder = None
        else:
            capture = BoundWindowCapture(self.binder, self.binding)  # type: ignore[arg-type]
            executor = "M5FieldRunner + foreground WindowBinder (real screen)"
            binder = self.binder
        return (
            M5FieldRunner(
                profile,
                capture,
                templates=self.templates_path,
                binder=binder,
                binding=self.binding,
                diagnostics_dir=diagnostics,
                stop_requested=self.stop_requested,
            ),
            executor,
        )

    def _load_field_profile(self):
        if self.profile_registry.active_version:
            return self.profile_registry.load()
        return load_profile(self.field_profile_path)

    def _load_bound_profile(self):
        if self.binding is not None and "M2 离线仿真页" in self.binding.title:
            return load_profile(DEFAULT_MOCK_SCREEN_PROFILE)
        return self._load_field_profile()

    def can_auto_update_bound(self, manual: Mapping[str, object] | ManualFields | None = None) -> bool:
        """Return whether the bound profile has every required editable control."""

        if self.binding is None:
            return False
        profile = self._load_bound_profile()
        required = self._auto_required_controls(self._resolve_manual(manual))
        return not auto_update_profile_issues(profile, required)

    @staticmethod
    def _write_screen_evidence(adapter: ScreenPageAdapter, diagnostics: str | Path) -> None:
        image = adapter.last_image
        result = adapter.last_result
        if image is None:
            return
        target = Path(diagnostics)
        target.mkdir(parents=True, exist_ok=True)
        image.save(target / "before.png")
        if result is None:
            return
        (target / "before_detect.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        annotate_image(image, result).save(target / "before_annotated.png")

    @staticmethod
    def _write_probe_report(
        payload: ControlProbeReport,
        diagnostics: str | Path | None,
        before_image=None,
        before_result=None,
        after_image=None,
        after_result=None,
    ) -> None:
        target = Path(diagnostics) if diagnostics else Path(".m6") / "diagnostics" / "basic-info" / (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f") + f"-{payload.control_id}"
        )
        target.mkdir(parents=True, exist_ok=True)
        payload.diagnostics_path = str(target)
        (target / "report.json").write_text(
            json.dumps(payload.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for name, image, result in (
            ("before", before_image, before_result),
            ("after", after_image, after_result),
        ):
            if image is None:
                continue
            image.save(target / f"{name}.png")
            if result is not None:
                (target / f"{name}_detect.json").write_text(
                    json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                annotate_image(image, result).save(target / f"{name}_annotated.png")

    def _resolve_manual(self, value: Mapping[str, object] | ManualFields | None) -> ManualFields:
        if value is not None:
            return _manual_values(value)
        if self.config_store.active_version:
            return ManualFields.from_mapping(self.config_store.load())
        return ManualFields({})

    @staticmethod
    def _auto_required_controls(manual: ManualFields) -> set[str]:
        controls = {
            "patent_no",
            "application_title",
            "patent_type",
            "application_date",
            "grant_date",
            "rights_holder_rows",
            "inventor_rows",
            "first_inventor_select",
            "patentee_merge",
            "inventor_merge",
            "attachments",
        }
        controls.update(name for name in manual.values if name != "attachment_name")
        return controls

    def _wrap(
        self,
        mode: M7Mode,
        executor: str,
        payload: M4Report | M5RunReport | M6BatchReport | ControlProbeReport,
        phase: str,
        source_file: str | None,
    ) -> M7RunReport:
        data = _payload_dict(payload)
        return M7RunReport(
            mode.value,
            str(data.get("status", "unknown")),
            phase,
            executor,
            payload,
            source_file,
            _safety_reasons(payload, phase),
            bool(data.get("save_attempted", False)),
            mode in {M7Mode.RECOGNITION_ONLY, M7Mode.STEP},
        )


__all__ = [
    "DEFAULT_FIELD_PROFILE",
    "DEFAULT_GOLDEN",
    "DEFAULT_MOCK_SCREEN_PROFILE",
    "DEFAULT_QUEUE",
    "DEFAULT_SIMULATION_PROFILE",
    "DEFAULT_TEMPLATES",
    "M7Error",
    "M7Mode",
    "M7RunReport",
    "M7SafetyReason",
    "M7Service",
    "ControlProbeReport",
    "load_workflow_sources",
]
