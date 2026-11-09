# The Watcher

每隔几分钟检查一次训练日志。发现报错、停滞或日志丢失时，Watcher 会在私有 GitHub 仓库里发 Issue。开启 GitHub 邮件通知后，可以在邮箱里收到提醒。

## 安装

服务器需要 Linux 或 Unix、Python 3.8+ 和正在运行的 cron。先建一个私有 GitHub 仓库，启用 Issues，再给这个仓库创建一个有 **Issues 读写权限**的 token。

```sh
git clone https://github.com/AoiTsuki420/the-watcher-code.git
cd the-watcher-code
python3 install.py
```

按提示填写，最后输入 `y` 安装。默认每 5 分钟检查一次日志。

## 添加和移除日志

在默认安装目录添加日志。任务名要唯一，日志文件要能读。

```sh
cd ~/.local/train_watch
python3 train_watch.py add my-experiment /path/to/train.log
```

用同一个任务名移除监控。原日志会保留。

```sh
python3 train_watch.py remove my-experiment
```

新目标从首次检查时的日志末尾开始读取。用自定义目录安装时，把命令中的目录换成实际路径，并将 `TRAIN_WATCH_DIR` 设为该目录。

## 查看运行情况

在运行目录里查看最近的巡检记录，或预览本轮检查。

```sh
tail -n 30 watch.log
python3 train_watch.py --dry-run
```

要测试邮件，用 `python3 train_watch.py --test-notify` 创建并关闭一条测试 Issue。

## 什么时候提醒

- **报错**。例如程序崩溃、CUDA 显存不足、进程以非零状态退出，或 loss 出现 NaN。普通 warning 会跳过。
- **停滞**。新目标默认 60 分钟没有更新就提醒。在 `config.json` 里改该目标的 `stall_minutes`，填 `0` 可关闭。安装器默认在连续停滞告警 48 小时后暂停该目标，阈值由 `alerts.auto_remove_stalled_hours` 控制。
- **日志丢失**。读过的日志文件消失时提醒。
- **正常完成**。安装时可以填写成功和失败标记文件，也可以参考配置示例里的 `completion_notification`。让实验在成功结束后生成成功标记。失败标记优先，每轮实验用新的 `run_key`，同一轮只提醒一次。首次检查时已完成的任务默认跳过。[配置示例](./config.example.json)

Issue 和邮件只显示任务名称、告警类型和状态。详细日志留在服务器上。

## 更新

拉取新版后，把 `train_watch.py` 更新到 cron 使用的路径，继续使用原来的 `config.json`、`state.json` 和 `github.token`。配置和 token 请保存在运行目录。

开发时可运行 `python3 -m unittest discover` 做离线检查。
