"""Build verifiable M0 patent-certificate baseline artifacts."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

import pdfplumber


PATENT_NUMBER = re.compile(r"专\s*利\s*号：\s*(ZL\s*[0-9\s]{12,}\.?\s*[0-9X])")
CERTIFICATE_NUMBER = re.compile(r"证书号第\s*(\d+)\s*号")
PUBLICATION_NUMBER = re.compile(r"授权公告号：\s*([A-Z]{2}\s*\d+\s*[A-Z])")
TITLE = re.compile(r"(?:发\s*明\s*名\s*称|实用新型名称)\s*：\s*(.*?)\n\s*专\s*利\s*权\s*人", re.S)
PATENTEES = re.compile(r"专\s*利\s*权\s*人：\s*(.*?)\n\s*地\s*址", re.S)
INVENTORS = re.compile(r"发\s*明\s*人：\s*(.*?)\n\s*专\s*利\s*号", re.S)
APPLICATION_DATE = re.compile(r"专利申请日：\s*([^\s]+)")
GRANT_DATE = re.compile(r"授权公告日：\s*([^\s]+)")
APPLICATION_APPLICANTS = re.compile(r"申请日时申请人：\s*(.*?)\n\s*申请日时发明人", re.S)
APPLICATION_INVENTORS = re.compile(r"申请日时发明人：\s*(.*?)\n\s*国家知识产权局", re.S)


def source_index(path: Path) -> int:
    match = re.match(r"(\d+)", path.name)
    if not match:
        raise ValueError(f"source file does not start with an index: {path.name}")
    return int(match.group(1))


def value(pattern: re.Pattern[str], text: str, name: str) -> str:
    match = pattern.search(text)
    if not match:
        raise ValueError(f"missing {name}")
    return match.group(1).strip()


def compact_patent_number(raw: str) -> str:
    return re.sub(r"\s+", "", raw)


def iso_date(chinese_date: str) -> str:
    match = re.fullmatch(r"(\d{4})年(\d{2})月(\d{2})日", chinese_date)
    if not match:
        raise ValueError(f"unexpected date format: {chinese_date}")
    return "-".join(match.groups())


def split_names(raw: str) -> list[str]:
    # CNIPA's text layer omits the semicolon when a name list wraps to the next
    # visual line.  In the certificate list fields, that line break is a name
    # separator rather than part of a person's or organization's name.
    names = [part.strip() for part in raw.replace("\n", ";").split(";")]
    if not names or any(not name for name in names):
        raise ValueError(f"cannot split list: {raw!r}")
    return names


def certificate_type(text: str) -> str:
    if "发 明 专 利 证 书" in text:
        return "invention"
    if "实用新型专利证书" in text:
        return "utility_model"
    raise ValueError("unrecognized certificate type")


def extract(path: Path) -> dict:
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if pages != 1:
        raise ValueError(f"expected one page, got {pages}")
    if not text.strip():
        raise ValueError("no extractable text layer")

    current_patentees_raw = value(PATENTEES, text, "current patentees")
    inventors_raw = value(INVENTORS, text, "inventors")
    application_patentees_raw = value(APPLICATION_APPLICANTS, text, "application-date applicants")
    application_inventors_raw = value(APPLICATION_INVENTORS, text, "application-date inventors")
    patent_number_raw = value(PATENT_NUMBER, text, "patent number")
    patent_number = compact_patent_number(patent_number_raw)
    if not re.fullmatch(r"ZL\d{12}\.[\dX]", patent_number):
        raise ValueError(f"unexpected patent number format: {patent_number}")

    result = {
        "source_file": str(path),
        "sample_index": source_index(path),
        "certificate_template": "cnipa_electronic_one_page_text_layer_2024",
        "page_count": pages,
        "patent_type": certificate_type(text),
        "certificate_no": value(CERTIFICATE_NUMBER, text, "certificate number"),
        "title": value(TITLE, text, "title"),
        "patent_no_raw": patent_number_raw,
        "patent_no": patent_number,
        "publication_no": re.sub(r"\s+", "", value(PUBLICATION_NUMBER, text, "publication number")),
        "application_date": iso_date(value(APPLICATION_DATE, text, "application date")),
        "grant_publication_date": iso_date(value(GRANT_DATE, text, "grant publication date")),
        "current_patentees": split_names(current_patentees_raw),
        "application_date_applicants": split_names(application_patentees_raw),
        "inventors": split_names(inventors_raw),
        "application_date_inventors": split_names(application_inventors_raw),
        "source_evidence": {
            "current_patentees_raw": current_patentees_raw,
            "inventors_raw": inventors_raw,
        },
        "review": {
            "status": "verified_against_certificate_text_layer",
            "needs_review": [],
            "notes": [],
        },
    }
    if result["inventors"] != result["application_date_inventors"]:
        result["review"]["needs_review"].append("inventor_list_changed_since_application")
    if "\n" in inventors_raw:
        result["review"]["notes"].append("inventor_list_cross_line_reconstructed")
    if len(result["current_patentees"]) > 1:
        result["review"]["notes"].append("joint_current_patentees")
    if len(result["application_date_applicants"]) > 1:
        result["review"]["notes"].append("joint_application_date_applicants")
    return result


def inventory_markdown(records: list[dict]) -> str:
    rows = [
        "# M0 样本清单与模板分类",
        "",
        "基线范围为证书目录中的 1–50 号文件。所有样本均为一页、带可提取文本层的中国国家知识产权局电子专利证书；按证书正文而非文件名核验。",
        "",
        "| 序号 | 模板 | 类型 | 专利号 | 名称 | 页数 | 联合权利人 | 发明人数量 |",
        "|---:|---|---|---|---|---:|---|---:|",
    ]
    for record in records:
        rows.append(
            "| {sample_index} | {certificate_template} | {patent_type} | {patent_no} | {title} | {page_count} | {joint} | {inventor_count} |".format(
                **record,
                joint="是" if len(record["current_patentees"]) > 1 else "否",
                inventor_count=len(record["inventors"]),
            )
        )
    return "\n".join(rows) + "\n"


def write_static_documents(output_dir: Path, records: list[dict]) -> None:
    counts = Counter(record["patent_type"] for record in records)
    (output_dir / "README.md").write_text(
        "# M0 需求与样本基线\n\n"
        f"生成日期：{date.today().isoformat()}\n\n"
        f"本轮基线包含 {len(records)} 份证书（发明 {counts['invention']} 份、实用新型 {counts['utility_model']} 份）。"
        "所有关键字段均已从证书文本层提取，发明人和权利人按分号切分，保留原始名单以支持复核。\n\n"
        "本目录中的 `golden/` 是 M1 回归的结构化标准答案；`web_field_map.md`、`page_profile.md` 与 `business_open_questions.md` 分别记录已核验的网页映射、页面锚点和后续待确认项。\n",
        encoding="utf-8",
    )
    (output_dir / "sample_inventory.md").write_text(inventory_markdown(records), encoding="utf-8")
    (output_dir / "web_field_map.md").write_text(
        "# 网页字段地图（M0 已核验）\n\n"
        "依据 2026-08-03 提供的 Edge 截图核验。页面锚点为“科技项目管理系统 信息版”与“专利信息库”；首版仅更新证书可直接确认的字段。未由证书提供的内部字段一律不猜填。\n\n"
        "## 证书驱动的更新字段\n\n"
        "| 网页字段 | 证书来源/转换规则 | 更新及验证规则 |\n"
        "|---|---|---|\n"
        "| 专利号 | `patent_no` 移除 `ZL` 和小数点，保留末位数字或 `X` | 例如 `ZL201810637498.0` 填为 `2018106374980`；回读输入框核验 |\n"
        "| 申请名称 | `title` | 全量替换后回读核验 |\n"
        "| 专利状态 | 当前页固定选项“授权” | 仅在当前值不是“授权”时选择；回读选项文本 |\n"
        "| 申请类型 | `patent_type` 映射“发明”或“实用新型” | 仅勾选对应的一项；验证另一项未选中 |\n"
        "| 申请受理日 | `application_date` | 按 `YYYY-MM-DD` 输入；截图样本与证书申请日一致 |\n"
        "| 授权公告日 | `grant_publication_date` | 按 `YYYY-MM-DD` 输入并回读 |\n"
        "| 专利权人表 | `current_patentees`，保持证书顺序 | 逐条填充并核对表格行；不使用“申请日时申请人”替代 |\n"
        "| 第一发明人 | `inventors[0]` | 通过人员选择器匹配姓名；只在唯一匹配时写入 |\n"
        "| 发明人表 | `inventors`，保持证书顺序 | 逐条填充并读取表格末行验证；姓名不唯一则暂停 |\n"
        "| 专利权人合并 | 由已核对的权利人表派生 | 不独立猜填；以系统自动回填为准 |\n"
        "| 发明人合并 | 由已核对的发明人表派生 | 不独立猜填；以系统自动回填为准 |\n"
        "\n## 明确排除或保持原值的字段\n\n"
        "| 网页字段/区域 | 处理方式 | 原因 |\n"
        "|---|---|---|\n"
        "| 申请号 | 不更新 | 截图示例 `00923` 是内部编号，证书没有该值 |\n"
        "| 所属机构、申请部门、专利单位 | 保持页面已有值 | 不能从证书推出 |\n"
        "| 是否联合申请、联合申请说明 | 不更新 | 当前截图不足以确认复选/下拉的业务口径 |\n"
        "| 交付日、授权通知日、业务领域、代理机构 | 保持原值或留空 | 证书没有对应信息 |\n"
        "| 第一发明人身份证号、联系方式 | 不更新 | 属于人员主数据，证书未提供 |\n"
        "| PCT、科技/工程项目、经办人及联系方式 | 不更新 | 非证书字段 |\n"
        "| 补充附件、受理附件、授权通知附件 | 不上传、不删除 | 页面已提供证书“预览/下载”；附件自动化移至后续功能 |\n"
        "\n## 执行边界\n\n"
        "1. 首版以 Edge 为唯一受支持浏览器；其他浏览器待现场验证后再纳入支持矩阵。\n"
        "2. 先识别页面锚点和字段标签，再按锚点相对位置操作；禁止用全局固定坐标。\n"
        "3. 每个更新字段写入后立即截屏或 OCR 回读验证；页面、窗口或焦点变化时暂停。\n"
        "4. 程序完成所有可确认字段的核验后停在“保存”前，由用户检查并手动保存。\n",
        encoding="utf-8",
    )
    (output_dir / "business_open_questions.md").write_text(
        "# 业务待确认项\n\n"
        "## 已由截图和当前范围确认\n\n"
        "1. 目标页面为 Edge 中的“科技项目管理系统 信息版 - 专利信息库”。\n"
        "2. 网页“专利号”使用去除 `ZL` 和小数点后的号码。\n"
        "3. 网页“申请受理日”使用证书“专利申请日”；“授权公告日”使用证书同名字段。\n"
        "4. 当前“专利权人”和“发明人”分别使用证书当前权利人、当前发明人及其原始顺序。\n"
        "5. 只更新证书可确认字段；内部字段不补全、不猜填。\n"
        "6. 原始证书已可在页面补充附件中预览/下载；首版不上传、不删除附件。\n"
        "7. 自动化完成核验后由用户手动点击保存。\n\n"
        "## 后续功能前仍需确认\n\n"
        "1. 联合申请下拉框的业务口径，以及是否需随多权利人自动选择。\n"
        "2. 人员、部门、代理机构选择弹窗的搜索、唯一匹配和确认步骤。\n"
        "3. Edge 的 Windows 缩放、浏览器缩放和常用窗口分辨率支持矩阵。\n"
        "4. 是否能导出经审核的失败截图、日志和识别坐标 JSON。\n",
        encoding="utf-8",
    )
    (output_dir / "page_profile.md").write_text(
        "# Edge 页面 Profile v1\n\n"
        "## 页面锚点\n\n"
        "- 顶部系统标题：`科技项目管理系统 信息版`\n"
        "- 模块标签：`专利信息库`\n"
        "- 首个区块：`基本信息`\n"
        "- 后续区块：`专利出处`、`经办人`、`附件`\n\n"
        "自动化必须同时识别模块标签和当前区块标题，才允许执行对应字段动作。\n\n"
        "## 可验证的页面结构\n\n"
        "- “基本信息”包含专利状态、专利号、申请名称、申请类型、申请受理日和授权公告日。\n"
        "- “专利权人”为动态表格；单位名称是可回读的表格列。\n"
        "- “第一发明人”与“发明人（含第一发明人）”为独立人员控件/表格；身份证号、联系方式不属于证书数据。\n"
        "- “补充附件”表格中的既有证书可预览或下载，首版只核验文件存在，不触发上传或删除。\n\n"
        "## 证据样本\n\n"
        "截图样本对应专利 `ZL201810637498.0`：网页显示专利号 `2018106374980`、申请受理日 `2018-06-20`、授权公告日 `2025-03-04`，与证书字段转换一致。\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("m0"))
    args = parser.parse_args()

    source_files = sorted(
        (path for path in args.source.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"),
        key=source_index,
    )
    if not source_files:
        raise ValueError("no PDF files found")
    records = [extract(path) for path in source_files]
    indices = [record["sample_index"] for record in records]
    if len(indices) != len(set(indices)):
        raise ValueError("duplicate sample indices")

    golden_dir = args.output / "golden"
    golden_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        filename = f"{record['sample_index']:03d}-{record['patent_no']}.json"
        (golden_dir / filename).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "sample_inventory.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "baseline_validation.json").write_text(
        json.dumps(
            {
                "sample_count": len(records),
                "one_page_text_layer_samples": len(records),
                "certificate_templates": sorted(set(record["certificate_template"] for record in records)),
                "patent_type_counts": Counter(record["patent_type"] for record in records),
                "joint_current_patentee_samples": [record["sample_index"] for record in records if len(record["current_patentees"]) > 1],
                "status": "passed",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    write_static_documents(args.output, records)


if __name__ == "__main__":
    main()
