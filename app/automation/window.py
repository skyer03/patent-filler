"""Foreground-window binding and screenshot capture for Windows."""

from __future__ import annotations

import ctypes
import os
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


class WindowBindingError(RuntimeError):
    pass


class AmbiguousWindowError(WindowBindingError):
    pass


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }


@dataclass
class WindowBinding:
    hwnd: int
    title: str
    rect: WindowRect
    bound_at: str

    def to_dict(self) -> dict:
        return {"hwnd": self.hwnd, "title": self.title, "rect": self.rect.to_dict(), "bound_at": self.bound_at}


class WindowBinder:
    """Bind by window identity, refreshing its rectangle before each capture."""

    def __init__(self, rect_provider: Callable[[int], WindowRect] | None = None) -> None:
        self._rect_provider = rect_provider or self._get_rect

    @staticmethod
    def normalize_title(title: str) -> str:
        """Normalize titles copied from Edge, including zero-width characters."""

        value = unicodedata.normalize("NFKC", str(title))
        value = "".join(char for char in value if char not in "\u200b\u200c\u200d\ufeff")
        return " ".join(value.split()).casefold()

    @classmethod
    def title_matches(cls, query: str, window_title: str) -> bool:
        normalized_query = cls.normalize_title(query)
        return bool(normalized_query) and normalized_query in cls.normalize_title(window_title)

    @classmethod
    def is_browser_title(cls, title: str) -> bool:
        normalized = cls.normalize_title(title)
        return "microsoft edge" in normalized or "google chrome" in normalized

    def list_visible_windows(self, *, browser_only: bool = False) -> list[tuple[int, str]]:
        """Return visible top-level windows for UI-assisted binding."""

        if os.name != "nt":
            return []
        windows: list[tuple[int, str]] = []
        user32 = ctypes.windll.user32
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @enum_proc_type
        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            window_title = buffer.value.strip()
            if window_title and (not browser_only or self.is_browser_title(window_title)):
                windows.append((int(hwnd), window_title))
            return True

        user32.EnumWindows(enum_proc, 0)
        return windows

    def bind_by_title(self, title: str) -> WindowBinding:
        if os.name != "nt":
            raise WindowBindingError("窗口绑定工具仅支持 Windows 桌面窗口。")
        if not self.normalize_title(title):
            raise WindowBindingError("窗口标题不能为空。")
        matches = [
            (hwnd, window_title)
            for hwnd, window_title in self.list_visible_windows()
            if self.title_matches(title, window_title)
        ]
        if not matches:
            raise WindowBindingError(f"未找到包含标题的窗口：{title}")
        if len(matches) > 1:
            candidates = "；".join(window_title for _hwnd, window_title in matches[:5])
            suffix = "……" if len(matches) > 5 else ""
            raise AmbiguousWindowError(f"匹配到多个窗口，请把标题填写得更具体。候选：{candidates}{suffix}")
        hwnd, window_title = matches[0]
        rect = self._rect_provider(hwnd)
        return WindowBinding(hwnd, window_title, rect, datetime.now(timezone.utc).isoformat())

    def refresh(self, binding: WindowBinding) -> WindowBinding:
        rect = self._rect_provider(binding.hwnd)
        return WindowBinding(binding.hwnd, binding.title, rect, binding.bound_at)

    def capture(self, binding: WindowBinding, *, activate: bool = True):
        """Capture the current bound window, not the rectangle from bind time.

        By default the target is brought to the foreground immediately before
        capture so another visible window cannot cover part of the bound
        rectangle. Callers that enforce foreground focus separately can set
        ``activate=False`` to avoid silently recovering from focus loss.
        """

        if os.name != "nt":
            raise WindowBindingError("窗口截图仅支持 Windows 桌面窗口。")
        from PIL import ImageGrab

        current = self.refresh(binding)
        binding.rect = current.rect
        if current.rect.width <= 0 or current.rect.height <= 0:
            raise WindowBindingError("目标窗口没有可截图的有效区域。")
        if activate:
            self.activate(binding)
        time.sleep(0.15)
        return ImageGrab.grab(bbox=(current.rect.left, current.rect.top, current.rect.right, current.rect.bottom))

    @staticmethod
    def activate(binding: WindowBinding) -> None:
        """Bring a bound, visible Windows window to the foreground.

        ``SetForegroundWindow`` may return before the foreground window has
        actually changed, so wait briefly before reporting activation failure.
        """

        if os.name != "nt":
            raise WindowBindingError("窗口激活仅支持 Windows 桌面窗口。")
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        if not user32.IsWindow(binding.hwnd):
            raise WindowBindingError("绑定的窗口已经不存在，请重新绑定。")

        # Windows can reject a foreground request issued by a thread whose
        # input queue differs from the current foreground window.  Temporarily
        # attach the queues for the activation handoff, then detach them
        # before returning so browser and Tk input remain independent.
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        target_thread = user32.GetWindowThreadProcessId(binding.hwnd, None)
        attached_threads: list[int] = []
        for thread_id in (foreground_thread, target_thread):
            if thread_id and thread_id != current_thread and thread_id not in attached_threads:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append(int(thread_id))

        # SW_RESTORE also brings a minimized browser back before capture.
        try:
            user32.ShowWindow(binding.hwnd, 9)
            user32.BringWindowToTop(binding.hwnd)
            user32.SetForegroundWindow(binding.hwnd)
            user32.SetActiveWindow(binding.hwnd)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if WindowBinder.is_foreground(binding):
                    return
                time.sleep(0.03)
        finally:
            for thread_id in reversed(attached_threads):
                user32.AttachThreadInput(current_thread, thread_id, False)
        raise WindowBindingError("无法确认绑定窗口已置于前台，请先手动激活该窗口。")

    @staticmethod
    def is_foreground(binding: WindowBinding) -> bool:
        """Return whether the bound window is still the foreground window."""

        return bool(WindowBinder.foreground_state(binding)["is_foreground"])

    @staticmethod
    def foreground_state(binding: WindowBinding) -> dict[str, object]:
        """Return read-only foreground diagnostics for a bound window."""

        if os.name != "nt":
            return {
                "is_foreground": True,
                "target_hwnd": int(binding.hwnd),
                "target_valid": True,
                "target_visible": True,
                "target_root": int(binding.hwnd),
                "foreground_hwnd": int(binding.hwnd),
                "foreground_root": int(binding.hwnd),
                "foreground_title": binding.title,
            }
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        target_valid = bool(user32.IsWindow(binding.hwnd))
        target_visible = bool(target_valid and user32.IsWindowVisible(binding.hwnd))
        target_root = user32.GetAncestor(binding.hwnd, 2) or binding.hwnd
        foreground = user32.GetForegroundWindow()
        foreground_root = user32.GetAncestor(foreground, 2) if foreground else 0
        buffer = ctypes.create_unicode_buffer(512)
        if foreground:
            user32.GetWindowTextW(foreground, buffer, len(buffer))
        same_window = bool(foreground and int(foreground) == int(binding.hwnd))
        same_root = bool(foreground_root and int(foreground_root) == int(target_root))
        return {
            "is_foreground": target_visible and (same_window or same_root),
            "target_hwnd": int(binding.hwnd),
            "target_valid": target_valid,
            "target_visible": target_visible,
            "target_root": int(target_root),
            "foreground_hwnd": int(foreground or 0),
            "foreground_root": int(foreground_root or 0),
            "foreground_title": buffer.value,
        }

    @staticmethod
    def screen_point(binding: WindowBinding, local_x: int, local_y: int) -> tuple[int, int]:
        """Translate a fresh local screenshot coordinate into screen space."""

        return binding.rect.left + local_x, binding.rect.top + local_y

    @staticmethod
    def _get_rect(hwnd: int) -> WindowRect:
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise WindowBindingError("无法读取目标窗口位置。")
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)
