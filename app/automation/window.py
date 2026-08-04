"""Foreground-window binding and screenshot capture for Windows."""

from __future__ import annotations

import ctypes
import os
import time
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

    def bind_by_title(self, title: str) -> WindowBinding:
        if os.name != "nt":
            raise WindowBindingError("窗口绑定工具仅支持 Windows 桌面窗口。")
        if not title.strip():
            raise WindowBindingError("窗口标题不能为空。")
        matches: list[tuple[int, str]] = []
        user32 = ctypes.windll.user32
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @enum_proc_type
        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            window_title = buffer.value
            if title.casefold() in window_title.casefold():
                matches.append((int(hwnd), window_title))
            return True

        user32.EnumWindows(enum_proc, 0)
        if not matches:
            raise WindowBindingError(f"未找到包含标题的窗口：{title}")
        if len(matches) > 1:
            raise AmbiguousWindowError("匹配到多个窗口，请把标题填写得更具体。")
        hwnd, window_title = matches[0]
        rect = self._rect_provider(hwnd)
        return WindowBinding(hwnd, window_title, rect, datetime.now(timezone.utc).isoformat())

    def refresh(self, binding: WindowBinding) -> WindowBinding:
        rect = self._rect_provider(binding.hwnd)
        return WindowBinding(binding.hwnd, binding.title, rect, binding.bound_at)

    def capture(self, binding: WindowBinding):
        """Capture the current bound window, not the rectangle from bind time.

        The target is brought to the foreground immediately before capture so
        another visible window cannot cover part of the bound rectangle.
        """

        if os.name != "nt":
            raise WindowBindingError("窗口截图仅支持 Windows 桌面窗口。")
        from PIL import ImageGrab

        current = self.refresh(binding)
        binding.rect = current.rect
        if current.rect.width <= 0 or current.rect.height <= 0:
            raise WindowBindingError("目标窗口没有可截图的有效区域。")
        self.activate(binding)
        time.sleep(0.15)
        return ImageGrab.grab(bbox=(current.rect.left, current.rect.top, current.rect.right, current.rect.bottom))

    @staticmethod
    def activate(binding: WindowBinding) -> None:
        """Bring a bound, visible Windows window to the foreground."""

        if os.name != "nt":
            raise WindowBindingError("窗口激活仅支持 Windows 桌面窗口。")
        user32 = ctypes.windll.user32
        if not user32.IsWindow(binding.hwnd):
            raise WindowBindingError("绑定的窗口已经不存在，请重新绑定。")
        # SW_RESTORE also brings a minimized browser back before capture.
        user32.ShowWindow(binding.hwnd, 9)
        if not user32.SetForegroundWindow(binding.hwnd):
            raise WindowBindingError("无法将绑定窗口置于前台，请先手动激活该窗口。")

    @staticmethod
    def is_foreground(binding: WindowBinding) -> bool:
        """Return whether the bound window is still the foreground window."""

        if os.name != "nt":
            return True
        user32 = ctypes.windll.user32
        if not user32.IsWindow(binding.hwnd) or not user32.IsWindowVisible(binding.hwnd):
            return False
        return int(user32.GetForegroundWindow()) == int(binding.hwnd)

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
