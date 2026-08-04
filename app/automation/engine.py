"""M3 black-box automation engine and screen-input proof of concept.

The engine deliberately knows nothing about HTML or browser DOM.  A
``PageAdapter`` exposes only observations and visible user actions.  This
keeps the same state machine usable with the offline mock page and with a
future OCR-backed Edge adapter.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Protocol, Sequence

from .modes import Action
from .profile import PageProfile
from .recognizer import BoundingBox, RecognitionResult
from .window import WindowBinding, WindowBinder


class AutomationError(RuntimeError):
    """Base error for an action that cannot be safely completed."""


class StopRequested(AutomationError):
    """Raised when the global emergency stop is activated."""


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass(frozen=True)
class AttachmentSnapshot:
    name: str
    can_preview: bool
    can_download: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "can_preview": self.can_preview,
            "can_download": self.can_download,
        }


@dataclass(frozen=True)
class PageSnapshot:
    """The small, non-DOM observation required by the M3 state machine."""

    page_state: str = "unknown"
    edit_state: str = "unknown"
    values: Mapping[str, str] = field(default_factory=dict)
    selected_options: Mapping[str, str] = field(default_factory=dict)
    checked: Mapping[str, bool] = field(default_factory=dict)
    tables: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    selected_person: str | None = None
    person_candidates: tuple[str, ...] = ()
    visible_anchor: str | None = None
    attachments: tuple[AttachmentSnapshot, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "page_state": self.page_state,
            "edit_state": self.edit_state,
            "values": dict(self.values),
            "selected_options": dict(self.selected_options),
            "checked": dict(self.checked),
            "tables": {key: list(value) for key, value in self.tables.items()},
            "selected_person": self.selected_person,
            "person_candidates": list(self.person_candidates),
            "visible_anchor": self.visible_anchor,
            "attachments": [item.to_dict() for item in self.attachments],
        }


class PageAdapter(Protocol):
    """Actions available to a black-box page driver.

    Implementations may use screenshots and coordinates, but must not use DOM
    selectors.  Every mutating method is followed by ``observe`` by the
    engine.
    """

    def observe(self) -> PageSnapshot:
        ...

    def click(self, control_id: str) -> None:
        ...

    def fill(self, control_id: str, value: str) -> None:
        ...

    def select(self, control_id: str, visible_text: str) -> None:
        ...

    def set_checked(self, control_id: str, checked: bool) -> None:
        ...

    def add_row(self, table_id: str, value: str) -> None:
        ...

    def search_person(self, query: str) -> None:
        ...

    def choose_person(self, name: str) -> None:
        ...

    def scroll_to(self, anchor_id: str) -> None:
        ...


@dataclass
class StepResult:
    action: Action
    status: VerificationStatus
    attempts: int
    before: PageSnapshot | None = None
    after: PageSnapshot | None = None
    error_code: str | None = None
    message: str | None = None

    @property
    def verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    def to_dict(self) -> dict[str, object]:
        return {
            "control_id": self.action.control_id,
            "kind": self.action.kind,
            "value": self.action.value,
            "status": self.status.value,
            "attempts": self.attempts,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass
class AutomationReport:
    status: str
    steps: list[StepResult] = field(default_factory=list)
    reason: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "completed" and all(step.verified for step in self.steps)

    @property
    def executed(self) -> list[str]:
        return [step.action.control_id for step in self.steps if step.verified]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "verified": self.verified,
            "executed": self.executed,
            "steps": [step.to_dict() for step in self.steps],
        }


class AutomationEngine:
    """Run safe, verified actions against a fresh page observation."""

    FORBIDDEN_IDS = frozenset({"save", "return"})
    FORBIDDEN_KINDS = frozenset({"delete", "save", "return", "submit"})

    def __init__(
        self,
        profile: PageProfile,
        adapter: PageAdapter,
        *,
        max_retries: int = 1,
        stop_requested: Callable[[], bool] | None = None,
        focus_ok: Callable[[], bool] | None = None,
        before_action: Callable[[Action], None] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数")
        self.profile = profile
        self.adapter = adapter
        self.max_retries = max_retries
        self.stop_requested = stop_requested or (lambda: False)
        self.focus_ok = focus_ok or (lambda: True)
        self.before_action = before_action

    def run(self, actions: Sequence[Action]) -> AutomationReport:
        report = AutomationReport("ready")
        for action in actions:
            if self.stop_requested():
                report.status = "paused"
                report.reason = "emergency_stop"
                break
            if not self.focus_ok():
                report.status = "paused"
                report.reason = "focus_lost"
                break

            step = self._run_step(action)
            report.steps.append(step)
            if not step.verified:
                report.status = "paused" if step.status is VerificationStatus.PAUSED else step.status.value
                report.reason = step.error_code or step.message
                break

        if report.status == "ready":
            report.status = "completed"
        return report

    def _run_step(self, action: Action) -> StepResult:
        blocked = self._blocked_reason(action)
        if blocked is not None:
            return StepResult(
                action,
                VerificationStatus.BLOCKED,
                attempts=0,
                error_code=blocked[0],
                message=blocked[1],
            )

        attempts = 0
        before: PageSnapshot | None = None
        after: PageSnapshot | None = None
        last_message: str | None = None
        last_code: str | None = None
        retryable = self._retryable(action)
        attempt_limit = self.max_retries + 1 if retryable else 1
        for attempts in range(1, attempt_limit + 1):
            try:
                if self.stop_requested():
                    raise StopRequested("emergency_stop")
                if not self.focus_ok():
                    return StepResult(
                        action,
                        VerificationStatus.PAUSED,
                        attempts - 1,
                        before,
                        after,
                        "focus_lost",
                        "目标窗口失焦，已暂停。",
                    )
                before = self.adapter.observe()
                if self.before_action is not None:
                    self.before_action(action)
                if before.page_state != "ready":
                    return StepResult(
                        action,
                        VerificationStatus.BLOCKED,
                        attempts - 1,
                        before,
                        error_code="page_not_ready",
                        message=f"页面状态不是 ready：{before.page_state}。",
                    )
                control = self.profile.controls_by_id.get(action.control_id)
                if control is not None and control.required_state == "editing" and before.edit_state != "editing":
                    return StepResult(
                        action,
                        VerificationStatus.BLOCKED,
                        attempts - 1,
                        before,
                        error_code="edit_state_required",
                        message=f"控件不在编辑态：{action.control_id}。",
                    )
                self._execute(action, before)
                after = self.adapter.observe()
                ok, code, message = self._verify(action, before, after)
                if ok:
                    return StepResult(action, VerificationStatus.VERIFIED, attempts, before, after)
                last_code, last_message = code, message
            except StopRequested:
                return StepResult(
                    action,
                    VerificationStatus.PAUSED,
                    max(0, attempts - 1),
                    before,
                    after,
                    "emergency_stop",
                    "已触发全局急停。",
                )
            except AutomationError as error:
                last_code, last_message = "action_error", str(error)
            except Exception as error:  # adapters are external side effects
                last_code, last_message = "adapter_error", str(error)

        return StepResult(
            action,
            VerificationStatus.FAILED,
            attempts,
            before,
            after,
            last_code or "verification_failed",
            last_message or "动作完成后未通过回读验证。",
        )

    @staticmethod
    def _retryable(action: Action) -> bool:
        # Toggling edit mode or appending a row is not idempotent.  Retrying
        # either can undo a successful action or create a duplicate row.
        return action.kind.casefold().strip() in {
            "fill",
            "text",
            "date",
            "select",
            "dropdown",
            "check",
            "checkbox",
            "uncheck",
            "uncheckbox",
            "scroll",
            "scroll_to",
        }

    def _blocked_reason(self, action: Action) -> tuple[str, str] | None:
        kind = action.kind.casefold().strip()
        control_id = action.control_id.casefold().strip()
        if kind in self.FORBIDDEN_KINDS or control_id in self.FORBIDDEN_IDS:
            return "destructive_action", "保存、返回、提交和删除动作必须由用户人工确认。"
        if "delete" in kind or "delete" in control_id or "删除" in kind or "删除" in control_id:
            return "destructive_action", "动态表格删除动作被 M3 安全策略禁止。"
        if control_id not in self.profile.controls_by_id and not (
            kind == "scroll" and control_id in self.profile.anchors_by_id
        ):
            return "unknown_control", f"profile 中没有控件或滚动锚点：{action.control_id}"
        control = self.profile.controls_by_id.get(control_id)
        read_only_verification = kind in {"verify_attachment", "verify_attachments"}
        if kind in {"scroll", "scroll_to"} and control_id in self.profile.anchors_by_id:
            return None
        if control is not None and not read_only_verification and (control.destructive or not control.editable):
            return "unsafe_control", f"控件不允许自动操作：{action.control_id}"
        return None

    def _execute(self, action: Action, before: PageSnapshot) -> None:
        kind = action.kind.casefold().strip()
        control = self.profile.controls_by_id.get(action.control_id)
        if control is not None and control.required_state == "editing" and before.edit_state != "editing":
            raise AutomationError(f"控件不在编辑态：{action.control_id}")
        if kind in {"click", "edit", "open_person"}:
            self.adapter.click(action.control_id)
            return
        if kind in {"fill", "text", "date"}:
            self.adapter.fill(action.control_id, action.value)
            return
        if kind in {"select", "dropdown"}:
            self.adapter.select(action.control_id, action.value)
            return
        if kind in {"check", "checkbox"}:
            if not before.checked.get(action.control_id, False):
                self.adapter.set_checked(action.control_id, True)
            return
        if kind in {"uncheck", "uncheckbox"}:
            if before.checked.get(action.control_id, False):
                self.adapter.set_checked(action.control_id, False)
            return
        if kind in {"add_row", "add_table_row"}:
            self.adapter.add_row(action.control_id, action.value)
            return
        if kind in {"person", "person_select", "select_person"}:
            self.adapter.click(action.control_id)
            self.adapter.search_person(action.value)
            candidates = self.adapter.observe().person_candidates
            if candidates != (action.value,):
                raise AutomationError(f"人员匹配不唯一：{action.value} -> {list(candidates)}")
            self.adapter.choose_person(action.value)
            return
        if kind in {"scroll", "scroll_to"}:
            self.adapter.scroll_to(action.control_id)
            return
        if kind in {"verify_attachment", "verify_attachments"}:
            return
        raise AutomationError(f"不支持的动作类型：{action.kind}")

    def _verify(
        self,
        action: Action,
        before: PageSnapshot,
        after: PageSnapshot,
    ) -> tuple[bool, str | None, str | None]:
        kind = action.kind.casefold().strip()
        if after.page_state != "ready":
            return False, "page_not_ready_after_action", f"动作后页面状态为 {after.page_state}。"
        if kind in {"click", "edit"} and action.control_id == "summary_edit":
            return after.edit_state == "editing", "edit_state_not_entered", "未进入长文本编辑态。"
        if kind in {"fill", "text", "date"}:
            return (
                after.values.get(action.control_id) == action.value,
                "value_mismatch",
                f"控件回读值与目标不一致：{action.control_id}",
            )
        if kind in {"select", "dropdown"}:
            return (
                after.selected_options.get(action.control_id) == action.value,
                "option_mismatch",
                f"下拉框未回读到选项：{action.value}",
            )
        if kind in {"check", "checkbox", "uncheck", "uncheckbox"}:
            desired = kind in {"check", "checkbox"}
            return (
                after.checked.get(action.control_id) is desired,
                "checked_state_mismatch",
                f"复选框状态未回读为 {desired}。",
            )
        if kind in {"add_row", "add_table_row"}:
            old_rows = before.tables.get(action.control_id, ())
            new_rows = after.tables.get(action.control_id, ())
            prefix_ok = tuple(new_rows[: len(old_rows)]) == tuple(old_rows)
            appended_ok = bool(new_rows) and new_rows[-1] == action.value
            return (
                prefix_ok and len(new_rows) == len(old_rows) + 1 and appended_ok,
                "table_mismatch",
                "动态表格新增后行数、原有顺序或末行值校验失败。",
            )
        if kind in {"person", "person_select"}:
            return (
                after.selected_person == action.value,
                "person_mismatch",
                f"第一发明人未回读为唯一匹配人员：{action.value}",
            )
        if kind in {"scroll", "scroll_to"}:
            return (
                after.visible_anchor == action.control_id,
                "scroll_target_not_visible",
                f"滚动后未识别到目标区块：{action.control_id}",
            )
        if kind in {"verify_attachment", "verify_attachments"}:
            valid = bool(after.attachments) and all(
                item.can_preview and item.can_download for item in after.attachments
            )
            if valid and action.value:
                valid = any(item.name == action.value for item in after.attachments)
            return valid, "attachment_unavailable", "既有附件缺少预览或下载入口。"
        return True, None, None


class InputBackend(Protocol):
    """Minimal OS input surface used by the screen executor."""

    def click(self, x: int, y: int) -> None:
        ...

    def write(self, text: str) -> None:
        ...

    def key(self, name: str) -> None:
        ...

    def scroll(self, amount: int) -> None:
        ...


class Win32InputBackend:
    """Small standard-library-only input backend for Windows PoC runs."""

    def _user32(self):
        if os.name != "nt":
            raise AutomationError("Win32 输入后端仅支持 Windows。")
        return ctypes.windll.user32

    def click(self, x: int, y: int) -> None:
        user32 = self._user32()
        if not user32.SetCursorPos(int(x), int(y)):
            raise AutomationError("无法移动鼠标到目标控件。")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    def write(self, text: str) -> None:
        # KEYEVENTF_UNICODE accepts Chinese characters without a clipboard.
        user32 = self._user32()
        for char in text:
            code = ord(char)
            user32.keybd_event(0, code, 0x0004, 0)
            user32.keybd_event(0, code, 0x0004 | 0x0002, 0)

    def key(self, name: str) -> None:
        user32 = self._user32()
        keys = {"enter": 0x0D, "escape": 0x1B, "tab": 0x09, "ctrl": 0x11, "a": 0x41}
        normalized = name.casefold()
        if normalized == "ctrl+a":
            user32.keybd_event(keys["ctrl"], 0, 0, 0)
            user32.keybd_event(keys["a"], 0, 0, 0)
            user32.keybd_event(keys["a"], 0, 0x0002, 0)
            user32.keybd_event(keys["ctrl"], 0, 0x0002, 0)
            return
        if normalized not in keys:
            raise AutomationError(f"Win32 输入后端不支持按键：{name}")
        vk = keys[normalized]
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)

    def scroll(self, amount: int) -> None:
        self._user32().mouse_event(0x0800, 0, 0, int(amount), 0)


class ScreenActionExecutor:
    """Coordinate adapter for the existing M2 recognition result.

    It performs only one low-level action.  The M3 engine remains responsible
    for fresh recognition and after-action verification.
    """

    def __init__(
        self,
        binding: WindowBinding,
        recognition: RecognitionResult,
        *,
        backend: InputBackend | None = None,
        binder: type[WindowBinder] = WindowBinder,
    ) -> None:
        self.binding = binding
        self.recognition = recognition
        self.backend = backend or Win32InputBackend()
        self.binder = binder

    def execute(self, action: Action, local_x: int | None = None, local_y: int | None = None) -> None:
        located = self.recognition.controls.get(action.control_id)
        if located is None:
            located_anchor = self.recognition.anchors.get(action.control_id)
            if located_anchor is None:
                raise AutomationError(f"控件或滚动锚点未识别：{action.control_id}")
            box = located_anchor.box
        else:
            box = located.box
        x = box.left + box.width // 2 if local_x is None else local_x
        y = box.top + box.height // 2 if local_y is None else local_y
        screen_x, screen_y = self.binder.screen_point(self.binding, x, y)
        kind = action.kind.casefold().strip()
        self.backend.click(screen_x, screen_y)
        if kind in {"fill", "text", "date"}:
            self.backend.key("ctrl+a")
            self.backend.write(action.value)
        elif kind in {"select", "dropdown"}:
            # Native selects accept visible-text keyboard search; no index is used.
            self.backend.write(action.value)
            self.backend.key("enter")
        elif kind in {"scroll", "scroll_to"}:
            self.backend.scroll(int(action.value or "-640"))
        elif kind in {"click", "edit", "check", "checkbox", "uncheck", "uncheckbox", "add_row", "person", "person_select"}:
            return
        else:
            raise AutomationError(f"屏幕执行器不支持的动作类型：{action.kind}")


class InMemoryPageAdapter:
    """Deterministic local page model used by the M3 regression tests."""

    def __init__(
        self,
        *,
        values: Mapping[str, str] | None = None,
        selected_options: Mapping[str, str] | None = None,
        checked: Mapping[str, bool] | None = None,
        tables: Mapping[str, Sequence[str]] | None = None,
        people: Mapping[str, str] | None = None,
        attachments: Sequence[AttachmentSnapshot] | None = None,
    ) -> None:
        self.page_state = "ready"
        self.edit_state = "read_only"
        self.values: dict[str, str] = {
            "patent_no": "2018106374980",
            "application_title": "一种柔性光伏支架抗风稳定装置、柔性光伏支架及光伏系统",
            "application_date": "2018-06-20",
            "grant_date": "2025-03-04",
            "summary_text": "用于现场验证编辑入口、长文本回读和编辑态保护。",
        }
        self.selected_options: dict[str, str] = {"patent_status": "授权", "pct_count": "请选择"}
        self.checked: dict[str, bool] = {"joint_application": False}
        self.tables: dict[str, list[str]] = {
            "rights_holder_rows": ["华电科工股份有限公司"],
            "inventor_rows": ["张三", "李四"],
        }
        self.people = {"张三": "技术部 / 1001", "李四": "工程部 / 1002"}
        self.selected_person: str | None = None
        self.person_candidates: tuple[str, ...] = ()
        self.visible_anchor: str | None = "basic_info"
        self.attachments = (AttachmentSnapshot("证书样本.pdf", True, True),)
        if values is not None:
            self.values.update(values)
        if selected_options is not None:
            self.selected_options.update(selected_options)
        if checked is not None:
            self.checked.update(checked)
        if tables is not None:
            self.tables = {key: list(value) for key, value in tables.items()}
        if people is not None:
            self.people = dict(people)
        if attachments is not None:
            self.attachments = tuple(attachments)

    def observe(self) -> PageSnapshot:
        return PageSnapshot(
            page_state=self.page_state,
            edit_state=self.edit_state,
            values=dict(self.values),
            selected_options=dict(self.selected_options),
            checked=dict(self.checked),
            tables={key: tuple(value) for key, value in self.tables.items()},
            selected_person=self.selected_person,
            person_candidates=self.person_candidates,
            visible_anchor=self.visible_anchor,
            attachments=self.attachments,
        )

    def click(self, control_id: str) -> None:
        if control_id == "summary_edit":
            self.edit_state = "read_only" if self.edit_state == "editing" else "editing"
        elif control_id == "first_inventor_select":
            self.page_state = "modal"
        else:
            # Add-row and other controls have dedicated adapter operations.
            return

    def fill(self, control_id: str, value: str) -> None:
        if control_id == "summary_text" and self.edit_state != "editing":
            raise AutomationError("技术摘要尚未进入编辑态。")
        self.values[control_id] = value

    def select(self, control_id: str, visible_text: str) -> None:
        options = {
            "patent_status": {"待审核", "授权", "失效"},
            "patent_type": {"发明", "实用新型"},
            "pct_count": {"请选择", "是", "否"},
        }
        if visible_text not in options.get(control_id, set()):
            raise AutomationError(f"未识别到下拉选项：{visible_text}")
        self.selected_options[control_id] = visible_text

    def set_checked(self, control_id: str, checked: bool) -> None:
        if control_id not in self.checked:
            raise AutomationError(f"未识别到复选框：{control_id}")
        self.checked[control_id] = checked

    def add_row(self, table_id: str, value: str) -> None:
        if table_id not in self.tables:
            raise AutomationError(f"未识别到动态表格：{table_id}")
        self.tables[table_id].append(value)

    def search_person(self, query: str) -> None:
        self.person_candidates = tuple(name for name in self.people if query.casefold() in name.casefold())

    def choose_person(self, name: str) -> None:
        if self.page_state != "modal" or self.person_candidates != (name,):
            raise AutomationError("人员选择器没有唯一匹配。")
        self.selected_person = name
        self.person_candidates = ()
        self.page_state = "ready"

    def scroll_to(self, anchor_id: str) -> None:
        self.visible_anchor = anchor_id


def run_m3_poc(profile: PageProfile) -> AutomationReport:
    """Run the offline M3 acceptance sequence without opening a browser."""

    adapter = InMemoryPageAdapter()
    actions = [
        Action("patent_no", "fill", "2018106374980"),
        Action("application_date", "date", "2018-06-20"),
        Action("pct_count", "select", "否"),
        Action("joint_application", "check", "true"),
        Action("summary_edit", "edit"),
        Action("summary_text", "fill", "M3 长文本编辑态回读测试。"),
        Action("rights_holder_rows", "add_row", "厦门力煌机械有限公司"),
        Action("inventor_rows", "add_row", "王五"),
        Action("first_inventor_select", "person", "张三"),
        Action("operator", "scroll"),
        Action("attachments", "verify_attachments"),
    ]
    return AutomationEngine(profile, adapter).run(actions)
