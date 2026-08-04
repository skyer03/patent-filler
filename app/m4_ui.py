"""Tk-based M4 end-to-end workflow launcher."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .automation import WindowBinder, load_profile
from .certificate import CertificateParser, ParseError
from .domain import CertificateDraft
from .jsonio import import_drafts
from .m4 import M4RegressionReport, M4Workflow, ManualFields, run_m4_regression
from .ocr import OcrUnavailableError


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "PROJECT_PLAN_M2_PROFILE.json"
GOLDEN_PATH = ROOT / "m0" / "golden"


class M4ToolApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Tk,
        profile_path: str | Path = PROFILE_PATH,
        golden_path: str | Path = GOLDEN_PATH,
    ) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.profile = load_profile(profile_path)
        self.golden_path = Path(golden_path)
        self.binder = WindowBinder()
        self.binding = None
        self.drafts: list[CertificateDraft] = []
        self.manual = ManualFields({})
        self.diagnostics_dir: Path | None = None
        self.last_result: M4RegressionReport | None = None
        self._build()

    def _build(self) -> None:
        self.master.title("M4 端到端填报 MVP")
        self.master.minsize(1120, 720)
        self.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(toolbar, text="选择草稿/PDF", command=self.open_file).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="选择 PDF 目录", command=self.open_directory).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="加载人工配置", command=self.open_manual).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="运行当前草稿", command=self.run_current).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="运行 50 份回归", command=self.run_regression).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="导出报告", command=self.export_report).pack(side=tk.LEFT, padx=5)

        bind_row = ttk.Frame(self)
        bind_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(bind_row, text="窗口标题包含").pack(side=tk.LEFT)
        self.window_title = ttk.Entry(bind_row, width=34)
        self.window_title.insert(0, "专利信息库")
        self.window_title.pack(side=tk.LEFT, padx=5)
        ttk.Button(bind_row, text="绑定窗口", command=self.bind_window).pack(side=tk.LEFT)
        ttk.Button(bind_row, text="诊断目录", command=self.choose_diagnostics).pack(side=tk.LEFT, padx=5)
        self.binding_status = ttk.Label(bind_row, text="使用离线模拟绑定")
        self.binding_status.pack(side=tk.LEFT, padx=8)

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)

        ttk.Label(left, text="草稿任务").pack(anchor=tk.W)
        self.task_list = tk.Listbox(left, exportselection=False)
        self.task_list.pack(fill=tk.BOTH, expand=True)
        self.task_list.bind("<<ListboxSelect>>", self._select_task)

        ttk.Label(right, text="M4 最终报告（保存/删除/提交不会自动执行）").pack(anchor=tk.W)
        self.result = tk.Text(right, wrap=tk.WORD, state=tk.DISABLED)
        self.result.pack(fill=tk.BOTH, expand=True)
        self.status = ttk.Label(self, text="请选择草稿，或直接运行 50 份离线回归。")
        self.status.pack(anchor=tk.W, pady=(8, 0))

    def open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择已复核 JSON 或专利证书 PDF",
            filetypes=[("JSON/PDF", "*.json *.pdf"), ("JSON", "*.json"), ("PDF", "*.pdf")],
        )
        if not path:
            return
        try:
            source = Path(path)
            self.drafts = import_drafts(source) if source.suffix.casefold() == ".json" else [CertificateParser().parse_file(source)]
        except (OSError, ValueError, ParseError, OcrUnavailableError) as error:
            messagebox.showerror("导入失败", str(error))
            return
        for index, draft in enumerate(self.drafts, start=1):
            if draft.sample_index is None:
                draft.sample_index = index
        self._refresh_tasks()
        self.status.config(text=f"已加载 {len(self.drafts)} 份草稿；请先确认待复核字段。")

    def open_directory(self) -> None:
        directory = filedialog.askdirectory(title="选择 PDF 目录")
        if not directory:
            return
        parser = CertificateParser()
        drafts: list[CertificateDraft] = []
        errors: list[str] = []
        for index, path in enumerate(sorted(Path(directory).glob("*.pdf")), start=1):
            try:
                draft = parser.parse_file(path)
                draft.sample_index = index
                drafts.append(draft)
            except (OSError, ParseError, OcrUnavailableError, ValueError) as error:
                errors.append(f"{path.name}: {error}")
        self.drafts = drafts
        self._refresh_tasks()
        self.status.config(text=f"已加载 {len(drafts)} 份 PDF。")
        if errors:
            messagebox.showwarning("部分文件未解析", "\n".join(errors[:20]))

    def open_manual(self) -> None:
        path = filedialog.askopenfilename(title="选择人工/配置 JSON", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.manual = ManualFields.from_mapping(data)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("配置加载失败", str(error))
            return
        self.status.config(text=f"已加载 {len(self.manual.values)} 个人工/配置字段。")

    def bind_window(self) -> None:
        try:
            self.binding = self.binder.bind_by_title(self.window_title.get())
        except Exception as error:
            messagebox.showerror("窗口绑定失败", str(error))
            return
        rect = self.binding.rect
        self.binding_status.config(text=f"已绑定：{self.binding.title} ({rect.width}×{rect.height})")
        self.status.config(text="窗口已绑定；当前 M4 回归仍会在保存前停止。")

    def choose_diagnostics(self) -> None:
        directory = filedialog.askdirectory(title="选择诊断包目录")
        if directory:
            self.diagnostics_dir = Path(directory)
            self.status.config(text=f"诊断包目录：{directory}")

    def run_current(self) -> None:
        selection = self.task_list.curselection()
        if not selection:
            if not self.drafts:
                messagebox.showinfo("没有草稿", "请先选择 JSON/PDF 或 PDF 目录。")
                return
            index = 0
        else:
            index = selection[0]
        draft = self.drafts[index]
        diagnostics = self._diagnostic_path(draft)
        report = M4Workflow(self.profile, binding=self.binding).run(
            draft, self.manual, diagnostics=diagnostics
        )
        self.last_result = M4RegressionReport([report])
        self._show_result(self.last_result)
        self._refresh_tasks()
        self.status.config(text=f"当前草稿：{report.status}；{report.reason or ''}")

    def run_regression(self) -> None:
        self.last_result = run_m4_regression(
            self.profile,
            self.golden_path,
            supplements=self.manual,
            diagnostics_dir=self.diagnostics_dir,
            binding=self.binding,
        )
        self._show_result(self.last_result)
        self.status.config(text=f"M4 回归完成：{self.last_result.passed}/{self.last_result.total}。")

    def export_report(self) -> None:
        if self.last_result is None:
            messagebox.showinfo("没有报告", "请先运行当前草稿或 50 份回归。")
            return
        path = filedialog.asksaveasfilename(
            title="导出 M4 最终报告",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        Path(path).write_text(
            json.dumps(self.last_result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.status.config(text=f"报告已导出：{path}")

    def _diagnostic_path(self, draft: CertificateDraft) -> Path | None:
        if self.diagnostics_dir is None:
            return None
        sample = draft.sample_index or 1
        return self.diagnostics_dir / f"sample-{sample:03d}.json"

    def _refresh_tasks(self) -> None:
        self.task_list.delete(0, tk.END)
        for draft in self.drafts:
            marker = "待复核" if draft.needs_review else "已确认"
            self.task_list.insert(tk.END, f"[{marker}] {Path(draft.source_file).name or '草稿'}")

    def _select_task(self, _event: object) -> None:
        selection = self.task_list.curselection()
        if selection:
            draft = self.drafts[selection[0]]
            self.status.config(text=f"已选择：{Path(draft.source_file).name}")

    def _show_result(self, result: M4RegressionReport) -> None:
        value = result.to_dict()
        self.result.config(state=tk.NORMAL)
        self.result.delete("1.0", tk.END)
        self.result.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
        self.result.config(state=tk.DISABLED)


def launch(
    profile_path: str | Path = PROFILE_PATH,
    golden_path: str | Path = GOLDEN_PATH,
) -> None:
    root = tk.Tk()
    app = M4ToolApp(root, profile_path, golden_path)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


__all__ = ["M4ToolApp", "launch"]
