"""Command-line entry points for parsing, export, and golden regression."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .certificate import CertificateParser, ParseError
from .ocr import OcrUnavailableError
from .domain import REQUIRED_FIELDS
from .jsonio import export_drafts, import_drafts
from .version import APP_VERSION


DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[1] / "PROJECT_PLAN_M2_PROFILE.json"
DEFAULT_M5_PROFILE_PATH = Path(__file__).resolve().parents[1] / "resources" / "web_profiles" / "intranet_v1.json"
DEFAULT_M5_TEMPLATES_PATH = Path(__file__).resolve().parents[1] / "resources" / "image_templates" / "intranet_v1"


def _source_index(path: Path) -> int:
    match = re.match(r"(\d+)", path.name)
    if not match:
        raise ValueError(f"PDF 文件名必须以样本序号开始：{path.name}")
    return int(match.group(1))


def compare_records(actual: dict, expected: dict) -> list[str]:
    differences: list[str] = []
    for name in REQUIRED_FIELDS:
        if actual.get(name) != expected.get(name):
            differences.append(f"{name}: {actual.get(name)!r} != {expected.get(name)!r}")
    return differences


def parse_directory(source: Path, parser: CertificateParser) -> list:
    files = sorted(source.glob("*.pdf"), key=_source_index)
    if not files:
        raise ValueError("未找到 PDF 文件")
    drafts = []
    for path in files:
        draft = parser.parse_file(path)
        draft.sample_index = _source_index(path)
        drafts.append(draft)
    return drafts


def load_m4_drafts(source: Path, parser: CertificateParser) -> list:
    """Load one PDF, a PDF directory, or a reviewed JSON draft for M4."""

    if source.is_dir():
        return parse_directory(source, parser)
    if source.suffix.casefold() == ".json":
        return import_drafts(source)
    if source.suffix.casefold() == ".pdf":
        return [parser.parse_file(source)]
    raise ValueError(f"M4 输入必须是 PDF、PDF 目录或 JSON 草稿：{source}")


def golden_regression(source: Path, golden_dir: Path) -> int:
    parser = CertificateParser()
    failed = 0
    for draft in parse_directory(source, parser):
        golden_path = golden_dir / f"{draft.sample_index:03d}-{draft.patent_no}.json"
        if not golden_path.exists():
            print(f"缺少 golden：{golden_path.name}")
            failed += 1
            continue
        expected = json.loads(golden_path.read_text(encoding="utf-8"))
        differences = compare_records(draft.to_dict(), expected)
        if differences:
            print(f"失败 {draft.sample_index:03d}: " + "; ".join(differences))
            failed += 1
    print(f"golden 回归：{'通过' if failed == 0 else f'{failed} 份失败'}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="专利证书解析与屏幕自动化工具")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    parser.add_argument("--native-host", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--native-store",
        type=Path,
        default=Path(".m6") / "dom-bridge",
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(dest="command", required=False)
    parse = commands.add_parser("parse", help="解析单个 PDF 并输出 JSON")
    parse.add_argument("pdf", type=Path)
    parse.add_argument("--output", type=Path)
    batch = commands.add_parser("batch", help="批量解析目录中的 PDF")
    batch.add_argument("source", type=Path)
    batch.add_argument("--output", type=Path, required=True)
    regression = commands.add_parser("regression", help="与 golden JSON 比较")
    regression.add_argument("source", type=Path)
    regression.add_argument("--golden", type=Path, default=Path("m0/golden"))
    commands.add_parser("ui", help="启动人工校对界面")
    m2 = commands.add_parser("m2", help="启动 M2 截图识别与窗口绑定工具")
    m2.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE_PATH,
        help="页面 profile JSON",
    )
    m3 = commands.add_parser("m3", help="运行离线 M3 自动化引擎 PoC")
    m3.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE_PATH,
        help="页面 profile JSON",
    )
    m4 = commands.add_parser("m4", help="启动 M4 端到端小程序；--headless 运行控制台回归")
    m4.add_argument("source", type=Path, nargs="?", help="PDF、PDF 目录或已复核 JSON；留空运行 50 份 golden")
    m4.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH, help="页面 profile JSON")
    m4.add_argument("--golden", type=Path, default=Path("m0/golden"), help="留空 source 时使用的 golden 目录")
    m4.add_argument("--manual", type=Path, help="人工/配置字段 JSON")
    m4.add_argument("--window-title", help="绑定已打开的 Edge/Chrome 窗口标题（默认使用离线模拟绑定）")
    m4.add_argument("--diagnostics", type=Path, help="诊断 JSON 文件或诊断目录")
    m4.add_argument("--output", type=Path, help="输出最终报告 JSON")
    m4.add_argument("--max-retries", type=int, default=1, help="可重试动作的最大重试次数")
    m4.add_argument("--headless", action="store_true", help="不启动窗口，只在控制台运行回归")
    m5 = commands.add_parser("m5", help="运行 M5 内网现场只识别或单步模式")
    m5.add_argument("--mode", choices=("recognition_only", "step"), default="recognition_only")
    m5.add_argument("--image", type=Path, help="现场截图；不提供时使用 --window-title 截取绑定窗口")
    m5.add_argument("--profile", type=Path, default=DEFAULT_M5_PROFILE_PATH, help="M5 网页 profile JSON")
    m5.add_argument("--templates", type=Path, default=DEFAULT_M5_TEMPLATES_PATH, help="M5 图像模板目录")
    m5.add_argument("--window-title", help="Windows 中要绑定的 Edge/Chrome 窗口标题")
    m5.add_argument("--actions", type=Path, help="单步动作 JSON；格式为数组或 {actions: [...]} ")
    m5.add_argument("--diagnostics", type=Path, help="现场步骤截图和 detect JSON 输出目录")
    m5.add_argument("--annotated", type=Path, help="只识别模式输出标注截图")
    m5.add_argument("--output", type=Path, help="输出 M5 运行报告 JSON")
    m5.add_argument("--yes", action="store_true", help="单步模式自动确认每个动作；仅用于用户明确授权的现场运行")
    m5_package = commands.add_parser("m5-package", help="构建 M5 现场离线源安装包")
    m5_package.add_argument("--output", type=Path, default=Path("build") / "m5_field_package.zip")
    m5_package.add_argument("--root", type=Path, help="项目根目录，默认使用当前项目")
    m6 = commands.add_parser("m6", help="管理 M6 本地任务队列")
    m6_commands = m6.add_subparsers(dest="m6_action", required=True)
    m6_enqueue = m6_commands.add_parser("enqueue", help="加入 PDF 或已复核 JSON 草稿")
    m6_enqueue.add_argument("source", type=Path, nargs="+")
    m6_enqueue.add_argument("--queue", type=Path, default=Path(".m6") / "queue.json")
    m6_enqueue.add_argument("--max-attempts", type=int, default=2)
    m6_list = m6_commands.add_parser("list", help="列出任务状态")
    m6_list.add_argument("--queue", type=Path, default=Path(".m6") / "queue.json")
    m6_recover = m6_commands.add_parser("recover", help="将上次异常退出的 running 任务暂停")
    m6_recover.add_argument("--queue", type=Path, default=Path(".m6") / "queue.json")
    m6_retry = m6_commands.add_parser("retry", help="显式重试或恢复一个任务")
    m6_retry.add_argument("task_id")
    m6_retry.add_argument("--queue", type=Path, default=Path(".m6") / "queue.json")
    m6_retry.add_argument("--force", action="store_true")
    m6_backup = m6_commands.add_parser("backup", help="创建本地脱敏 ZIP 备份")
    m6_backup.add_argument("source", type=Path)
    m6_backup.add_argument("--output", type=Path, required=True)
    m6_backup.add_argument("--include-sensitive", action="store_true", help="仅在受控本地备份时关闭 JSON 脱敏")
    m6_package = commands.add_parser("m6-package", help="构建 M6 离线安装与运维包")
    m6_package.add_argument("--output", type=Path, default=Path("build") / "m6_offline_package.zip")
    m6_package.add_argument("--root", type=Path, help="项目根目录，默认使用当前项目")
    m7 = commands.add_parser("m7", help="统一桌面入口；默认启动 M7 任务界面")
    m7.add_argument("source", type=Path, nargs="?", help="单个 PDF/JSON 或包含 PDF/JSON 的目录")
    m7.add_argument(
        "--mode",
        choices=("auto_update", "simulation", "recognition_only", "step", "controlled_batch"),
        default="simulation",
    )
    m7.add_argument("--image", type=Path, help="现场截图；只识别/单步模式可用")
    m7.add_argument("--actions", type=Path, help="单步动作 JSON")
    m7.add_argument("--manual", type=Path, help="人工/配置字段 JSON")
    m7.add_argument("--window-title", help="绑定已打开的 Edge/Chrome 窗口标题")
    m7.add_argument("--queue", type=Path, default=Path(".m6") / "queue.json")
    m7.add_argument("--diagnostics", type=Path, help="诊断 JSON 文件或目录")
    m7.add_argument("--annotated", type=Path, help="只识别模式的标注截图")
    m7.add_argument("--output", type=Path, help="输出 M7 报告 JSON")
    m7.add_argument("--limit", type=int, help="受控批量最多处理的任务数")
    m7.add_argument("--yes", action="store_true", help="单步模式跳过逐动作确认；仅用于明确授权的现场运行")
    m7.add_argument("--headless", action="store_true", help="不启动桌面界面，直接运行指定模式")
    m7_package = commands.add_parser("m7-package", help="构建最终 M7 离线安装与运维包")
    m7_package.add_argument("--output", type=Path, default=Path("build") / "m7_offline_package.zip")
    m7_package.add_argument("--root", type=Path, help="项目根目录，默认使用当前项目")
    dom = commands.add_parser("dom", help="准备或查看 Edge 扩展的本地已审核任务")
    dom_commands = dom.add_subparsers(dest="dom_action", required=True)
    dom_prepare = dom_commands.add_parser("prepare", help="将一份已复核 PDF/JSON 准备为扩展任务")
    dom_prepare.add_argument("source", type=Path)
    dom_prepare.add_argument("--manual", type=Path, help="人工/配置字段 JSON")
    dom_prepare.add_argument("--store", type=Path, default=Path(".m6") / "dom-bridge")
    dom_prepare.add_argument("--profile-version", default="dom-poc-v1")
    dom_prepare.add_argument(
        "--include-complex",
        action="store_true",
        help="仅在动态表格和人员选择器已完成 DOM Profile 校准后启用",
    )
    dom_prepare.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="允许扩展覆盖当前页面的非空字段；仍不会保存、提交、返回或删除",
    )
    dom_status = dom_commands.add_parser("status", help="查看扩展任务及逐字段结果")
    dom_status.add_argument("--store", type=Path, default=Path(".m6") / "dom-bridge")
    mock_site = commands.add_parser("mock-site", help="启动离线 M2 仿真网页")
    mock_site.add_argument("--port", type=int, default=8765)
    mock_site.add_argument("--open", action="store_true", dest="open_browser", help="启动后打开默认浏览器")
    recognize = commands.add_parser("recognize", help="对截图执行 OCR/锚点识别")
    recognize.add_argument("image", type=Path)
    recognize.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    recognize.add_argument("--output", type=Path)
    recognize.add_argument("--annotated", type=Path, help="输出画框后的截图")
    recognize.add_argument(
        "--templates",
        type=Path,
        default=Path("resources/image_templates"),
        help="图像模板目录",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.native_host:
        from .dom_bridge import NativeMessageHost, TaskStore

        return NativeMessageHost(TaskStore(args.native_store)).serve()
    if args.command is None:
        from .m7_ui import launch

        try:
            launch()
        except Exception as error:
            if error.__class__.__name__ == "TclError":
                print(
                    "错误：当前 Python 的 Tcl/Tk 桌面组件不可用。请修复或重新安装带 Tcl/Tk 的官方 Windows Python 后再运行 python -m app。",
                    file=sys.stderr,
                )
                return 2
            raise
        return 0
    if args.command == "ui":
        from .ui import launch

        launch()
        return 0
    if args.command == "m2":
        from .m2_ui import launch

        launch(args.profile)
        return 0
    if args.command == "m3":
        from .automation import load_profile, run_m3_poc

        report = run_m3_poc(load_profile(args.profile))
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.verified else 4
    if args.command == "m4":
        if not args.headless and not any(
            (args.source, args.manual, args.window_title, args.diagnostics, args.output)
        ):
            from .m4_ui import launch

            launch(args.profile, args.golden)
            return 0
        from .automation import Action, WindowBinder, load_profile
        from .m4 import M4RegressionReport, M4Workflow, ManualFields, run_m4_regression

        profile = load_profile(args.profile)
        manual = ManualFields.from_mapping(
            json.loads(args.manual.read_text(encoding="utf-8")) if args.manual else None
        )
        binding = WindowBinder().bind_by_title(args.window_title) if args.window_title else None
        if args.source is None:
            result = run_m4_regression(
                profile,
                args.golden,
                supplements=manual,
                diagnostics_dir=args.diagnostics,
                binding=binding,
                max_retries=args.max_retries,
            )
        else:
            parser = CertificateParser()
            drafts = load_m4_drafts(args.source, parser)
            workflow = M4Workflow(profile, binding=binding, max_retries=args.max_retries)
            reports = []
            for index, draft in enumerate(drafts, start=1):
                if draft.sample_index is None:
                    draft.sample_index = index
                diagnostic_path = None
                if args.diagnostics:
                    diagnostic_path = (
                        args.diagnostics
                        if args.diagnostics.suffix.casefold() == ".json" and len(drafts) == 1
                        else args.diagnostics / f"sample-{draft.sample_index:03d}.json"
                    )
                reports.append(workflow.run(draft, manual, diagnostics=diagnostic_path))
            result = M4RegressionReport(reports)
        output = json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0 if result.verified else 4
    if args.command == "m5-package":
        from .m5_package import build_m5_package

        target = build_m5_package(args.output, args.root)
        print(json.dumps({"status": "completed", "package": str(target)}, ensure_ascii=False))
        return 0
    if args.command == "m6-package":
        from .m6_package import build_m6_package

        target = build_m6_package(args.output, args.root)
        print(json.dumps({"status": "completed", "package": str(target)}, ensure_ascii=False))
        return 0
    if args.command == "m7-package":
        from .m7_package import build_m7_package

        target = build_m7_package(args.output, args.root)
        print(json.dumps({"status": "completed", "package": str(target)}, ensure_ascii=False))
        return 0
    if args.command == "dom":
        from .dom_bridge import DomBridgeError, TaskStore
        from .m7 import load_workflow_sources

        try:
            store = TaskStore(args.store)
            if args.dom_action == "prepare":
                drafts = load_workflow_sources(args.source)
                if len(drafts) != 1:
                    raise DomBridgeError("Edge 扩展任务每次只接受一份证书。")
                manual = (
                    json.loads(args.manual.read_text(encoding="utf-8"))
                    if args.manual is not None
                    else None
                )
                task = store.prepare(
                    drafts[0],
                    manual,
                    profile_version=args.profile_version,
                    include_complex=args.include_complex,
                    allow_overwrite=args.allow_overwrite,
                )
                output_data: object = {
                    "status": "ready_for_fill",
                    "task_id": task["task_id"],
                    "profile_version": task["profile_version"],
                    "field_count": len(task["fields"]),
                    "store": str(store.root),
                }
            else:
                output_data = store.status()
            print(json.dumps(output_data, ensure_ascii=False, indent=2))
            return 0
        except (DomBridgeError, OSError, ValueError, json.JSONDecodeError) as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2
    if args.command == "m7":
        if not args.headless and not any((args.source, args.image, args.actions, args.manual, args.window_title)):
            from .m7_ui import launch

            try:
                launch()
            except Exception as error:
                if error.__class__.__name__ == "TclError":
                    print(
                        "错误：当前 Python 的 Tcl/Tk 桌面组件不可用。请修复或重新安装带 Tcl/Tk 的官方 Windows Python 后再运行 python -m app。",
                        file=sys.stderr,
                    )
                    return 2
                raise
            return 0
        from .automation import Action
        from .m5 import load_actions
        from .m7 import M7Error, M7Mode, M7Service, load_workflow_sources

        try:
            service = M7Service(queue_path=args.queue)
            if args.window_title:
                service.bind_window(args.window_title)
            manual = None
            if args.manual:
                manual = json.loads(args.manual.read_text(encoding="utf-8"))
            if args.mode == M7Mode.AUTO_UPDATE.value:
                if args.source is None or not args.window_title:
                    raise M7Error("auto_update 模式需要单份 PDF/JSON 和 --window-title。")
                drafts = load_workflow_sources(args.source)
                if len(drafts) != 1:
                    raise M7Error("auto_update 模式每次只能处理一份证书。")
                report = service.run_auto_update(drafts[0], manual, diagnostics=args.diagnostics)
                output_data, ok = report.to_dict(), report.status == "completed" and report.verified
            elif args.mode == M7Mode.SIMULATION.value:
                if args.source is None:
                    raise M7Error("simulation 模式需要 PDF/JSON 草稿或目录。")
                drafts = load_workflow_sources(args.source)
                reports = [
                    service.run_simulation(draft, manual, diagnostics=args.diagnostics)
                    for draft in drafts
                ]
                output_data: object = reports[0].to_dict() if len(reports) == 1 else {
                    "format": "m7-unified-reports-v1",
                    "mode": args.mode,
                    "reports": [item.to_dict() for item in reports],
                }
                ok = all(item.status == "completed" for item in reports)
            elif args.mode == M7Mode.RECOGNITION_ONLY.value:
                report = service.run_recognition_only(
                    image=args.image,
                    annotated=args.annotated,
                    diagnostics=args.diagnostics,
                )
                output_data, ok = report.to_dict(), report.status == "recognized"
            elif args.mode == M7Mode.STEP.value:
                if args.actions is None:
                    raise M7Error("step 模式需要 --actions 动作 JSON。")
                actions = load_actions(args.actions)

                def confirm(action: Action) -> bool:
                    if args.yes:
                        return True
                    answer = input(f"确认执行 {action.control_id} ({action.kind})? [y/N] ")
                    return answer.strip().casefold() in {"y", "yes"}

                report = service.run_step(
                    actions,
                    image=args.image,
                    confirm=confirm,
                    diagnostics=args.diagnostics,
                )
                output_data, ok = report.to_dict(), report.status == "completed"
            else:
                if args.source is not None:
                    sources = (
                        sorted([*args.source.glob("*.pdf"), *args.source.glob("*.json")])
                        if args.source.is_dir()
                        else [args.source]
                    )
                    service.enqueue(sources)
                report = service.run_controlled_batch(manual, diagnostics=args.diagnostics, limit=args.limit)
                output_data, ok = report.to_dict(), report.status == "completed"
            output = json.dumps(output_data, ensure_ascii=False, indent=2) + "\n"
            if args.output is None:
                print(output, end="")
            else:
                args.output.write_text(output, encoding="utf-8")
            return 0 if ok else 4
        except (M7Error, OSError, ValueError, json.JSONDecodeError) as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2
    if args.command == "m6":
        from .m6 import BackupManager, TaskQueue

        queue = TaskQueue(args.queue) if args.m6_action != "backup" else None
        if args.m6_action == "enqueue":
            sources: list[Path] = []
            for source in args.source:
                if source.is_dir():
                    sources.extend(sorted(source.glob("*.pdf")))
                else:
                    sources.append(source)
            tasks = queue.enqueue_many(sources, max_attempts=args.max_attempts)
            print(json.dumps({"status": "completed", "tasks": [task.to_dict() for task in tasks]}, ensure_ascii=False, indent=2))
            return 0
        if args.m6_action == "list":
            print(json.dumps({"format": "m6-task-queue-v1", "tasks": [task.to_dict() for task in queue.tasks]}, ensure_ascii=False, indent=2))
            return 0
        if args.m6_action == "recover":
            recovered = queue.recover_orphaned()
            print(json.dumps({"status": "completed", "recovered": recovered}, ensure_ascii=False))
            return 0
        if args.m6_action == "retry":
            task = queue.retry(args.task_id, force=args.force)
            print(json.dumps({"status": "completed", "task": task.to_dict()}, ensure_ascii=False, indent=2))
            return 0
        if args.m6_action == "backup":
            target = BackupManager.create(args.source, args.output, redact_json=not args.include_sensitive)
            print(json.dumps({"status": "completed", "backup": str(target)}, ensure_ascii=False))
            return 0
    if args.command == "m5":
        from .m5 import BoundWindowCapture, FileCapture, M5Error, M5FieldRunner, load_actions
        from .automation import WindowBinder, load_profile

        try:
            if args.image and args.window_title:
                raise M5Error("--image 与 --window-title 只能二选一。")
            profile = load_profile(args.profile)
            binding = None
            binder = None
            if args.window_title:
                binder = WindowBinder()
                binding = binder.bind_by_title(args.window_title)
                capture = BoundWindowCapture(binder, binding)
            elif args.image:
                capture = FileCapture(args.image)
            else:
                raise M5Error("M5 运行需要 --image 现场截图或 --window-title 绑定现场浏览器。")
            runner = M5FieldRunner(
                profile,
                capture,
                templates=args.templates,
                binder=binder,
                binding=binding,
                diagnostics_dir=args.diagnostics,
            )
            if args.mode == "recognition_only":
                report = runner.recognize_only(annotated=args.annotated, report_path=args.output)
            else:
                if args.actions is None:
                    raise M5Error("单步模式需要 --actions 动作 JSON。")
                actions = load_actions(args.actions)

                def confirm(action: Action) -> bool:
                    if args.yes:
                        return True
                    try:
                        answer = input(f"确认执行 {action.control_id} ({action.kind})? [y/N] ")
                    except EOFError:
                        return False
                    return answer.strip().casefold() in {"y", "yes"}

                report = runner.run_step(actions, confirm=confirm, report_path=args.output)
            output = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
            if args.output is None:
                print(output, end="")
            return 0 if report.status in {"recognized", "completed", "paused"} else 3
        except (M5Error, OSError, ValueError) as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2
    if args.command == "mock-site":
        from .mock_server import launch_mock_site

        launch_mock_site(args.port, args.open_browser)
        return 0
    if args.command == "recognize":
        from PIL import Image

        from .automation import AnchorRecognizer, load_profile
        from .automation.recognizer import TemplateMatcher, annotate_image

        profile = load_profile(args.profile)
        image = Image.open(args.image).convert("RGB")
        result = AnchorRecognizer(profile).recognize_image(image)
        template_matches = TemplateMatcher().locate_directory(image, args.templates)
        result_data = result.to_dict()
        result_data["template_matches"] = [
            {"name": match.name, "box": match.box.to_dict(), "score": match.score}
            for match in template_matches
        ]
        output = json.dumps(result_data, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        if args.annotated:
            annotate_image(image, result, template_matches).save(args.annotated)
        return 0 if result.safe_for_input else 3
    try:
        parser = CertificateParser()
        if args.command == "parse":
            draft = parser.parse_file(args.pdf)
            output = json.dumps(draft.to_dict(), ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.write_text(output, encoding="utf-8")
            else:
                print(output, end="")
            return 0
        if args.command == "batch":
            export_drafts(args.output, parse_directory(args.source, parser))
            return 0
        return golden_regression(args.source, args.golden)
    except (ParseError, OcrUnavailableError, ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
