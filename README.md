# 专利证书自动识别填报：M1/M2/M3/M4/M5/M6/M7

当前默认真实网页路线已切换为企业 Edge DOM 扩展：桌面程序继续本地解析和人工校对，扩展通过 Native Messaging 读取唯一的已审核任务，在现有已登录 Edge 中逐字段预填并回读，最后停在人工保存前。屏幕 OCR/键鼠执行器保留为高级诊断和兜底，不再是默认真实网页执行器。

快速准备扩展任务：

```powershell
python -m app dom prepare m0\golden\001-ZL202010430096.0.json
python -m app dom status
```

扩展安装、权限最小化、脱敏校准与现场验收见 [`EDGE_EXTENSION_OPERATIONS.md`](EDGE_EXTENSION_OPERATIONS.md)。

M1 提供本地专利证书解析、人工校对和结构化 JSON 导入/导出。M2 增加离线仿真页、截图查看器、OCR/模板锚点识别和 Windows 窗口绑定。M3 增加不依赖 DOM 的轻量自动化引擎 PoC：每个动作都在新观察后执行并回读验证；保存、返回和删除仍不会自动执行。

## 安装与启动

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    python -m app ui

文本层证书使用 PyMuPDF 直接解析。扫描证书会自动进入 PaddleOCR 回退路径；项目提供 CPU 运行时和本地中文模型，不需要安装 Tesseract。OCR 不可用时，程序会明确报错并保留人工校对入口，不会输出看似成功的猜测结果。

## 命令行

    # 解析一份证书
    python -m app parse "C:\path\证书.pdf" --output draft.json

    # 批量解析并导出草稿
    python -m app batch "C:\path\证书目录" --output drafts.json

    # 用 M0 golden JSON 回归
    python -m app regression "C:\path\证书目录" --golden m0\golden

    # 运行单元测试
    python -m unittest discover -s tests -v

    # 启动 M2 离线仿真页（默认不打开浏览器）
    python -m app mock-site --open

    # 启动 M2 截图识别与窗口绑定工具
    python -m app m2

    # 运行离线 M3 自动化引擎 PoC（不打开浏览器、不点击保存）
    python -m app m3

    # 对截图识别并输出识别框 JSON；OCR 不可用或锚点不足时返回非零状态
    python -m app recognize "C:\path\window.png" --annotated "C:\path\window.annotated.png"

regression 会对专利类型、专利号、名称、申请日、公告日、权利人列表和发明人列表逐字段严格比较。名单或关键字段未识别时会出现在 review.needs_review，不能静默通过。

## M2 安全边界

- 页面 profile 使用标签和区块相对定位，不保存固定屏幕坐标；窗口移动后重新截图即可重新定位。
- 点击“截取绑定窗口”时，工具会临时隐藏自身并将绑定的浏览器置于前台，避免把 M2 工具窗口一起截入；截图完成后工具自动恢复。
- 识别必须同时通过系统标题、模块标题和“基本信息”锚点；错误页面、加载中、校验失败、弹窗遮挡和 OCR 不可用都会停机。
- `simulation` 仅允许接入本地模拟页的安全动作；`recognition_only` 只输出计划；`step` 每个动作前等待确认。
- 摘要、预期效益、项目出处、身份证号、联系方式和经办人值标为人工/配置来源；模拟页只验证控件定位和回读，不猜填。

## M3 自动化边界

- `app/automation/engine.py` 提供观察—动作—验证状态机、文本框/长文本编辑态/日期/下拉/复选框/动态表格/人员选择器/滚动/附件核验动作，以及可注入的 Windows 输入后端。
- `python -m app m3` 使用 `InMemoryPageAdapter` 做离线回归；`M3_SELECTION.md` 记录轻量执行器的选型结论和真实 Edge 现场限制。
- 动态表格只允许新增并验证原有顺序，删除/保存/返回/提交统一阻断；人员选择必须唯一匹配；敏感人员主数据仍不自动猜填。

## M4 端到端 MVP

```text
python -m app m4
```

会启动和 M2 类似的 M4 Tk 小程序。程序内可以选择草稿/PDF、加载人工配置、绑定窗口、运行当前草稿或运行 50 份离线回归。证书字段导入、动态权利人/发明人表格、第一发明人唯一选择、页面滚动、既有附件核验和最终回读都不会点击保存。

如果只需要在控制台执行 50 份回归，使用：

```text
python -m app m4 --headless
```

单份或一批 PDF/JSON 草稿可直接运行：

```text
python -m app m4 "C:\path\证书目录" --manual manual.json --output m4-report.json --diagnostics diagnostics
```

`manual.json` 只能提供明确的人工/配置字段；未提供的摘要、项目、PCT、人员主数据和经办人字段会跳过，不会从证书猜填。`--window-title` 可在 Windows 上绑定已打开的浏览器窗口；暂停、急停、可重试动作和错误状态会保留在最终报告中。诊断包默认脱敏身份证号、联系方式和经办人敏感字段。
## M5 内网现场适配

M5 默认使用 `resources/web_profiles/intranet_v1.json`，先对现场截图执行只识别：

