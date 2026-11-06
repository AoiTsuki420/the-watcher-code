"""Optional one-shot ETA reminders for jobs that publish progress JSON."""
import datetime
import hashlib
import json
import math
import os
import time


def _path(value):
    return os.path.expandvars(os.path.expanduser(value))


def _positive(cfg, key, default):
    value = float(cfg.get(key, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(key + " must be finite and positive")
    return value


def _read_json(path):
    with open(_path(path), encoding="utf-8") as stream:
        return json.load(stream)


def check_eta(lc, state, notifier, now=None, dry_run=False, persist=None, logger=print):
    """Read {time, completed, total}; notify once per target and run_key.

    completed counts processed units, not necessarily successful results.
    Heartbeat and failure marker paths are optional. All clocks use Unix seconds.
    """
    cfg = lc.get("eta_reminder") or {}
    if not lc.get("enabled") or not cfg.get("enabled"):
        return None
    name = lc.get("name") or os.path.basename(lc.get("path") or "log")
    rec = state.setdefault("eta_reminders", {}).setdefault(name, {})
    try:
        run_key = cfg["run_key"]
        if not isinstance(run_key, str) or not run_key.strip():
            raise ValueError("run_key must be a nonempty string")
        threshold = _positive(cfg, "threshold_seconds", 3600)
        window = _positive(cfg, "window_seconds", 3600)
        minimum = _positive(cfg, "min_window_seconds", 900)
        progress_timeout = _positive(cfg, "progress_timeout_seconds", 300)
        heartbeat_timeout = _positive(cfg, "heartbeat_timeout_seconds", 90)
        if minimum > window:
            raise ValueError("min_window_seconds must not exceed window_seconds")
        if rec.get("run_key") != run_key:
            rec.clear()
            rec.update(run_key=run_key, samples=[], notified=False)
        if rec.get("notified"):
            return rec
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError("invalid current time")
        if any(os.path.isfile(_path(p)) for p in lc.get("completion_files", [])):
            rec["status"] = "processing_complete"
            return rec
        if cfg.get("failure_path") and os.path.isfile(_path(cfg["failure_path"])):
            rec["status"] = "failed"
            return rec
        if cfg.get("active_path"):
            heartbeat = float(_read_json(cfg["active_path"])["time"])
            if not math.isfinite(heartbeat) or not 0 <= now - heartbeat <= heartbeat_timeout:
                rec["status"] = "stale_heartbeat"
                return rec
        progress = _read_json(cfg["progress_path"])
        completed, total = progress["completed"], progress["total"]
        if (type(completed) is not int or type(total) is not int
                or not 0 <= completed <= total):
            raise ValueError("expected integer counters: 0 <= completed <= total")
        observed = float(progress["time"])
        if not math.isfinite(observed):
            raise ValueError("invalid progress timestamp")
        if not 0 <= now - observed <= progress_timeout:
            rec["status"] = "stale_progress"
            return rec
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        rec.update(status="invalid_or_unavailable_progress", error_type=type(exc).__name__)
        return rec

    rec.pop("error_type", None)
    pending = total - completed
    if pending == 0:
        rec["status"] = "processing_complete"
        return rec
    samples = rec.setdefault("samples", [])
    if (rec.get("total", total) != total or (samples and
            (observed < samples[-1][0] or completed < samples[-1][1]))):
        samples.clear()
    rec.update(total=total, completed=completed, pending=pending, last_progress=observed)
    if samples and observed == samples[-1][0]:
        if completed != samples[-1][1]:
            rec["status"] = "inconsistent_timestamp"
            return rec
    else:
        samples.append([observed, completed])
    # Keep only the configured recent window; do not estimate from stale history.
    samples[:] = [sample for sample in samples if sample[0] >= observed - window]
    # Bound state size while retaining the oldest observation in this window.
    if len(samples) > 1024:
        samples[1:-1023] = []
    elapsed = observed - samples[0][0]
    done = completed - samples[0][1]
    if elapsed < minimum or done <= 0:
        rec["status"] = "warming_up_or_no_progress"
        return rec
    rate = done / elapsed
    remaining = pending / rate
    rec.update(status="tracking", units_per_hour=rate * 3600,
               remaining_seconds=remaining, window_seconds=elapsed)
    if remaining > threshold:
        return rec
    if dry_run:
        rec["status"] = "would_notify"
        return rec

    # Include the target so two jobs may safely share a run_key and alert repo.
    identity = json.dumps([name, run_key], ensure_ascii=False).encode("utf-8")
    tag = "<!-- watcher-eta:" + hashlib.sha256(identity).hexdigest() + " -->"
    finish = datetime.datetime.fromtimestamp(
        now + remaining, datetime.timezone.utc).isoformat(timespec="minutes")
    unit = str(cfg.get("unit") or "项")
    title = "⏳ %s：预计剩余约 %.0f 分钟" % (name, remaining / 60)
    body = (
        "任务 **%s** 的预计剩余时间已进入提醒阈值（%.0f 分钟）。\n\n"
        "- 预计剩余：约 %.0f 分钟\n- 预计处理结束：%s（UTC）\n"
        "- 最近 %.0f 分钟速度：约 %.1f %s/小时\n"
        "- 已处理：%d；总量：%d；待处理：%d（%s）\n\n"
        "这是按近期处理速度估算的一次性提醒，后续速度变化会影响完成时间。"
        "已处理数量不代表结果全部成功或通过验收；请以任务自身结果为准。\n\n%s"
    ) % (name, threshold / 60, remaining / 60, finish, elapsed / 60,
         rate * 3600, unit, completed, total, pending, unit, tag)
    try:
        number = 0
        if getattr(notifier, "github_configured", False):
            issues = notifier._api(
                "GET", "/repos/%s/issues?state=all&per_page=100" % notifier.repo)
            number = next((int(issue["number"]) for issue in issues
                           if tag in (issue.get("body") or "")), 0)
        if not number:
            number = notifier.create_issue(title, body)
        if number:
            rec.update(notified=True, notified_at=now, issue_number=number, status="notified")
            if persist:
                persist()
            logger("ETA reminder delivered once: issue=%s remaining_minutes=%.1f"
                   % (number, remaining / 60))
        else:
            rec["status"] = "notification_not_delivered_retry"
    except Exception as exc:
        rec.update(status="notification_failed_retry", error_type=type(exc).__name__)
        logger("ETA reminder delivery will retry: " + type(exc).__name__)
    return rec
