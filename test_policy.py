"""Stall lifecycle and outbound privacy regression checks; fake transports only."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import train_watch as watch


class PolicyTests(unittest.TestCase):
    def test_threshold_override_and_disabled_policy(self):
        now = 1000000
        active = {"job:stall": {"since": now - 48 * 3600}}
        target = dict(name="job", enabled=True)
        self.assertFalse(watch.retire_stalled_target(target, active, 48, now))
        self.assertTrue(watch.retire_stalled_target(target, active, 48, now + 1))
        self.assertFalse(target["enabled"])
        self.assertEqual(target["auto_removed"]["reason"], "stall_timeout")
        for hours in (0, 72, -1, "invalid", float("inf")):
            with self.subTest(hours=hours):
                target = dict(name="job", enabled=True, auto_remove_stalled_hours=hours)
                self.assertFalse(watch.retire_stalled_target(target, active, 48, now + 1))
                self.assertTrue(target["enabled"])

    def test_recovered_rule_not_retired(self):
        target = dict(name="job", enabled=True)
        self.assertFalse(watch.retire_stalled_target(target, {}, 48, 1000000))

    def test_alert_summary_does_not_include_other_tasks_or_log_details(self):
        body = watch.render_alert(
            [{"log": "job", "rule": "crash", "path": "/secret/file",
              "matched": "private-token", "excerpt": "192.0.2.1"}],
            [("other-job", ["private-log"])], "告警")
        self.assertIn("job", body)
        for value in ["other-job", "/secret/file", "private-token", "192.0.2.1", "private-log"]:
            self.assertNotIn(value, body)

    def test_all_outbound_transports_use_only_names_and_status(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = {"logs": [{"name": "batch-job"}],
                   "github": {"repo": "example/private", "assignee": "private-user"}}
            notifier = watch.Notifier(cfg, folder)
            notifier.token = "fake-test-token"
            requests, emails = [], []

            def api(method, path, payload=None):
                requests.append(copy.deepcopy(payload))
                return {"number": 42}

            secret = "SECRET_TOKEN /private/run/log.txt 192.0.2.1 person@example.com"
            with patch.object(notifier, "_api", side_effect=api), \
                    patch.object(notifier, "_send_email", side_effect=lambda *args: emails.append(args)):
                notifier.create_issue("训练告警：崩溃/报错", "batch-job " + secret)
                notifier.comment(42, "已恢复 batch-job " + secret)
                notifier.comment(42, "自动移出监控 batch-job " + secret)
                marker = "<!-- watcher-completion:" + "a" * 64 + " -->"
                notifier.create_issue("正常完成 batch-job", secret + marker)
            self.assertEqual(len(emails), 4)
            for item in requests + emails:
                text = json.dumps(item, ensure_ascii=False)
                for value in secret.split():
                    self.assertNotIn(value, text)
                self.assertNotIn("cc @", text)
                self.assertIn("batch-job", text)
            self.assertIn(marker, requests[-1]["body"])
            self.assertIn("已恢复", requests[1]["body"])
            self.assertIn("已移出监控", requests[2]["body"])


class MainTests(unittest.TestCase):
    def exercise(self, *, dry_run=False, fresh=False, completed=False, missing=False, enabled=True):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            logfile = p / "job.log"
            logfile.write_text("healthy\n")
            now = 1000000.0
            os.utime(str(logfile), (now if fresh else now - 7200,) * 2)
            target = dict(name="job", enabled=enabled, path=str(logfile), stall_minutes=60)
            if completed:
                marker = p / "DONE"
                marker.touch()
                target["completion_files"] = [str(marker)]
            if missing:
                logfile.unlink()
            config = dict(logs=[target], alerts={"auto_remove_stalled_hours": 48})
            state = {"active_rules": {"job:stall": {
                "since": now - 49 * 3600, "log": "job", "rule": "stall"}},
                "files": {"job": {"inode": logfile.stat().st_ino if not missing else 0,
                                   "offset": 8}},
                "incident": {"number": 42, "last_comment_ts": now, "rules_ever": ["job:stall"]}}
            (p / "config.json").write_text(json.dumps(config))
            (p / "state.json").write_text(json.dumps(state))
            before_cfg = (p / "config.json").read_bytes()
            before_state = (p / "state.json").read_bytes()
            events = []

            class Fake:
                def __init__(self, *args, **kwargs):
                    pass

                def create_issue(self, *args):
                    if not dry_run:
                        events.append("create")
                    return 42

                def comment(self, *args):
                    if not dry_run:
                        events.append("comment")

                def close(self, *args):
                    if not dry_run:
                        events.append("close")

            with patch.dict(os.environ, TRAIN_WATCH_DIR=folder), \
                    patch.object(sys, "argv", ["watch"] + (["--dry-run"] if dry_run else [])), \
                    patch.object(watch, "Notifier", Fake), \
                    patch.object(watch.time, "time", return_value=now):
                watch.main()
            cfg_after = json.loads((p / "config.json").read_text())
            state_after = json.loads((p / "state.json").read_text())
            self.assertEqual(logfile.exists(), not missing)
            if dry_run:
                self.assertEqual(before_cfg, (p / "config.json").read_bytes())
                self.assertEqual(before_state, (p / "state.json").read_bytes())
                self.assertFalse(events)
            elif fresh or completed or missing:
                self.assertTrue(cfg_after["logs"][0]["enabled"])
                if not missing:
                    self.assertNotIn("job:stall", state_after["active_rules"])
            else:
                self.assertFalse(cfg_after["logs"][0]["enabled"])
                self.assertFalse(state_after["active_rules"])
                self.assertNotIn("job", state_after["files"])
                self.assertNotIn("incident", state_after)
                self.assertEqual(events, ["comment", "close"])

    def test_retirement_keeps_file_and_closes_incident(self):
        self.exercise()

    def test_dry_run_preserves_config_and_state(self):
        self.exercise(dry_run=True)

    def test_update_or_completion_prevents_retirement(self):
        self.exercise(fresh=True)
        self.exercise(completed=True)

    def test_missing_file_does_not_use_old_stall_for_retirement(self):
        self.exercise(missing=True)

    def test_disabled_target_closes_existing_stall_incident(self):
        self.exercise(enabled=False)

    def test_add_and_remove_log_without_polling(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            config = path / "config.json"
            original = {"logs": [{"path": "/old/retained.log"}], "alerts": {"reminder_hours": 24}}
            config.write_text(json.dumps(original), encoding="utf-8")
            logfile = path / "new train.log"
            logfile.write_text("training\n", encoding="utf-8")

            def run(*args):
                with patch.dict(os.environ, TRAIN_WATCH_DIR=folder), \
                        patch.object(sys, "argv", ["watch", *args]), \
                        patch.object(watch, "Notifier", side_effect=AssertionError("unexpected poll")):
                    watch.main()

            run("add", "new-job", str(logfile))
            added = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(added["logs"], original["logs"] + [
                {"name": "new-job", "path": str(logfile), "enabled": True,
                 "start_at": "end", "stall_minutes": 60}])
            for args in (("add", "retained.log", str(logfile)), ("remove", "unknown")):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    run(*args)
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(json.loads(config.read_text(encoding="utf-8")), added)
            run("remove", "new-job")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), original)
            self.assertEqual(logfile.read_text(encoding="utf-8"), "training\n")
            self.assertFalse((path / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
