"""M6 local reliability primitives.

M6 keeps the batch layer deliberately independent from the screen executor.
It persists only task metadata and checkpoints, stops the queue on the first
unsafe result, and lets the user explicitly retry or resume a task.  Profile
and non-certificate configuration versions are local JSON files; no network
service is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .automation.profile import PageProfile, load_profile


QUEUE_FORMAT = "m6-task-queue-v1"
PROFILE_REGISTRY_FORMAT = "m6-profile-registry-v1"
CONFIG_STORE_FORMAT = "m6-config-store-v1"
BACKUP_FORMAT = "m6-backup-v1"
REDACTED = "[REDACTED]"


class M6Error(RuntimeError):
    """Base error for the M6 state and operations layer."""


class QueueError(M6Error):
    """Raised for invalid or inconsistent queue state."""


class M6SafetyStop(M6Error):
    """A recoverable safety condition that must stop the current task."""

    def __init__(self, error_code: str, reason: str, checkpoint: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.error_code = error_code
        self.reason = reason
        self.checkpoint = dict(checkpoint or {})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _version_filename(version: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("._")
    return cleaned or hashlib.sha256(version.encode("utf-8")).hexdigest()[:12]


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    exact = {
        "first_inventor_id",
        "first_inventor_contact",
        "operator_phone",
        "operator_email",
        "id_card",
        "phone",
        "mobile",
        "email",
        "contact",
        "身份证号",
        "联系方式",
        "手机",
        "邮箱",
    }
    return (
        normalized in exact
        or normalized.endswith(("_phone", "_mobile", "_email", "_contact", "_id_card"))
        or any(token in normalized for token in ("身份证", "联系方式", "手机号码", "电子邮箱"))
    )


def redact_payload(value: object) -> object:
    """Recursively redact known sensitive fields in an exportable payload."""

    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_sensitive_key(key) and item is not None else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    return value


def write_redacted_json(path: str | Path, value: object) -> Path:
    """Write a JSON export with sensitive fields removed from the payload."""

    target = Path(path)
    _atomic_write_json(target, redact_payload(value))
    return target


@dataclass
class QueueTask:
    task_id: str
    source_file: str
    draft_file: str | None = None
    status: str = "queued"
    attempts: int = 0
    max_attempts: int = 2
    checkpoint: dict[str, Any] = field(default_factory=dict)
    report_path: str | None = None
    diagnostics_path: str | None = None
    last_error: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": self.task_id,
            "source_file": self.source_file,
            "draft_file": self.draft_file,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "checkpoint": redact_payload(self.checkpoint) if redact else self.checkpoint,
            "report_path": self.report_path,
            "diagnostics_path": self.diagnostics_path,
            "last_error": redact_payload(self.last_error) if redact else self.last_error,
            "metadata": redact_payload(self.metadata) if redact else self.metadata,
            "history": redact_payload(self.history) if redact else self.history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QueueTask":
        try:
            return cls(
                task_id=str(value["task_id"]),
                source_file=str(value["source_file"]),
                draft_file=str(value["draft_file"]) if value.get("draft_file") else None,
                status=str(value.get("status", "queued")),
                attempts=int(value.get("attempts", 0)),
                max_attempts=max(1, int(value.get("max_attempts", 2))),
                checkpoint=dict(value.get("checkpoint", {})),
                report_path=str(value["report_path"]) if value.get("report_path") else None,
                diagnostics_path=(
                    str(value["diagnostics_path"]) if value.get("diagnostics_path") else None
                ),
                last_error=dict(value["last_error"]) if value.get("last_error") else None,
                metadata=dict(value.get("metadata", {})),
                history=list(value.get("history", [])),
                created_at=str(value.get("created_at", _now())),
                updated_at=str(value.get("updated_at", _now())),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise QueueError(f"任务记录格式无效：{value!r}") from error


class TaskQueue:
    """A crash-safe, single-process persistent queue for certificate tasks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.tasks: list[QueueTask] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("format") != QUEUE_FORMAT:
                raise QueueError(f"不支持的任务队列格式：{data.get('format')!r}")
            self.tasks = [QueueTask.from_dict(item) for item in data.get("tasks", [])]
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise QueueError(f"无法读取任务队列：{self.path}") from error

    def _save(self) -> None:
        _atomic_write_json(
            self.path,
            {
                "format": QUEUE_FORMAT,
                "updated_at": _now(),
                "tasks": [task.to_dict() for task in self.tasks],
            },
        )

    def get(self, task_id: str) -> QueueTask:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise QueueError(f"任务不存在：{task_id}")

    def enqueue(
        self,
        source_file: str | Path,
        *,
        draft_file: str | Path | None = None,
        task_id: str | None = None,
        max_attempts: int = 2,
        metadata: Mapping[str, Any] | None = None,
        deduplicate: bool = True,
    ) -> QueueTask:
        source = str(Path(source_file))
        if deduplicate:
            for existing in self.tasks:
                if existing.source_file == source and existing.status != "completed":
                    return existing
        if max_attempts < 1:
            raise QueueError("max_attempts 必须大于等于 1。")
        identifier = task_id or f"task-{uuid.uuid4().hex[:12]}"
        if any(item.task_id == identifier for item in self.tasks):
            raise QueueError(f"任务 ID 已存在：{identifier}")
        task = QueueTask(
            task_id=identifier,
            source_file=source,
            draft_file=str(Path(draft_file)) if draft_file else None,
            max_attempts=max_attempts,
            metadata=dict(redact_payload(dict(metadata or {}))),
        )
        task.history.append({"at": task.created_at, "event": "enqueued", "status": "queued"})
        self.tasks.append(task)
        self._save()
        return task

    def enqueue_many(
        self,
        source_files: Iterable[str | Path],
        *,
        max_attempts: int = 2,
        deduplicate: bool = True,
    ) -> list[QueueTask]:
        return [
            self.enqueue(path, max_attempts=max_attempts, deduplicate=deduplicate)
            for path in source_files
        ]

    def claim_next(self) -> QueueTask | None:
        for task in self.tasks:
            if task.status != "queued":
                continue
            if task.attempts >= task.max_attempts:
                self._transition(task, "failed", "max_attempts_exceeded", "已达到最大尝试次数。")
                continue
            task.status = "running"
            task.attempts += 1
            task.updated_at = _now()
            task.last_error = None
            task.history.append(
                {"at": task.updated_at, "event": "claimed", "status": "running", "attempt": task.attempts}
            )
            self._save()
            return task
        return None

    def save_checkpoint(
        self,
        task_id: str,
        checkpoint: Mapping[str, Any],
        *,
        phase: str | None = None,
        step: str | None = None,
    ) -> QueueTask:
        task = self.get(task_id)
        if task.status != "running":
            raise QueueError(f"只有 running 任务可以写入 checkpoint：{task_id}")
        task.checkpoint.update(dict(redact_payload(dict(checkpoint))))
        if phase is not None:
            task.checkpoint["phase"] = phase
        if step is not None:
            task.checkpoint["step"] = step
        task.updated_at = _now()
        self._save()
        return task

    def complete(
        self,
        task_id: str,
        *,
        checkpoint: Mapping[str, Any] | None = None,
        report_path: str | Path | None = None,
        diagnostics_path: str | Path | None = None,
    ) -> QueueTask:
        task = self.get(task_id)
        if checkpoint:
            task.checkpoint.update(dict(redact_payload(dict(checkpoint))))
        task.report_path = str(report_path) if report_path else task.report_path
        task.diagnostics_path = str(diagnostics_path) if diagnostics_path else task.diagnostics_path
        self._transition(task, "completed", "completed", "任务完成。")
        return task

    def pause(
        self,
        task_id: str,
        *,
        error_code: str,
        reason: str,
        checkpoint: Mapping[str, Any] | None = None,
        report_path: str | Path | None = None,
        diagnostics_path: str | Path | None = None,
    ) -> QueueTask:
        return self._stop(
            task_id,
            "paused",
            error_code,
            reason,
            checkpoint=checkpoint,
            report_path=report_path,
            diagnostics_path=diagnostics_path,
        )

    def fail(
        self,
        task_id: str,
        *,
        error_code: str,
        reason: str,
        checkpoint: Mapping[str, Any] | None = None,
        report_path: str | Path | None = None,
        diagnostics_path: str | Path | None = None,
    ) -> QueueTask:
        return self._stop(
            task_id,
            "failed",
            error_code,
            reason,
            checkpoint=checkpoint,
            report_path=report_path,
            diagnostics_path=diagnostics_path,
        )

    def _stop(
        self,
        task_id: str,
        status: str,
        error_code: str,
        reason: str,
        *,
        checkpoint: Mapping[str, Any] | None,
        report_path: str | Path | None,
        diagnostics_path: str | Path | None,
    ) -> QueueTask:
        task = self.get(task_id)
        if checkpoint:
            task.checkpoint.update(dict(redact_payload(dict(checkpoint))))
        task.report_path = str(report_path) if report_path else task.report_path
        task.diagnostics_path = str(diagnostics_path) if diagnostics_path else task.diagnostics_path
        self._transition(task, status, error_code, reason)
        return task

    def _transition(self, task: QueueTask, status: str, error_code: str, reason: str) -> None:
        task.status = status
        task.updated_at = _now()
        task.last_error = {"error_code": error_code, "reason": str(redact_payload(reason))}
        task.history.append(
            {
                "at": task.updated_at,
                "event": "transition",
                "status": status,
                "error_code": error_code,
                "reason": str(redact_payload(reason)),
            }
        )
        self._save()

    def retry(self, task_id: str, *, force: bool = False) -> QueueTask:
        task = self.get(task_id)
        if task.status not in {"failed", "paused"}:
            raise QueueError(f"只有 failed/paused 任务可以重试：{task_id}")
        if not force and task.attempts >= task.max_attempts:
            raise QueueError("任务已达到最大尝试次数；需要显式 force 重试。")
        task.status = "queued"
        task.updated_at = _now()
        task.last_error = None
        task.history.append({"at": task.updated_at, "event": "retry_requested", "status": "queued"})
        self._save()
        return task

    def resume(self, task_id: str, *, force: bool = False) -> QueueTask:
        return self.retry(task_id, force=force)

    def recover_orphaned(self) -> int:
        recovered = 0
        for task in self.tasks:
            if task.status == "running":
                self._transition(task, "paused", "orphaned_task", "上次进程未正常结束，已停在 checkpoint。")
                recovered += 1
        return recovered

    def pending(self) -> list[QueueTask]:
        return [task for task in self.tasks if task.status in {"queued", "running", "paused"}]


