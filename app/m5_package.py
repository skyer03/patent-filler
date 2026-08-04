"""Build the reproducible M5 field package."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_FILES = (
    "README.md",
    "PROJECT_PLAN.md",
    "requirements.txt",
    "m5/FIELD_ACCEPTANCE_CHECKLIST.md",
    "app/__init__.py",
    "app/__main__.py",
    "app/cli.py",
    "app/m5.py",
    "app/m5_package.py",
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
    return """# M5 现场包

这是只依赖本地文件的现场源安装包。它不连接公司内网、不注入网页脚本，也不会自动点击保存、提交、返回或删除。

## 首次安装

1. 在有权限的 Windows 终端执行 `install.ps1`；脚本只使用包内的 `vendor` 目录，不访问网络。
2. 如果现场没有预置依赖 wheel，请由管理员把与 `requirements.txt` 匹配的 wheel 放入 `vendor` 后再安装。
3. 首次进入内网后，先执行只识别：

   `run_recognition.cmd C:\\path\\field.png`

4. 审核标注截图和 JSON 后，再准备显式的 `actions.json` 做单步运行。没有明确授权时不要使用 `--yes`。

`resources/image_templates/intranet_v1` 当前没有伪造的像素模板；现场审核后的稳定控件小截图应按 manifest 登记后再放入该目录。
"""


def _install_ps1() -> str:
    return r'''$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $PackageRoot ".venv"
if (-not (Test-Path $VenvPath)) {
    python -m venv $VenvPath
}
& (Join-Path $VenvPath "Scripts\python.exe") -m pip install --no-index --find-links (Join-Path $PackageRoot "vendor") -r (Join-Path $PackageRoot "requirements.txt")
Write-Host "M5 现场包已安装到 $VenvPath"
'''


def _recognition_cmd() -> str:
    return r'''@echo off
setlocal
set "ROOT=%~dp0"
"%ROOT%.venv\Scripts\python.exe" -m app m5 --mode recognition_only --profile "%ROOT%resources\web_profiles\intranet_v1.json" --templates "%ROOT%resources\image_templates\intranet_v1" --image "%~1" --annotated "%ROOT%field.annotated.png" --output "%ROOT%field.report.json"
endlocal
'''


def build_m5_package(output: str | Path, root: str | Path | None = None) -> Path:
    project_root = Path(root) if root else Path(__file__).resolve().parents[1]
    project_root = project_root.resolve()
    target = Path(output)
    if not target.is_absolute():
        target = project_root / target
    target.parent.mkdir(parents=True, exist_ok=True)
    missing = [item for item in PACKAGE_FILES if not (project_root / item).is_file()]
    if missing:
        raise FileNotFoundError(f"M5 安装包缺少文件：{', '.join(missing)}")

    manifest = {
        "format": "m5-field-package-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": "resources/web_profiles/intranet_v1.json",
        "template_manifest": "resources/image_templates/intranet_v1/manifest.json",
        "offline_dependencies": False,
        "network_access": "not_required_by_installer",
        "safety": {
            "recognition_only_default": True,
            "save_submit_return_delete": "blocked",
            "sensitive_values_in_logs": "redacted",
        },
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("M5_PACKAGE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("install/README.md", _install_readme())
        archive.writestr("install/install.ps1", _install_ps1())
        archive.writestr("install/run_recognition.cmd", _recognition_cmd())
        for relative in PACKAGE_FILES:
            archive.write(project_root / relative, relative)
        archive.writestr(
            "vendor/README.md",
            "将经管理员审核的离线 wheel 放在此目录；安装脚本使用 --no-index，不会联网下载。\n",
        )
    return target


__all__ = ["build_m5_package"]
