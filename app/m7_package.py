"""Build the final offline M7 installation and acceptance package."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_FILES = (
    "README.md",
    "start_m7.cmd",
    "start_mock_site.cmd",
    "PROJECT_PLAN.md",
    "M7_OPERATIONS.md",
    "M7_FIELD_ACCEPTANCE_RECORD.md",
    "EDGE_EXTENSION_OPERATIONS.md",
    "requirements.txt",
    "PROJECT_PLAN_M2_PROFILE.json",
    "app/__init__.py",
    "app/__main__.py",
    "app/cli.py",
    "app/certificate.py",
    "app/domain.py",
    "app/dom_bridge.py",
    "app/jsonio.py",
    "app/ocr.py",
    "app/m4.py",
    "app/m5.py",
    "app/m6.py",
    "app/m7.py",
    "app/m7_ui.py",
    "app/m7_package.py",
    "app/version.py",
    "app/mock_server.py",
    "app/ui.py",
    "app/automation/__init__.py",
    "app/automation/engine.py",
    "app/automation/modes.py",
    "app/automation/profile.py",
    "app/automation/recognizer.py",
    "app/automation/screen_adapter.py",
    "app/automation/window.py",
    "mock_site/index.html",
    "resources/web_profiles/intranet_v1.json",
    "resources/web_profiles/intranet_actual_v1.json",
    "resources/image_templates/intranet_v1/README.md",
    "resources/image_templates/intranet_v1/manifest.json",
    "edge_extension/manifest.json",
    "edge_extension/service_worker.js",
    "edge_extension/content.js",
    "edge_extension/popup.html",
    "edge_extension/popup.css",
    "edge_extension/popup.js",
    "edge_extension/README.md",
    "edge_extension/profiles/dom_profile.json",
    "edge_extension/install/native_host_launcher.cs",
    "edge_extension/install/configure_enterprise_extension.ps1",
    "edge_extension/install/register_native_host.ps1",
    "edge_extension/install/unregister_native_host.ps1",
    "tests/test_m7.py",
    "tests/test_dom_bridge.py",
    "tests/test_paddle_ocr.py",
    "tests/test_screen_adapter.py",
)


def _files(project_root: Path) -> list[Path]:
    files = [project_root / item for item in PACKAGE_FILES]
    files.extend(sorted((project_root / "resources" / "ocr_models" / "paddle").rglob("inference.*")))
    files.extend(sorted((project_root / "m0" / "golden").glob("*.json")))
    files.extend(sorted(item for item in (project_root / ".runtime" / "python312-full").rglob("*") if item.is_file()))
    files.extend(sorted(item for item in (project_root / ".runtime" / "paddle_cache").rglob("*") if item.is_file()))
    return files


def _install_readme() -> str:
    return """# M7 统一离线安装包

M7 的启动入口是 `python -m app`，也可以双击 `install/start_m7.cmd`。真实网页默认使用 `edge_extension/` 中的企业 Edge DOM 扩展；屏幕 OCR/键鼠执行器仅保留为高级诊断。程序默认停在保存前，不会自动点击保存、提交、返回或删除，也不会自动创建下一条记录。

首次安装：

1. 将管理员审核过的离线 wheel 放入 `vendor/`。
2. 在 Windows PowerShell 执行 `install/install.ps1`。
3. 先执行 `install/run_m7_tests.cmd` 和 `install/run_m7_golden.cmd`。
4. 按 `EDGE_EXTENSION_OPERATIONS.md` 生成精确 Origin 扩展、加载扩展并注册当前用户本机组件。
5. 打开目标网页，在 M7 首页上传证书、人工校对并准备扩展任务，再从 Edge 扩展开始预填。

真实网页识别使用随包提供的 PaddleOCR CPU 运行时和本地中文模型；缺失时一键更新会安全停止，不会发送输入。

升级前先按 `M7_OPERATIONS.md` 备份 `.m6/`；升级失败时保留旧目录并恢复备份，不要覆盖正在使用的运行状态目录。
"""


def _install_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $PackageRoot ".venv"
if (-not (Test-Path $VenvPath)) {
    python -m venv $VenvPath
}
& (Join-Path $VenvPath "Scripts\python.exe") -m pip install --no-index --find-links (Join-Path $PackageRoot "vendor") -r (Join-Path $PackageRoot "requirements.txt")
Write-Host "M7 离线包已安装到 $VenvPath"
'''


def _upgrade_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $PackageRoot ".venv"
if (-not (Test-Path $VenvPath)) {
    throw "未找到现有 .venv；请先执行 install.ps1。"
}
& (Join-Path $VenvPath "Scripts\python.exe") -m pip install --no-index --find-links (Join-Path $PackageRoot "vendor") -r (Join-Path $PackageRoot "requirements.txt")
Write-Host "M7 依赖已按包内 requirements.txt 完成离线升级；请先运行离线测试和只识别。"
'''


def _start_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\.runtime\python312-full\python.exe" (
  set "PYTHON=%ROOT%\.runtime\python312-full\python.exe"
) else (
  set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
)
"%PYTHON%" -m app
endlocal
'''


def _tests_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0.."
"%ROOT%\.venv\Scripts\python.exe" -m unittest discover -s "%ROOT%\tests" -v
endlocal
'''


def _golden_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0.."
if not exist "%ROOT%\.m6" mkdir "%ROOT%\.m6"
"%ROOT%\.venv\Scripts\python.exe" -m app m7 "%ROOT%\m0\golden" --headless --mode controlled_batch --queue "%ROOT%\.m6\golden-queue.json" --diagnostics "%ROOT%\.m6\golden-diagnostics"
endlocal
'''


def _install_readme() -> str:
    return """# M7 portable offline test package

