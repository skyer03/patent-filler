# M7 统一入口、安装、测试与现场操作说明

> 2026-08-10：真实网页默认执行器已切换为企业 Edge DOM 扩展与 Native Messaging。本文件中的屏幕识别/单步操作继续作为高级诊断路径；扩展安装、脱敏校准和正式预填流程以 [`EDGE_EXTENSION_OPERATIONS.md`](EDGE_EXTENSION_OPERATIONS.md) 为准。

M7 的统一入口是：

```text
.runtime\python312-full\python.exe -m app
```

也可以显式启动 `python -m app m7`。程序默认停在最终保存前，不会自动点击保存、提交、返回或删除，也不会自动创建下一条记录。

如果启动提示“Python 的 Tcl/Tk 桌面组件不可用”，请使用官方 Windows Python 安装程序执行 Modify/Repair，并安装 `tcl/tk and IDLE` 组件；这属于桌面运行时，不能通过 `pip install` 补齐。

## 1. 安装与升级

在联网环境准备管理员审核的离线 wheel，放入 M7 安装包的 `vendor/`，然后在目标 Windows 终端执行：

```powershell
.\install\install.ps1
```

首次启动前先运行：

```powershell
.\install\run_m7_tests.cmd
.\install\run_m7_golden.cmd
```

升级前先暂停当前任务并确认没有 `running` 队列任务；然后执行：

```powershell
python -m app m6 backup .m6 --output .m6\backups\before-m7-upgrade.zip
.\install\upgrade.ps1
```

将新程序解压到新目录，不覆盖旧程序目录和旧 `.m6`。升级后先做离线测试、50 份 golden 和现场只识别；失败时保留诊断并切回旧程序目录。

## 2. 一键自动填写

1. 按 `EDGE_EXTENSION_OPERATIONS.md` 安装精确 Origin 的扩展和当前用户级 Native Messaging 组件。
2. 在现有已登录 Edge 中打开当前要填写的空白专利表单。
3. 启动 M7，选择一份 PDF 或已校对 JSON；必填字段缺失、低置信度或 `待复核` 时不能准备扩展任务。
4. 必要时在字段表中修正并确认，然后点击“准备 Edge 扩展任务”。
5. 在当前页面打开扩展，点击“读取当前任务”和“开始预填”。扩展逐字段检查唯一 DOM 目标、冲突值和写后回读。
6. 扩展显示完成后，在网页上逐字段检查并手动保存。

默认已有相同值会跳过，已有冲突值会立即停止；如确需更新已有记录，必须在桌面程序勾选“允许覆盖已有值”并确认后重新生成任务。覆盖任务仍逐字段回读，且不会保存、提交、返回、删除或新建记录。控件零匹配/多匹配、Profile版本不符、错误页、加载中、弹窗或 DOM 回读失败时立即停止，后续字段不再执行。屏幕失焦、OCR和坐标错误只属于高级旧版执行器，不再影响默认 DOM 路线。

摘要、项目、PCT、人员主数据和经办人等非证书字段只读取当前已激活的本地配置，未配置字段保持原状。程序始终禁止自动保存、提交、返回、删除或新建下一条记录。

仿真、只识别、显式动作调试、受控批量、配置/Profile 和诊断功能位于“高级设置”。旧 Profile 如果没有回读定义，只能执行只识别或显式单步，不能启用一键更新。

## 3. 队列恢复

```powershell
python -m app m6 list --queue .m6\queue.json
python -m app m6 recover --queue .m6\queue.json
python -m app m6 retry task-xxxxxxxxxxxx --queue .m6\queue.json
```

程序异常退出后会把 `running` 任务变为 `paused`，不会自动重跑，也不会跳过到下一条。重试前先查看 checkpoint、报告和脱敏诊断；超过最大尝试次数时必须显式使用 `--force`。

## 4. 测试操作说明

### 4.1 启动本地测试网页和程序

先在项目根目录打开两个 PowerShell 窗口。窗口 A 启动空白测试表单：

```powershell
.runtime\python312-full\python.exe -m app mock-site --open
```

真实屏幕识别使用项目内置 PaddleOCR CPU 运行时和中文模型，不需要安装 Tesseract；运行时或本地模型缺失时，程序会安全停止，不会发送输入。

