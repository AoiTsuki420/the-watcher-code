"""Explicit completion markers and one-shot private notifications; no network."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import train_watch as watch


class Fake:
    github_configured = False

    def __init__(self):
        self.events = []

    def create_issue(self, title, body):
        self.events.append((title, body))
        return 42


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.done = self.folder / "DONE"
        self.failed = self.folder / "FAILED"
        self.target = dict(name="job-log", enabled=True, completion_files=[str(self.failed)],
                           completion_notification=dict(enabled=True, name="experiment",
                                                        run_key="run-1", success_files=[str(self.done)],
                                                        failure_files=[str(self.failed)]))
        self.state, self.notifier = {}, Fake()

    def check(self, **kwargs):
        return watch.check_completion(self.target, self.state, self.notifier,
                                      logger=lambda message: None, **kwargs)

    def test_completion_sends_once_and_persists(self):
        self.assertEqual(self.check()["status"], "watching")
        self.done.touch()
        saved = []
        self.assertTrue(self.check(persist=lambda: saved.append(copy.deepcopy(self.state)))["notified"])
        self.assertTrue(saved)
        self.assertIn("正常完成", self.notifier.events[0][0])
        self.assertNotIn(str(self.folder), self.notifier.events[0][1])
        self.state = json.loads(json.dumps(self.state))
        self.check()
        self.assertEqual(len(self.notifier.events), 1)

    def test_failure_marker_has_priority(self):
        self.check()
        self.done.touch()
        self.failed.touch()
        self.assertEqual(self.check()["status"], "failed")
        self.assertFalse(self.notifier.events)

    def test_terminal_completion_files_are_not_success_markers(self):
        self.failed.touch()
        self.assertEqual(self.check()["status"], "failed")
        self.assertFalse(self.notifier.events)

    def test_existing_completion_is_silent_unless_opted_in(self):
        self.done.touch()
        self.assertEqual(self.check()["status"], "already_complete")
        self.assertFalse(self.notifier.events)
        self.target["completion_notification"]["notify_existing"] = True
        self.assertTrue(self.check()["notified"])

    def test_all_markers_required_unless_configured_otherwise(self):
        other = self.folder / "OTHER"
        self.target["completion_notification"]["success_files"].append(str(other))
        self.check()
        self.done.touch()
        self.assertEqual(self.check()["status"], "watching")
        self.target["completion_notification"]["require_all"] = False
        self.assertTrue(self.check()["notified"])

    def test_multiple_logs_share_one_experiment_notification(self):
        self.check()
        self.done.touch()
        self.check()
        self.target["name"] = "another-log"
        self.check()
        self.assertEqual(len(self.notifier.events), 1)

    def test_new_run_notifies_again(self):
        self.check()
        self.done.touch()
        self.check()
        self.done.unlink()
        self.target["completion_notification"]["run_key"] = "run-2"
        self.check()
        self.done.touch()
        self.check()
        self.assertEqual(len(self.notifier.events), 2)

    def test_dry_run_does_not_send(self):
        self.check()
        self.done.touch()
        self.assertEqual(self.check(dry_run=True)["status"], "would_notify")
        self.assertFalse(self.notifier.events)

    def test_failed_delivery_retries(self):
        self.check()
        self.done.touch()
        with patch.object(self.notifier, "create_issue", side_effect=TimeoutError):
            self.check()
        self.assertTrue(self.check()["notified"])

    def test_lost_response_is_recovered_without_duplicate(self):
        self.check()
        self.done.touch()
        self.check()
        body = self.notifier.events[0][1]
        next(iter(self.state["completion_notifications"].values()))["notified"] = False
        self.notifier.github_configured = True
        self.notifier.repo = "example/private"
        with patch.object(self.notifier, "_api", create=True,
                          return_value=[dict(number=42, body=body)]):
            self.assertTrue(self.check()["notified"])
        self.assertEqual(len(self.notifier.events), 1)

    def test_disabled_and_invalid_config_are_silent(self):
        self.target["enabled"] = False
        self.assertIsNone(self.check())
        self.target["enabled"] = True
        self.target["completion_notification"]["success_files"] = []
        self.assertIsNone(self.check())
        self.assertFalse(self.notifier.events)

    def test_privacy_layer_preserves_status_name_and_opaque_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            notifier = watch.Notifier({"logs": [self.target]}, folder)
            marker = "<!-- watcher-completion:" + "a" * 64 + " -->"
            title, body = notifier._safe_message("任务正常完成：experiment", "secret /private/log " + marker)
            self.assertIn("正常完成", title)
            self.assertIn("experiment", body)
            self.assertIn(marker, body)
            self.assertNotIn("secret", body)
            self.assertNotIn("/private/log", body)

    def test_main_detects_success_even_if_log_is_absent(self):
        self.target["path"] = str(self.folder / "job.log")
        (self.folder / "config.json").write_text(json.dumps({"logs": [self.target]}))
        with patch.dict(os.environ, TRAIN_WATCH_DIR=str(self.folder)), \
                patch.object(sys, "argv", ["watch"]), \
                patch.object(watch, "Notifier", return_value=self.notifier), \
                contextlib.redirect_stdout(io.StringIO()):
            watch.main()
            self.assertFalse(self.notifier.events)
            self.done.touch()
            watch.main()
            watch.main()
        self.assertEqual(len(self.notifier.events), 1)
        state = json.loads((self.folder / "state.json").read_text())
        self.assertTrue(next(iter(state["completion_notifications"].values()))["notified"])


if __name__ == "__main__":
    unittest.main()
