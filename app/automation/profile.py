"""Versioned page profile used by the M2 recognizer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    """Raised when a page profile is incomplete or ambiguous."""


@dataclass(frozen=True)
class AnchorSpec:
    id: str
    text: str
    kind: str = "label"
    required: bool = False
    min_confidence: float | None = None


@dataclass(frozen=True)
class LocatorSpec:
    anchor: str
    placement: str = "right_of"
    dx: int = 8
    dy: int = -2
    width: int = 260
    height: int = 30

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LocatorSpec":
        try:
            return cls(
                anchor=str(value["anchor"]),
                placement=str(value.get("placement", "right_of")),
                dx=int(value.get("dx", 8)),
                dy=int(value.get("dy", -2)),
                width=int(value.get("width", 260)),
                height=int(value.get("height", 30)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProfileError(f"控件定位器无效：{value!r}") from error


@dataclass(frozen=True)
class ControlSpec:
    id: str
    label: str
    section: str
    kind: str
    locator: LocatorSpec
    editable: bool = True
    destructive: bool = False
    required_state: str = "ready"
    source: str = "manual_or_config"
    readback: "ReadbackSpec" = field(default_factory=lambda: ReadbackSpec())


@dataclass(frozen=True)
class ReadbackSpec:
    """Profile-declared way to read a visible control without DOM access.

    ``options`` intentionally stays data-driven.  Geometry differs between
    approved intranet profiles, while the runner supports a small, audited
    set of methods (clipboard, visual choice, table clipboard and OCR).
    """

    method: str = "unsupported"
    normalizer: str = "text"
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: object) -> "ReadbackSpec":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ProfileError(f"控件回读配置无效：{value!r}")
        method = str(value.get("method", "unsupported")).strip()
        normalizer = str(value.get("normalizer", "text")).strip()
        options = {key: item for key, item in value.items() if key not in {"method", "normalizer"}}
        return cls(method, normalizer, options)


@dataclass(frozen=True)
class PageProfile:
    id: str
    version: str
    anchors: tuple[AnchorSpec, ...]
    controls: tuple[ControlSpec, ...]
    minimum_anchors: tuple[str, ...]

    @property
    def anchors_by_id(self) -> dict[str, AnchorSpec]:
        return {anchor.id: anchor for anchor in self.anchors}

    @property
    def controls_by_id(self) -> dict[str, ControlSpec]:
        return {control.id: control for control in self.controls}

    def validate(self) -> None:
        anchor_ids = {anchor.id for anchor in self.anchors}
        if len(anchor_ids) != len(self.anchors):
            raise ProfileError("页面锚点 ID 必须唯一")
        control_ids = {control.id for control in self.controls}
        if len(control_ids) != len(self.controls):
            raise ProfileError("控件 ID 必须唯一")
        missing = set(self.minimum_anchors) - anchor_ids
        if missing:
            raise ProfileError(f"最低页面锚点未定义：{sorted(missing)}")
        for control in self.controls:
            if control.locator.anchor not in anchor_ids:
                raise ProfileError(f"控件 {control.id} 引用了未知锚点 {control.locator.anchor}")

    def coverage(self) -> dict[str, str]:
        """Return the control-to-anchor map used in the M2 coverage report."""

        return {control.id: control.locator.anchor for control in self.controls}


def load_profile(path: str | Path) -> PageProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        profile = PageProfile(
            id=str(data["id"]),
            version=str(data["version"]),
            anchors=tuple(
                AnchorSpec(
                    id=str(item["id"]),
                    text=str(item["text"]),
                    kind=str(item.get("kind", "label")),
                    required=bool(item.get("required", False)),
                    min_confidence=(
                        float(item["min_confidence"])
                        if item.get("min_confidence") is not None
                        else None
                    ),
                )
                for item in data["anchors"]
            ),
            controls=tuple(
                ControlSpec(
                    id=str(item["id"]),
                    label=str(item["label"]),
                    section=str(item["section"]),
                    kind=str(item["kind"]),
                    locator=LocatorSpec.from_dict(item["locator"]),
                    editable=bool(item.get("editable", True)),
                    destructive=bool(item.get("destructive", False)),
                    required_state=str(item.get("required_state", "ready")),
                    source=str(item.get("source", "manual_or_config")),
                    readback=ReadbackSpec.from_dict(item.get("readback")),
                )
                for item in data["controls"]
            ),
            minimum_anchors=tuple(str(item) for item in data["minimum_anchors"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProfileError("页面 profile 缺少必需字段") from error
    profile.validate()
    return profile
