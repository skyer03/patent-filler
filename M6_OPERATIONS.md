# M6 安装、升级、恢复与操作手册

M6 把批量能力限制在本地、可回退的状态层。队列只保存任务路径、阶段 checkpoint、错误码和诊断路径；不会自动点击保存、提交、返回或删除，也不会把身份证号、手机、邮箱等敏感值写入导出的诊断和脱敏备份。

## 1. 本地目录

建议把运行状态集中在项目旁边的 `.m6/`：

```text
.m6/
  queue.json                 # 原子写入的任务队列
  profiles/                  # 页面 profile 版本仓库
  manual-fields/             # 非证书字段配置版本仓库
  diagnostics/               # 每个任务的报告和失败证据
  backups/                   # 用户确认后创建的 ZIP 备份
```

队列状态包括 `queued`、`running`、`paused`、`failed`、`completed`。程序异常退出后，下一次启动先执行 `recover_orphaned()`，把未完成的 `running` 任务变成 `paused`，保留最后一个 checkpoint；不会从头静默重跑，也不会直接继续下一个任务。

命令行可查看和维护队列：

```text
python -m app m6 enqueue "C:\patents\001.pdf" "C:\patents\002.json" --queue .m6\queue.json
python -m app m6 list --queue .m6\queue.json
python -m app m6 recover --queue .m6\queue.json
python -m app m6 retry task-xxxxxxxxxxxx --queue .m6\queue.json
```

`retry`/`resume` 必须由用户显式执行；达到最大尝试次数后只能使用 `--force`，并应先查看诊断证据。

## 2. 批量运行规则

实际运行使用 `app.m6.run_m4_queue` 或 `M6BatchRunner`。默认 `stop_on_failure=True`：一个任务出现页面异常、低置信度、编辑态不符、动态表格行数/顺序/回读异常、窗口失焦或 M4 校对未通过时，当前任务安全停止，后续任务保持 `queued`。

每个处理器在解析、校对、自动化和报告阶段调用 `context.save_checkpoint(...)`。恢复前必须检查 checkpoint 指向的阶段和诊断截图，再决定重试或人工修正后继续。`validate_safety_state` 提供统一的页面状态、置信度、编辑态和动态表格安全门。

M6 不自动创建下一条记录；也不默认批量填写非证书字段。只有在业务确认字段来源、必填规则和人工复核方式后，才可通过显式 `VersionedConfigStore` 配置提供这些值。

## 3. 网站小幅变化兼容策略

页面 profile 按版本保存，使用 `ProfileRegistry`：

```python
from app.m6 import ProfileRegistry

profiles = ProfileRegistry(".m6/profiles")
profiles.install("resources/web_profiles/intranet_v1.json", activate=True)
check = profiles.check_compatibility(
    {"system_title", "module_title", "basic_info"},
    required_controls={"patent_no", "application_title"},
)
if not check.compatible:
    raise RuntimeError(check.missing_anchors or check.missing_controls)
```

只要最低页面锚点或本次动作所需控件缺失，就只识别并停机；可选锚点缺失只作为 warning，不能据此扩大自动填写范围。现场确认的新 profile 先安装但不激活，完成只识别和短名单回归后再 `activate(version)`。发现误识别时执行 `rollback()`，不要直接覆盖旧版本。

## 4. 非证书字段配置、回滚和脱敏

```python
from app.m6 import VersionedConfigStore

configs = VersionedConfigStore(".m6/manual-fields")
configs.save("2026-08-04-a", {"operator_name": "经办人", "operator_phone": "本地值"}, activate=True)
configs.export_redacted(".m6/manual-fields-review.json")
configs.rollback()
```

配置版本文件只在本机使用。诊断、队列导出和 ZIP 备份默认遮挡身份证号、联系方式、手机、邮箱等字段；如需受控恢复完整本地配置，必须由授权人员明确选择非脱敏备份，并在备份目录权限和生命周期上执行公司要求。不要把完整配置或截图上传到公共位置。

## 5. 安装、升级和备份

### 首次安装

1. 使用管理员审核过的离线 wheel 填充安装包的 `vendor/`。
2. 在目标 Windows 终端执行 `install/install.ps1`；脚本使用 `--no-index`，不会联网下载。
3. 先运行解析单元测试、M4 离线样本和 M5 只识别，确认 Python、Tesseract（扫描证书需要）和窗口策略。
4. 创建 `.m6/`，导入已审核的 profile，但先保持只识别模式。

### 升级

1. 停止当前批量任务，确认队列中没有 `running` 状态。
2. 先备份 `.m6/`：

   ```text
   python -m app m6 backup .m6 --output .m6\backups\before-upgrade.zip
   ```

3. 解压新版本到新的目录，运行 `install/upgrade.ps1`；不要覆盖旧目录中的 `.m6/`。
4. 先做 profile 兼容性检查和现场只识别，再运行一个短名单、长名单、联合权利人和扫描证书样本。
5. 通过人工逐字段复核后才切换批量运行。失败时回到旧程序目录或恢复备份，并按 checkpoint 重新审核。

### 恢复

恢复是人工确认动作，目标目录必须明确且不应是当前正在运行的目录：

```python
from app.m6 import BackupManager
BackupManager.restore(".m6/backups/before-upgrade.zip", ".m6-recovered")
```

恢复后先检查 `queue.json`、profile 激活版本和配置激活版本，再执行 `recover`；不要直接把恢复出的 `running` 任务当作已成功。

## 6. 每次现场运行清单

- [ ] 目标 Edge/Chrome 窗口标题、前台状态、Windows/浏览器缩放和 profile 版本已确认。
- [ ] 先只识别，最低锚点和本次控件均通过；错误页、加载页、弹窗和校验失败页均未继续操作。
- [ ] 当前任务的证书草稿已人工复核；低置信度名单已明确确认。
- [ ] 动态权利人/发明人表格的已有行未删除，新增行顺序和合并字段回读一致。
- [ ] 每个关键动作后已回读；编辑态不符、失焦、页面变化或任意异常均已暂停。
- [ ] 用户在最终保存前完成网页逐字段审核；程序未点击保存、提交、返回或删除。
- [ ] 失败报告、截图和 profile/config 版本已放入脱敏诊断目录。
