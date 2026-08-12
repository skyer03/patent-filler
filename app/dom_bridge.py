"""Local-only task store and Native Messaging host for the Edge extension.

The browser extension never receives a PDF, browser credential, or arbitrary
filesystem path.  It can only fetch the single reviewed task in ``TaskStore``
and report bounded field results back to that store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .domain import CertificateDraft
from .m4 import ManualFields, review_draft


TASK_FORMAT = "patent-dom-task-v1"
RESULT_FORMAT = "patent-dom-result-v1"
DEFAULT_PROFILE_VERSION = "dom-poc-v3"
DEFAULT_STORE = Path(".m6") / "dom-bridge"
MAX_NATIVE_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_STORE_FILE_BYTES = 4 * 1024 * 1024
ALLOWED_REQUESTS = frozenset({"get_ready_task", "report_step", "finish_task", "cancel_task", "get_status"})
ALLOWED_FIELD_KINDS = frozenset({"text", "date", "select", "radio", "checkbox", "table", "person"})
ALLOWED_OVERWRITE_POLICIES = frozenset({"empty_or_same", "reviewed_value"})
ALLOWED_NORMALIZERS = frozenset({"trim", "patent_no", "date", "boolean", "list", "merged_list"})
ALLOWED_FIELD_SOURCES = frozenset({"certificate", "manual", "derived_from_reviewed_table"})
ALLOWED_CANCEL_REASONS = frozenset({"user_stop", "desktop_stop"})


class DomBridgeError(RuntimeError):
    """Raised when a reviewed task or native message is unsafe or invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _web_patent_no(value: str) -> str:
    normalized = value.strip().upper()
    if normalized.startswith("ZL"):
        normalized = normalized[2:]
    return normalized.replace(".", "")


def _web_patent_type(value: str) -> str:
    return {"invention": "发明", "utility_model": "实用新型", "design": "外观设计"}.get(value, value)


def _as_checkbox(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "是"}:
        return True
    if normalized in {"false", "0", "no", "否"}:
        return False
    raise DomBridgeError("联合申请必须明确填写为是/否或 true/false。")


def _confidence(draft: CertificateDraft, field_name: str) -> float:
    evidence = draft.field_evidence.get(field_name)
    return float(evidence.confidence) if evidence is not None else 1.0


def _field(
    field_id: str,
    kind: str,
    value: str | bool | list[str],
    *,
    source: str,
    confidence: float = 1.0,
    normalizer: str = "trim",
    overwrite_policy: str = "empty_or_same",
) -> dict[str, object]:
    if kind not in ALLOWED_FIELD_KINDS:
        raise DomBridgeError(f"不支持的 DOM 字段类型：{kind}")
    if overwrite_policy not in ALLOWED_OVERWRITE_POLICIES:
        raise DomBridgeError(f"不支持的 DOM 覆盖策略：{overwrite_policy}")
    return {
        "field_id": field_id,
        "kind": kind,
        "value": value,
        "source": source,
        "confidence": round(confidence, 4),
        "confirmed": True,
        "normalizer": normalizer,
        "overwrite_policy": overwrite_policy,
    }


