"""CNIPA certificate parsing with text-layer-first and OCR fallback paths."""

from __future__ import annotations

import re
from pathlib import Path

import fitz

from .domain import CertificateDraft, FieldEvidence, REQUIRED_FIELDS
from .ocr import extract_document_text

fitz.TOOLS.mupdf_display_errors(False)


class ParseError(ValueError):
    pass


def _label(name: str) -> str:
    return r"\s*".join(re.escape(character) for character in name)


def _between(text: str, label: str, next_label: str) -> str | None:
    pattern = re.compile(
        _label(label) + r"\s*[：:]\s*(.*?)\s*(?=" + _label(next_label) + r"\s*[：:])",
        re.S,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _single(text: str, label: str) -> str | None:
    pattern = re.compile(_label(label) + r"\s*[：:]\s*([^\n]+)")
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _until_text(text: str, label: str, endpoint: str) -> str | None:
    pattern = re.compile(
        _label(label) + r"\s*[：:]\s*(.*?)\s*(?=" + _label(endpoint) + r")",
        re.S,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _iso_date(value: str) -> str | None:
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", _compact(value))
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _split_names(raw: str) -> list[str] | None:
    normalized = raw.replace("；", ";").replace("\r", "").replace("\n", ";")
    names = [part.strip() for part in normalized.split(";")]
    return names if names and all(names) else None


class CertificateParser:
    """Parse known CNIPA electronic certificates without guessing missing data."""

    def parse_file(self, source: str | Path) -> CertificateDraft:
        path = Path(source)
        try:
            document = fitz.open(path)
        except (fitz.FileDataError, OSError) as error:
            raise ParseError(f"无法打开 PDF：{path}") from error
        try:
            page_texts = [page.get_text("text") for page in document]
            text = "\n".join(page_texts).strip()
            method = "text_layer"
            if not text or self._required_labels_missing(text):
                text = extract_document_text(document)
                method = "ocr"
            return self.parse_text(text, path, len(document), method)
        finally:
            document.close()

    @staticmethod
    def _required_labels_missing(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return any(label not in compact for label in ("专利号", "专利申请日", "授权公告日"))

    def parse_text(
        self, text: str, source: str | Path = "", page_count: int = 1, method: str = "text_layer"
    ) -> CertificateDraft:
        if not text.strip():
            raise ParseError("证书未提取到可用文字")
        template = self._template(text, page_count, method)
        draft = CertificateDraft(str(source), template, page_count)
        confidence = 1.0 if method == "text_layer" else 0.78

        values: dict[str, str | None] = {
            "certificate_no": self._certificate_no(text),
            "title": _between(text, "发明名称", "专利权人") or _between(text, "实用新型名称", "专利权人"),
            "patent_no_raw": _single(text, "专利号"),
            "publication_no": _single(text, "授权公告号"),
            "application_date": _single(text, "专利申请日"),
            "grant_publication_date": _single(text, "授权公告日"),
            "current_patentees": _between(text, "专利权人", "地址"),
            "application_date_applicants": _between(text, "申请日时申请人", "申请日时发明人"),
            "inventors": _between(text, "发明人", "专利号"),
            "application_date_inventors": _until_text(text, "申请日时发明人", "国家知识产权局"),
        }
        draft.patent_type = self._patent_type(text)
        if draft.patent_type is None:
            draft.add_review("patent_type")

        for field_name, raw in values.items():
            if raw is None:
                draft.add_review(field_name)
                continue
            draft.field_evidence[field_name] = FieldEvidence(raw, 1, method, confidence)

        draft.certificate_no = values["certificate_no"]
        draft.title = values["title"]
        draft.patent_no_raw = values["patent_no_raw"]
        draft.patent_no = self._patent_no(values["patent_no_raw"], draft)
        draft.publication_no = _compact(values["publication_no"]) if values["publication_no"] else None
        draft.application_date = self._date(values["application_date"], "application_date", draft)
        draft.grant_publication_date = self._date(values["grant_publication_date"], "grant_publication_date", draft)
        for field_name in (
            "current_patentees",
            "application_date_applicants",
            "inventors",
            "application_date_inventors",
        ):
            parsed = _split_names(values[field_name]) if values[field_name] else None
            if parsed is None:
                draft.add_review(field_name)
                parsed = []
            setattr(draft, field_name, parsed)

        if draft.inventors and draft.application_date_inventors and draft.inventors != draft.application_date_inventors:
            draft.add_review("inventors")
            draft.notes.append("inventor_list_changed_since_application")
        if values["inventors"] and "\n" in values["inventors"]:
            draft.notes.append("inventor_list_cross_line_reconstructed")
        if len(draft.current_patentees) > 1:
            draft.notes.append("joint_current_patentees")
        if len(draft.application_date_applicants) > 1:
            draft.notes.append("joint_application_date_applicants")
        if template == "unknown":
            draft.add_review("certificate_template")
        for field_name in REQUIRED_FIELDS:
            if not getattr(draft, field_name):
                draft.add_review(field_name)
        return draft

    @staticmethod
    def _template(text: str, page_count: int, method: str) -> str:
        if method == "ocr":
            return "cnipa_scanned_ocr"
        compact = re.sub(r"\s+", "", text)
        if page_count == 1 and ("发明专利证书" in compact or "实用新型专利证书" in compact):
            return "cnipa_electronic_one_page_text_layer_2024"
        if page_count == 2:
            return "cnipa_two_page_text_layer"
        return "unknown"

    @staticmethod
    def _patent_type(text: str) -> str | None:
        compact = re.sub(r"\s+", "", text)
        if "发明专利证书" in compact:
            return "invention"
        if "实用新型专利证书" in compact:
            return "utility_model"
        return None

    @staticmethod
    def _certificate_no(text: str) -> str | None:
        match = re.search(r"证书号\s*第?\s*(\d+)\s*号", text)
        return match.group(1) if match else None

    @staticmethod
    def _patent_no(raw: str | None, draft: CertificateDraft) -> str | None:
        if not raw:
            return None
        normalized = _compact(raw).upper()
        if not re.fullmatch(r"ZL\d{12}\.[0-9X]", normalized):
            draft.add_review("patent_no")
        return normalized

    @staticmethod
    def _date(raw: str | None, field_name: str, draft: CertificateDraft) -> str | None:
        if not raw:
            return None
        normalized = _iso_date(raw)
        if normalized is None:
            draft.add_review(field_name)
        return normalized
