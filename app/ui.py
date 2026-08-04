"""Small local review UI for correcting parsed certificate drafts."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .certificate import CertificateParser, ParseError
from .domain import CertificateDraft
from .jsonio import export_drafts, import_drafts
from .ocr import OcrUnavailableError


EDITABLE_FIELDS = (
    "patent_type",
    "certificate_no",
    "title",
    "patent_no_raw",
    "patent_no",
    "publication_no",
    "application_date",
    "grant_publication_date",
    "current_patentees",
    "application_date_applicants",
    "inventors",
    "application_date_inventors",
)
LIST_FIELDS = {
    "current_patentees",
    "application_date_applicants",
    "inventors",
    "application_date_inventors",
}


class ReviewApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.drafts: list[CertificateDraft] = []
        self.current_index: int | None = None
        self.current_field: str | None = None
        self._build()

    def _build(self) -> None:
        self.master.title("专利证书解析 MVP")
        self.master.minsize(980, 620)
        self.pack(fill=tk.BOTH, expand=True)

        actions = ttk.Frame(self)
        actions.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(actions, text="选择 PDF", command=self.open_pdfs).pack(side=tk.LEFT)
        ttk.Button(actions, text="导入 JSON", command=self.open_json).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="导出 JSON", command=self.save_json).pack(side=tk.LEFT)
        self.status = ttk.Label(actions, text="选择 PDF 开始解析。")
        self.status.pack(side=tk.RIGHT)

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)

        ttk.Label(left, text="任务").pack(anchor=tk.W)
        self.task_list = tk.Listbox(left, exportselection=False)
        self.task_list.pack(fill=tk.BOTH, expand=True)
        self.task_list.bind("<<ListboxSelect>>", self._select_draft)

        ttk.Label(right, text="字段（选择后可在下方修正）").pack(anchor=tk.W)
        columns = ("value", "confidence", "review")
        self.fields = ttk.Treeview(right, columns=columns, show="tree headings", height=15)
        self.fields.heading("#0", text="字段")
        self.fields.heading("value", text="规范值")
        self.fields.heading("confidence", text="置信度")
        self.fields.heading("review", text="状态")
        self.fields.column("#0", width=150, stretch=False)
        self.fields.column("value", width=500)
        self.fields.column("confidence", width=80, stretch=False)
        self.fields.column("review", width=110, stretch=False)
        self.fields.pack(fill=tk.BOTH, expand=True)
        self.fields.bind("<<TreeviewSelect>>", self._select_field)

        editor = ttk.LabelFrame(right, text="人工修正", padding=8)
        editor.pack(fill=tk.X, pady=(8, 0))
        self.editor_label = ttk.Label(editor, text="尚未选择字段")
        self.editor_label.pack(anchor=tk.W)
        self.editor = tk.Text(editor, height=4, wrap=tk.WORD)
        self.editor.pack(fill=tk.X, pady=4)
        editor_actions = ttk.Frame(editor)
        editor_actions.pack(fill=tk.X)
        ttk.Button(editor_actions, text="应用并确认", command=self.apply_edit).pack(side=tk.LEFT)
        self.evidence = ttk.Label(editor_actions, text="")
        self.evidence.pack(side=tk.LEFT, padx=10)

        ttk.Label(
            right,
            text="名单字段请每行填写一项。应用并确认会清除该字段的待复核标记，原始解析值仍保留在导出的 field_evidence 中。",
            wraplength=730,
        ).pack(anchor=tk.W, pady=(8, 0))

    def open_pdfs(self) -> None:
        paths = filedialog.askopenfilenames(title="选择专利证书 PDF", filetypes=[("PDF", "*.pdf")])
        if not paths:
            return
        parser = CertificateParser()
        parsed: list[CertificateDraft] = []
        errors: list[str] = []
        for path in paths:
            try:
                parsed.append(parser.parse_file(path))
            except (ParseError, OcrUnavailableError, OSError) as error:
                errors.append(f"{Path(path).name}: {error}")
        self.drafts.extend(parsed)
        self._refresh_tasks()
        self.status.config(text=f"已解析 {len(parsed)} 份；待复核字段需人工确认。")
        if errors:
            messagebox.showwarning("部分文件未解析", "\n".join(errors))

    def open_json(self) -> None:
        path = filedialog.askopenfilename(title="导入草稿 JSON", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            self.drafts.extend(import_drafts(path))
        except (json.JSONDecodeError, OSError, ValueError) as error:
            messagebox.showerror("导入失败", str(error))
            return
        self._refresh_tasks()
        self.status.config(text="已导入草稿。")

    def save_json(self) -> None:
        if not self.drafts:
            messagebox.showinfo("没有草稿", "请先解析 PDF 或导入 JSON。")
            return
        path = filedialog.asksaveasfilename(
            title="导出结构化草稿",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            export_drafts(path, self.drafts)
        except OSError as error:
            messagebox.showerror("导出失败", str(error))
            return
        self.status.config(text=f"已导出 {len(self.drafts)} 份草稿。")

    def _refresh_tasks(self) -> None:
        self.task_list.delete(0, tk.END)
        for draft in self.drafts:
            marker = "待复核" if draft.needs_review else "已解析"
            self.task_list.insert(tk.END, f"[{marker}] {Path(draft.source_file).name or '导入草稿'}")
        if self.drafts:
            self.task_list.selection_clear(0, tk.END)
            self.task_list.selection_set(len(self.drafts) - 1)
            self._show_draft(len(self.drafts) - 1)

    def _select_draft(self, _event: object) -> None:
        selection = self.task_list.curselection()
        if selection:
            self._show_draft(selection[0])

    def _show_draft(self, index: int) -> None:
        self.current_index = index
        self.current_field = None
        self.editor.delete("1.0", tk.END)
        self.editor_label.config(text="尚未选择字段")
        self.evidence.config(text="")
        for item in self.fields.get_children():
            self.fields.delete(item)
        draft = self.drafts[index]
        for name in EDITABLE_FIELDS:
            value = getattr(draft, name)
            display = "\n".join(value) if isinstance(value, list) else (value or "")
            evidence = draft.field_evidence.get(name)
            confidence = f"{evidence.confidence:.0%}" if evidence else "人工/导入"
            state = "待复核" if name in draft.needs_review else "已确认"
            self.fields.insert("", tk.END, iid=name, text=name, values=(display, confidence, state))

    def _select_field(self, _event: object) -> None:
        selection = self.fields.selection()
        if not selection or self.current_index is None:
            return
        self.current_field = selection[0]
        draft = self.drafts[self.current_index]
        value = getattr(draft, self.current_field)
        display = "\n".join(value) if isinstance(value, list) else (value or "")
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", display)
        evidence = draft.field_evidence.get(self.current_field)
        raw = evidence.raw_value.replace("\n", " / ") if evidence else "无原始证据（人工/导入）"
        self.editor_label.config(text=f"修正字段：{self.current_field}")
        self.evidence.config(text=f"原始值：{raw[:100]}")

    def apply_edit(self) -> None:
        if self.current_index is None or self.current_field is None:
            messagebox.showinfo("尚未选择字段", "请先在字段表中选择一个字段。")
            return
        draft = self.drafts[self.current_index]
        raw_value = self.editor.get("1.0", "end-1c").strip()
        value = [line.strip() for line in raw_value.splitlines() if line.strip()] if self.current_field in LIST_FIELDS else raw_value
        if not value:
            messagebox.showwarning("值不能为空", "清空字段不会视为人工确认。")
            return
        setattr(draft, self.current_field, value)
        if self.current_field in draft.needs_review:
            draft.needs_review.remove(self.current_field)
        draft.notes.append(f"{self.current_field}_manually_confirmed")
        self._show_draft(self.current_index)
        self.fields.selection_set(self.current_field)
        self.status.config(text=f"已确认：{self.current_field}")


def launch() -> None:
    root = tk.Tk()
    ReviewApp(root)
    root.mainloop()
