# The Watcher

在服务器上定时检查训练日志，通过私有 GitHub 仓库的 Issue 发出告警。Python 3.8+，仅使用标准库；依赖 Linux/Unix 的 `fcntl` 文件锁。GitHub Actions 不负责执行监控。

## 文件与来源

- `train_watch.py`：增量日志巡检、告警状态管理和 GitHub/可选 SMTP 通知。
- `eta.py`：可选的通用任务剩余时间提醒，支持自定义阈值和计量单位。
- `config.example.json`：全部使用占位符，监控和 ETA 默认关闭。
- `test_eta.py`、`test_policy.py`：ETA、停滞移出及通知隐私的隔离回归检查，不访问网络、不发邮件。

主程序顶部旧注释的“每小时”仅为示例，执行频率由 cron 决定；下面使用每 5 分钟。

## GitHub 仓库与通知设置

公开的 `the-watcher-code` 只用于分享代码和通用示例，不应作为私人训练告警的接收仓库。先在自己的 GitHub 账号下新建一个 **Private** 仓库，确认协作者与应用访问权限符合预期，再把它的 `OWNER/REPO` 填入本地配置。告警仓库可以为空，无需 Actions。不要将已有的私人告警仓库改成 Public。

在 GitHub 设置中为这个私有告警仓库创建细粒度个人访问令牌，仅选择该仓库并授予 Issues 读写权限；将其安全保存在运行服务器。然后在 GitHub 的通知设置中启用参与或被提及事件的邮件通知，确认接收邮箱。指派用户必须对目标仓库有权限；可用下面说明的手动通知测试验证链路。

## 配置与运行

将两个模块放在同一目录。源码目录与运行配置目录可以分开：

```sh
git clone https://github.com/AoiTsuki420/the-watcher-code.git
cd the-watcher-code
install -d -m 700 "$HOME/.local/train_watch"
cp config.example.json "$HOME/.local/train_watch/config.json"
chmod 600 "$HOME/.local/train_watch/config.json"
```

在服务器本地编辑 `config.json`：

1. `github.repo` 填接收告警的私有仓库 `OWNER/REPO`，`assignee` 填可被指派的 GitHub 用户名。该用户需要启用对应 GitHub 邮件通知；创建 Issue 不保证邮件实际入箱。
2. 将具有目标仓库 Issues 读写权限的 GitHub 令牌，通过本地安全编辑器写入运行目录 `github.token`，设置权限 `600`。不要将令牌写入命令行、源码或配置示例。运行时通知不需要 Contents 写权限。可选 `github.proxy` 只在本地配置。
3. 填写日志绝对路径和唯一 `name`，将需要监控的 `logs[].enabled` 改为 `true`。首次使用 `start_at: "end"` 只监控以后新增的日志。
4. 先预览，再运行一次。没有配置 GitHub 时，通知仅写入运行目录 `outbox.log`。

```sh
python3 train_watch.py --dry-run
python3 train_watch.py
```

默认运行目录为 `~/.local/train_watch`，可用 `TRAIN_WATCH_DIR=/absolute/runtime/path` 覆盖。`--dry-run` 不写状态、不发送通知，但仍创建运行目录和 `.lock`，也可能读取本地凭据。`--test-notify` 会真实创建并关闭测试 Issue，并在启用 SMTP 时发送邮件；仅在需要验证通知链路时手动运行。

在 `crontab -e` 添加下面一行，将源码目录替换为实际绝对路径；先确认没有重复的现有巡检任务：

```cron
*/5 * * * * /usr/bin/python3 /absolute/path/the-watcher-code/train_watch.py >> /home/YOUR_USER/.local/train_watch/watch.log 2>&1
```

运行用户须能读取目标日志，并能写入运行目录。锁会跳过重叠执行。`watch.log` 需要按本机策略轮转；脚本不自动安装 cron。

## 检测与恢复

- 持久保存 inode、字节偏移和未完成行；处理日志轮转和截断。
- 默认匹配 Python 异常、CUDA OOM、NCCL 错误，以及 loss 等指标的 NaN/Inf。可通过 `crash_patterns`、`nan_patterns` 覆盖默认正则。
- `ignore_patterns` 在命中行及其后最多 24 行内匹配时抑制该命中；应使用窄规则，避免隐藏真实异常。
- `stall_minutes` 控制停滞阈值，设为 0 可关闭。任意 `completion_files` 存在时抑制停滞告警，不会自动验证文件内容。
- 文件消失告警仅在此前记录过该文件后触发。首次配置的路径不存在时不会立即报缺失，应先确认路径。
- 同一活动规则去重；持续问题默认每 12 小时提醒。崩溃/数值异常在累计 `resolve_after_lines` 条未再命中的新增非空日志后解除；全部规则解除后更新并关闭 Issue。这是日志启发式判断，不代表训练已经通过验收。
- `notify_on_start`、日常 `heartbeat.enabled` 默认关闭。监控只发通知，不重启任务、不修复缓存、不启动训练。

