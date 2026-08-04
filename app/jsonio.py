"""JSON import/export helpers for reviewed drafts."""

from __future__ import annotations

import json
from pathlib import Path

from .domain import CertificateDraft


def export_drafts(path: str | Path, drafts: list[CertificateDraft]) -> None:
    Path(path).write_text(
        json.dumps([draft.to_dict() for draft in drafts], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def import_drafts(path: str | Path) -> list[CertificateDraft]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    return [CertificateDraft.from_dict(record) for record in records]