This package includes a portable Python runtime, PaddleOCR dependencies,
local model cache, desktop application, Edge extension, and Native Messaging
installer. It is intended for a same-platform Windows x64 test computer and
does not require Python or network access on that computer.

1. Extract the whole package to a short path such as `C:\\PatentAutofillTest`.
2. Run `install\\start_m7.cmd`, or run `start_mock_site.cmd` for the local mock page.
3. Follow `EDGE_EXTENSION_OPERATIONS.md` to generate an exact-origin extension
   and register the current-user Native Messaging host.
4. Upload and review one certificate in M7, prepare the extension task, then
   use Edge to read and fill it. Saving remains manual.

The included runtime is preferred. `install\\install.ps1` only falls back to a
local Python/venv when the bundled runtime is unavailable.
"""


def _install_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledPython = Join-Path $PackageRoot ".runtime\python312-full\python.exe"
if (Test-Path $BundledPython) {
    Write-Host "Bundled Python runtime is ready: $BundledPython"
    exit 0
}
$VenvPath = Join-Path $PackageRoot ".venv"
if (-not (Test-Path $VenvPath)) { python -m venv $VenvPath }
& (Join-Path $VenvPath "Scripts\python.exe") -m pip install --no-index --find-links (Join-Path $PackageRoot "vendor") -r (Join-Path $PackageRoot "requirements.txt")
Write-Host "Fallback offline Python environment is ready: $VenvPath"
'''


def _tests_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\.runtime\python312-full\python.exe" (
  set "PYTHON=%ROOT%\.runtime\python312-full\python.exe"
) else (
  set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
)
"%PYTHON%" -m unittest discover -s "%ROOT%\tests" -v
endlocal
'''


def _golden_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0.."
if not exist "%ROOT%\.m6" mkdir "%ROOT%\.m6"
if exist "%ROOT%\.runtime\python312-full\python.exe" (
  set "PYTHON=%ROOT%\.runtime\python312-full\python.exe"
) else (
  set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
)
"%PYTHON%" -m app m7 "%ROOT%\m0\golden" --headless --mode controlled_batch --queue "%ROOT%\.m6\golden-queue.json" --diagnostics "%ROOT%\.m6\golden-diagnostics"
endlocal
'''


def build_m7_package(output: str | Path, root: str | Path | None = None) -> Path:
    project_root = (Path(root) if root else Path(__file__).resolve().parents[1]).resolve()
    target = Path(output)
    if not target.is_absolute():
        target = project_root / target
    target.parent.mkdir(parents=True, exist_ok=True)
    package_files = _files(project_root)
    runtime_marker = project_root / ".runtime" / "python312-full" / "python.exe"
    if not runtime_marker.is_file():
        raise FileNotFoundError("Portable Python runtime is missing: .runtime\\python312-full\\python.exe")
    missing = [str(item.relative_to(project_root)) for item in package_files if not item.is_file()]
    if missing:
        raise FileNotFoundError(f"M7 安装包缺少文件：{', '.join(missing)}")

    manifest = {
        "format": "m7-unified-offline-package-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entrypoint": "python -m app",
        "golden_samples": len([item for item in package_files if item.parent.name == "golden"]),
        "network_access": "not_required_by_installer",
        "portable_runtime": {
            "python": ".runtime/python312-full/python.exe",
            "paddle_cache": ".runtime/paddle_cache",
            "platform": "Windows x64",
        },
        "execution_boundaries": {
            "dom_extension": "single reviewed local task; exact-origin enterprise extension; unique DOM target and readback after every field; stop before save",
            "auto_update": "legacy advanced screen executor; single bound foreground page; stop before save",
            "simulation": "InMemoryPageAdapter",
            "recognition_only": "M5 screenshot recognition; no input",
            "step": "M5 foreground window executor with action-after-readback evidence",
            "controlled_batch": "M6 queue over offline reviewed drafts; stop_on_failure",
        },
        "safety": {
            "save_submit_return_delete": "manual_only",
            "auto_create_next_record": False,
            "non_certificate_batch_fill": False,
            "sensitive_exports": "redacted_by_default",
        },
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("M7_PACKAGE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("install/README.md", _install_readme())
        archive.writestr("install/install.ps1", _install_ps1())
        archive.writestr("install/upgrade.ps1", _upgrade_ps1())
        archive.writestr("install/start_m7.cmd", _start_cmd())
        archive.writestr("install/run_m7_tests.cmd", _tests_cmd())
        archive.writestr("install/run_m7_golden.cmd", _golden_cmd())
        archive.writestr(
            "vendor/README.md",
            "将经管理员审核的离线 wheel 放在此目录；安装脚本使用 --no-index，不会联网下载。\n",
        )
        for item in package_files:
            archive.write(item, item.relative_to(project_root))
    return target


__all__ = ["build_m7_package"]