@dataclass(frozen=True)
class TaskRunResult:
    status: str = "completed"
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    reason: str | None = None
    report_path: str | None = None
    diagnostics_path: str | None = None


@dataclass
class M6ExecutionContext:
    queue: TaskQueue
    task: QueueTask

    @property
    def checkpoint(self) -> dict[str, Any]:
        return dict(self.task.checkpoint)

    def save_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        *,
        phase: str | None = None,
        step: str | None = None,
    ) -> QueueTask:
        return self.queue.save_checkpoint(self.task.task_id, checkpoint, phase=phase, step=step)


@dataclass
class M6BatchReport:
    status: str
    processed: int
    completed: int
    paused: int
    failed: int
    stopped_on_failure: bool
    tasks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == "completed" and self.failed == 0 and self.paused == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "m6-batch-report-v1",
            "status": self.status,
            "verified": self.verified,
            "processed": self.processed,
            "completed": self.completed,
            "paused": self.paused,
            "failed": self.failed,
            "stopped_on_failure": self.stopped_on_failure,
            "tasks": self.tasks,
        }


Handler = Callable[[QueueTask, M6ExecutionContext], TaskRunResult | Mapping[str, Any] | None]


class M6BatchRunner:
    """Run queued tasks and stop before the next task after an unsafe result."""

    def __init__(
        self,
        queue: TaskQueue,
        handler: Handler,
        *,
        stop_on_failure: bool = True,
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.stop_on_failure = stop_on_failure

    def run(self, *, limit: int | None = None) -> M6BatchReport:
        self.queue.recover_orphaned()
        results: list[dict[str, Any]] = []
        stopped = False
        while limit is None or len(results) < limit:
            task = self.queue.claim_next()
            if task is None:
                break
            context = M6ExecutionContext(self.queue, task)
            try:
                raw_result = self.handler(task, context)
                result = self._coerce_result(raw_result)
                if result.status == "completed":
                    final = self.queue.complete(
                        task.task_id,
                        checkpoint=result.checkpoint,
                        report_path=result.report_path,
                        diagnostics_path=result.diagnostics_path,
                    )
                elif result.status == "paused":
                    final = self.queue.pause(
                        task.task_id,
                        error_code=result.error_code or "safety_stop",
                        reason=result.reason or "任务已安全暂停。",
                        checkpoint=result.checkpoint,
                        report_path=result.report_path,
                        diagnostics_path=result.diagnostics_path,
                    )
                else:
                    final = self.queue.fail(
                        task.task_id,
                        error_code=result.error_code or "task_failed",
                        reason=result.reason or "任务失败。",
                        checkpoint=result.checkpoint,
                        report_path=result.report_path,
                        diagnostics_path=result.diagnostics_path,
                    )
            except M6SafetyStop as error:
                final = self.queue.pause(
                    task.task_id,
                    error_code=error.error_code,
                    reason=error.reason,
                    checkpoint=error.checkpoint,
                )
            except Exception as error:  # noqa: BLE001 - queue must persist unexpected failures
                final = self.queue.fail(
                    task.task_id,
                    error_code="handler_exception",
                    reason=str(error),
                    checkpoint=context.checkpoint,
                )
            results.append(final.to_dict())
            if final.status != "completed" and self.stop_on_failure:
                stopped = True
                break

        completed = sum(item["status"] == "completed" for item in results)
        paused = sum(item["status"] == "paused" for item in results)
        failed = sum(item["status"] == "failed" for item in results)
        status = "failed" if failed else ("paused" if paused else "completed")
        return M6BatchReport(status, len(results), completed, paused, failed, stopped, results)

    @staticmethod
    def _coerce_result(value: TaskRunResult | Mapping[str, Any] | None) -> TaskRunResult:
        if value is None:
            return TaskRunResult()
        if isinstance(value, TaskRunResult):
            if value.status not in {"completed", "paused", "failed"}:
                raise QueueError(f"任务结果状态无效：{value.status}")
            return value
        if isinstance(value, Mapping):
            result = TaskRunResult(
                status=str(value.get("status", "completed")),
                checkpoint=dict(value.get("checkpoint", {})),
                error_code=str(value["error_code"]) if value.get("error_code") else None,
                reason=str(value["reason"]) if value.get("reason") else None,
                report_path=str(value["report_path"]) if value.get("report_path") else None,
                diagnostics_path=(
                    str(value["diagnostics_path"]) if value.get("diagnostics_path") else None
                ),
            )
            if result.status not in {"completed", "paused", "failed"}:
                raise QueueError(f"任务结果状态无效：{result.status}")
            return result
        raise QueueError(f"任务处理器返回值无效：{value!r}")


def validate_safety_state(
    *,
    page_state: str,
    edit_state: str | None = None,
    expected_edit_state: str | None = None,
    confidence: float | None = None,
    minimum_confidence: float = 0.8,
    dynamic_table_ok: bool = True,
) -> None:
    """Raise a recoverable stop for the M6 unsafe-state exit conditions."""

    if page_state != "ready":
        raise M6SafetyStop("page_not_ready", f"页面状态不是 ready：{page_state}。")
    if expected_edit_state is not None and edit_state != expected_edit_state:
        raise M6SafetyStop(
            "edit_state_mismatch",
            f"编辑态不符：期望 {expected_edit_state}，实际 {edit_state}。",
        )
    if confidence is not None and confidence < minimum_confidence:
        raise M6SafetyStop(
            "low_confidence",
            f"识别置信度 {confidence:.3f} 低于阈值 {minimum_confidence:.3f}。",
        )
    if not dynamic_table_ok:
        raise M6SafetyStop("dynamic_table_abnormal", "动态表格行数、顺序或回读异常。")


@dataclass(frozen=True)
class ProfileCompatibility:
    profile_id: str
    profile_version: str
    compatible: bool
    missing_anchors: tuple[str, ...] = ()
    missing_controls: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def check_profile_compatibility(
    profile: PageProfile,
    observed_anchor_ids: Iterable[str],
    *,
    required_controls: Iterable[str] = (),
) -> ProfileCompatibility:
    """Check required anchors before a small website change can drive input."""

    observed = set(observed_anchor_ids)
    missing_anchors = tuple(sorted(set(profile.minimum_anchors) - observed))
    missing_controls = tuple(sorted(set(required_controls) - set(profile.controls_by_id)))
    optional_missing = sorted(
        anchor.id for anchor in profile.anchors if anchor.id not in observed and anchor.id not in profile.minimum_anchors
    )
    warnings = tuple(f"optional_anchor_missing:{item}" for item in optional_missing)
    return ProfileCompatibility(
        profile.id,
        profile.version,
        not missing_anchors and not missing_controls,
        missing_anchors,
        missing_controls,
        warnings,
    )


class ProfileRegistry:
    """Local version store for page profiles, with explicit activation/rollback."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.versions_dir = self.root / "versions"
        self.manifest_path = self.root / "manifest.json"
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"format": PROFILE_REGISTRY_FORMAT, "active_version": None, "versions": {}, "history": []}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if data.get("format") != PROFILE_REGISTRY_FORMAT:
                raise M6Error("页面 profile 仓库格式不受支持。")
            return data
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise M6Error(f"无法读取页面 profile 仓库：{self.manifest_path}") from error

    def _save_manifest(self) -> None:
        _atomic_write_json(self.manifest_path, self._manifest)

    @property
    def active_version(self) -> str | None:
        return self._manifest.get("active_version")

    def install(self, profile_path: str | Path, *, activate: bool = False) -> str:
        source = Path(profile_path)
        profile = load_profile(source)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        filename = _version_filename(profile.version) + ".json"
        target = self.versions_dir / filename
        if target.exists():
            existing = load_profile(target)
            if existing.version != profile.version or existing.id != profile.id:
                filename = f"{_version_filename(profile.version)}-{hashlib.sha256(profile.version.encode()).hexdigest()[:8]}.json"
                target = self.versions_dir / filename
        shutil.copy2(source, target)
        self._manifest.setdefault("versions", {})[profile.version] = {
            "id": profile.id,
            "version": profile.version,
            "path": str(Path("versions") / filename),
            "installed_at": _now(),
        }
        if activate or self.active_version is None:
            self.activate(profile.version)
        else:
            self._save_manifest()
        return profile.version

    def versions(self) -> list[dict[str, Any]]:
        return list(self._manifest.get("versions", {}).values())

    def load(self, version: str | None = None) -> PageProfile:
        selected = version or self.active_version
        if not selected:
            raise M6Error("页面 profile 仓库尚未激活版本。")
        entry = self._manifest.get("versions", {}).get(selected)
        if not entry:
            raise M6Error(f"页面 profile 版本不存在：{selected}")
        return load_profile(self.root / entry["path"])

    def activate(self, version: str) -> PageProfile:
        profile = self.load(version)
        previous = self.active_version
        if previous and previous != version:
            self._manifest.setdefault("history", []).append(previous)
        self._manifest["active_version"] = version
        self._save_manifest()
        return profile

    def rollback(self, version: str | None = None) -> PageProfile:
        target = version
        history = self._manifest.setdefault("history", [])
        if target is None:
            while history and history[-1] == self.active_version:
                history.pop()
            if not history:
                raise M6Error("没有可回滚的页面 profile 版本。")
            target = history.pop()
        profile = self.load(target)
        self._manifest["active_version"] = target
        self._save_manifest()
        return profile

    def check_compatibility(
        self,
        observed_anchor_ids: Iterable[str],
        *,
        version: str | None = None,
        required_controls: Iterable[str] = (),
    ) -> ProfileCompatibility:
        return check_profile_compatibility(
            self.load(version), observed_anchor_ids, required_controls=required_controls
        )


class VersionedConfigStore:
    """Versioned local store for non-certificate/manual field configuration."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.versions_dir = self.root / "versions"
        self.manifest_path = self.root / "manifest.json"
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"format": CONFIG_STORE_FORMAT, "active_version": None, "versions": {}, "history": []}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if data.get("format") != CONFIG_STORE_FORMAT:
                raise M6Error("人工字段配置仓库格式不受支持。")
            return data
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise M6Error(f"无法读取人工字段配置仓库：{self.manifest_path}") from error

    def _save_manifest(self) -> None:
        _atomic_write_json(self.manifest_path, self._manifest)

    @property
    def active_version(self) -> str | None:
        return self._manifest.get("active_version")

    def save(self, version: str, values: Mapping[str, Any], *, activate: bool = False) -> Path:
        if not version.strip():
            raise M6Error("配置版本不能为空。")
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        target = self.versions_dir / f"{_version_filename(version)}.json"
        _atomic_write_json(target, dict(values))
        self._manifest.setdefault("versions", {})[version] = {
            "version": version,
            "path": str(Path("versions") / target.name),
            "saved_at": _now(),
        }
        if activate or self.active_version is None:
            self.activate(version)
        else:
            self._save_manifest()
        return target

    def versions(self) -> list[dict[str, Any]]:
        return list(self._manifest.get("versions", {}).values())

    def load(self, version: str | None = None) -> dict[str, Any]:
        selected = version or self.active_version
        if not selected:
            raise M6Error("人工字段配置尚未激活版本。")
        entry = self._manifest.get("versions", {}).get(selected)
        if not entry:
            raise M6Error(f"人工字段配置版本不存在：{selected}")
        return json.loads((self.root / entry["path"]).read_text(encoding="utf-8"))

    def activate(self, version: str) -> dict[str, Any]:
        values = self.load(version)
        previous = self.active_version
        if previous and previous != version:
            self._manifest.setdefault("history", []).append(previous)
        self._manifest["active_version"] = version
        self._save_manifest()
        return values

    def rollback(self, version: str | None = None) -> dict[str, Any]:
        target = version
        history = self._manifest.setdefault("history", [])
        if target is None:
            if not history:
                raise M6Error("没有可回滚的人工字段配置版本。")
            target = history.pop()
        values = self.load(target)
        self._manifest["active_version"] = target
        self._save_manifest()
        return values

    def export_redacted(self, path: str | Path, *, version: str | None = None) -> Path:
        return write_redacted_json(path, self.load(version))


