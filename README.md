# The Watcher

在服务器上定时检查训练日志，通过私有 GitHub 仓库的 Issue 发出告警。Python 3.8+，仅使用标准库；依赖 Linux/Unix 的 `fcntl` 文件锁。GitHub Actions 不负责执行监控。

## 文件与来源

- `train_watch.py`：增量日志巡检、告警状态管理和 GitHub/可选 SMTP 通知。
- `eta.py`：可选的通用任务剩余时间提醒，支持自定义阈值和计量单位。
- `config.example.json`：全部使用占位符，监控和 ETA 默认关闭。
- `install.py`：交互式安装配置和当前用户 cron，隐藏输入 token。
- `test_eta.py`、`test_policy.py`、`test_install.py`、`test_warnings.py`、`test_completion.py`：隔离回归检查，不访问真实 GitHub、不改真实 cron、不发邮件。

主程序顶部旧注释的“每小时”仅为示例，执行频率由 cron 决定；下面使用每 5 分钟。

## GitHub 仓库与通知设置

公开的 `the-watcher-code` 只用于分享代码和通用示例，不应作为私人训练告警的接收仓库。先在自己的 GitHub 账号下新建一个 **Private** 仓库，确认协作者与应用访问权限符合预期，再把它的 `OWNER/REPO` 填入本地配置。告警仓库可以为空，无需 Actions。不要将已有的私人告警仓库改成 Public。

在 GitHub 设置中为这个私有告警仓库创建细粒度个人访问令牌，仅选择该仓库并授予 Issues 读写权限；将其安全保存在运行服务器。然后在 GitHub 的通知设置中启用参与或被提及事件的邮件通知，确认接收邮箱。指派用户必须对目标仓库有权限；可用下面说明的手动通知测试验证链路。

## 交互式安装（推荐）

先准备一个私有告警仓库和仅对该仓库授予 Issues 读写权限的 token，并确认服务器有 Python 3.8+、`crontab` 命令及正在运行的 cron/crond 服务。若系统未安装 cron，需要先由管理员安装启用；脚本不使用 sudo，也不更改系统服务。

```sh
git clone https://github.com/AoiTsuki420/the-watcher-code.git
cd the-watcher-code
python3 install.py
```

按提示填写：

1. 独立运行目录，默认 `~/.local/train_watch`，必须位于 Git 仓库之外。
2. 私有告警仓库、接收提醒的 GitHub 用户名及 token。token 隐藏输入，不出现在命令行或配置摘要中；支持 HTTP(S) 代理。代理留空时沿用已有配置或当前环境代理，并保存在本地配置中供 cron 使用；输入 `-` 则不使用代理。
3. 任务名称、当前用户可读且已存在的日志文件路径，可一次添加多个目标。
4. 停滞告警阈值（默认 60 分钟），以及可选的正常完成标记、失败标记路径。标记文件可以尚未生成；只填写成功专用标记，不要把任意终态文件当成成功。
5. 持续告警后移出监控的阈值（默认 48 小时）及巡检间隔（默认每 5 分钟）。两种阈值均可设为 0 关闭。
6. 查看不含 token 的摘要，输入 `y` 完成安装。取消时不写配置或 cron。

安装器只读验证仓库为 Private、Issues 已开启、令牌能读取 Issues、用户可被指派；不自动建仓或创建 token。只读验证无法证明 Issues 写权限，也不保证邮件入箱，仍需正确授予权限并启用 GitHub 邮件通知。

确认后，脚本把两个运行模块复制到运行目录，生成 `config.json` 和独立的 `github.token`，目录权限为 `700`、受管文件为 `600`，并安装带标记的用户 cron。真实配置和 token 不写进 Git 仓库。普通监控从首次巡检时的日志末尾开始，ETA 默认关闭。

重复运行安装器可保留已有目标和读取状态、添加新名称的目标、复用已有 token，并更新受管 cron，不会重复追加。修改已有目标请直接编辑运行目录的 `config.json`。如果发现已有手工配置的 Watcher cron，安装器会停止：先用 `crontab -l` 检查，在 `crontab -e` 中只移除你确认由本次安装接管的那条 Watcher 任务，再重跑安装器；其他 cron 任务保留。

文件写入或 cron 更新失败时会尝试恢复原配置、源码、token 和 cron；若检测到并发修改则停止，避免覆盖。安装器不会重置 `state.json`、删除日志或自动发送测试邮件。安装完成后会显示显式使用运行目录的测试命令；需要验证邮件时手动执行该命令，它会创建并关闭测试 Issue。

查看安装结果：

```sh
crontab -l
tail -n 30 ~/.local/train_watch/watch.log
```

如果选择了自定义运行目录，请替换上面的日志路径。克隆目录后续 `git pull` 不会自动更新已安装副本；需要更新时，可将新版 `train_watch.py`、`eta.py` 复制到运行目录，保留本地配置、token 和状态。

## 手动配置与运行

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

运行用户须能读取目标日志，并能写入运行目录。锁会跳过重叠执行。`watch.log` 需要按本机策略轮转；上面的手动方式需要自己添加 cron，交互式安装器会自动添加。