def build_dom_task(
    draft: CertificateDraft,
    manual: Mapping[str, object] | ManualFields | None = None,
    *,
    profile_version: str = DEFAULT_PROFILE_VERSION,
    include_complex: bool = False,
    allow_overwrite: bool = False,
) -> dict[str, object]:
    """Build the browser payload only after the certificate draft is reviewed."""

    review = review_draft(draft)
    if not review.approved:
        raise DomBridgeError("证书草稿尚未通过人工复核：" + ", ".join(review.issues))
    supplements = manual if isinstance(manual, ManualFields) else ManualFields.from_mapping(manual)
    if not profile_version.strip():
        raise DomBridgeError("DOM Profile 版本不能为空。")
    if not isinstance(allow_overwrite, bool):
        raise DomBridgeError("DOM 覆盖已有值选项必须是布尔值。")
    overwrite_policy = "reviewed_value" if allow_overwrite else "empty_or_same"

    fields = [
        _field(
            "patent_no",
            "text",
            _web_patent_no(draft.patent_no or ""),
            source="certificate",
            confidence=_confidence(draft, "patent_no"),
            normalizer="patent_no",
            overwrite_policy=overwrite_policy,
        ),
        _field(
            "application_title",
            "text",
            draft.title or "",
            source="certificate",
            confidence=_confidence(draft, "title"),
            overwrite_policy=overwrite_policy,
        ),
        _field(
            "patent_type",
            "radio",
            _web_patent_type(draft.patent_type or ""),
            source="certificate",
            confidence=_confidence(draft, "patent_type"),
            overwrite_policy=overwrite_policy,
        ),
        _field(
            "application_date",
            "date",
            draft.application_date or "",
            source="certificate",
            confidence=_confidence(draft, "application_date"),
            normalizer="date",
            overwrite_policy=overwrite_policy,
        ),
        _field(
            "grant_date",
            "date",
            draft.grant_publication_date or "",
            source="certificate",
            confidence=_confidence(draft, "grant_publication_date"),
            normalizer="date",
            overwrite_policy=overwrite_policy,
        ),
    ]
    if "joint_application" in supplements.values:
        fields.append(
            _field(
                "joint_application",
                "checkbox",
                _as_checkbox(supplements.get("joint_application")),
                source="manual",
                normalizer="boolean",
                overwrite_policy=overwrite_policy,
            )
        )

    if include_complex:
        fields.extend(
            [
                _field(
                    "rights_holder_rows",
                    "table",
                    list(draft.current_patentees),
                    source="certificate",
                    confidence=_confidence(draft, "current_patentees"),
                    normalizer="list",
                    overwrite_policy=overwrite_policy,
                ),
                _field(
                    "inventor_rows",
                    "table",
                    list(draft.inventors),
                    source="certificate",
                    confidence=_confidence(draft, "inventors"),
                    normalizer="list",
                    overwrite_policy=overwrite_policy,
                ),
                _field(
                    "first_inventor_select",
                    "person",
                    draft.inventors[0],
                    source="certificate",
                    confidence=_confidence(draft, "inventors"),
                    overwrite_policy=overwrite_policy,
                ),
                _field(
                    "patentee_merge",
                    "text",
                    ",".join(draft.current_patentees),
                    source="derived_from_reviewed_table",
                    confidence=_confidence(draft, "current_patentees"),
                    normalizer="trim",
                    overwrite_policy=overwrite_policy,
                ),
                _field(
                    "inventor_merge",
                    "text",
                    ",".join(draft.inventors),
                    source="derived_from_reviewed_table",
                    confidence=_confidence(draft, "inventors"),
                    normalizer="trim",
                    overwrite_policy=overwrite_policy,
                ),
            ]
        )

    # Complex controls are represented in the same protocol, but remain
    # absent from the first real-page task until a reviewed DOM Profile exists.
    task_id = uuid.uuid4().hex
    return {
        "format": TASK_FORMAT,
        "task_id": task_id,
        "status": "ready_for_fill",
        "created_at": _now(),
        "profile_version": profile_version.strip(),
        "source": {"sample_index": draft.sample_index},
        "fields": fields,
        "safety": {
            "destructive_actions": [],
            "save_submit_return_delete": "manual_only",
            "auto_create_next_record": False,
            "overwrite_existing": allow_overwrite,
        },
    }


def _atomic_write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_STORE_FILE_BYTES:
            raise DomBridgeError("DOM 任务文件超过允许大小。")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DomBridgeError("当前没有已审核的 Edge 扩展任务。") from error
    except (OSError, json.JSONDecodeError) as error:
        raise DomBridgeError(f"DOM 任务文件无法读取：{error}") from error
    if not isinstance(value, dict):
        raise DomBridgeError("DOM 任务文件必须是 JSON 对象。")
    return value