停滞告警连续超过 `alerts.auto_remove_stalled_hours` 小时后，自动将该目标的 `enabled` 设为 `false`，移出监控。配置示例为 48 小时，可在单个日志目标上用同名字段覆盖；设为 0 关闭自动移出；未配置此规则时也不自动移出。计时从首次停滞告警开始，恢复更新后重置。完成标记和日志缺失不会触发自动移出；不会删除原始日志。配置中的 `auto_removed` 记录原因和时间。重新启用时将 `enabled` 改回 `true`；如需只读新日志，设置 `start_at: "end"`，并可删除旧 `auto_removed` 记录。

`state.json` 保存偏移、事件和 ETA 去重信息；不要随意删除。改用新运行目录会失去既有去重状态。巡检异常会打印 traceback，顶层捕获后进程仍可能返回 0，因此不能只靠退出码判断健康，需检查 `watch.log` 和状态更新时间。

## 可选 ETA 提醒

普通日志监控不需要 ETA；保持 `eta_reminder.enabled` 为 `false` 即可，此时也不需要部署 `eta.py`。需要提前完成提醒时，将 `eta.py` 放在主程序同目录，再开启配置。此模块适用于批处理、文件转换、数据下载、训练步骤等可报告已处理量和总量的任务。

任务需自行原子更新一个进度 JSON 文件，监控程序不会从任意日志中自动推断进度：

```json
{"time":1700000000,"completed":80000,"total":100000}
```

`time` 为实际采样时刻的 Unix 秒时间戳；示例时间须换成当前时间。`completed`、`total` 为非负整数，且 `completed <= total`；可以表示文件数、样本数、步骤数等同一种单位。任务生产端自行决定失败或跳过是否算作已处理，完成数量不等于成功数量。

在对应日志目标的 `eta_reminder` 中配置：

| 字段 | 含义与默认值 |
|---|---|
| `enabled` | 是否启用，默认关闭 |
| `run_key` | 必填，本轮唯一标识；同一轮保持稳定，新一轮更换 |
| `progress_path` | 必填，进度 JSON 的路径 |
| `threshold_seconds` | 剩余多久以内提醒，默认 3600 秒 |
| `window_seconds` | 估速使用的近期窗口，默认 3600 秒 |
| `min_window_seconds` | 最短有效观测跨度，默认 900 秒，不能大于窗口 |
| `progress_timeout_seconds` | 进度允许的最大年龄，默认 300 秒 |
| `unit` | 进度计量单位，默认“项”；最终通知摘要省略进度详情 |
| `active_path` | 可选心跳 JSON 路径，留空不检查心跳；格式为 `{"time":1700000000}` |
| `heartbeat_timeout_seconds` | 启用心跳检查时允许的最大年龄，默认 90 秒 |
| `failure_path` | 可选失败标记文件路径，存在时不发 ETA 提醒 |

路径支持 `~` 和环境变量；建议填写绝对路径。日志目标的 `completion_files` 同样可抑制 ETA 提醒。采样间隔应明显短于最短观测跨度，并与进度超时匹配。例如每 5 分钟巡检、每分钟刷新进度，可以使用默认参数。

估算采用近期窗口内 `completed` 的增量速度。在至少覆盖最短观测跨度、有正增量且预计剩余进入阈值时，每个目标的每个 `run_key` 提醒一次。总量变化、计数回退或时间回退会重置样本；完成、失败、过期或无效数据不会触发提醒。内部预计结束时间使用 UTC；最终通知只显示任务名称和“预计接近完成”状态，不执行后续任务。

发送失败会在下次巡检重试。恢复通知结果时只查最近 100 个 Issue，去重并非无限历史或严格 exactly-once 保证。仅写本地 outbox 时不算 GitHub 投递成功，满足提醒条件的后续巡检可能继续写入 outbox。

本版本使用通用的 `completed`/`total` 协议，不再读取旧专用扩展的 `valid`/`skipped`/`pending` 格式；迁移时需调整进度生产端或增加转换步骤。

## 隐私与凭据

接收真实监控信息的告警仓库应保持 Private；脱敏的源码仓库可以公开。私有仓库中的 Issue 仍对有权限的协作者、组织管理员或安装的相关应用可见；单条 Issue 不能额外设成“仅本人”。如需要只有自己能看，使用个人私有且无其他协作者的告警仓库，并检查应用访问权限。

新建 Issue、后续评论及 SMTP 邮件只包含配置中明确填写的任务 `name`、告警类型与状态，不发送日志内容、异常堆栈、路径、服务器地址、处理计数或进度详情。请将 `name` 填为适合通知展示的任务名称，不要在名称里写秘密。ETA 使用不含原始运行标识的散列标记去重。详细信息仅留在服务器本地状态和日志中。历史 Issue 和已发送邮件不会被自动改写或撤回。

`.gitignore` 排除了真实 `config.json`、token/password 文件、状态、日志、锁和缓存。只提交源码、示例、文档及隔离测试；上传前仍应检查暂存差异，忽略规则不会移除历史提交中的秘密。

## 离线验证

```sh
python3 -m py_compile train_watch.py eta.py test_eta.py
python3 test_eta.py
python3 -m unittest test_policy
```

测试使用临时目录与 Fake Notifier，覆盖 ETA 门槛、过期数据、计数校验、失败重试、去重及真实主入口的暂停目标清理和 dry-run 状态保护。
