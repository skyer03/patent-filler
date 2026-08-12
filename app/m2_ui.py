"""Tk-based screenshot, recognition, and window-binding tool for M2."""

from __future__ import annotations

import json
import tkinter as tk
import time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .automation import AnchorRecognizer, WindowBinder, load_profile
from .automation.recognizer import TemplateMatcher, annotate_image
from .mock_server import MockSiteServer


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "PROJECT_PLAN_M2_PROFILE.json"


class M2ToolApp(ttk.Frame):
    def __init__(self, master: tk.Tk, profile_path: str | Path = PROFILE_PATH) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.profile = load_profile(profile_path)
        self.binder = WindowBinder()
        self.binding = None
        self.server: MockSiteServer | None = None
        self.image: Image.Image | None = None
        self.image_tk: ImageTk.PhotoImage | None = None
        self.current_path: Path | None = None
        self._build()

    def _build(self) -> None:
        self.master.title("M2 截图识别与窗口绑定工具")
        self.master.minsize(1120, 720)
        self.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(toolbar, text="模式").pack(side=tk.LEFT)
        self.mode = ttk.Combobox(toolbar, state="readonly", values=("模拟模式", "只识别模式", "单步模式"), width=12)
        self.mode.current(0)
        self.mode.pack(side=tk.LEFT, padx=(5, 14))
        ttk.Button(toolbar, text="启动本地模拟页", command=self.start_mock).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="打开截图", command=self.open_image).pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="截取绑定窗口", command=self.capture_window).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="识别并画框", command=self.recognize).pack(side=tk.LEFT, padx=5)

        bind_row = ttk.Frame(self)
        bind_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(bind_row, text="窗口标题包含").pack(side=tk.LEFT)
        self.window_title = ttk.Entry(bind_row, width=42)
        self.window_title.insert(0, "专利信息库")
        self.window_title.pack(side=tk.LEFT, padx=5)
        ttk.Button(bind_row, text="绑定窗口", command=self.bind_window).pack(side=tk.LEFT)
        self.binding_status = ttk.Label(bind_row, text="未绑定窗口")
        self.binding_status.pack(side=tk.LEFT, padx=10)

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        image_frame = ttk.Frame(body, padding=(0, 0, 8, 0))
        result_frame = ttk.Frame(body)
        body.add(image_frame, weight=4)
        body.add(result_frame, weight=1)

        self.canvas = tk.Canvas(image_frame, background="#20252c", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._render())
        ttk.Label(result_frame, text="识别结果 / 安全状态").pack(anchor=tk.W)
        self.result = tk.Text(result_frame, width=42, height=30, wrap=tk.WORD, state=tk.DISABLED)
        self.result.pack(fill=tk.BOTH, expand=True)
        self.status = ttk.Label(self, text="请选择截图，或先启动模拟页并绑定浏览器窗口。")
        self.status.pack(anchor=tk.W, pady=(8, 0))

    def start_mock(self) -> None:
        if self.server is None:
            try:
                self.server = MockSiteServer()
                url = self.server.start()
            except OSError as error:
                messagebox.showerror("模拟页启动失败", str(error))
                self.server = None
                return
        import webbrowser

        webbrowser.open(self.server.url)
        self.status.config(text=f"模拟页已启动：{self.server.url}")

    def bind_window(self) -> None:
        try:
            self.binding = self.binder.bind_by_title(self.window_title.get())
        except Exception as error:
            messagebox.showerror("窗口绑定失败", str(error))
            return
        rect = self.binding.rect
        self.binding_status.config(text=f"已绑定：{self.binding.title} ({rect.width}×{rect.height})")
        self.status.config(text="已绑定窗口；截图时会刷新窗口位置，不使用固定屏幕坐标。")

    def capture_window(self) -> None:
        if self.binding is None:
            messagebox.showinfo("未绑定窗口", "请先绑定目标浏览器窗口。")
            return
        # The tool itself is a normal top-level window and can overlap the
        # browser.  Hide it before capture; WindowBinder.activate() then puts
        # the bound browser in front and the tool is restored afterwards.
        previous_state = self.master.state()
        self.master.withdraw()
        error: Exception | None = None
        image: Image.Image | None = None
        try:
            self.master.update_idletasks()
            self.master.update()
            time.sleep(0.25)
            image = self.binder.capture(self.binding)
        except Exception as caught:
            error = caught
        finally:
            self.master.deiconify()
            if previous_state == "zoomed":
                self.master.state("zoomed")
            self.master.lift()
            self.master.focus_force()
        if error is not None:
            messagebox.showerror("截图失败", str(error))
            return
        if image is None:
            messagebox.showerror("截图失败", "未得到目标窗口截图。")
            return
        path = filedialog.asksaveasfilename(
            title="保存窗口截图", defaultextension=".png", filetypes=[("PNG", "*.png")]
        )
        if not path:
            return
        image.save(path)
        self.image = image.convert("RGB")
        self.current_path = Path(path)
        self._render()
        self.status.config(text=f"已保存截图：{path}")

    def open_image(self) -> None:
        path = filedialog.askopenfilename(title="打开截图", filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.bmp")])
        if not path:
            return
        try:
            self.image = Image.open(path).convert("RGB")
        except OSError as error:
            messagebox.showerror("打开失败", str(error))
            return
        self.current_path = Path(path)
        self._render()
        self.status.config(text=f"已打开截图：{path}")

    def recognize(self) -> None:
        if self.image is None:
            messagebox.showinfo("没有截图", "请先打开截图或截取绑定窗口。")
            return
        result = AnchorRecognizer(self.profile).recognize_image(self.image)
        template_matches = TemplateMatcher().locate_directory(
            self.image, ROOT / "resources" / "image_templates"
        )
        result_data = result.to_dict()
        result_data["template_matches"] = [
            {"name": match.name, "box": match.box.to_dict(), "score": match.score}
            for match in template_matches
        ]
        self._write_result(result_data)
        self.image = annotate_image(self.image, result, template_matches)
        self._render()
        state = "允许识别后输入" if result.safe_for_input else "安全停机：不允许输入"
        if "ocr_unavailable" in result.issues:
            state += "；请使用随项目提供的 Python 运行时，并确认 PaddleOCR 本地模型已安装"
        self.status.config(text=f"{state}；方法：{', '.join(result.methods) or '无'}")

    def _write_result(self, value: dict) -> None:
        self.result.config(state=tk.NORMAL)
        self.result.delete("1.0", tk.END)
        self.result.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
        self.result.config(state=tk.DISABLED)

    def _render(self) -> None:
        self.canvas.delete("all")
        if self.image is None or self.canvas.winfo_width() < 2 or self.canvas.winfo_height() < 2:
            return
        width = max(1, self.canvas.winfo_width() - 8)
        height = max(1, self.canvas.winfo_height() - 8)
        scale = min(width / self.image.width, height / self.image.height, 1.0)
        shown = self.image.resize((int(self.image.width * scale), int(self.image.height * scale)))
        self.image_tk = ImageTk.PhotoImage(shown)
        self.canvas.create_image(4, 4, anchor=tk.NW, image=self.image_tk)


def launch(profile_path: str | Path = PROFILE_PATH) -> None:
    root = tk.Tk()
    app = M2ToolApp(root, profile_path)
    root.protocol("WM_DELETE_WINDOW", lambda: _close(root, app))
    root.mainloop()


def _close(root: tk.Tk, app: M2ToolApp) -> None:
    if app.server is not None:
        app.server.stop()
    root.destroy()
