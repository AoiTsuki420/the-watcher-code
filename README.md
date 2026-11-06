# The Watcher

在服务器上定时检查训练日志，通过私有 GitHub 仓库的 Issue 发出告警。Python 3.8+，仅使用标准库；依赖 Linux/Unix 的 `fcntl` 文件锁。GitHub Actions 不负责执行监控。

## 文件与来源

- `train_watch.py`：增量日志巡检、告警状态管理和 GitHub/可选 SMTP 通知。
- `rns_eta.py`：现有 embedding 流程的一次性剩余时间提醒扩展。
- `config.example.json`：全部使用占位符，监控和 ETA 默认关闭。
- `test_eta.py`：隔离回归检查，使用 Fake Notifier，不访问网络、不发邮件。

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

`state.json` 保存偏移、事件和 ETA 去重信息；不要随意删除。改用新运行目录会失去既有去重状态。巡检异常会打印 traceback，顶层捕获后进程仍可能返回 0，因此不能只靠退出码判断健康，需检查 `watch.log` 和状态更新时间。

## 可选 ETA 扩展

这是现有三 Pod embedding 工作流的原始扩展，通知文案仍描述该工作流及其训练验收条件。用于其他任务前需要适配文案和生产端协议。普通日志监控无需开启。

启用时，填写 `eta_reminder` 的所有路径、`expected_total` 和本轮唯一 `run_key`；同一轮保持 `run_key` 稳定。使用绝对路径，ETA 扩展不会展开这些路径中的 `~` 或环境变量。

生产端需原子更新 JSON，`time` 为 Unix 秒时间戳：

```json
{"time": 1700000000}
```

以上为 `active_path` 心跳格式；`progress_path` 格式如下：

```json
{"schema":1,"time":1700000000,"total":100000,"valid":80000,"skipped":0,"pending":20000}
```

`valid + skipped + pending` 必须等于 `total`，计数为非负整数。实际运行须填当前时间戳。协调器心跳超过 90 秒、进度超过 300 秒、失败标记存在或完成标记存在时不发 ETA 提醒。最近约一小时样本至少覆盖 15 分钟、有正增量，且预计剩余不超过 3600 秒时提醒一次；处理速度计入有效与跳过数量，处理完成不代表跳过项已修复。失败通知会重试；恢复通知结果时只查最近 100 个 Issue，去重并非无限历史或严格 exactly-once 保证。

## 隐私与凭据

代码仓库和告警仓库都应保持 Private。私有仓库中的 Issue 仍对有权限的协作者、组织管理员或安装的相关应用可见；单条 Issue 不能额外设成“仅本人”。如需要只有自己能看，使用个人私有且无其他协作者的告警仓库，并检查应用访问权限。

告警正文会包含日志绝对路径、错误上下文和日志尾部；脚本没有自动脱敏功能。只监控适合发往该私有仓库的日志；日志含秘密时须先在生产端脱敏，或适配通知正文后再启用。关闭/删除 Issue 与撤销仓库权限不能撤回已经发出的邮件或别人已保存的内容。

`.gitignore` 排除了真实 `config.json`、token/password 文件、状态、日志、锁和缓存。只提交源码、示例、文档及隔离测试；上传前仍应检查暂存差异，忽略规则不会移除历史提交中的秘密。

## 离线验证

```sh
python3 -m py_compile train_watch.py rns_eta.py test_eta.py
python3 test_eta.py
```

测试使用临时目录与 Fake Notifier，覆盖 ETA 门槛、过期数据、计数校验、失败重试、去重及真实主入口的暂停目标清理和 dry-run 状态保护。
