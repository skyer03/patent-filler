"""Domain objects shared by the parser, CLI, and review UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REQUIRED_FIELDS = (
    "patent_type",
    "patent_no",
    "title",
    "application_date",
    "grant_publication_date",
    "current_patentees",
    "inventors",
)


@dataclass
class FieldEvidence:
    raw_value: str
    page: int
    method: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_value": self.raw_value,
            "page": self.page,
            "method": self.method,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FieldEvidence":
        return cls(
            raw_value=str(value.get("raw_value", "")),
            page=int(value.get("page", 1)),
            method=str(value.get("method", "import")),
            confidence=float(value.get("confidence", 0.0)),
        )


@dataclass
class CertificateDraft:
    source_file: str
    certificate_template: str
    page_count: int
    patent_type: str | None = None
    certificate_no: str | None = None
    title: str | None = None
    patent_no_raw: str | None = None
    patent_no: str | None = None
    publication_no: str | None = None
    application_date: str | None = None
    grant_publication_date: str | None = None
    current_patentees: list[str] = field(default_factory=list)
    application_date_applicants: list[str] = field(default_factory=list)
    inventors: list[str] = field(default_factory=list)
    application_date_inventors: list[str] = field(default_factory=list)
    sample_index: int | None = None
    field_evidence: dict[str, FieldEvidence] = field(default_factory=dict)
    needs_review: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_review(self, field_name: str) -> None:
        if field_name not in self.needs_review:
            self.needs_review.append(field_name)

    def to_dict(self) -> dict[str, Any]:
        evidence = {name: item.to_dict() for name, item in self.field_evidence.items()}
        source_evidence: dict[str, str] = {}
        if "current_patentees" in self.field_evidence:
            source_evidence["current_patentees_raw"] = self.field_evidence["current_patentees"].raw_value
        if "inventors" in self.field_evidence:
            source_evidence["inventors_raw"] = self.field_evidence["inventors"].raw_value
        return {
            "source_file": self.source_file,
            "sample_index": self.sample_index,
            "certificate_template": self.certificate_template,
            "page_count": self.page_count,
            "patent_type": self.patent_type,
            "certificate_no": self.certificate_no,
            "title": self.title,
            "patent_no_raw": self.patent_no_raw,
            "patent_no": self.patent_no,
            "publication_no": self.publication_no,
            "application_date": self.application_date,
            "grant_publication_date": self.grant_publication_date,
            "current_patentees": self.current_patentees,
            "application_date_applicants": self.application_date_applicants,
            "inventors": self.inventors,
            "application_date_inventors": self.application_date_inventors,
            "source_evidence": source_evidence,
            "field_evidence": evidence,
            "review": {
                "status": "needs_manual_review" if self.needs_review else "parsed",
                "needs_review": self.needs_review,
                "notes": self.notes,
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CertificateDraft":
        review = value.get("review", {})
        evidence = {
            name: FieldEvidence.from_dict(item)
            for name, item in value.get("field_evidence", {}).items()
        }
        legacy_evidence = value.get("source_evidence", {})
        for field_name, legacy_name in (
            ("current_patentees", "current_patentees_raw"),
            ("inventors", "inventors_raw"),
        ):
            if field_name not in evidence and legacy_name in legacy_evidence:
                evidence[field_name] = FieldEvidence(
                    raw_value=str(legacy_evidence[legacy_name]),
                    page=1,
                    method="import",
                    confidence=1.0,
                )
        return cls(
            source_file=str(value.get("source_file", "")),
            sample_index=value.get("sample_index"),
            certificate_template=str(value.get("certificate_template", "unknown")),
            page_count=int(value.get("page_count", 0)),
            patent_type=value.get("patent_type"),
            certificate_no=value.get("certificate_no"),
            title=value.get("title"),
            patent_no_raw=value.get("patent_no_raw"),
            patent_no=value.get("patent_no"),
            publication_no=value.get("publication_no"),
            application_date=value.get("application_date"),
            grant_publication_date=value.get("grant_publication_date"),
            current_patentees=list(value.get("current_patentees", [])),
            application_date_applicants=list(value.get("application_date_applicants", [])),
            inventors=list(value.get("inventors", [])),
            application_date_inventors=list(value.get("application_date_inventors", [])),
            field_evidence=evidence,
            needs_review=list(review.get("needs_review", [])),
            notes=list(review.get("notes", [])),
        )