## 检测与恢复

- 持久保存 inode、字节偏移和未完成行；处理日志轮转和截断。
- 默认匹配 Python 异常、CUDA OOM、NCCL 错误，以及 loss 等指标的 NaN/Inf。可通过 `crash_patterns`、`nan_patterns` 覆盖默认正则。
- 默认崩溃规则也识别 `Fatal Python error` 和非零 `exitcode` / `exit code`（例如 1、-9）；退出码 0 不告警。自定义 `crash_patterns` 会覆盖默认列表，应包含该文件实际使用的失败信号。
- 普通 WARNING/WARN、Python Warning 和 `[skip data/sample/batch …]` 及其连续异常堆栈不通知。该上下文支持跨巡检保存，最多覆盖 256 行；新的日志级别或普通进度行会结束上下文，明确的进程失败标记仍会报警。可用 `nonfatal_patterns` 自定义额外的非致命上下文起始正则（覆盖默认 skip/Python Warning 正则，WARNING/WARN 仍识别）。这是日志文本判断，不能单凭某个异常词确认实验是否退出；堆栈以外独立出现的异常、实际数值异常、文件缺失和停滞仍按原规则处理。
- `ignore_patterns` 在命中行及其后最多 24 行内匹配时抑制该命中；应使用窄规则，避免隐藏真实异常。
- `stall_minutes` 控制停滞阈值，设为 0 可关闭。任意 `completion_files` 存在时抑制停滞告警，不会自动验证文件内容。
- 文件消失告警仅在此前记录过该文件后触发。首次配置的路径不存在时不会立即报缺失，应先确认路径。
- 同一活动规则去重；持续问题默认每 12 小时提醒。崩溃/数值异常在累计 `resolve_after_lines` 条未再命中的新增非空日志后解除；全部规则解除后更新并关闭 Issue。这是日志启发式判断，不代表训练已经通过验收。
- `notify_on_start`、日常 `heartbeat.enabled` 默认关闭。监控只发通知，不重启任务、不修复缓存、不启动训练。

停滞告警连续超过 `alerts.auto_remove_stalled_hours` 小时后，自动将该目标的 `enabled` 设为 `false`，移出监控。配置示例为 48 小时，可在单个日志目标上用同名字段覆盖；设为 0 关闭自动移出；未配置此规则时也不自动移出。计时从首次停滞告警开始，恢复更新后重置。完成标记和日志缺失不会触发自动移出；不会删除原始日志。配置中的 `auto_removed` 记录原因和时间。重新启用时将 `enabled` 改回 `true`；如需只读新日志，设置 `start_at: "end"`，并可删除旧 `auto_removed` 记录。

`state.json` 保存偏移、事件和 ETA 去重信息；不要随意删除。改用新运行目录会失去既有去重状态。巡检异常会打印 traceback，顶层捕获后进程仍可能返回 0，因此不能只靠退出码判断健康，需检查 `watch.log` 和状态更新时间。

## 正常完成提醒

可在安装时填写成功标记路径，或在日志目标中启用 `completion_notification`。实验在全部步骤正常结束后，应原子生成一个**仅代表成功**的文件。监控器不会把日志停更、文件消失、进程不见或任意退出当作成功。

```json
"completion_notification": {
  "enabled": true,
  "name": "my-experiment",
  "run_key": "run-2026-01",
  "success_files": ["/path/to/SUCCEEDED.json"],
  "failure_files": ["/path/to/FAILED.json"],
  "require_all": true,
  "notify_existing": false
}
```

`name` 可选，默认日志目标的名称；`run_key` 必填，每次新实验更换。成功标记的文件内容不解析，因此必须选用可信的成功专用文件。默认要求所有 `success_files` 都存在，`require_all: false` 则任意一个存在即可；任何 `failure_files` 存在时不发完成提醒。`completion_files` 可能同时包含成功和失败终态文件，只用于抑制停滞，不能直接等同于成功。

首次巡检就已成功的历史实验默认静默；`notify_existing: true` 可显式补发，包括先前被记为历史完成而未通知的实验。已通知的运行不会反复提醒，新实验需更换运行标识。正常情况下，先建立 `watching` 状态，再在标记出现后的下一次巡检创建一条完成 Issue，由 GitHub 邮件通知；正文只含任务名称和“正常完成”。不额外创建关闭/评论消息。多个日志目标使用相同 `name` 和 `run_key` 可共用一条实验完成提醒，应采用一致的成功/失败标记配置。

去重信息保存在 `state.json` 的 `completion_notifications` 中。投递失败会重试，并用不包含运行明文信息的散列标记检查最近 100 个 Issue，以恢复可能已成功的投递；这不是无限历史或严格 exactly-once 保证。不要删除状态文件。只配置这项功能不会生成成功标记、修改实验代码或停止监控。

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
python3 -m unittest test_install
python3 -m unittest test_warnings
python3 -m unittest test_completion
```

测试使用临时目录与 Fake Notifier，覆盖 ETA 门槛、过期数据、计数校验、失败重试、去重及真实主入口的暂停目标清理和 dry-run 状态保护。
