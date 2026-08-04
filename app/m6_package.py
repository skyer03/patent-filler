"""Build the offline M6 installation and operations package."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_FILES = (
    "README.md",
    "PROJECT_PLAN.md",
    "M6_OPERATIONS.md",
    "requirements.txt",
    "app/__init__.py",
    "app/__main__.py",
    "app/cli.py",
    "app/certificate.py",
    "app/domain.py",
    "app/jsonio.py",
    "app/m4.py",
    "app/m5.py",
    "app/m6.py",
    "app/m6_package.py",
    "app/automation/__init__.py",
    "app/automation/engine.py",
    "app/automation/modes.py",
    "app/automation/profile.py",
    "app/automation/recognizer.py",
    "app/automation/window.py",
    "resources/web_profiles/intranet_v1.json",
    "resources/image_templates/intranet_v1/README.md",
    "resources/image_templates/intranet_v1/manifest.json",
)


def _install_readme() -> str:
    return """# M6 离线安装包

本包用于 M6 阶段的本地队列、断点续填、profile/配置版本和诊断脱敏。安装程序不访问网络，也不会自动点击保存、提交、返回或删除。

## 首次安装

1. 由管理员将审核过的离线 wheel 放入 `vendor/`。
2. 在 Windows 终端执行 `install/install.ps1`。
3. 安装后先用 `python -m unittest discover -s tests -v`（若包内包含测试）或现场预置的验收样本做离线冒烟测试。
4. 首次现场运行先选择只识别；用户确认 profile 和页面锚点后才进行单步操作。

## 升级与回滚

升级前先备份 `.m6` 数据目录。复制新版本文件后执行 `install/upgrade.ps1`；若 profile 或人工配置验收失败，使用 M6 页面 profile/配置仓库的显式 rollback，再恢复队列 checkpoint。
"""


def _install_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $PackageRoot ".venv"
if (-not (Test-Path $VenvPath)) {
    python -m venv $VenvPath
}
& (Join-Path $VenvPath "Scripts\python.exe") -m pip install --no-index --find-links (Join-Path $PackageRoot "vendor") -r (Join-Path $PackageRoot "requirements.txt")
Write-Host "M6 离线包已安装到 $VenvPath"
'''


def _upgrade_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $PackageRoot ".venv"
if (-not (Test-Path $VenvPath)) {
    throw "未找到现有 .venv；请先执行 install.ps1。"
}
& (Join-Path $VenvPath "Scripts\python.exe") -m pip install --no-index --find-links (Join-Path $PackageRoot "vendor") -r (Join-Path $PackageRoot "requirements.txt")
Write-Host "M6 依赖已按包内 requirements.txt 完成离线升级；请按 M6_OPERATIONS.md 做 profile/配置兼容性检查。"
'''


def _queue_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0"
"%ROOT%.venv\Scripts\python.exe" -m app m6 list --queue "%ROOT%.m6\queue.json"
endlocal
'''


def build_m6_package(output: str | Path, root: str | Path | None = None) -> Path:
    project_root = Path(root) if root else Path(__file__).resolve().parents[1]
    project_root = project_root.resolve()
    target = Path(output)
    if not target.is_absolute():
        target = project_root / target
    target.parent.mkdir(parents=True, exist_ok=True)
    missing = [item for item in PACKAGE_FILES if not (project_root / item).is_file()]
    if missing:
        raise FileNotFoundError(f"M6 安装包缺少文件：{', '.join(missing)}")

    manifest = {
        "format": "m6-offline-package-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "network_access": "not_required_by_installer",
        "queue_state": "local_json_with_atomic_writes",
        "safety": {
            "stop_on_unsafe_result": True,
            "save_submit_return_delete": "blocked",
            "sensitive_exports": "redacted_by_default",
        },
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("M6_PACKAGE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("install/README.md", _install_readme())
        archive.writestr("install/install.ps1", _install_ps1())
        archive.writestr("install/upgrade.ps1", _upgrade_ps1())
        archive.writestr("install/show_queue.cmd", _queue_cmd())
        archive.writestr(
            "vendor/README.md",
            "将经管理员审核的离线 wheel 放在此目录；安装脚本使用 --no-index，不会联网下载。\n",
        )
        for relative in PACKAGE_FILES:
            archive.write(project_root / relative, relative)
    return target


__all__ = ["build_m6_package"]