def _value_evidence(value: object) -> dict[str, object]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    return {
        "present": bool(text),
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
    }


@dataclass
class TaskStore:
    root: Path = DEFAULT_STORE

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    @property
    def task_path(self) -> Path:
        return self.root / "ready_task.json"

    @property
    def result_path(self) -> Path:
        return self.root / "result.json"

    def prepare(
        self,
        draft: CertificateDraft,
        manual: Mapping[str, object] | ManualFields | None = None,
        *,
        profile_version: str = DEFAULT_PROFILE_VERSION,
        include_complex: bool = False,
        allow_overwrite: bool = False,
    ) -> dict[str, object]:
        task = build_dom_task(
            draft,
            manual,
            profile_version=profile_version,
            include_complex=include_complex,
            allow_overwrite=allow_overwrite,
        )
        _atomic_write(self.task_path, task)
        _atomic_write(
            self.result_path,
            {
                "format": RESULT_FORMAT,
                "task_id": task["task_id"],
                "status": "ready_for_fill",
                "created_at": _now(),
                "updated_at": _now(),
                "steps": [],
                "save_attempted": False,
            },
        )
        return task

    def get_ready_task(self) -> dict[str, Any]:
        task = _load_object(self.task_path)
        if task.get("format") != TASK_FORMAT:
            raise DomBridgeError("DOM 任务格式不受支持。")
        if task.get("status") != "ready_for_fill":
            raise DomBridgeError(f"当前任务状态不是 ready_for_fill：{task.get('status')}")
        if not re.fullmatch(r"[0-9a-f]{32}", str(task.get("task_id", ""))):
            raise DomBridgeError("DOM 任务 ID 无效。")
        profile_version = str(task.get("profile_version", ""))
        if not profile_version or len(profile_version) > 80:
            raise DomBridgeError("DOM Profile 版本无效。")
        fields = task.get("fields")
        if not isinstance(fields, list) or not fields:
            raise DomBridgeError("DOM 任务没有可填写字段。")
        field_ids: set[str] = set()
        for item in fields:
            self._validate_field(item)
            field_id = str(item["field_id"])
            if field_id in field_ids:
                raise DomBridgeError(f"DOM 任务包含重复字段：{field_id}")
            field_ids.add(field_id)
        safety = task.get("safety")
        if not isinstance(safety, Mapping) or safety.get("destructive_actions") != []:
            raise DomBridgeError("DOM 任务安全边界无效。")
        if safety.get("save_submit_return_delete") != "manual_only" or safety.get("auto_create_next_record") is not False:
            raise DomBridgeError("DOM 任务不得授权保存、删除或自动新建记录。")
        if not isinstance(safety.get("overwrite_existing"), bool):
            raise DomBridgeError("DOM 任务覆盖已有值安全标记无效。")
        expected_policy = "reviewed_value" if safety["overwrite_existing"] else "empty_or_same"
        if any(item.get("overwrite_policy") != expected_policy for item in fields):
            raise DomBridgeError("DOM 字段覆盖策略与任务安全标记不一致。")
        return task

    def status(self) -> dict[str, object]:
        if not self.task_path.exists():
            return {"status": "empty", "task": None, "result": None}
        task = _load_object(self.task_path)
        result = _load_object(self.result_path) if self.result_path.exists() else None
        safe_task = {
            "task_id": task.get("task_id"),
            "status": task.get("status"),
            "profile_version": task.get("profile_version"),
            "field_count": len(task.get("fields", [])) if isinstance(task.get("fields"), list) else 0,
            "source": task.get("source"),
        }
        return {"status": str(task.get("status", "unknown")), "task": safe_task, "result": result}

    def report_step(self, request: Mapping[str, object]) -> dict[str, object]:
        task = self.get_ready_task()
        self._require_task_id(task, request)
        field_id = str(request.get("field_id", ""))
        expected = next((item for item in task["fields"] if item.get("field_id") == field_id), None)
        if expected is None:
            raise DomBridgeError(f"任务中没有字段：{field_id}")
        status = str(request.get("status", ""))
        if status not in {"filled", "unchanged", "blocked", "failed"}:
            raise DomBridgeError(f"非法字段结果状态：{status}")
        result = _load_object(self.result_path)
        if result.get("task_id") != task.get("task_id"):
            raise DomBridgeError("任务结果与当前任务不匹配。")
        steps = result.setdefault("steps", [])
        if not isinstance(steps, list):
            raise DomBridgeError("DOM 任务结果 steps 格式错误。")
        if result.get("status") in {"blocked", "failed", "cancelled", "completed_waiting_for_manual_save"}:
            raise DomBridgeError("DOM 任务已经停止，不再接受字段结果。")
        expected_index = len(steps)
        if expected_index >= len(task["fields"]) or task["fields"][expected_index].get("field_id") != field_id:
            raise DomBridgeError("字段结果不是任务定义的下一个步骤。")
        error_code = str(request.get("error_code", ""))[:160]
        if error_code and not re.fullmatch(r"[a-z0-9_.:-]+", error_code):
            error_code = "invalid_error_code"
        step = {
            "field_id": field_id,
            "kind": expected.get("kind"),
            "status": status,
            "before": _value_evidence(request.get("before")),
            "after": _value_evidence(request.get("after")),
            "verified": bool(request.get("verified", False)),
            "overwrote_existing": bool(request.get("overwrote_existing", False)),
            "error_code": error_code or None,
            "at": _now(),
        }
        steps.append(step)
        if status in {"blocked", "failed"} or not step["verified"]:
            result["status"] = "blocked" if status == "blocked" else "failed"
        else:
            result["status"] = "running"
        result["updated_at"] = _now()
        _atomic_write(self.result_path, result)
        return step

    def finish(self, request: Mapping[str, object]) -> dict[str, object]:
        task = self.get_ready_task()
        self._require_task_id(task, request)
        result = _load_object(self.result_path)
        steps = result.get("steps", [])
        expected_ids = [str(item.get("field_id")) for item in task["fields"]]
        completed_ids = [
            str(item.get("field_id"))
            for item in steps
            if isinstance(item, Mapping)
            and item.get("status") in {"filled", "unchanged"}
            and item.get("verified") is True
        ]
        if result.get("status") in {"blocked", "failed"} or completed_ids != expected_ids:
            result["status"] = "blocked"
            result["error_code"] = "incomplete_or_unverified_steps"
        else:
            result["status"] = "completed_waiting_for_manual_save"
        result["updated_at"] = _now()
        result["save_attempted"] = False
        task["status"] = result["status"]
        task["updated_at"] = _now()
        _atomic_write(self.result_path, result)
        _atomic_write(self.task_path, task)
        return result

    def cancel(self, request: Mapping[str, object]) -> dict[str, object]:
        task = _load_object(self.task_path)
        self._require_task_id(task, request)
        if task.get("status") != "ready_for_fill":
            raise DomBridgeError("DOM 任务已结束，不能覆盖为取消状态。")
        result = _load_object(self.result_path)
        reason = str(request.get("reason_code", "user_stop"))
        if reason not in ALLOWED_CANCEL_REASONS:
            reason = "user_stop"
        task["status"] = "cancelled"
        task["updated_at"] = _now()
        result["status"] = "cancelled"
        result["reason_code"] = reason
        result["updated_at"] = _now()
        result["save_attempted"] = False
        _atomic_write(self.task_path, task)
        _atomic_write(self.result_path, result)
        return result

    @staticmethod
    def _require_task_id(task: Mapping[str, object], request: Mapping[str, object]) -> None:
        if not request.get("task_id") or request.get("task_id") != task.get("task_id"):
            raise DomBridgeError("消息 task_id 与当前已审核任务不匹配。")

    @staticmethod
    def _validate_field(value: object) -> None:
        if not isinstance(value, Mapping):
            raise DomBridgeError("DOM 任务字段必须是 JSON 对象。")
        field_id = str(value.get("field_id", ""))
        if not field_id or len(field_id) > 80:
            raise DomBridgeError("DOM 字段 ID 无效。")
        if value.get("kind") not in ALLOWED_FIELD_KINDS:
            raise DomBridgeError(f"DOM 字段类型无效：{field_id}")
        if value.get("confirmed") is not True:
            raise DomBridgeError(f"DOM 字段尚未人工确认：{field_id}")
        if value.get("overwrite_policy") not in ALLOWED_OVERWRITE_POLICIES:
            raise DomBridgeError(f"DOM 字段覆盖策略无效：{field_id}")
        if value.get("normalizer") not in ALLOWED_NORMALIZERS:
            raise DomBridgeError(f"DOM 字段规范化规则无效：{field_id}")
        if value.get("source") not in ALLOWED_FIELD_SOURCES:
            raise DomBridgeError(f"DOM 字段来源无效：{field_id}")
        field_value = value.get("value")
        if value.get("kind") == "checkbox":
            if not isinstance(field_value, bool):
                raise DomBridgeError(f"DOM 复选字段值无效：{field_id}")
        elif value.get("kind") == "table":
            if not isinstance(field_value, list) or len(field_value) > 100:
                raise DomBridgeError(f"DOM 表格字段值无效：{field_id}")
            if any(not isinstance(item, str) or len(item) > 2000 for item in field_value):
                raise DomBridgeError(f"DOM 表格行值无效：{field_id}")
        elif not isinstance(field_value, str) or len(field_value) > 20000:
            raise DomBridgeError(f"DOM 字段值无效：{field_id}")


