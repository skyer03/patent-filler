"""Tk desktop interface for the M7 unified workflow."""

from __future__ import annotations

import json
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .automation import Action
from .domain import CertificateDraft
from .m4 import ManualFields, review_draft
from .m5 import load_actions
from .m7 import M7Error, M7Mode, M7RunReport, M7Service, load_workflow_sources
from .ui import EDITABLE_FIELDS, LIST_FIELDS
from .version import APP_VERSION


class M7ToolApp(ttk.Frame):
    """One task window for import, review, field execution and M6 controls."""

    PROBE_LABELS = {
        "patent_no": "专利号",
        "application_title": "申请名称",
        "patent_type": "申请类型",
        "application_date": "申请受理日",
        "grant_date": "授权公告日",
        "joint_application": "联合申请",
    }

    def __init__(self, master: tk.Tk, service: M7Service | None = None) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.stop_event = threading.Event()
        self.service = service or M7Service(stop_requested=self.stop_event.is_set)
        self.drafts: list[CertificateDraft] = []
        self.current_index: int | None = None
        self.current_field: str | None = None
        self.manual = ManualFields({})
        self.image_path: Path | None = None
        self.actions_path: Path | None = None
        self.diagnostics_dir: Path | None = None
        self.last_report: M7RunReport | None = None
        self.auto_running = False
        self.source_loading = False
        self.include_complex = tk.BooleanVar(value=False)
        self.allow_overwrite = tk.BooleanVar(value=False)
        self._build()

    def _build(self) -> None:
        self.master.title(f"专利证书自动填写 [{APP_VERSION}]")
        self.master.minsize(1160, 760)
        self.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self, text=f"当前版本：{APP_VERSION}", foreground="#555555").pack(
            anchor=tk.W, pady=(0, 6)
        )

        ttk.Label(self, text="打开已登录网页 → 上传证书 → 准备 Edge 扩展任务", font=("Microsoft YaHei", 15, "bold")).pack(
            anchor=tk.W, pady=(0, 10)
        )
        steps = ttk.Frame(self)
        steps.pack(fill=tk.X, pady=(0, 8))

        bind_card = ttk.LabelFrame(steps, text="1  打开页面", padding=8)
        bind_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        ttk.Button(bind_card, text="屏幕诊断时绑定窗口", command=self.bind_window).pack(anchor=tk.W)
        self.binding_status = ttk.Label(bind_card, text="请在现有 Edge 中登录并打开空白专利记录；扩展预填无需桌面绑定。", wraplength=330)
        self.binding_status.pack(anchor=tk.W, pady=(6, 0))

        upload_card = ttk.LabelFrame(steps, text="2  上传证书", padding=8)
        upload_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6)
        ttk.Button(upload_card, text="选择 PDF 或已校对 JSON", command=self.open_file).pack(anchor=tk.W)
        self.source_status = ttk.Label(upload_card, text="尚未上传文件", wraplength=330)
        self.source_status.pack(anchor=tk.W, pady=(6, 0))

        run_card = ttk.LabelFrame(steps, text="3  自动填写", padding=8)
        run_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        self.start_button = ttk.Button(
            run_card,
            text="准备 Edge 扩展任务",
            command=self.prepare_extension_task,
            state=tk.DISABLED,
        )
        self.start_button.pack(side=tk.LEFT)
        ttk.Button(run_card, text="安全停止", command=self.emergency_stop).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(
            run_card,
            text="启用已校准复杂控件（权利人/发明人）",
            variable=self.include_complex,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(
            run_card,
            text="允许覆盖已有值（人工确认）",
            variable=self.allow_overwrite,
        ).pack(side=tk.LEFT, padx=6)
        self.progress_status = ttk.Label(run_card, text="等待上传并人工确认证书", wraplength=250)
        self.progress_status.pack(anchor=tk.W, pady=(6, 0))

        self.advanced_visible = False
        self.advanced_toggle = ttk.Button(self, text="展开高级设置", command=self.toggle_advanced)
        self.advanced_toggle.pack(anchor=tk.W, pady=(0, 6))
        self.advanced_frame = ttk.LabelFrame(self, text="高级设置（仿真、诊断、配置与队列）", padding=6)

        toolbar = ttk.Frame(self.advanced_frame)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(toolbar, text="导入目录", command=self.open_directory).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="人工配置", command=self.open_manual).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="现场截图", command=self.choose_image).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="动作 JSON", command=self.choose_actions).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="诊断目录", command=self.choose_diagnostics).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="导出报告", command=self.export_report).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="旧版屏幕填写", command=self.start_auto_update).pack(side=tk.LEFT)

        probe_frame = ttk.LabelFrame(self.advanced_frame, text="基本信息逐项测试", padding=6)
        probe_frame.pack(fill=tk.X, pady=(0, 8))
        probe_row = ttk.Frame(probe_frame)
        probe_row.pack(fill=tk.X)
        ttk.Label(probe_row, text="控件").pack(side=tk.LEFT)
        self.probe_control = tk.StringVar(value=self.PROBE_LABELS["patent_no"])
        self.probe_combo = ttk.Combobox(
            probe_row,
            textvariable=self.probe_control,
            values=list(self.PROBE_LABELS.values()),
            state="readonly",
            width=22,
        )
        self.probe_combo.pack(side=tk.LEFT, padx=6)
        self.probe_combo.bind("<<ComboboxSelected>>", lambda _event: self._fill_probe_value())
        ttk.Label(probe_row, text="测试值").pack(side=tk.LEFT)
        self.probe_value = tk.StringVar()
        ttk.Entry(probe_row, textvariable=self.probe_value, width=42).pack(side=tk.LEFT, padx=6)
        ttk.Button(probe_row, text="只识别定位", command=self.run_probe_recognition).pack(side=tk.LEFT, padx=2)
        ttk.Button(probe_row, text="读取当前值", command=self.read_probe_value).pack(side=tk.LEFT, padx=2)
        ttk.Button(probe_row, text="确认并测试", command=self.run_probe_action).pack(side=tk.LEFT, padx=2)
        self.probe_hint = ttk.Label(
            probe_frame,
            text="前五项默认取当前证书草稿值；联合申请必须明确选择是/否。每次只执行一个字段。",
            foreground="#555555",
        )
        self.probe_hint.pack(anchor=tk.W, pady=(5, 0))

        mode_row = ttk.Frame(self.advanced_frame)
        mode_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(mode_row, text="高级运行模式").pack(side=tk.LEFT)
        self.mode = tk.StringVar(value=M7Mode.SIMULATION.value)
        ttk.Combobox(
            mode_row,
            textvariable=self.mode,
            values=[item.value for item in M7Mode if item is not M7Mode.AUTO_UPDATE],
            state="readonly",
            width=20,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(mode_row, text="运行高级模式", command=self.run_current).pack(side=tk.LEFT)

        queue_row = ttk.Frame(self.advanced_frame)
        queue_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(queue_row, text="当前加入队列", command=self.enqueue_current).pack(side=tk.LEFT)
        ttk.Button(queue_row, text="全部加入队列", command=self.enqueue_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(queue_row, text="查看队列", command=self.show_queue).pack(side=tk.LEFT)
        ttk.Button(queue_row, text="恢复孤立任务", command=self.recover_queue).pack(side=tk.LEFT, padx=4)
        ttk.Button(queue_row, text="重试任务", command=self.retry_task).pack(side=tk.LEFT)
        ttk.Button(queue_row, text="暂停任务", command=self.pause_task).pack(side=tk.LEFT, padx=4)
        ttk.Button(queue_row, text="Profile 版本", command=self.install_profile).pack(side=tk.LEFT)
        ttk.Button(queue_row, text="回滚 Profile", command=self.rollback_profile).pack(side=tk.LEFT, padx=4)
        ttk.Button(queue_row, text="保存配置版本", command=self.save_config_version).pack(side=tk.LEFT)
        ttk.Button(queue_row, text="回滚配置", command=self.rollback_config).pack(side=tk.LEFT, padx=4)

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.body = body
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        center = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(center, weight=3)
        body.add(right, weight=3)

        ttk.Label(left, text="证书任务").pack(anchor=tk.W)
        self.task_list = tk.Listbox(left, exportselection=False)
        self.task_list.pack(fill=tk.BOTH, expand=True)
        self.task_list.bind("<<ListboxSelect>>", self._select_draft)

        ttk.Label(center, text="证书字段校对").pack(anchor=tk.W)
        self.fields = ttk.Treeview(center, columns=("value", "confidence", "review"), show="tree headings")
        self.fields.heading("#0", text="字段")
        self.fields.heading("value", text="规范值")
        self.fields.heading("confidence", text="置信度")
        self.fields.heading("review", text="状态")
        self.fields.column("#0", width=150, stretch=False)
        self.fields.column("value", width=360)
        self.fields.column("confidence", width=75, stretch=False)
        self.fields.column("review", width=80, stretch=False)
        self.fields.pack(fill=tk.BOTH, expand=True)
        self.fields.bind("<<TreeviewSelect>>", self._select_field)
        editor = ttk.LabelFrame(center, text="人工修正并确认", padding=6)
        editor.pack(fill=tk.X, pady=(6, 0))
        self.editor_label = ttk.Label(editor, text="选择字段后编辑；名单每行一项")
        self.editor_label.pack(anchor=tk.W)
        self.editor = tk.Text(editor, height=4, wrap=tk.WORD)
        self.editor.pack(fill=tk.X, pady=4)
        ttk.Button(editor, text="应用并确认字段", command=self.apply_edit).pack(anchor=tk.W)

        ttk.Label(right, text="实时日志（本次运行）").pack(anchor=tk.W, pady=(6, 0))
        self.live_log_text = tk.Text(right, height=9, wrap=tk.WORD, state=tk.DISABLED)
        self.live_log_text.pack(fill=tk.X, pady=(2, 6))
        ttk.Label(right, text="M7 报告与安全停机原因").pack(anchor=tk.W)
        self.report_text = tk.Text(right, wrap=tk.WORD, state=tk.DISABLED)
        self.report_text.pack(fill=tk.BOTH, expand=True)
        self.status = ttk.Label(self, text="程序只填写当前页面，并在保存前停止；不会自动保存、提交、返回、删除或新建记录。")
        self.status.pack(anchor=tk.W, pady=(8, 0))
        self._fill_probe_value()

    def prepare_extension_task(self) -> None:
        """Publish the reviewed draft; the extension performs DOM input."""

        try:
            draft = self._current_draft(required=True)
            if len(self.drafts) != 1:
                raise M7Error("Edge 扩展每次只能处理一份证书。")
            self._require_review(draft)
            if self.allow_overwrite.get() and not messagebox.askyesno(
                "确认覆盖已有值",
                "已启用覆盖模式。扩展可能覆盖当前页面中与证书不同的非空字段；仍不会保存、提交、返回或删除。\n\n确定要生成覆盖任务吗？",
                icon="warning",
            ):
                return
            task = self.service.prepare_dom_task(
                draft,
                self.manual if self.manual.values else None,
                include_complex=self.include_complex.get(),
                allow_overwrite=self.allow_overwrite.get(),
            )
        except Exception as error:
            messagebox.showerror("无法准备扩展任务", str(error))
            return
        self.progress_status.config(text="任务已就绪，请在 Edge 扩展中点击“读取当前任务”")
        self.status.config(
            text=(
                "扩展任务已生成；允许覆盖已有值，完成后仍需人工检查并保存。"
                if self.allow_overwrite.get()
                else "扩展任务已生成；扩展只会填写空值或相同值，完成后仍需人工检查并保存。"
            )
        )
        self._show_json(
            {
                "status": task["status"],
                "task_id": task["task_id"],
                "profile_version": task["profile_version"],
                "field_count": len(task["fields"]),
                "next": "在 Edge 扩展中读取任务并开始预填",
            }
        )
        messagebox.showinfo(
            "扩展任务已就绪",
            "请切换到已登录的专利填报页，在“专利证书安全预填”Edge 扩展中点击“读取当前任务”，然后开始预填。",
        )

    def _probe_diagnostics(self) -> Path | None:
        """Use an explicit directory only when the user selected one.

        The service creates a timestamped basic-info directory when this is
        ``None``.
        """

        return self.diagnostics_dir

    def _probe_id(self) -> str:
        label = self.probe_control.get()
        return next((control_id for control_id, text in self.PROBE_LABELS.items() if text == label), label)

    def _fill_probe_value(self) -> None:
        control_id = self._probe_id()
        if control_id == "joint_application":
            self.probe_value.set("否")
            return
        if self.current_index is None or not self.drafts:
            self.probe_value.set("")
            return
        draft = self.drafts[self.current_index]
        patent_no = str(draft.patent_no or "").strip().upper()
        if patent_no.startswith("ZL"):
            patent_no = patent_no[2:]
        patent_no = patent_no.replace(".", "").replace(" ", "")
        patent_type = {
            "invention": "发明",
            "utility_model": "实用新型",
            "design": "外观设计",
        }.get(str(draft.patent_type or "").strip(), str(draft.patent_type or ""))
        values = {
            "patent_no": patent_no,
            "application_title": getattr(draft, "application_title", None) or draft.title or "",
            "patent_type": patent_type,
            "application_date": draft.application_date or "",
            "grant_date": getattr(draft, "grant_date", None) or draft.grant_publication_date or "",
        }
        self.probe_value.set(str(values.get(control_id, "")))

    def _show_probe_report(self, report: M7RunReport) -> None:
        self.last_report = report
        self._show_report(report)
        self.status.config(text=f"基本信息 {report.payload.control_id}：{report.status}；{self._reason_text(report)}")

    def run_probe_recognition(self) -> None:
        try:
            if self.service.binding is None and self.image_path is None:
                raise M7Error("请先绑定网页，或选择现场截图。")
            diagnostics = self.diagnostics_dir or (
                Path(".m6")
                / "diagnostics"
                / "basic-info"
                / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            )
            report = self.service.run_recognition_only(
                image=self.image_path,
                annotated=self._annotated_path() or diagnostics / "field.annotated.png",
                diagnostics=diagnostics,
            )
        except Exception as error:
            messagebox.showerror("只识别定位失败", str(error))
            return
        self.last_report = report
        self._show_report(report)
        self.status.config(text=f"只识别定位：{report.status}；{self._reason_text(report)}")

    def read_probe_value(self) -> None:
        try:
            report = self.service.inspect_basic_control(
                self._probe_id(), diagnostics=self._probe_diagnostics()
            )
        except Exception as error:
            messagebox.showerror("读取当前值失败", str(error))
            return
        self._show_probe_report(report)

    def run_probe_action(self) -> None:
        control_id = self._probe_id()
        value = self.probe_value.get().strip()
        if control_id == "joint_application" and value not in {"是", "否", "true", "false", "yes", "no", "1", "0"}:
            messagebox.showwarning("需要明确选择", "联合申请只能填写 是 或 否。")
            return
        if not value:
            messagebox.showwarning("缺少测试值", "请先填写本次测试值。")
            return
        if not messagebox.askyesno(
            "确认单字段测试",
            f"即将只测试“{self.PROBE_LABELS.get(control_id, control_id)}”。\n"
            "动作前会先回读；冲突或无法判断时不会发送输入。继续吗？",
        ):
            return
        try:
            report = self.service.run_basic_probe(
                control_id,
                value,
                diagnostics=self._probe_diagnostics(),
            )
        except Exception as error:
            messagebox.showerror("逐项测试失败", str(error))
            return
        self._show_probe_report(report)

    def open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择证书 PDF 或已复核 JSON",
            filetypes=[("PDF/JSON", "*.pdf *.json"), ("PDF", "*.pdf"), ("JSON", "*.json")],
        )
        if path:
            self._load_source(Path(path))

    def open_directory(self) -> None:
        path = filedialog.askdirectory(title="选择包含 PDF/JSON 的目录")
        if path:
            self._load_source(Path(path))

    def _load_source(self, source: Path) -> None:
        if self.source_loading:
            return
        self.source_loading = True
        self.source_status.config(text=f"正在识别：{source.name}")
        self.status.config(text="正在解析证书；界面保持可响应，请不要重复选择文件。")

        def worker() -> None:
            try:
                drafts = load_workflow_sources(source)
            except Exception as error:  # parser/OCR/file failures return to Tk thread
                message = str(error)
                self.master.after(0, lambda: self._finish_source_error(message))
                return
            self.master.after(0, lambda: self._finish_source_load(source, drafts))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_source_error(self, message: str) -> None:
        self.source_loading = False
        self.source_status.config(text="导入失败")
        self.status.config(text=f"证书识别失败：{message}")
        messagebox.showerror("导入失败", message)

    def _finish_source_load(self, source: Path, drafts: list[CertificateDraft]) -> None:
        self.source_loading = False
        self.drafts = drafts
        self._refresh_tasks()
        if len(self.drafts) != 1:
            self.source_status.config(text=f"已导入 {len(self.drafts)} 份；一键更新每次只接受一份")
        else:
            review = review_draft(self.drafts[0])
            if review.approved:
                self.source_status.config(text=f"已就绪：{source.name}")
            else:
                self.source_status.config(text="需要修正：" + ", ".join(review.issues))
        self.status.config(text=f"已导入 {len(self.drafts)} 份；待复核字段会阻止自动填写。")
        self._update_auto_state()

    def open_manual(self) -> None:
        path = filedialog.askopenfilename(title="选择人工/配置 JSON", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            self.manual = ManualFields.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            messagebox.showerror("配置加载失败", str(error))
            return
        self.status.config(text=f"已加载 {len(self.manual.values)} 个人工/配置字段；敏感值只在本地使用。")

    def bind_window(self) -> None:
        try:
            browser_windows = self.service.binder.list_visible_windows(browser_only=True)
        except Exception:
            # Keep the manual title path available if Windows enumeration is
            # temporarily unavailable (for example during desktop startup).
            browser_windows = []
        if len(browser_windows) == 1:
            # A single visible browser is unambiguous.  Avoid making the user
            # retype a title that may contain Edge's zero-width characters.
            title = browser_windows[0][1]
        else:
            prompt = "输入 Edge/Chrome 窗口标题片段（例如：专利信息库 或 科技项目管理系统）："
            if browser_windows:
                candidates = "\n".join(f"- {window_title}" for _hwnd, window_title in browser_windows[:8])
                suffix = "\n……" if len(browser_windows) > 8 else ""
                prompt += f"\n\n当前检测到的浏览器窗口：\n{candidates}{suffix}"
            title = simpledialog.askstring("绑定浏览器", prompt, initialvalue="")
        if not title:
            return
        try:
            binding = self.service.bind_window(title)
        except Exception as error:
            messagebox.showerror("窗口绑定失败", str(error))
            return
        self.image_path = None
        self.binding_status.config(text=f"已绑定真实窗口：{binding.title} ({binding.rect.width}×{binding.rect.height})")
        self.status.config(text="网页已绑定；开始后会逐项填写并自动回读，异常立即停止。")
        self._update_auto_state()

    def choose_image(self) -> None:
        path = filedialog.askopenfilename(title="选择现场截图", filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp")])
        if path:
            self.service.binding = None
            self.image_path = Path(path)
            self.binding_status.config(text="使用现场截图；未绑定真实浏览器窗口")
            self.status.config(text=f"只识别/单步现场截图：{path}")
            self._update_auto_state()

    def choose_actions(self) -> None:
        path = filedialog.askopenfilename(title="选择显式单步动作 JSON", filetypes=[("JSON", "*.json")])
        if path:
            self.actions_path = Path(path)
            self.status.config(text=f"已选择动作文件：{path}")

    def choose_diagnostics(self) -> None:
        path = filedialog.askdirectory(title="选择本地诊断目录")
        if path:
            self.diagnostics_dir = Path(path)
            self.status.config(text=f"诊断目录：{path}（默认脱敏）")

    def toggle_advanced(self) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced_frame.pack(fill=tk.X, pady=(0, 8), after=self.advanced_toggle)
            self.advanced_toggle.config(text="收起高级设置")
        else:
            self.advanced_frame.pack_forget()
            self.advanced_toggle.config(text="展开高级设置")

    def start_auto_update(self) -> None:
        try:
            draft = self._current_draft(required=True)
            if len(self.drafts) != 1:
                raise M7Error("一键更新每次只能处理一份证书。")
            self._require_review(draft)
            if self.service.binding is None:
                raise M7Error("请先绑定已经打开的专利信息网页。")
            if not self.service.can_auto_update_bound(self.manual if self.manual.values else None):
                raise M7Error("当前真实页面 Profile 尚未完成六项基本信息及后续控件校准；请先逐项测试。")
        except Exception as error:
            messagebox.showerror("无法开始", str(error))
            return
        self.stop_event.clear()
        self._clear_live_log()
        self.auto_running = True
        self._update_auto_state()
        # Activate Edge on Tk's foreground/UI thread before starting the
        # worker. Windows may reject a later foreground request from a worker.
        self.master.update_idletasks()
        try:
            self.service.binder.activate(self.service.binding)
            if not self.service.binder.is_foreground(self.service.binding):
                raise M7Error("目标浏览器窗口未能切换到前台，已停止。")
        except Exception as error:
            self.auto_running = False
            self._update_auto_state()
            messagebox.showerror("无法激活目标网页", str(error))
            return
        self.progress_status.config(text="正在检查页面，请不要操作鼠标和键盘…")
        self.status.config(text="自动填写进行中；页面必须保持前台，可随时点击安全停止。")
        threading.Thread(target=self._run_auto_worker, args=(draft,), daemon=True).start()

    def _run_auto_worker(self, draft: CertificateDraft) -> None:
        def progress(_phase: str, current: int, total: int, message: str) -> None:
            suffix = f"（{current}/{total}）" if total else ""
            self.master.after(0, lambda: self.progress_status.config(text=message + suffix))

        def trace_callback(event: dict[str, object]) -> None:
            at = event.get("at", "")
            step = event.get("step", "")
            status = event.get("status", "")
            details = {
                key: value
                for key, value in event.items()
                if key not in {"seq", "at", "step", "status", "text", "raw_value"}
            }
            line = f"{at} [{step}] {status}"
            if details:
                line += " " + json.dumps(details, ensure_ascii=False, sort_keys=True)
            self.master.after(0, lambda line=line: self._append_live_log(line))

        try:
            report = self.service.run_auto_update(
                draft,
                self.manual if self.manual.values else None,
                diagnostics=self._diagnostic_file(draft),
                progress=progress,
                trace_callback=trace_callback,
            )
        except Exception as error:  # UI boundary
            self.master.after(0, lambda: self._finish_auto_error(str(error)))
            return
        self.master.after(0, lambda: self._finish_auto(report))

    def _finish_auto_error(self, message: str) -> None:
        self.auto_running = False
        self._update_auto_state()
        self.progress_status.config(text="已停止")
        self.status.config(text=f"自动填写未完成：{message}")
        self._append_live_log(f"UI error: {message}")
        messagebox.showerror("自动填写未完成", message)

    def _finish_auto(self, report: M7RunReport) -> None:
        self.auto_running = False
        self.last_report = report
        self._show_report(report)
        self._append_live_log(f"finished status={report.status} phase={report.phase}")
        self._update_auto_state()
        if report.status == "completed" and report.verified:
            message = "填写完成，请检查网页内容后手动点击保存。"
            self.progress_status.config(text="填写完成，等待人工检查和保存")
            self.status.config(text=message)
            messagebox.showinfo("填写完成", message)
        else:
            reason = self._reason_text(report)
            self.progress_status.config(text="已安全停止，请处理提示后重试")
            self.status.config(text=f"自动填写已停止：{reason}")
            messagebox.showwarning("已安全停止", f"没有继续执行后续动作。\n{reason}")

    def _update_auto_state(self) -> None:
        ready = (
            not self.auto_running
            and len(self.drafts) == 1
            and review_draft(self.drafts[0]).approved
        )
        self.start_button.config(state=tk.NORMAL if ready else tk.DISABLED)

    def run_current(self) -> None:
        self.stop_event.clear()
        mode = self.mode.get()
        if mode == M7Mode.CONTROLLED_BATCH.value:
            self.status.config(text="受控批量运行中；可点击急停，当前任务会在安全边界暂停。")
            threading.Thread(target=self._run_batch_worker, daemon=True).start()
            return
        try:
            if mode == M7Mode.SIMULATION.value:
                draft = self._current_draft(required=True)
                self._require_review(draft)
                report = self.service.run_simulation(
                    draft,
                    self.manual if self.manual.values else None,
                    diagnostics=self._diagnostic_file(draft),
                )
            elif mode == M7Mode.RECOGNITION_ONLY.value:
                report = self.service.run_recognition_only(
                    image=self.image_path,
                    annotated=self._annotated_path(),
                    diagnostics=self.diagnostics_dir,
                )
            elif mode == M7Mode.STEP.value:
                if self.actions_path is None:
                    raise M7Error("单步模式需要显式动作 JSON；保存、提交、返回和删除动作会被拒绝。")
                actions = load_actions(self.actions_path)
                report = self.service.run_step(
                    actions,
                    image=self.image_path,
                    confirm=lambda action: messagebox.askyesno(
                        "确认单步动作", f"确认执行 {action.control_id} ({action.kind})？\n动作后仍需人工回读。"
                    ),
                    diagnostics=self.diagnostics_dir,
                )
        except Exception as error:  # UI boundary: preserve a visible stop reason
            messagebox.showerror("运行未启动", str(error))
            return
        self.last_report = report
        self._show_report(report)
        self.status.config(text=f"{mode}：{report.status}；{self._reason_text(report)}")

    def _run_batch_worker(self) -> None:
        try:
            report = self.service.run_controlled_batch(
                self.manual if self.manual.values else None,
                diagnostics=self.diagnostics_dir,
            )
        except Exception as error:  # UI boundary: return failures to the Tk thread
            self.master.after(0, lambda: messagebox.showerror("批量运行失败", str(error)))
            self.master.after(0, lambda: self.status.config(text=f"controlled_batch 启动失败：{error}"))
            return

        def finish() -> None:
            self.last_report = report
            self._show_report(report)
            self.status.config(text=f"controlled_batch：{report.status}；{self._reason_text(report)}")

        self.master.after(0, finish)

    def _require_review(self, draft: CertificateDraft) -> None:
        review = review_draft(draft)
        if not review.approved:
            raise M7Error("证书草稿仍有待复核字段：" + ", ".join(review.issues))

    def _current_draft(self, *, required: bool) -> CertificateDraft | None:
        if self.current_index is None and self.drafts:
            self.current_index = 0
        if self.current_index is None:
            if required:
                raise M7Error("请先导入 PDF 或 JSON 草稿。")
            return None
        return self.drafts[self.current_index]

    def _diagnostic_file(self, draft: CertificateDraft) -> Path | None:
        if self.diagnostics_dir is None:
            return None
        return self.diagnostics_dir / f"sample-{draft.sample_index or 1:03d}.json"

    def _annotated_path(self) -> Path | None:
        if self.diagnostics_dir is None:
            return None
        return self.diagnostics_dir / "field.annotated.png"

    def emergency_stop(self) -> None:
        self.stop_event.set()
        try:
            self.service.cancel_dom_task()
        except Exception:
            # The in-page extension has its own stop button.  A local store
            # failure must not prevent the legacy screen stop event.
            pass
        self.progress_status.config(text="正在安全停止…")
        self.status.config(text="已触发安全停止；扩展或屏幕执行器在当前字段边界停止。")

    def enqueue_current(self) -> None:
        draft = self._current_draft(required=False)
        if draft is None or not draft.source_file:
            messagebox.showinfo("没有可入队任务", "请先导入带有源文件路径的 PDF/JSON 草稿。")
            return
        try:
            tasks = self.service.enqueue([draft.source_file])
        except Exception as error:
            messagebox.showerror("入队失败", str(error))
            return
        self.status.config(text=f"已加入队列：{tasks[0].task_id}")

    def enqueue_all(self) -> None:
        sources = [draft.source_file for draft in self.drafts if draft.source_file]
        if not sources:
            messagebox.showinfo("没有可入队任务", "请先导入 PDF/JSON 草稿。")
            return
        try:
            tasks = self.service.enqueue(sources)
        except Exception as error:
            messagebox.showerror("入队失败", str(error))
            return
        self.status.config(text=f"已加入 {len(tasks)} 个任务；受控批量会在首个失败处停机。")

    def show_queue(self) -> None:
        self._show_json({"format": "m7-queue-view-v1", "tasks": self.service.queue_snapshot()})

    def recover_queue(self) -> None:
        try:
            count = self.service.recover_queue()
        except Exception as error:
            messagebox.showerror("恢复失败", str(error))
            return
        self.status.config(text=f"已恢复 {count} 个异常退出的 running 任务为 paused；不会静默重跑。")
        self.show_queue()

    def retry_task(self) -> None:
        task_id = simpledialog.askstring("重试任务", "输入 task_id：")
        if not task_id:
            return
        try:
            task = self.service.retry_task(task_id)
        except Exception as error:
            messagebox.showerror("重试失败", str(error))
            return
        self.status.config(text=f"已显式重试：{task.task_id}；请重新运行受控批量。")
        self.show_queue()

    def pause_task(self) -> None:
        task_id = simpledialog.askstring("暂停任务", "输入 running task_id：")
        if not task_id:
            return
        try:
            task = self.service.pause_task(task_id)
        except Exception as error:
            messagebox.showerror("暂停失败", str(error))
            return
        self.status.config(text=f"已暂停：{task.task_id}；请检查 checkpoint 和诊断后再重试。")
        self.show_queue()

    def install_profile(self) -> None:
        path = filedialog.askopenfilename(title="选择待安装的页面 profile", filetypes=[("JSON", "*.json")])
        if not path:
            return
        activate = messagebox.askyesno("激活 Profile", "安装后立即激活这个 profile？\n建议先只识别和短名单回归。")
        try:
            version = self.service.install_profile(path, activate=activate)
        except Exception as error:
            messagebox.showerror("Profile 安装失败", str(error))
            return
        self.status.config(text=f"Profile {version} 已安装；激活={activate}。")
        self._show_json(self.service.configuration_snapshot())

    def rollback_profile(self) -> None:
        version = simpledialog.askstring("回滚 Profile", "输入目标版本；留空回滚到历史版本：")
        try:
            selected = self.service.rollback_profile(version or None)
        except Exception as error:
            messagebox.showerror("Profile 回滚失败", str(error))
            return
        self.status.config(text=f"Profile 已回滚到 {selected}；请重新执行只识别。")
        self._show_json(self.service.configuration_snapshot())

    def save_config_version(self) -> None:
        if not self.manual.values:
            messagebox.showinfo("没有配置", "请先加载人工/配置 JSON。")
            return
        version = simpledialog.askstring("保存配置版本", "输入配置版本号：")
        if not version:
            return
        try:
            target = self.service.save_manual_config(version, self.manual, activate=True)
        except Exception as error:
            messagebox.showerror("配置保存失败", str(error))
            return
        self.status.config(text=f"人工配置版本已保存并激活：{target}")
        self._show_json(self.service.configuration_snapshot())

    def rollback_config(self) -> None:
        version = simpledialog.askstring("回滚人工配置", "输入目标版本；留空回滚到历史版本：")
        try:
            values = self.service.rollback_manual_config(version or None)
            self.manual = ManualFields.from_mapping(values)
        except Exception as error:
            messagebox.showerror("配置回滚失败", str(error))
            return
        self.status.config(text="人工配置已回滚并载入当前任务；请重新检查敏感字段。")
        self._show_json(self.service.configuration_snapshot())

    def export_report(self) -> None:
        if self.last_report is None:
            messagebox.showinfo("没有报告", "请先运行一个模式。")
            return
        path = filedialog.asksaveasfilename(title="导出 M7 报告", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            Path(path).write_text(json.dumps(self.last_report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.status.config(text=f"M7 报告已导出：{path}")

    def _refresh_tasks(self) -> None:
        self.task_list.delete(0, tk.END)
        for draft in self.drafts:
            state = "待复核" if draft.needs_review else "已确认"
            self.task_list.insert(tk.END, f"[{state}] {Path(draft.source_file).name or '草稿'}")
        if self.drafts:
            self.task_list.selection_set(0)
            self._show_draft(0)

    def _select_draft(self, _event: object) -> None:
        selection = self.task_list.curselection()
        if selection:
            self._show_draft(selection[0])

    def _show_draft(self, index: int) -> None:
        self.current_index = index
        self._fill_probe_value()
        self.current_field = None
        self.editor.delete("1.0", tk.END)
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
        selected = self.fields.selection()
        draft = self._current_draft(required=False)
        if not selected or draft is None:
            return
        self.current_field = selected[0]
        value = getattr(draft, self.current_field)
        display = "\n".join(value) if isinstance(value, list) else (value or "")
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", display)
        self.editor_label.config(text=f"当前字段：{self.current_field}")

    def apply_edit(self) -> None:
        draft = self._current_draft(required=False)
        if draft is None or self.current_field is None:
            messagebox.showinfo("尚未选择字段", "请先选择一份草稿和一个字段。")
            return
        raw = self.editor.get("1.0", "end-1c").strip()
        value: object = [line.strip() for line in raw.splitlines() if line.strip()] if self.current_field in LIST_FIELDS else raw
        if not value:
            messagebox.showwarning("值不能为空", "清空字段不会视为人工确认。")
            return
        setattr(draft, self.current_field, value)
        if self.current_field in draft.needs_review:
            draft.needs_review.remove(self.current_field)
        draft.notes.append(f"{self.current_field}_manually_confirmed")
        field = self.current_field
        self._show_draft(self.current_index or 0)
        self.fields.selection_set(field)
        review = review_draft(draft)
        self.source_status.config(
            text="字段已全部确认，可开始更新" if review.approved else "仍需修正：" + ", ".join(review.issues)
        )
        self.status.config(text=f"已人工确认：{field}")
        self._update_auto_state()

    def _show_report(self, report: M7RunReport) -> None:
        self._show_json(report.to_dict())

    def _show_json(self, value: object) -> None:
        self.report_text.config(state=tk.NORMAL)
        self.report_text.delete("1.0", tk.END)
        self.report_text.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
        self.report_text.config(state=tk.DISABLED)

    def _clear_live_log(self) -> None:
        self.live_log_text.config(state=tk.NORMAL)
        self.live_log_text.delete("1.0", tk.END)
        self.live_log_text.insert(tk.END, "等待运行事件……\n")
        self.live_log_text.config(state=tk.DISABLED)

    def _append_live_log(self, line: str) -> None:
        self.live_log_text.config(state=tk.NORMAL)
        self.live_log_text.insert(tk.END, line.rstrip() + "\n")
        self.live_log_text.see(tk.END)
        self.live_log_text.config(state=tk.DISABLED)

    @staticmethod
    def _reason_text(report: M7RunReport) -> str:
        if not report.safety_reasons:
            return "无安全停机原因"
        return "; ".join(item.code for item in report.safety_reasons[:3])


def launch() -> None:
    root = tk.Tk()
    M7ToolApp(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


__all__ = ["M7ToolApp", "launch"]
