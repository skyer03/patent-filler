"""Offline M2 mock-site server."""

from __future__ import annotations

import functools
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MOCK_SITE_ROOT = Path(__file__).resolve().parents[1] / "mock_site"


class MockSiteServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, root: str | Path = MOCK_SITE_ROOT) -> None:
        self.host = host
        self.port = port
        self.root = Path(root).resolve()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            return f"http://{self.host}:{self.port}/"
        return f"http://{self.host}:{self._server.server_port}/"

    def start(self) -> str:
        if not (self.root / "index.html").exists():
            raise FileNotFoundError(f"模拟页不存在：{self.root / 'index.html'}")
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(self.root))
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="m2-mock-site", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def __enter__(self) -> "MockSiteServer":
        self.start()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.stop()


def launch_mock_site(port: int = 8765, open_browser: bool = False) -> None:
    server = MockSiteServer(port=port)
    print(f"M2 模拟页：{server.start()}")
    if open_browser:
        webbrowser.open(server.url)
    print("按 Ctrl+C 停止模拟页。")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        server.stop()
