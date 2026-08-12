# Edge DOM 预填、安装与现场校准说明

## 1. 运行边界

主流程为：桌面程序本地解析证书并人工确认 → 发布唯一的 `ready_for_fill` 任务 → Edge 扩展读取任务 → 在当前已登录页面按 DOM 逐字段预填和回读 → 用户人工检查并保存。

扩展、本机组件和安装脚本均不需要公网。扩展不会读取 PDF、Cookie、浏览历史或登录令牌，不会点击保存、提交、返回、删除，也不会自动创建下一条记录。

## 2. 准备任务

桌面界面中选择一份 PDF 或已校对 JSON，处理全部待复核字段，然后点击“准备 Edge 扩展任务”。也可使用命令行：

```powershell
python -m app dom prepare m0\golden\001-ZL202010430096.0.json
python -m app dom status
```

若需要明确填写“联合申请”，使用受控人工配置：

```json
{
  "manual_fields": {
    "joint_application": "否"
  }
}
```

再执行：

```powershell
python -m app dom prepare reviewed.json --manual manual.json
```

任务和结果只写入 `.m6\dom-bridge\`。生成新任务会替换上一个“等待扩展处理”的任务，确保扩展只能取得唯一任务。

## 3. 安装 PoC 扩展

先用实际专利系统的精确 Origin 生成安装目录。生成脚本会移除源码 PoC 中的 `activeTab` 权限，正式目录只保留该 Origin 的 `host_permissions`：

```powershell
.\edge_extension\install\configure_enterprise_extension.ps1 -PatentOrigin "https://实际专利系统域名"
```

打开 `edge://extensions`，启用开发人员模式，加载 `%LOCALAPPDATA%\PatentAutofill\EdgeExtension`。记录扩展 ID，然后注册当前用户级 Native Messaging 组件：

```powershell
.\edge_extension\install\register_native_host.ps1 -ExtensionId "扩展ID"
```

本机组件是一个不常驻、不监听端口的启动桥。Edge 只允许清单中指定的扩展 ID 调用它；它启动当前项目的 Python 运行时并执行 `python -m app --native-host`。

正式使用应由 IT 发布固定 ID 的企业扩展，并通过 Edge 策略限制安装来源、权限和运行站点；之后用正式 ID 重新注册本机组件。

## 4. 现场脱敏校准

1. 登录专利系统并打开空白新建记录。
2. 打开扩展，点击“导出脱敏 DOM 校准”。
3. 人工审核导出的 JSON 后再决定是否外发。
4. 根据控件属性更新 `edge_extension\profiles\dom_profile.json`，同时提升 Profile 版本。
5. 用同一 Profile 版本重新准备桌面任务。

导出内容只包括 Origin、已知字段标签、控件类型、允许名单属性、开放式 Shadow DOM 信息和 iframe Origin；不包括输入值、URL路径、Cookie、令牌、页面正文或业务记录文本。

跨域 iframe 必须逐个将精确 Origin 加入正式扩展 `host_permissions`，不得使用 `<all_urls>`。closed Shadow DOM、Canvas 或无法唯一定位的控件转人工/UIA处理。

## 5. 预填与安全停机

1. 在桌面程序准备任务。
2. 切换到当前已登录的空白记录页。
3. 在扩展中点击“读取当前任务”，检查字段数量与 Profile 版本。
4. 点击“开始预填”。
5. 完成后逐字段人工检查，再手动点击网页保存。

以下任一情况会停止后续字段：

- 页面指纹或 Profile 版本不一致；
- 目标控件匹配数量不是 1；
- 页面出现错误、加载、校验失败或遮挡状态；
- 当前值非空且与目标值不同；
- 下拉/单选选项不能唯一匹配；
- 写入后的真实控件值与目标值不同；
- 本机组件断开或用户点击安全停止。

## 6. 验收记录

先在本地仿真页完成六个基本字段测试，再在真实空白页重复执行。每次记录任务 ID、扩展版本、Profile 版本、字段结果和是否停在保存前。验收至少覆盖空白、相同值、冲突值、零匹配、多匹配、错误状态、用户停止、窗口移动和 100%/125% 缩放。

卸载当前用户本机组件：

```powershell
.\edge_extension\install\unregister_native_host.ps1
```

## 7. 复杂控件按现场校准启用

桌面界面默认只发布五个证书基础字段，以及人工明确提供时的“联合申请”。扩展 `1.2.0` / Profile `dom-poc-v3` 已按真实 DOM 校准动态权利人表、发明人表、第一发明人文本框和两个合并字段；这些字段仍需显式启用：

```powershell
python -m app dom prepare reviewed.json --include-complex
```

启用前逐项确认：两个表格本体、各自加号、新行名称单元格、行内输入框，以及 `ZL_DYFMRXM`、`ZL_ZLQRHB`、`ZL_FMRHB` 三个文本输入均只能匹配一个预期目标。默认模式只允许在现有行与审核草稿前缀一致时追加缺失尾部行；人工确认覆盖模式可按原行号改写名称并追加不足行，但页面行数多于目标名单时停止，不删除或重排。每次写入后使用 Enter 提交 ExtJS 编辑事务并完整回读。扩展不填写身份证号或联系方式，也没有删除行路径。第一发明人固定取证书从左到右的首位发明人，合并字段使用英文逗号连接审核后的顺序名单。

页面含跨域 iframe 时，将每个实际来源作为精确 Origin 传入生成脚本：

```powershell
.\edge_extension\install\configure_enterprise_extension.ps1 `
  -PatentOrigin "https://patent.example.internal" `
  -IframeOrigins @("https://frame.example.internal")
```

## 8. 交给现场人员的最终人工验收清单

本项目中的自动化检查只用于开发阶段发现明显回归，不代替真实内网页面的最终验收。建议你按以下顺序逐项调试，每个失败用新任务重试，不在同一任务上强行续跑：

1. 在空白新建页导出校准 JSON，人工确认其中没有输入值、页面正文、Cookie、令牌、URL 路径或业务记录文本。
2. 只启用基础字段，分别验证空白值、已有相同值、已有冲突值、零匹配、多匹配、错误页和加载中页面。
3. 对每次结果记录任务 ID、扩展版本、Profile 版本、预期值、页面回读值、停止原因，以及保存/删除按钮是否保持未触发。
4. 连续完成 20 次基础字段预填；窗口移动与 100%/125% 缩放各覆盖一次。
5. 使用 `--include-complex` 分别验证空表、已有正确前缀、已有错误行、短名单、长名单、第一发明人和两个合并字段。
6. 模拟扩展断开、本机组件未注册、任务文件损坏、Profile 版本不一致和用户中途停止。
7. 每次都停在保存前，由人工逐字段检查后自行决定是否保存；扩展测试期间不要授权自动保存、提交、删除或新建记录。

通过标准：错误页面和未知版本发送输入次数为零；默认冲突值从不覆盖，表格按行覆盖必须有桌面人工确认；表格不删除、不重排，未冲突的正确行不重复编辑；第一发明人与发明人首行一致；结果日志只保存值是否存在、长度和哈希；所有自动流程的保存、提交、返回、删除和新建次数均为零。

开发用的 [`tools/playwright_dom_smoke.js`](tools/playwright_dom_smoke.js) 仅是离线仿真参考。最终结论以你在获准的真实内网页面上填写的现场验收记录为准。
