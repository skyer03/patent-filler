"""M5 field-adaptation runner.

The field runner is deliberately screenshot based.  It provides two safe
entry points:

* recognition-only: capture one current page and export boxes, confidence and
  template matches without sending input;
* step: recapture before every action, check the bound foreground window and
  page state, optionally ask for confirmation, send one screen action, and
  recapture it for a safety/read-back record.

It does not use DOM selectors and it never executes save, return, submit or
delete actions.  Business values entered on a real page still require manual
read-back; OCR proving that the page is still safe is not treated as proof of
the field value.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from PIL import Image

from .automation import (
    Action,
    AnchorRecognizer,
    RecognitionResult,
    ScreenActionExecutor,
    TemplateMatcher,
    WindowBinder,
    WindowBinding,
    load_profile,
)
from .automation.recognizer import TemplateMatch, annotate_image
from .automation.window import WindowBindingError
from .automation.engine import AutomationError
from .automation.profile import PageProfile


class M5Error(RuntimeError):
    """Raised when a field run cannot be started safely."""


class ImageCapture(Protocol):
    def capture(self) -> Image.Image:
        ...


class FileCapture:
    """Repeatable screenshot source used for offline review and tests."""

    def __init__(self, image_path: str | Path) -> None:
        self.image_path = Path(image_path)

    def capture(self) -> Image.Image:
        try:
            return Image.open(self.image_path).convert("RGB")
        except (OSError, ValueError) as error:
            raise M5Error(f"无法读取现场截图：{self.image_path}") from error


class BoundWindowCapture:
    """Capture the currently bound browser window on Windows."""

    def __init__(self, binder: WindowBinder, binding: WindowBinding) -> None:
        self.binder = binder
        self.binding = binding

    def capture(self) -> Image.Image:
        try:
            return self.binder.capture(self.binding).convert("RGB")
        except WindowBindingError as error:
            raise M5Error(str(error)) from error


@dataclass(frozen=True)
class RecognitionSnapshot:
    result: RecognitionResult
    template_matches: tuple[TemplateMatch, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "recognition": self.result.to_dict(),
            "template_matches": [
                {
                    "name": match.name,
                    "box": match.box.to_dict(),
                    "score": match.score,
                }
                for match in self.template_matches
            ],
        }


@dataclass(frozen=True)
class M5StepRecord:
    """A deliberately conservative, exportable field step record."""

    sequence: int
    action: Action
    status: str
    before: RecognitionSnapshot | None = None
    after: RecognitionSnapshot | None = None
    error_code: str | None = None
    message: str | None = None
    manual_readback_required: bool = True
    before_image: str | None = None
    after_image: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "action": {
                "control_id": self.action.control_id,
                "kind": self.action.kind,
                "value": _redact_action_value(self.action),
            },
            "status": self.status,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "error_code": self.error_code,
            "message": self.message,
            "manual_readback_required": self.manual_readback_required,
            "before_image": self.before_image,
            "after_image": self.after_image,
        }


@dataclass
class M5RunReport:
    mode: str
    status: str
    profile_id: str
    profile_version: str
    started_at: str
    finished_at: str | None = None
    binding: WindowBinding | None = None
    recognition: RecognitionSnapshot | None = None
    annotated_image: str | None = None
    steps: list[M5StepRecord] = field(default_factory=list)
    reason: str | None = None
    input_executed: bool = False
    cross_window_guard: str = "foreground_window_checked_before_capture_and_action"

    @property
    def safe(self) -> bool:
        return self.status in {"recognized", "completed"} and not self.input_executed

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "m5-field-run-v1",
            "mode": self.mode,
            "status": self.status,
            "safe": self.safe,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "binding": self.binding.to_dict() if self.binding else None,
            "recognition": self.recognition.to_dict() if self.recognition else None,
            "annotated_image": self.annotated_image,
            "steps": [step.to_dict() for step in self.steps],
            "reason": self.reason,
            "input_executed": self.input_executed,
            "cross_window_guard": self.cross_window_guard,
        }


SENSITIVE_CONTROL_IDS = frozenset(
    {"first_inventor_id", "first_inventor_contact", "operator_phone", "operator_email"}
)
FORBIDDEN_CONTROL_IDS = frozenset({"save", "return", "submit"})
FORBIDDEN_KINDS = frozenset({"save", "return", "submit", "delete"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_action_value(action: Action) -> str:
    if action.control_id in SENSITIVE_CONTROL_IDS:
        return "[REDACTED]"
    return action.value


def _safe_path_label(path: Path | None) -> str | None:
    return str(path) if path is not None else None


class M5FieldRunner:
    """Run one screenshot-based field session.

    ``capture`` may be a fixed image for offline testing or a bound-window
    capture for a real Edge/Chrome session.  ``executor`` is only needed for
    step mode; recognition-only deliberately never calls it.
    """

    def __init__(
        self,
        profile: PageProfile,
        capture: ImageCapture,
        *,
        templates: str | Path | None = None,
        binder: WindowBinder | None = None,
        binding: WindowBinding | None = None,
        recognizer: AnchorRecognizer | None = None,
        matcher: TemplateMatcher | None = None,
        diagnostics_dir: str | Path | None = None,
        stop_requested: Callable[[], bool] | None = None,
        focus_ok: Callable[[], bool] | None = None,
    ) -> None:
        self.profile = profile
        self.capture_source = capture
        self.templates = Path(templates) if templates else None
        self.binder = binder
        self.binding = binding
        self.recognizer = recognizer or AnchorRecognizer(profile)
        self.matcher = matcher or TemplateMatcher()
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir else None
        self.stop_requested = stop_requested or (lambda: False)
        self.focus_ok = focus_ok or self._default_focus_ok

    def recognize_only(
        self,
        *,
        annotated: str | Path | None = None,
        report_path: str | Path | None = None,
    ) -> M5RunReport:
        started = _now()
        report = M5RunReport(
            "recognition_only",
            "starting",
            self.profile.id,
            self.profile.version,
            started,
            binding=self.binding,
        )
        self._initialize_diagnostics()
        try:
            image = self._capture()
            snapshot = self._recognize(image)
            report.recognition = snapshot
            if annotated is not None:
                target = Path(annotated)
                target.parent.mkdir(parents=True, exist_ok=True)
                annotate_image(image, snapshot.result, snapshot.template_matches).save(target)
                report.annotated_image = _safe_path_label(target)
            report.status = "recognized" if snapshot.result.safe_for_input else "blocked"
            if report.status == "blocked":
                report.reason = "; ".join(snapshot.result.issues or snapshot.result.missing_anchors)
        except (M5Error, OSError, ValueError) as error:
            report.status = "failed"
            report.reason = str(error)
        report.finished_at = _now()
        _write_report(report, report_path)
        return report

    def run_step(
        self,
        actions: Sequence[Action],
        *,
        confirm: Callable[[Action], bool] | None = None,
        executor_factory: Callable[[RecognitionResult], ScreenActionExecutor] | None = None,
        report_path: str | Path | None = None,
    ) -> M5RunReport:
        started = _now()
        report = M5RunReport(
            "step",
            "starting",
            self.profile.id,
            self.profile.version,
            started,
            binding=self.binding,
        )
        self._initialize_diagnostics()
        confirmation = confirm or (lambda _action: False)
        for sequence, action in enumerate(actions, start=1):
            if self.stop_requested():
                report.status = "paused"
                report.reason = "emergency_stop"
                break
            if not self.focus_ok():
                report.status = "paused"
                report.reason = "focus_lost"
                break
            before_image: Image.Image | None = None
            before: RecognitionSnapshot | None = None
            before_path: Path | None = None
            try:
                before_image = self._capture()
                before = self._recognize(before_image)
                before_path = self._save_step_image(sequence, "before", before_image)
                blocked = self._blocked_action(before.result, action)
                if blocked is not None:
                    self._append_step(
                        report,
                        M5StepRecord(
                            sequence,
                            action,
                            "blocked",
                            before,
                            error_code=blocked[0],
                            message=blocked[1],
                            before_image=_safe_path_label(before_path),
                        )
                    )
                    report.status = "blocked"
                    report.reason = blocked[1]
                    break
                if not confirmation(action):
                    self._append_step(
                        report,
                        M5StepRecord(
                            sequence,
                            action,
                            "paused",
                            before,
                            error_code="user_not_confirmed",
                            message="单步动作未获得用户确认。",
                            before_image=_safe_path_label(before_path),
                        )
                    )
                    report.status = "paused"
                    report.reason = "user_not_confirmed"
                    break
                if not self.focus_ok():
                    self._append_step(
                        report,
                        M5StepRecord(
                            sequence,
                            action,
                            "paused",
                            before,
                            error_code="focus_lost",
                            message="确认后目标浏览器失去前台，未发送动作。",
                            before_image=_safe_path_label(before_path),
                        )
                    )
                    report.status = "paused"
                    report.reason = "focus_lost"
                    break
                if executor_factory is not None:
                    executor = executor_factory(before.result)
                else:
                    if self.binding is None:
                        raise M5Error("单步执行需要绑定现场浏览器窗口。")
                    executor = ScreenActionExecutor(self.binding, before.result)
                located = before.result.controls[action.control_id]
                center_x = located.box.left + located.box.width // 2
                center_y = located.box.top + located.box.height // 2
                executor.execute(action, center_x, center_y)
                report.input_executed = True
                after_image = self._capture()
                after = self._recognize(after_image)
                after_path = self._save_step_image(sequence, "after", after_image)
                if not after.result.safe_for_input:
                    self._append_step(
                        report,
                        M5StepRecord(
                            sequence,
                            action,
                            "failed",
                            before,
                            after,
                            error_code="unsafe_after_action",
                            message="动作后页面状态或必需锚点不满足安全条件，已停止。",
                            before_image=_safe_path_label(before_path),
                            after_image=_safe_path_label(after_path),
                        )
                    )
                    report.status = "failed"
                    report.reason = "unsafe_after_action"
                    break
                self._append_step(
                    report,
                    M5StepRecord(
                        sequence,
                        action,
                        "dispatched",
                        before,
                        after,
                        message="动作已发送；字段值仍需人工回读确认。",
                        before_image=_safe_path_label(before_path),
                        after_image=_safe_path_label(after_path),
                    )
                )
            except (M5Error, AutomationError, OSError, ValueError) as error:
                self._append_step(
                    report,
                    M5StepRecord(
                        sequence,
                        action,
                        "failed",
                        before,
                        error_code="action_error",
                        message=str(error),
                        before_image=_safe_path_label(before_path),
                    )
                )
                report.status = "failed"
                report.reason = str(error)
                break
        else:
            report.status = "completed"
            report.reason = "单步序列已结束；保存/提交仍由用户人工完成。"
        report.finished_at = _now()
        _write_report(report, report_path)
        return report

    def _capture(self) -> Image.Image:
        if not self.focus_ok():
            raise M5Error("目标浏览器窗口未保持前台，已暂停。")
        image = self.capture_source.capture()
        if image.width <= 0 or image.height <= 0:
            raise M5Error("截图尺寸无效。")
        return image.convert("RGB")

    def _recognize(self, image: Image.Image) -> RecognitionSnapshot:
        result = self.recognizer.recognize_image(image)
        matches = tuple(self.matcher.locate_directory(image, self.templates)) if self.templates else ()
        return RecognitionSnapshot(result, matches)

    def _save_step_image(self, sequence: int, suffix: str, image: Image.Image) -> Path | None:
        if self.diagnostics_dir is None:
            return None
        steps = self.diagnostics_dir / "steps"
        steps.mkdir(parents=True, exist_ok=True)
        target = steps / f"{sequence:03d}_{suffix}.png"
        image.save(target)
        return target

    def _initialize_diagnostics(self) -> None:
        if self.diagnostics_dir is None:
            return
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format": "m5-diagnostics-v1",
            "created_at": _now(),
            "mode": "field",
            "profile_id": self.profile.id,
            "profile_version": self.profile.version,
            "template_directory": str(self.templates) if self.templates else None,
            "sensitive_values": "not recorded; action values are redacted",
        }
        (self.diagnostics_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        environment = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "os_name": os.name,
            "window_bound": self.binding is not None,
            "window_title": self.binding.title if self.binding else None,
            "window_rect": self.binding.rect.to_dict() if self.binding else None,
        }
        (self.diagnostics_dir / "environment.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (self.diagnostics_dir / "execution.log").write_text("", encoding="utf-8")

    def _append_step(self, report: M5RunReport, record: M5StepRecord) -> None:
        report.steps.append(record)
        if self.diagnostics_dir is None:
            return
        steps = self.diagnostics_dir / "steps"
        steps.mkdir(parents=True, exist_ok=True)
        detect = {
            "sequence": record.sequence,
            "before": record.before.to_dict() if record.before else None,
            "after": record.after.to_dict() if record.after else None,
        }
        (steps / f"{record.sequence:03d}_detect.json").write_text(
            json.dumps(detect, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (steps / f"{record.sequence:03d}_result.json").write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with (self.diagnostics_dir / "execution.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _blocked_action(
        self, recognition: RecognitionResult, action: Action
    ) -> tuple[str, str] | None:
        kind = action.kind.casefold().strip()
        if kind in FORBIDDEN_KINDS or action.control_id.casefold().strip() in FORBIDDEN_CONTROL_IDS:
            return "destructive_action", "保存、返回、提交和删除动作必须由用户人工确认。"
        if "delete" in kind or "delete" in action.control_id.casefold() or "删除" in kind:
            return "destructive_action", "动态表格删除动作被 M5 安全策略禁止。"
        if not recognition.safe_for_input:
            return "unsafe_recognition", "页面锚点、页面状态或识别结果不满足安全条件。"
        control = self.profile.controls_by_id.get(action.control_id)
        if control is None:
            return "unknown_control", f"profile 中没有控件：{action.control_id}"
        if control.destructive or not control.editable:
            return "unsafe_control", f"控件不允许自动操作：{action.control_id}"
        if control.required_state == "editing" and recognition.edit_state != "editing":
            return "edit_state_required", f"控件不在编辑态：{action.control_id}"
        if action.control_id not in recognition.controls:
            return "control_not_found", f"控件未在当前截图中识别：{action.control_id}"
        return None

    def _default_focus_ok(self) -> bool:
        if self.binding is None or self.binder is None or os.name != "nt":
            return True
        return self.binder.is_foreground(self.binding)


def load_actions(path: str | Path) -> list[Action]:
    """Load a small, explicit action list for a step run."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_actions = data.get("actions", data) if isinstance(data, dict) else data
    if not isinstance(raw_actions, list):
        raise M5Error("单步动作文件必须是数组，或包含 actions 数组的 JSON 对象。")
    actions: list[Action] = []
    for item in raw_actions:
        if not isinstance(item, dict) or "control_id" not in item or "kind" not in item:
            raise M5Error(f"单步动作格式无效：{item!r}")
        actions.append(Action(str(item["control_id"]), str(item["kind"]), str(item.get("value", ""))))
    return actions


def recognition_from_image(
    image_path: str | Path,
    profile_path: str | Path,
    *,
    templates: str | Path | None = None,
    annotated: str | Path | None = None,
    report_path: str | Path | None = None,
) -> M5RunReport:
    profile = load_profile(profile_path)
    return M5FieldRunner(
        profile,
        FileCapture(image_path),
        templates=templates,
    ).recognize_only(annotated=annotated, report_path=report_path)


def _write_report(report: M5RunReport, path: str | Path | None) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "BoundWindowCapture",
    "FileCapture",
    "M5Error",
    "M5FieldRunner",
    "M5RunReport",
    "M5StepRecord",
    "RecognitionSnapshot",
    "load_actions",
    "recognition_from_image",
]
