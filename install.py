#!/usr/bin/env python3
"""Interactive, user-level Watcher setup. Python standard library only."""
import argparse
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SOURCE = Path(__file__).resolve().parent


def clean(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("输入不能包含换行或控制字符")
    return value.strip()


def ask(label, default=""):
    value = clean(input(label + (" [%s]" % default if default != "" else "") + ": "))
    return value if value else str(default)


def number(label, default, *, integer=False, minimum=0, maximum=None):
    while True:
        try:
            value = float(ask(label, default))
            if (not math.isfinite(value) or value < minimum
                    or (maximum is not None and value > maximum)
                    or (integer and not value.is_integer())):
                raise ValueError()
            return int(value) if integer else value
        except ValueError:
            print("请输入范围内的有效数字。")


def repository(value):
    value = clean(value)
    if value.startswith("https://github.com/"):
        url = urllib.parse.urlsplit(value)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("请填写 OWNER/REPO 或不带认证信息的 GitHub 仓库地址")
        value = url.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("仓库格式应为 OWNER/REPO")
    if value.split("/")[1] in (".", ".."):
        raise ValueError("无效仓库名")
    return value


def absolute_path(value):
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(clean(value)))))


def validate_runtime(path):
    if path.is_symlink():
        raise ValueError("运行目录不能是符号链接")
    path = path.resolve()
    if path == Path.home().resolve() or path == Path(path.anchor):
        raise ValueError("请选择独立运行目录")
    if any((parent / ".git").exists() for parent in (path,) + tuple(path.parents)):
        raise ValueError("运行目录须位于 Git 仓库之外，避免提交凭据")
    if path.exists():
        if not path.is_dir() or path.stat().st_uid != os.getuid():
            raise ValueError("运行目录必须由当前用户拥有")
        if any(path.iterdir()) and not (path / "config.json").is_file():
            raise ValueError("该目录非空且不是已有 Watcher 运行目录")
    for name in ("config.json", "github.token", "state.json", ".lock", ".install.lock",
                 "train_watch.py", "eta.py", "watch.log"):
        if (path / name).is_symlink():
            raise ValueError("运行目录中的受管文件不能是符号链接")
    return path


def read_crontab():
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                            env=env, timeout=15)
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and "no crontab for" in result.stderr.lower():
        return ""
    raise RuntimeError("无法读取当前用户 crontab；请检查安装及权限")


def write_crontab(content):
    result = subprocess.run(["crontab", "-"], input=content, capture_output=True,
                            text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("写入 crontab 失败")


def cron_block(runtime, interval, python):
    key = hashlib.sha256(str(runtime).encode()).hexdigest()[:16]
    begin, end = "# BEGIN the-watcher " + key, "# END the-watcher " + key
    args = ["env", "TRAIN_WATCH_DIR=" + str(runtime), str(python), "-B",
            str(runtime / "train_watch.py")]
    # Cron interprets percent signs before the shell, even inside shell quotes.
    command = (" ".join(shlex.quote(arg) for arg in args)
               + " >> " + shlex.quote(str(runtime / "watch.log")) + " 2>&1")
    command = command.replace("%", r"\%")
    return begin, end, "\n".join([begin, "*/%d * * * * %s" % (interval, command), end])


def merge_cron(original, runtime, interval, python):
    begin, end, block = cron_block(runtime, interval, python)
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == begin]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == end]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("已有 Watcher cron 标记不完整，请先修复")
        outside = "".join(lines[:starts[0]] + lines[ends[0] + 1:])
        # Refuse unmarked or extra Watcher jobs; do not guess which can be deleted.
        if any("train_watch.py" in line and not line.lstrip().startswith("#")
               for line in outside.splitlines()):
            raise ValueError("发现额外 Watcher cron，请先手动确认，避免重复巡检")
        prefix = "".join(lines[:starts[0]])
        suffix = "".join(lines[ends[0] + 1:])
        return prefix + block + "\n" + suffix
    if any("train_watch.py" in line and not line.lstrip().startswith("#")
           for line in original.splitlines()):
        raise ValueError("发现已有 Watcher cron，请先手动处理；安装器不会新增第二套")
    return original + ("\n" if original and not original.endswith("\n") else "") + block + "\n"