class NativeMessageHost:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def handle(self, request: object) -> dict[str, object]:
        try:
            if not isinstance(request, Mapping):
                raise DomBridgeError("Native Messaging 请求必须是 JSON 对象。")
            request_type = str(request.get("type", ""))
            if request_type not in ALLOWED_REQUESTS:
                raise DomBridgeError(f"不允许的 Native Messaging 请求：{request_type}")
            if request_type == "get_ready_task":
                payload: object = self.store.get_ready_task()
            elif request_type == "report_step":
                payload = self.store.report_step(request)
            elif request_type == "finish_task":
                payload = self.store.finish(request)
            elif request_type == "cancel_task":
                payload = self.store.cancel(request)
            else:
                payload = self.store.status()
            return {"ok": True, "type": request_type, "payload": payload}
        except (DomBridgeError, OSError, ValueError) as error:
            return {
                "ok": False,
                "error": {"code": "dom_bridge_error", "message": str(error)},
            }

    def serve(self, source: BinaryIO | None = None, target: BinaryIO | None = None) -> int:
        source = source or sys.stdin.buffer
        target = target or sys.stdout.buffer
        while True:
            header = source.read(4)
            if not header:
                return 0
            if len(header) != 4:
                return 2
            length = struct.unpack("<I", header)[0]
            if length == 0 or length > MAX_NATIVE_MESSAGE_BYTES:
                response = {"ok": False, "error": {"code": "message_size", "message": "消息长度无效。"}}
            else:
                payload = source.read(length)
                if len(payload) != length:
                    return 2
                try:
                    request = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response = {"ok": False, "error": {"code": "invalid_json", "message": "消息不是有效 UTF-8 JSON。"}}
                else:
                    response = self.handle(request)
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            target.write(struct.pack("<I", len(encoded)))
            target.write(encoded)
            target.flush()


__all__ = [
    "DEFAULT_PROFILE_VERSION",
    "DEFAULT_STORE",
    "DomBridgeError",
    "NativeMessageHost",
    "TaskStore",
    "build_dom_task",
]
