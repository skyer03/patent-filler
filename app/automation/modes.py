"""M2 run modes and input safety policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .recognizer import RecognitionResult
from .profile import PageProfile


class Mode(str, Enum):
    SIMULATION = "simulation"
    RECOGNITION_ONLY = "recognition_only"
    STEP = "step"


@dataclass(frozen=True)
class Action:
    control_id: str
    kind: str
    value: str = ""


class ActionExecutor(Protocol):
    def execute(self, action: Action, x: int, y: int) -> None:
        ...


@dataclass
class ExecutionResult:
    mode: Mode
    status: str
    executed: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    reason: str | None = None

    @property
    def safe(self) -> bool:
        return self.status not in {"blocked", "error"}


class ModeRunner:
    """Execute only actions supported by a fresh, safe recognition result."""

    def __init__(
        self,
        profile: PageProfile,
        mode: Mode,
        executor: ActionExecutor | None = None,
        confirm: Callable[[Action], bool] | None = None,
    ) -> None:
        self.profile = profile
        self.mode = mode
        self.executor = executor
        self.confirm = confirm or (lambda _action: False)

    def run(self, recognition: RecognitionResult, actions: list[Action]) -> ExecutionResult:
        result = ExecutionResult(self.mode, "ready")
        if not recognition.safe_for_input:
            result.status = "blocked"
            result.reason = "页面锚点、页面状态或识别结果不满足安全条件。"
            result.blocked = [action.control_id for action in actions]
            return result

        for action in actions:
            control = self.profile.controls_by_id.get(action.control_id)
            location = recognition.controls.get(action.control_id)
            if control is None or location is None:
                result.status = "blocked"
                result.reason = f"控件未识别：{action.control_id}"
                result.blocked.append(action.control_id)
                break
            if control.destructive or not control.editable:
                result.status = "blocked"
                result.reason = f"禁止自动执行危险或人工确认控件：{action.control_id}"
                result.blocked.append(action.control_id)
                break
            if control.required_state == "editing" and recognition.edit_state != "editing":
                result.status = "blocked"
                result.reason = f"控件不在编辑态：{action.control_id}"
                result.blocked.append(action.control_id)
                break

            if self.mode is Mode.RECOGNITION_ONLY:
                result.planned.append(action.control_id)
                continue
            if self.mode is Mode.STEP and not self.confirm(action):
                result.status = "paused"
                result.reason = f"用户未确认：{action.control_id}"
                result.blocked.append(action.control_id)
                break
            if self.executor is None:
                result.status = "error"
                result.reason = "当前模式没有配置动作执行器。"
                result.blocked.append(action.control_id)
                break
            center_x = location.box.left + location.box.width // 2
            center_y = location.box.top + location.box.height // 2
            self.executor.execute(action, center_x, center_y)
            result.executed.append(action.control_id)

        if result.status == "ready":
            result.status = "planned" if self.mode is Mode.RECOGNITION_ONLY else "completed"
        return result
