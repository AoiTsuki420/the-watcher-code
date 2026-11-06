#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""train_watch.py — 训练日志巡检 + GitHub Issue 告警（仅标准库，适配 cron）

用法:
    python3 train_watch.py                # 巡检一次（cron 每小时调用）
    python3 train_watch.py --dry-run      # 预览动作：不写状态、不发通知
    python3 train_watch.py --test-notify  # 立即发一条测试通知（验证链路）
    TRAIN_WATCH_DIR=/path python3 train_watch.py   # 覆盖工作目录（测试用）

约定:
    - 所有文件位于工作目录（默认 ~/.local/train_watch）
    - GitHub 未配置（config.json 的 github.repo 为空）时，通知写入 outbox.log，
      watch.log 里会有醒目提示，不会真正发邮件
"""
import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import time
import traceback
import urllib.request
from datetime import datetime

DEFAULT_CRASH_PATTERNS = [
    r"Traceback \(most recent call last\)",
    r"(?i)\b(?:RuntimeError|ValueError|AssertionError|ImportError|KeyError|FileNotFoundError"
    r"|ChildFailedError|ProcessExitedException)\b",
    r"(?i)\b(?:CUDA|torch\.cuda)[^\n]{0,20}out of memory\b",
    r"(?i)\bCUDA error\b",
    r"(?i)\bNCCL\b[^\n]{0,60}\b(?:error|unhandled|timeout)\b",
    r"(?i)\bWatchdog caught collective\b",
    r"(?i)^\s*(?:Killed|Segmentation fault|core dumped)\b",
]

DEFAULT_NAN_PATTERNS = [
    r"(?i)\b(?:loss|val_loss|train_loss|gnorm|grad_norm|lr|ppl)\b[^\n]{0,30}?\b(?:nan|inf)\b",
    r"(?i)\b(?:nan|inf)\b[^\n]{0,10}\b(?:loss|detected)\b",
    r"(?i)\b(?:iptm|ptm|plddt|gpde|ranking_score|affinity_pred)\b\s*:\s*(?:nan|inf)\b",
]

DEFAULT_NONFATAL_PATTERNS = [
    r"(?i)^\s*\[(?:skip|skipping)\s+(?:data|sample|batch)\b",
    r"(?i)\b(?:UserWarning|RuntimeWarning|FutureWarning|DeprecationWarning):",
]
LOG_LEVEL = re.compile(
    r"(?:^|[\s\[|])(WARNING|WARN|ERROR|CRITICAL|FATAL|INFO|DEBUG)(?=$|[\s\]:|])", re.I)
EXCEPTION_LINE = re.compile(
    r"^\s*(?:[\w.]+(?:Error|Exception|Warning)|Exception|KeyboardInterrupt|SystemExit)(?::|$)")
FATAL_EXIT = re.compile(
    r"(?i)\b(?:ChildFailedError|ProcessExitedException|Watchdog caught collective|Fatal Python error)\b"
    r"|^\s*(?:Killed|Segmentation fault|core dumped)\b"
    r"|\bexit(?:code| code)\s*[:=]\s*(?!0\b)-?[1-9]\d*\b")

RULE_LABELS = {
    "crash": "崩溃/报错",
    "nan": "数值异常 (nan/inf)",
    "stall": "日志停滞",
    "missing": "日志文件消失",
}


def ts_str(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print("[%s] %s" % (ts_str(), msg), flush=True)


def base_dir():
    return os.environ.get("TRAIN_WATCH_DIR") or os.path.join(
        os.path.expanduser("~"), ".local", "train_watch")


def expand(path):
    return os.path.expanduser(os.path.expandvars(path or ""))


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def process_running(pattern):
    """返回 True/False/None（None=未配置或无法判断）"""
    if not pattern:
        return None
    try:
        res = subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=10)
        return res.returncode == 0
    except Exception:
        return None


def read_tail(path, n=15, max_bytes=65536):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
        lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        return lines[-n:]
    except Exception:
        return []


def nonfatal_mask(lines, previous=None, patterns=None):
    """Ignore warning/skip traceback blocks, carrying bounded context across polls.

    A new log level or ordinary progress line ends the block. Explicit process
    failure markers override inherited context, but not a warning on that line.
    This classifies log text, not the actual scheduler/process state.
    """
    remaining = min(256, max(0, int((previous or {}).get("remaining", 0))))
    patterns = DEFAULT_NONFATAL_PATTERNS if patterns is None else patterns
    compiled = [re.compile(pattern) for pattern in patterns]
    mask = set()
    for index, line in enumerate(lines):
        terminal = bool(EXCEPTION_LINE.match(line))
        level = LOG_LEVEL.search(line[:160]) if not terminal else None
        warning = level and level.group(1).upper() in ("WARNING", "WARN")
        if warning or any(pattern.search(line) for pattern in compiled):
            remaining = 256
            mask.add(index)
        elif FATAL_EXIT.search(line) or level:
            remaining = 0
        elif remaining and (line[:1].isspace() or terminal or
                            line.startswith(("Traceback (", "During handling", "The above exception"))):
            mask.add(index)
            remaining -= 1
        else:
            remaining = 0
    return mask, {"remaining": remaining}


def find_hits(lines, patterns, max_hits, ignore_patterns=None, skip_indices=None):
    """在 lines 中找命中；若命中行向下 25 行内出现 ignore_patterns 的内容，视为良性跳过。"""
    hits = []
    for idx, line in enumerate(lines):
        if skip_indices and idx in skip_indices:
            continue
        for pat in patterns:
            if pat.search(line):
                if ignore_patterns:
                    context = "\n".join(lines[idx:idx + 25])
                    if any(ig.search(context) for ig in ignore_patterns):
                        break
                hits.append((idx, line.strip()))
                break
        if len(hits) >= max_hits:
            break
    return hits


def excerpt(lines, idx, ctx=8):
    a = max(0, idx - ctx)
    b = min(len(lines), idx + ctx + 1)
    return "\n".join(lines[a:b])[:3000]


def retire_stalled_target(lc, active, default_hours, now=None):
    """Disable monitoring after a continuous stall; never delete the log."""
    now = time.time() if now is None else now
    name = lc.get("name") or os.path.basename(lc.get("path") or "log")
    stall = active.get(name + ":stall")
    if not lc.get("enabled") or not stall:
        return False
    try:
        hours = float(lc.get("auto_remove_stalled_hours", default_hours))
        since = float(stall["since"])
        if not math.isfinite(hours) or not math.isfinite(since) or hours < 0:
            raise ValueError("invalid stall retirement policy")
    except (ValueError, TypeError, KeyError, OverflowError):
        log("Invalid auto_remove_stalled_hours; automatic removal skipped")
        return False
    if hours == 0 or now - since <= hours * 3600:
        return False
    lc["enabled"] = False
    lc["auto_removed"] = {"reason": "stall_timeout", "at": now,
                          "stall_since": since, "threshold_hours": hours}
    return True


class Notifier(object):
    """GitHub Issue 通知；未配置或 dry-run 时降级为本地 outbox/dry-run 打印。"""

    def __init__(self, cfg, base, dry_run=False):
        gh = cfg.get("github") or {}
        self.repo = (gh.get("repo") or "").strip().strip("/")
        self.assignee = (gh.get("assignee") or "").strip().lstrip("@")
        self.proxy = (gh.get("proxy") or "").strip()
        self.dry_run = dry_run
        self.base = base
        # Names are explicitly allowed in notifications; no paths or log text.
        self.notification_names = [str(lc["name"]) for lc in (cfg.get("logs") or [])
                                   if lc.get("name")]
        token_file = expand(gh.get("token_file") or "github.token")
        if not os.path.isabs(token_file):
            token_file = os.path.join(base, token_file)
        self.token = self._load_token(token_file)

        em = cfg.get("email") or {}
        self.email_cfg = em
        self.email_enabled = bool(em.get("enabled"))
        self.email_password = ""
        if self.email_enabled:
            pw_file = expand(em.get("password_file") or "email.password")
            if not os.path.isabs(pw_file):
                pw_file = os.path.join(base, pw_file)
            self.email_password = self._load_token(pw_file)

    @staticmethod
    def _load_token(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except Exception:
            pass
        return ""

    @property
    def github_configured(self):
        return bool(self.repo) and bool(self.token)

    def _api(self, method, api_path, payload=None):
        url = "https://api.github.com" + api_path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "train-watch/1.0")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}

    def _send_email(self, subject, body):
        """SMTP 直发邮件（如 Outlook/Office365）；失败只记日志，不影响巡检。"""
        if self.dry_run:
            log("[dry-run] 将发送邮件: %s" % subject)
            return
        if not self.email_enabled:
            return
        cfg = self.email_cfg
        to = cfg.get("to") or []
        if isinstance(to, str):
            to = [to]
        to = [addr for addr in to if addr]
        username = (cfg.get("username") or "").strip()
        sender = (cfg.get("from") or username).strip()
        if not (sender and to):
            log("!! 邮件未配置完整（from/to 为空），跳过邮件通知")
            return
        if not self.email_password:
            log("!! 邮件未配置密码（email.password 为空），跳过邮件通知")
            return
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.header import Header
            host = cfg.get("smtp_host") or "smtp.office365.com"
            port = int(cfg.get("smtp_port") or 587)
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = sender
            msg["To"] = ", ".join(to)
            if cfg.get("use_ssl"):
                server = smtplib.SMTP_SSL(host, port, timeout=45)
            else:
                server = smtplib.SMTP(host, port, timeout=45)
                if cfg.get("use_starttls", True):
                    server.starttls()
            if username and self.email_password:
                server.login(username, self.email_password)
            server.sendmail(sender, to, msg.as_string())
            server.quit()
            log("已发送邮件: %s -> %s" % (subject, ", ".join(to)))
        except Exception as exc:
            log("!! 邮件发送失败: %s" % exc)
            self._local("邮件发送失败: " + subject, "%s\n\n%s" % (exc, body))

    def _safe_message(self, title, body):
        """Build outbound text from allowed names and fixed status labels only."""
        text = title + "\n" + body
        if "暂停或移除" in text or "自动移出" in text:
            status = "已移出监控"
        elif "已恢复" in text:
            status = "已恢复"
        elif "测试" in title:
            status = "通知测试"
        elif "预计剩余" in text or "预计接近结束" in text:
            status = "预计接近完成"
        elif "已开始" in text:
            status = "已开始"
        elif "巡检正常" in text:
            status = "正常"
        elif "持续告警" in text:
            status = "持续告警"
        elif "告警" in text:
            status = "告警"
        else:
            status = "状态更新"
        names = list(dict.fromkeys(name for name in self.notification_names if name in text))
        lines = ["任务：" + ("、".join(names) if names else "监控任务"), "状态：" + status]
        if status in ("告警", "持续告警"):
            rules = [label for label in RULE_LABELS.values() if label in text]
            if rules:
                lines.append("类型：" + "、".join(rules))
        # Preserve opaque ETA idempotency markers, never raw run identifiers.
        markers = re.findall(r"<!-- watcher-eta:([0-9a-f]{64}) -->", body)
        lines.extend("<!-- watcher-eta:" + marker + " -->" for marker in markers)
        return "监控提醒：" + status, "\n\n".join(lines)

    def create_issue(self, title, body):
        title, body = self._safe_message(title, body)
        self._send_email(title, body)
        if self.dry_run:
            log("[dry-run] 将创建 Issue: %s" % title)
            print("----- 通知预览 -----\n%s\n--------------------" % body[:2000])
            return 0
        if not self.github_configured:
            self._local("创建 Issue: " + title, body)
            return 0
        payload = {"title": title, "body": body}
        if self.assignee:
            payload["assignees"] = [self.assignee]
        data = self._api("POST", "/repos/%s/issues" % self.repo, payload)
        number = int(data.get("number") or 0)
        log("已创建 Issue #%s: %s" % (number, title))
        return number

    def comment(self, number, body):
        title, body = self._safe_message("监控状态更新", body)
        self._send_email(title, body)
        if self.dry_run:
            log("[dry-run] 将评论 Issue #%s" % number)
            print("----- 评论预览 -----\n%s\n--------------------" % body[:1500])
            return
        if not self.github_configured or not number:
            self._local("评论 Issue #%s" % (number or "local"), body)
            return
        self._api("POST", "/repos/%s/issues/%s/comments" % (self.repo, number), {"body": body})
        log("已评论 Issue #%s" % number)

    def close(self, number):
        if self.dry_run:
            log("[dry-run] 将关闭 Issue #%s" % number)
            return
        if not self.github_configured or not number:
            return
        self._api("PATCH", "/repos/%s/issues/%s" % (self.repo, number), {"state": "closed"})
        log("已关闭 Issue #%s" % number)

    def _local(self, title, body):
        with open(os.path.join(self.base, "outbox.log"), "a", encoding="utf-8") as fh:
            fh.write("\n===== %s (LOCAL) =====\n%s\n%s\n" % (ts_str(), title, body))
        log("!! GitHub 未配置：通知只写入了 outbox.log（不会发邮件）: %s" % title)


def render_alert(items, tails, heading):
    # Detailed matches remain in local state; notifications use affected tasks only.
    out = ["## " + heading, ""]
    for item in items:
        out.append("- **%s** · %s" %
                   (item["log"], RULE_LABELS.get(item["rule"], "告警")))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写状态/不发通知")
    ap.add_argument("--test-notify", action="store_true", help="发一条测试通知")
    args = ap.parse_args()

    base = base_dir()
    os.makedirs(base, exist_ok=True)

    # 单实例锁（cron 防重叠）
    lock_fh = open(os.path.join(base, ".lock"), "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("上一轮巡检还在执行，跳过本轮")
        return

    cfg = load_json(os.path.join(base, "config.json"), {}) or {}
    notifier = Notifier(cfg, base, dry_run=args.dry_run)

    if args.test_notify:
        number = notifier.create_issue(
            "✅ [测试] train_watch 通知链路",
            "收到这条说明链路正常，可忽略。\n\n时间: %s" % ts_str())
        if number:
            notifier.close(number)
        log("测试通知已发送")
        return

    state_path = os.path.join(base, "state.json")
    state = load_json(state_path, {}) or {}
    active = state.get("active_rules") or {}
    files_state = state.get("files") or {}

    alerts_cfg = cfg.get("alerts") or {}
    reminder_hours = float(alerts_cfg.get("reminder_hours", 12))
    max_read = int(alerts_cfg.get("max_read_bytes", 8 * 1024 * 1024))
    max_hits = int(alerts_cfg.get("max_hits", 3))

    newly = []      # 本轮新激活的规则 key
    resolved = []   # 本轮恢复的规则 key
    started = []    # 本轮"从停滞/空开始写入"的日志
    tails = []      # [(显示名, 末尾行)]
    auto_removed = []

    for lc in cfg.get("logs") or []:
        if not lc.get("enabled"):
            continue
        name = lc.get("name") or os.path.basename(lc.get("path") or "log")
        path = expand(lc.get("path") or "")
        stall_sec = float(lc.get("stall_minutes", 90)) * 60
        resolve_after = int(lc.get("resolve_after_lines", 200))
        proc_pat = lc.get("process_pattern") or ""
        crash_pats = [re.compile(p) for p in (lc.get("crash_patterns") or DEFAULT_CRASH_PATTERNS)]
        nan_pats = [re.compile(p) for p in (lc.get("nan_patterns") or DEFAULT_NAN_PATTERNS)]
        ignore_pats = [re.compile(p) for p in (lc.get("ignore_patterns") or [])]

        prev_file = files_state.get(name)

        if not path or not os.path.exists(path):
            if prev_file:
                key = name + ":missing"
                if key not in active:
                    active[key] = {"log": name, "rule": "missing", "path": path,
                                   "since": time.time(), "matched": "日志文件不存在"}
                    newly.append(key)
            continue
        if (name + ":missing") in active:
            del active[name + ":missing"]
            resolved.append(name + ":missing")

        st = os.stat(path)
        rotated = False
        if prev_file is None:
            offset = st.st_size if lc.get("start_at", "end") == "end" else 0
            partial = ""
        elif prev_file.get("inode") != st.st_ino or st.st_size < prev_file.get("offset", 0):
            rotated = True
            offset, partial = 0, ""
            log("检测到日志轮转/重建: %s" % path)
        else:
            offset = prev_file.get("offset", 0)
            partial = prev_file.get("partial", "")

        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(max_read)
        consumed = offset + len(chunk)
        text = partial + chunk.decode("utf-8", errors="replace")
        if text.endswith("\n"):
            partial = ""
            parts = text.split("\n")[:-1]
        else:
            parts = text.split("\n")
            partial = parts.pop() if parts else ""
        lines = [ln for ln in parts if ln.strip()]
        start_gap_sec = float(lc.get("start_gap_minutes", 30)) * 60
        grew = rotated or len(chunk) > 0
        prev_active = prev_file.get("active") if prev_file else None
        if prev_active is None:
            active_state = (time.time() - st.st_mtime) <= start_gap_sec
        elif grew:
            active_state = True
        elif time.time() - st.st_mtime > start_gap_sec:
            active_state = False
        else:
            active_state = prev_active
        if grew and prev_active is False and lc.get("notify_on_start"):
            started.append({"log": name, "path": path, "stall_disabled": stall_sec <= 0})
        files_state[name] = {"path": path, "inode": st.st_ino, "offset": consumed,
                             "mtime": st.st_mtime, "partial": partial, "size": st.st_size,
                             "active": active_state}

        previous_context = prev_file.get("nonfatal_context") if prev_file and not rotated else None
        skipped, context = nonfatal_mask(lines, previous_context, lc.get("nonfatal_patterns"))
        files_state[name]["nonfatal_context"] = context
        n_lines = len(lines)
        for rule, pats in (("crash", crash_pats), ("nan", nan_pats)):
            key = name + ":" + rule
            hits = find_hits(lines, pats, max_hits, ignore_pats, skipped)
            if hits:
                idx, matched = hits[0]
                first_time = key not in active
                active[key] = {"log": name, "rule": rule, "path": path,
                               "since": time.time(), "matched": matched,
                               "excerpt": excerpt(lines, idx), "lines_since_hit": 0}
                if first_time:
                    newly.append(key)
            elif key in active:
                active[key]["lines_since_hit"] = active[key].get("lines_since_hit", 0) + n_lines
                if active[key]["lines_since_hit"] >= resolve_after:
                    del active[key]
                    resolved.append(key)

        staleness = time.time() - st.st_mtime
        key = name + ":stall"
        completed = any(os.path.isfile(expand(p))
                        for p in (lc.get("completion_files") or []))
        if stall_sec > 0 and staleness > stall_sec and not completed:
            proc = process_running(proc_pat)
            if proc is False:
                detail = "日志已 %.0f 分钟无更新，且训练进程未在运行（可能已退出/崩溃）" % (staleness / 60)
            elif proc is True:
                detail = "日志已 %.0f 分钟无更新，训练进程仍在运行（疑似卡死）" % (staleness / 60)
            else:
                detail = "日志已 %.0f 分钟无更新（阈值 %.0f 分钟）" % (staleness / 60, stall_sec / 60)
            if key not in active:
                active[key] = {"log": name, "rule": "stall", "path": path,
                               "since": time.time(), "matched": detail}
                newly.append(key)
            else:
                active[key]["matched"] = detail
        elif key in active:
            del active[key]
            resolved.append(key)

        if retire_stalled_target(lc, active, alerts_cfg.get("auto_remove_stalled_hours", 0)):
            auto_removed.append(name)
            log("%sRemoved stalled target from monitoring: %s" %
                ("[dry-run] " if args.dry_run else "", name))
            continue
        tails.append((name, read_tail(path)))

    if auto_removed and not args.dry_run:
        save_json(os.path.join(base, "config.json"), cfg)

    configured_names = {lc.get("name") or os.path.basename(lc.get("path") or "log")
                        for lc in (cfg.get("logs") or []) if lc.get("enabled", True)}
    purged = [k for k in active if k.split(":", 1)[0] not in configured_names]
    for k in purged:
        del active[k]
    for fname in list(files_state):
        if fname not in configured_names:
            del files_state[fname]

    state["active_rules"] = active
    state["files"] = files_state
    state["last_run_ts"] = time.time()

    incident = state.get("incident")
    notify_keys = [key for key in newly if key in active]
    if not notify_keys and active and incident is None:
        notify_keys = list(active)  # 上一轮通知失败，补发

    if notify_keys:
        items = [active[k] for k in notify_keys if k in active]
        rules = sorted({RULE_LABELS.get(it["rule"], it["rule"]) for it in items})
        title = "🚨 训练告警：" + "、".join(rules)
        body = render_alert(items, tails, "训练告警")
        if incident is None:
            number = notifier.create_issue(title, body)
            state["incident"] = {"number": number, "created_ts": time.time(),
                                 "last_comment_ts": time.time(),
                                 "rules_ever": list(active)}
        elif not incident.get("number"):
            incident["number"] = notifier.create_issue(title, body)
            incident["last_comment_ts"] = time.time()
            state["incident"] = incident
        else:
            notifier.comment(incident["number"], render_alert(items, tails, "新增告警"))
            incident["last_comment_ts"] = time.time()
            state["incident"] = incident
    elif incident:
        if not active:
            if purged:
                msg = ("✅ 告警关闭：以下日志已暂停或移除监控\n\n"
                       + "\n".join("- `%s`" % k for k in sorted(purged)))
            else:
                names = "、".join(sorted({RULE_LABELS.get(k.split(":")[-1], k)
                                          for k in incident.get("rules_ever", [])}))
                tail_text = "\n".join(tails[0][1][-5:]) if tails else "(无)"
                task_names = sorted({key.rsplit(":", 1)[0]
                                     for key in incident.get("rules_ever", [])})
                msg = "✅ 已恢复：%s\n任务：%s" % (names, "、".join(task_names))
            notifier.comment(incident["number"], msg)
            notifier.close(incident["number"])
            state.pop("incident", None)
        elif time.time() - incident.get("last_comment_ts", 0) > reminder_hours * 3600:
            items = [active[k] for k in active]
            notifier.comment(incident["number"],
                             render_alert(items, tails, "⏰ 持续告警提醒（每 %.0f 小时）" % reminder_hours))
            incident["last_comment_ts"] = time.time()
            state["incident"] = incident

    if auto_removed and active and (state.get("incident") or {}).get("number"):
        notifier.comment(state["incident"]["number"],
                         "自动移出监控：" + "、".join(auto_removed))

    if started:
        names = "、".join(s["log"] for s in started)
        body = ["## ▶️ 训练已开始", ""]
        for s in started:
            body.append("- **%s** · %s" % (s["log"], ts_str()))
            body.append("  - 日志: `%s`" % s["path"])
            if s.get("stall_disabled"):
                body.append("  - 注: 该日志的停滞告警当前关闭（stall_minutes=0），需要的话随时可以打开")
            tail = next((t for n, t in tails if n == s["log"]), [])
            if tail:
                body.append("")
                body.append("```text")
                body.extend(tail[-5:])
                body.append("```")
            body.append("")
        number = notifier.create_issue("▶️ 训练已开始：" + names, "\n".join(body))
        if number:
            notifier.close(number)

    hb = cfg.get("heartbeat") or {}
    if hb.get("enabled") and not args.dry_run and not state.get("incident") and tails:
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("heartbeat_last") != today and datetime.now().hour >= int(hb.get("hour", 9)):
            body = ["## ✅ 巡检正常 · %s" % today, "", "所有启用的训练日志均未发现异常。", ""]
            for name, tail in tails:
                body.append("### %s · 最后输出" % name)
                body.append("```text")
                body.extend(tail[-3:])
                body.append("```")
                body.append("")
            number = notifier.create_issue("✅ 巡检正常 · %s" % today, "\n".join(body))
            if number:
                notifier.close(number)
            state["heartbeat_last"] = today

    for lc in (cfg.get("logs") or []):
        if lc.get("enabled") and (lc.get("eta_reminder") or {}).get("enabled"):
            from eta import check_eta
            check_eta(lc, state, notifier, dry_run=args.dry_run,
                      persist=lambda: save_json(state_path, state), logger=log)

    if not args.dry_run:
        save_json(state_path, state)
    log("巡检完成: 活动告警=%s%s%s" % (
        ", ".join(sorted(active)) or "无",
        ("，新增=%s" % ",".join(newly)) if newly else "",
        " (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        # 不让 cron 因异常中断后续巡检