浏览器应打开 `http://127.0.0.1:8765/`，标题为“专利信息库 - M2 离线仿真页”。页面默认是空白新记录；“载入冲突样例”用于验证程序是否会拒绝覆盖，“空白新记录”用于恢复。

窗口 B 启动 M7：

```powershell
.runtime\python312-full\python.exe -m app
```

按 `EDGE_EXTENSION_OPERATIONS.md` 为 `http://127.0.0.1:8765` 生成 PoC 扩展并注册本机组件。在 M7 中上传 `m0\golden\001-ZL202010430096.0.json`，点击“准备 Edge 扩展任务”，再从扩展读取并开始预填。期望：六个基本字段自动填写并回读；冲突样例保持原值；保存按钮没有被点击。

真实屏幕识别使用项目内置 PaddleOCR CPU 运行时和中文模型，不需要安装 Tesseract；运行时或本地模型缺失时，程序会安全停止，不会发送输入。

### 4.2 自动化回归

在项目根目录、已激活虚拟环境中执行：

```powershell
.runtime\python312-full\python.exe -m unittest discover -s tests -v
```

期望：所有测试通过，其中包括 M7 一键编排、差异规划、冲突/低置信度/急停保护、真实/模拟执行器边界、队列恢复、敏感字段脱敏和离线 50 份受控批量。

单独运行 M7 测试：

```powershell
.runtime\python312-full\python.exe -m unittest tests.test_m7 -v
```

运行 M7 离线 50 份 golden：

```powershell
.runtime\python312-full\python.exe -m app m7 m0\golden --headless --mode controlled_batch --queue .m7-test\queue.json --diagnostics .m7-test\diagnostics
```

期望报告中 `status` 为 `completed`、`processed` 为 `50`、`completed` 为 `50`、`paused`/`failed` 为 `0`；队列中 50 个任务均为 `completed`。该命令只使用 `InMemoryPageAdapter`，不打开浏览器、不发送屏幕输入、不点击保存。

现场只识别冒烟测试：

```powershell
.runtime\python312-full\python.exe -m app m7 --headless --mode recognition_only --window-title "专利信息库" --output .m7-test\field-report.json --annotated .m7-test\field.annotated.png --diagnostics .m7-test\field-diagnostics
```

现场单步测试需准备动作文件：

```json
{
  "actions": [
    {"control_id": "patent_no", "kind": "fill", "value": "2020104300960"}
  ]
}
```

```powershell
.runtime\python312-full\python.exe -m app m7 --headless --mode step --window-title "专利信息库" --actions actions.json --diagnostics .m7-test\step-diagnostics
```

第一次现场验收不使用 `--yes`。逐动作确认后检查动作前后截图、识别 JSON、执行日志和网页实际值；任何失焦、页面异常、编辑态不符、动态表格异常或回读不一致都应为 `paused`/`blocked`/`failed`，并留下诊断证据。

### 4.3 基本信息逐项测试（本轮新增）

展开“高级设置”后，使用“只识别定位”导出当前页面标注与脱敏报告；再在“基本信息逐项测试”中按“专利号 → 申请名称 → 申请受理日 → 授权公告日 → 申请类型 → 联合申请”逐项操作。每次测试只允许一个字段，确认后才执行，动作后会截图并回读。联合申请必须手动选择“是/否”。

报告中的 `input_status` 明确区分 `blocked_before_input`、`not_sent` 和 `sent`，`verification_status` 区分回读成功与失败。默认诊断目录为 `.m6/diagnostics/basic-info/<时间>`；保存、提交、返回、删除和自动新建始终不进入测试白名单。

虚拟页标题包含“专利信息库 - M2 离线仿真页”时使用模拟 Profile；真实页标题为“科技项目管理系统 信创版”时使用现场 Profile。现场 Profile 未完成校准前，一键更新保持禁用，只识别定位和逐项测试可用于生成校准证据。

## 5. 现场样本验收

至少按 [`M7_FIELD_ACCEPTANCE_RECORD.md`](M7_FIELD_ACCEPTANCE_RECORD.md) 记录四类样本：短名单、长名单、联合权利人和扫描证书。每类样本逐字段人工复核，记录窗口、缩放、profile 版本、动作后回读和保存前停机结果。验收记录不能用模拟成功替代真实网页证据。