def verify_github(repo, token, assignee, proxy):
    """Read-only validation; no test Issues or email are sent."""
    # Use the same explicit proxy that cron will read from config, not an
    # unrecorded shell environment that may disappear in scheduled jobs.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"https": proxy, "http": proxy} if proxy else {}))

    def get(path):
        request = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": "Bearer " + token, "User-Agent": "the-watcher-setup",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        with opener.open(request, timeout=20) as response:
            data = response.read()
            return json.loads(data) if data else None

    try:
        info = get("/repos/" + repo)
        if info.get("private") is not True:
            raise ValueError("告警仓库必须为 Private；请勿使用公开源码仓库")
        if not info.get("has_issues"):
            raise ValueError("请先启用目标仓库的 Issues")
        get("/repos/" + repo + "/issues?state=open&per_page=1")
        get("/repos/" + repo + "/assignees/" + assignee)
    except urllib.error.HTTPError as error:
        raise RuntimeError("GitHub 验证失败（HTTP %s）；检查仓库、令牌权限和指派用户"
                           % error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("无法连接 GitHub；请检查网络或代理设置") from None


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".setup-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_install(runtime, config, token, original_cron, new_cron, original_config,
                  original_token, source=SOURCE):
    """Preserve state and unrelated jobs. Roll back files on installation failure."""
    import fcntl
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(runtime), 0o700)
    with (runtime / ".install.lock").open("a") as install_lock, \
            (runtime / ".lock").open("a") as watch_lock:
        os.chmod(str(runtime / ".install.lock"), 0o600)
        os.chmod(str(runtime / ".lock"), 0o600)
        fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(watch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = lambda name: (runtime / name).read_bytes() if (runtime / name).exists() else None
        if current("config.json") != original_config or current("github.token") != original_token:
            raise RuntimeError("配置或令牌在填写过程中已改变，请重新运行安装器")
        if read_crontab() != original_cron:
            raise RuntimeError("crontab 在填写过程中已改变，请重新运行安装器")
        content = {
            "train_watch.py": (source / "train_watch.py").read_bytes(),
            "eta.py": (source / "eta.py").read_bytes(),
            "config.json": json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
            "github.token": (token + "\n").encode("utf-8"),
        }
        for name in ("train_watch.py", "eta.py"):
            compile(content[name], name, "exec")
        saved = {name: current(name) for name in content}
        try:
            for name, data in content.items():
                atomic_write(runtime / name, data)
            write_crontab(new_cron)
            if read_crontab() != new_cron:
                raise RuntimeError("crontab 写入后核对失败")
        except BaseException:
            for name, data in saved.items():
                if data is None:
                    if (runtime / name).exists():
                        (runtime / name).unlink()
                else:
                    atomic_write(runtime / name, data)
            # Do not overwrite a concurrent crontab change during rollback.
            try:
                if read_crontab() == new_cron:
                    write_crontab(original_cron)
            except Exception:
                print("无法确认 cron 回滚结果，请运行 crontab -l 检查。", file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description="交互式配置 Watcher 和当前用户 cron，无需 sudo")
    parser.parse_args()
    if os.name != "posix":
        raise RuntimeError("安装器需要 Linux/Unix；Windows 请在 WSL 或服务器中运行")
    if not sys.stdin.isatty():
        raise RuntimeError("请在交互终端运行，token 必须通过隐藏输入填写")
    if not shutil.which("crontab"):
        raise RuntimeError("未找到 crontab；请先安装并启用 cron/crond，再运行安装器")
    runtime = validate_runtime(absolute_path(ask("运行目录（须在 Git 仓库之外）",
                                                str(Path.home() / ".local/train_watch"))))
    original_config = (runtime / "config.json").read_bytes() if (runtime / "config.json").exists() else None
    original_token = (runtime / "github.token").read_bytes() if (runtime / "github.token").exists() else None
    config = json.loads(original_config) if original_config else {}
    if not isinstance(config, dict) or not isinstance(config.get("logs", []), list):
        raise ValueError("已有配置格式不正确，请先修复")
    original_cron = read_crontab()
    # Check duplicates before requesting credentials.
    merge_cron(original_cron, runtime, 5, Path(sys.executable).resolve())
    gh = dict(config.get("github") or {})
    if gh.get("token_file", "github.token") != "github.token":
        raise ValueError("已有配置使用自定义 token_file，请手动维护，避免覆盖认证设置")
    repo = repository(ask("私有告警仓库（OWNER/REPO 或 GitHub URL）", gh.get("repo", "")))
    assignee = ask("接收提醒的 GitHub 用户名", gh.get("assignee") or repo.split("/")[0]).lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9-]+", assignee):
        raise ValueError("无效 GitHub 用户名")
    # getpass fallback may echo; require an actual terminal and treat its warning as an error.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("GitHub token（隐藏输入" + ("，留空保留已有 token" if original_token else "") + "）：").strip()
    if not token and original_token:
        token = next((line.strip() for line in original_token.decode().splitlines()
                      if line.strip() and not line.lstrip().startswith("#")), "")
    if not token or clean(token) != token or any(char.isspace() for char in token):
        raise ValueError("token 不能为空或包含空白字符")
    environment_proxy = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http", "")
    proxy_input = ask("GitHub HTTP(S) 代理（留空沿用已有/环境设置，输入 - 不使用）")
    proxy = "" if proxy_input == "-" else (proxy_input or gh.get("proxy", environment_proxy))
    if proxy and urllib.parse.urlsplit(proxy).scheme not in ("http", "https"):
        raise ValueError("代理地址须使用 http:// 或 https://")
    print("正在只读验证私有仓库、Issues 访问和指派用户，不发送通知……")
    verify_github(repo, token, assignee, proxy)
    print("验证通过。只读检查不能确认 Issues 写权限，请确保令牌授予 Issues: Read and write。")
    logs = list(config.get("logs") or [])
    while True:
        name = ask("任务名称（会出现在邮件中）")
        if not name:
            raise ValueError("任务名称不能为空")
        if any(item.get("name") == name for item in logs):
            raise ValueError("该任务名称已存在；已有目标将保留，请使用新名称或手动编辑配置")
        path = absolute_path(ask("日志路径（可使用 ~）"))
        if not path.is_file() or not os.access(str(path), os.R_OK):
            raise ValueError("日志文件必须存在且当前用户可读；请确认文件生成后重试")
        stall = number("多少分钟无更新后告警（0 关闭）", 60)
        logs.append(dict(name=name, path=str(path), enabled=True, start_at="end",
                         stall_minutes=stall, resolve_after_lines=200, notify_on_start=False,
                         completion_files=[], eta_reminder={"enabled": False}))
        if ask("继续添加其他日志？y/N", "N").lower() not in ("y", "yes"):
            break
    alerts = dict(config.get("alerts") or {})
    alerts["auto_remove_stalled_hours"] = number("停滞告警持续多少小时后移出监控（0 关闭）",
                                               alerts.get("auto_remove_stalled_hours", 48))
    interval = number("巡检间隔（分钟，1–59）", 5, integer=True, minimum=1, maximum=59)
    gh.update(repo=repo, assignee=assignee, token_file="github.token", proxy=proxy)
    config.update(github=gh, logs=logs, alerts=alerts)
    config.setdefault("email", {"enabled": False})
    config.setdefault("heartbeat", {"enabled": False})
    new_cron = merge_cron(original_cron, runtime, interval, Path(sys.executable).resolve())
    print("\n安装摘要")
    print("运行目录：", runtime)
    print("私有告警仓库：", repo)
    print("任务：", "、".join(str(item.get("name", "未命名")) for item in logs))
    print("巡检：每 %d 分钟；停滞移出阈值：%s 小时" % (interval, alerts["auto_remove_stalled_hours"]))
    print("token 不显示、不写入命令行；已有监控目标与 state.json 保留。")
    if ask("确认安装配置和当前用户 cron？y/N", "N").lower() not in ("y", "yes"):
        print("已取消，未写入配置或 cron。")
        return
    apply_install(runtime, config, token, original_cron, new_cron, original_config, original_token)
    print("安装完成。配置和 token 权限为 600，运行目录为 700。")
    print("cron 将执行运行目录中的独立源码副本；请确认系统 cron/crond 服务正在运行。")
    print("首次巡检从日志末尾开始；ETA 默认关闭。未发送测试通知。")
    print("可手动验证邮件（会创建并关闭测试 Issue）：")
    print(" ", " ".join(shlex.quote(arg) for arg in ["env", "TRAIN_WATCH_DIR=" + str(runtime),
          sys.executable, str(runtime / "train_watch.py"), "--test-notify"]))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消安装。", file=sys.stderr)
        sys.exit(1)
    except (ValueError, RuntimeError, OSError, getpass.GetPassWarning) as error:
        # Never print arbitrary network/subprocess exceptions or local config values.
        print("安装未完成：" + (str(error) if isinstance(error, (ValueError, RuntimeError))
                                  else "请检查文件权限、依赖及是否有巡检正在运行"), file=sys.stderr)
        sys.exit(1)