```text
python -m app m5 --mode recognition_only --image "C:\path\field.png" --annotated field.annotated.png --output field.report.json
```

现场 Windows 运行时也可以绑定已打开的 Edge/Chrome 窗口；窗口会在每次截图前刷新并检查前台状态：

```text
python -m app m5 --mode recognition_only --window-title "专利信息库" --output field.report.json
```

单步动作必须显式提供 JSON，动作前会重新截图并检查页面锚点/状态，保存、提交、返回和删除始终阻止。动作完成后只记录页面安全回读，字段业务值仍需用户逐项核对：

```json
{"actions": [{"control_id": "patent_no", "kind": "fill", "value": "2020104300960"}]}
```

```text
python -m app m5 --mode step --window-title "专利信息库" --actions actions.json --diagnostics diagnostics
```

构建现场源安装包：

```text
python -m app m5-package --output build/m5_field_package.zip
```

安装包不联网安装依赖，现场需将管理员审核后的离线 wheel 放入包内 `vendor/`。真实内网像素模板不能由离线开发环境伪造；首轮只识别通过后，按 `resources/image_templates/intranet_v1/manifest.json` 登记经审核的稳定控件模板。

## M6 稳定性与批量能力

M6 增加本地持久化任务队列、阶段 checkpoint、失败恢复、profile/人工配置版本回滚、诊断脱敏和 ZIP 备份。默认遇到页面异常、低置信度、编辑态不符或动态表格异常立即安全暂停当前任务，后续任务不自动继续；保存、提交、返回和删除仍由用户人工执行。

```text
# 查看和维护队列
python -m app m6 enqueue "C:\path\patents" --queue .m6\queue.json
python -m app m6 list --queue .m6\queue.json
python -m app m6 recover --queue .m6\queue.json

# 升级前创建脱敏备份
python -m app m6 backup .m6 --output .m6\backups\before-upgrade.zip

# 构建 M6 离线安装包
python -m app m6-package --output build\m6_offline_package.zip
```

M6 的安装、升级、profile 兼容性检查、配置回滚和现场运行清单见 [`M6_OPERATIONS.md`](M6_OPERATIONS.md)。

## M7 一键自动填写

M7 首页默认流程是“打开现有已登录 Edge 页面、上传并校对证书、准备 Edge 扩展任务”。直接启动：

```text
python -m app
```

桌面界面依赖官方 Windows Python 自带的 Tcl/Tk；若启动提示该组件不可用，请用 Python 安装程序执行 Modify/Repair 并勾选 `tcl/tk and IDLE`。

也可以显式启动 `python -m app m7`。打开待填写的 Edge 专利信息页后：

1. 上传一份 PDF 或已校对 JSON，修正并确认所有待复核字段；
2. 点击“准备 Edge 扩展任务”；
3. 在当前专利页面打开扩展，点击“读取当前任务”和“开始预填”；
4. 扩展完成后人工检查并手动保存。

程序只发布唯一的已审核本地任务。扩展按真实 DOM 控件逐项填写并回读：默认空字段会填写、相同值会跳过、已有冲突值立即停止；如需更新已有值，必须在桌面程序明确勾选并确认“允许覆盖已有值”后重新发布覆盖任务。页面指纹/Profile 不一致、控件不唯一或回读失败时仍立即停止。填写成功后停在保存前，由用户检查并手动保存。

仿真、只识别、显式单步、受控批量、配置/Profile 和诊断入口保留在“高级设置”中：

- `simulation` 只使用 `InMemoryPageAdapter`，用于可重复的离线验证，不代表真实网页已填写；
- `recognition_only` 使用 M5 截图识别，不发送输入；
- `step` 和“旧版屏幕填写”保留兼容的显式动作调试入口；正式单文件填写使用 Edge DOM 扩展；
- `controlled_batch` 使用 M6 本地队列和 checkpoint，默认首个失败即停机，并只在离线模拟页执行批量回归。

Edge 扩展 Profile 通过脱敏 DOM 校准文件维护。`dom-poc-v3` 在基础字段之外已校准权利人表、发明人表、第一发明人及两个合并字段；复杂字段仍需在准备任务时显式启用。窗口尺寸和缩放不参与 DOM 坐标定位，但仍应在现场验证页面响应式布局。展开 M7 高级设置仍可使用屏幕“只识别定位”“读取当前值”“确认并测试”。

程序不会自动点击保存、提交、返回或删除，也不会自动创建下一条记录。M7 的安装、升级、测试命令和现场操作见 [`M7_OPERATIONS.md`](M7_OPERATIONS.md)，现场验收记录使用 [`M7_FIELD_ACCEPTANCE_RECORD.md`](M7_FIELD_ACCEPTANCE_RECORD.md)。构建最终离线包：

```text
python -m app m7-package --output build/m7_offline_package.zip
```

For the bundled Windows OCR runtime, run `start_m7.cmd` or `start_mock_site.cmd` from the project root. The local PaddleOCR CPU models are stored under `resources/ocr_models/paddle`; Tesseract is not required.
