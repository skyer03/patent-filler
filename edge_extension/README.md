# Edge 扩展 PoC

该扩展只负责将本机已审核任务预填到当前 Edge 页面。它不读取 PDF、Cookie、浏览历史或登录令牌，不连接公网，也不会点击保存、提交、返回、删除或新建记录。

## 安装

1. 在 PowerShell 运行。生成脚本会移除 PoC 使用的 `activeTab`，正式目录只保留指定 Origin 的 `host_permissions`：

   ```powershell
   .\edge_extension\install\configure_enterprise_extension.ps1 -PatentOrigin "https://实际专利系统域名"
   ```

2. 打开 `edge://extensions`，启用开发人员模式，加载脚本输出的 `%LOCALAPPDATA%\PatentAutofill\EdgeExtension`。记录 Edge 显示的扩展 ID。
3. 注册当前用户级本机组件：

   ```powershell
   .\edge_extension\install\register_native_host.ps1 -ExtensionId "edge显示的32位扩展ID"
   ```

4. 用桌面程序上传并确认一份证书，点击“准备 Edge 扩展任务”。
5. 切换到空白专利记录页，打开扩展，依次点击“读取当前任务”和“开始预填”。
6. 扩展提示完成后人工核对页面，再手动保存。

正式部署时应将扩展发布到企业受控渠道或 Edge Add-ons，获得固定扩展 ID 后重新注册本机组件，并由 IT 使用扩展白名单/强制安装策略部署。开发人员模式只用于 PoC。

## 脱敏校准

在空白新建页点击“导出脱敏 DOM 校准”。导出内容只有 Origin、已知字段标签、控件类型、允许名单内的属性和 iframe Origin；不包含输入值、Cookie、令牌、URL 路径、页面正文或业务记录文本。导出文件仍应先由业务人员审核，再决定是否外发。

## 安全边界

- Profile 版本不匹配、页面指纹不匹配、控件零匹配/多匹配或写后回读不一致时立即停止；默认任务遇到已有非空冲突值也停止，覆盖任务仅在桌面程序人工确认后允许更新基础字段。
- 默认只允许 `empty_or_same` 覆盖策略；只有桌面程序明确勾选并确认“允许覆盖已有值”后，任务字段才使用 `reviewed_value`。覆盖模式仍不会处理保存、提交、返回、删除或自动新建。
- 当前 PoC 默认只启用六个基本字段，其中“联合申请”只有在人工配置中明确提供时才进入任务。
- 遇到跨域 iframe，需要把该 iframe 的精确 Origin 加入正式扩展的 `host_permissions`；不得使用 `<all_urls>`。
- closed Shadow DOM、Canvas 或无语义自定义控件转人工/UIA处理，不恢复整页图像点击。

## 复杂控件

扩展 `1.2.0` / Profile `dom-poc-v3` 已按真实 DOM 实现动态权利人/发明人表格、第一发明人文本框和两个合并字段。任务默认不包含这些字段；使用 `python -m app dom prepare reviewed.json --include-complex` 显式启用。表格加号使用以元素中心为坐标、带正确按钮状态和点击次数的完整 Pointer/Mouse 事件序列；写入行内编辑器后使用 Enter 提交 ExtJS CellEditing 事务，并仅在 Store 反映到表格时继续。默认模式仍要求现有行是目标名单的正确前缀；人工确认覆盖模式可按原行号改写名称并追加缺失尾行，但现有行多于目标名单时停止，始终不删除或重排。表格重绘期间按唯一 DOM 值节点回读，不把暂时隐藏的值节点误判为缺失。第一发明人直接写入证书首位发明人，合并字段使用英文逗号，身份证号和联系方式始终不填。

最终验证由现场人员在获准的真实内网页面完成。离线脚本和单元检查不作为正式验收结论，人工步骤见项目根目录的 `EDGE_EXTENSION_OPERATIONS.md`。