class BackupManager:
    """Create local ZIP backups and reject unsafe archive extraction paths."""

    @staticmethod
    def create(
        source: str | Path,
        output: str | Path,
        *,
        redact_json: bool = True,
    ) -> Path:
        source_path = Path(source).resolve()
        target = Path(output).resolve()
        if not source_path.exists():
            raise M6Error(f"备份源不存在：{source_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            files = [path for path in source_path.rglob("*") if path.is_file() and path.resolve() != target]
            root_name = source_path.name
        else:
            files = [source_path]
            root_name = source_path.name
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = {
                "format": BACKUP_FORMAT,
                "created_at": _now(),
                "source": str(source_path),
                "redact_json": redact_json,
            }
            archive.writestr("M6_BACKUP_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for path in files:
                relative = (
                    Path(root_name)
                    if source_path.is_file()
                    else Path(root_name) / path.relative_to(source_path)
                )
                if redact_json and path.suffix.casefold() == ".json":
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        archive.write(path, str(relative))
                    else:
                        archive.writestr(
                            str(relative),
                            json.dumps(redact_payload(payload), ensure_ascii=False, indent=2) + "\n",
                        )
                else:
                    archive.write(path, str(relative))
        return target

    @staticmethod
    def restore(archive_path: str | Path, destination: str | Path) -> Path:
        archive_file = Path(archive_path).resolve()
        target_root = Path(destination).resolve()
        if not archive_file.is_file():
            raise M6Error(f"备份文件不存在：{archive_file}")
        target_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_file) as archive:
            for member in archive.infolist():
                member_path = (target_root / member.filename).resolve()
                try:
                    common = os.path.commonpath((str(target_root), str(member_path)))
                except ValueError as error:
                    raise M6Error("备份包含无效路径。") from error
                if common != str(target_root):
                    raise M6Error(f"备份包含越界路径：{member.filename}")
                if member.is_dir():
                    member_path.mkdir(parents=True, exist_ok=True)
                    continue
                member_path.parent.mkdir(parents=True, exist_ok=True)
                member_path.write_bytes(archive.read(member))
        return target_root


def run_m4_queue(
    queue: TaskQueue,
    profile: PageProfile,
    *,
    supplements: Mapping[str, Any] | None = None,
    diagnostics_dir: str | Path | None = None,
    max_retries: int = 1,
    stop_on_failure: bool = True,
    limit: int | None = None,
) -> M6BatchReport:
    """Run one PDF or reviewed JSON draft per queue task through the M4 guardrails."""

    from .certificate import CertificateParser
    from .jsonio import import_drafts
    from .m4 import M4Workflow, ManualFields

    manual = ManualFields.from_mapping(supplements)
    output_root = Path(diagnostics_dir) if diagnostics_dir else None

    def handler(task: QueueTask, context: M6ExecutionContext) -> TaskRunResult:
        source = Path(task.source_file)
        context.save_checkpoint({}, phase="load")
        if source.suffix.casefold() == ".json":
            drafts = import_drafts(source)
            if len(drafts) != 1:
                raise M6SafetyStop("ambiguous_draft", "一个队列任务必须对应一份已复核 JSON 草稿。")
            draft = drafts[0]
        elif source.suffix.casefold() == ".pdf":
            draft = CertificateParser().parse_file(source)
        else:
            raise M6SafetyStop("unsupported_source", f"不支持的任务输入：{source.suffix}")
        context.save_checkpoint({"sample_index": draft.sample_index}, phase="review")
        report_path = output_root / task.task_id / "m4-report.json" if output_root else None
        diagnostic_path = output_root / task.task_id / "diagnostics.json" if output_root else None
        report = M4Workflow(profile, max_retries=max_retries).run(
            draft,
            manual,
            diagnostics=diagnostic_path,
        )
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(report_path, report.to_dict(include_steps=False))
        context.save_checkpoint({"phase": report.phase, "report_status": report.status})
        if report.verified:
            return TaskRunResult(
                checkpoint={"phase": "completed", "sample_index": draft.sample_index},
                report_path=str(report_path) if report_path else None,
                diagnostics_path=str(diagnostic_path) if diagnostic_path else None,
            )
        status = "paused" if report.status in {"blocked", "paused"} else "failed"
        return TaskRunResult(
            status=status,
            checkpoint={"phase": report.phase, "sample_index": draft.sample_index},
            error_code="m4_safety_stop" if status == "paused" else "m4_failed",
            reason=report.reason or "M4 任务未完成。",
            report_path=str(report_path) if report_path else None,
            diagnostics_path=str(diagnostic_path) if diagnostic_path else None,
        )

    return M6BatchRunner(queue, handler, stop_on_failure=stop_on_failure).run(limit=limit)


__all__ = [
    "BACKUP_FORMAT",
    "M6BatchReport",
    "M6BatchRunner",
    "M6Error",
    "M6ExecutionContext",
    "M6SafetyStop",
    "ProfileCompatibility",
    "ProfileRegistry",
    "QueueError",
    "QueueTask",
    "TaskQueue",
    "TaskRunResult",
    "VersionedConfigStore",
    "BackupManager",
    "check_profile_compatibility",
    "redact_payload",
    "run_m4_queue",
    "validate_safety_state",
    "write_redacted_json",
]
