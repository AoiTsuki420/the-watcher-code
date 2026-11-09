"""Setup tests use temporary directories and fake GitHub/crontab transports."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import install as setup


class CronTests(unittest.TestCase):
    def test_repeated_install_preserves_other_jobs(self):
        original = "MAILTO=me@example.test\n# daily backup\n0 3 * * * backup\n"
        runtime = Path("/home/user/monitor")
        first = setup.merge_cron(original, runtime, 5, Path("/usr/bin/python3"))
        second = setup.merge_cron(first, runtime, 10, Path("/usr/bin/python3"))
        self.assertTrue(second.startswith(original))
        self.assertEqual(second.count("train_watch.py"), 1)
        self.assertIn("*/10 * * * *", second)
        self.assertEqual(setup.merge_cron(second, runtime, 10, Path("/usr/bin/python3")), second)

    def test_existing_manual_job_or_broken_markers_rejected(self):
        runtime = Path("/home/user/monitor")
        begin, end, _ = setup.cron_block(runtime, 5, Path("/usr/bin/python3"))
        for original in ("*/5 * * * * python3 /old/train_watch.py\n", begin + "\n",
                         end + "\n" + begin + "\n"):
            with self.subTest(original=original), self.assertRaises(ValueError):
                setup.merge_cron(original, runtime, 5, Path("/usr/bin/python3"))

    def test_shell_paths_and_cron_percent_are_escaped(self):
        _, _, block = setup.cron_block(Path("/home/user/a b'c%20"), 5, Path("/opt/python 3/bin/python"))
        self.assertIn(r"\%20", block)
        self.assertIn("'\"'\"'", block)
        self.assertIn("TRAIN_WATCH_DIR=", block)
        self.assertNotIn("token", block)

    def test_input_validation(self):
        self.assertEqual(setup.repository("https://github.com/example/alerts.git"), "example/alerts")
        for value in ("../repo", "example/..", "x/y\ncommand", "https://evil.test/x/y"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                setup.repository(value)

    def test_read_crontab_does_not_treat_permission_error_as_empty(self):
        with patch.object(setup.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "permission denied"
            with self.assertRaises(RuntimeError):
                setup.read_crontab()


class GitHubTests(unittest.TestCase):
    def test_read_only_private_repository_validation(self):
        calls = []

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps(self.value).encode() if self.value is not None else b""

        class Opener:
            def open(self, request, **kwargs):
                calls.append(request)
                values = [{"private": True, "has_issues": True}, [], None]
                return Response(values[len(calls) - 1])

        with patch.object(setup.urllib.request, "build_opener", return_value=Opener()):
            setup.verify_github("example/private", "dummy-test-value", "example", "")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(request.get_method() == "GET" for request in calls))
        self.assertTrue(all(request.full_url.startswith("https://api.github.com/") for request in calls))

    def test_public_repository_is_rejected(self):
        with patch.object(setup.urllib.request, "build_opener") as factory:
            factory.return_value.open.return_value.__enter__.return_value.read.return_value = \
                b'{"private": false, "has_issues": true}'
            with self.assertRaises(ValueError):
                setup.verify_github("example/public", "dummy-test-value", "example", "")

    def test_transport_error_does_not_expose_credentials(self):
        secret = "DUMMY_SECRET_VALUE"
        with patch.object(setup.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = urllib.error.URLError(secret)
            with self.assertRaises(RuntimeError) as caught:
                setup.verify_github("example/private", secret, "example", "")
        self.assertNotIn(secret, str(caught.exception))


@unittest.skipUnless(os.name == "posix", "Unix file permissions and flock")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.runtime = self.folder / "runtime"
        self.runtime.mkdir()
        self.old_config = b'{"logs": [{"name": "existing", "enabled": true}]}'
        self.old_token = b"old-dummy-value\n"
        (self.runtime / "config.json").write_bytes(self.old_config)
        (self.runtime / "github.token").write_bytes(self.old_token)
        self.state = b'{"files": {"existing": {"offset": 123}}}'
        (self.runtime / "state.json").write_bytes(self.state)
        self.config = json.loads(self.old_config)
        self.config["github"] = {"repo": "example/private", "token_file": "github.token"}
        self.cron = "0 3 * * * backup\n"
        self.new_cron = setup.merge_cron(self.cron, self.runtime, 5, Path("/usr/bin/python3"))
        self.source = self.folder / "source"
        self.source.mkdir()
        (self.source / "train_watch.py").write_text("# Test source only\n")

    def apply(self):
        setup.apply_install(self.runtime, self.config, "new-dummy-value", self.cron,
                            self.new_cron, self.old_config, self.old_token, self.source)

    def test_install_preserves_state_and_protects_token(self):
        cron = [self.cron]
        with patch.object(setup, "read_crontab", side_effect=lambda: cron[0]), \
                patch.object(setup, "write_crontab", side_effect=lambda text: cron.__setitem__(0, text)):
            self.apply()
        self.assertEqual(cron[0], self.new_cron)
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.state)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        for name in ("config.json", "github.token", "train_watch.py"):
            self.assertEqual(stat.S_IMODE((self.runtime / name).stat().st_mode), 0o600)
        self.assertNotIn("new-dummy-value", (self.runtime / "config.json").read_text())
        self.assertNotIn("new-dummy-value", cron[0])

    def test_failed_cron_write_rolls_back_files(self):
        with patch.object(setup, "read_crontab", return_value=self.cron), \
                patch.object(setup, "write_crontab", side_effect=RuntimeError("fake failure")):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual((self.runtime / "config.json").read_bytes(), self.old_config)
        self.assertEqual((self.runtime / "github.token").read_bytes(), self.old_token)
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.state)
        self.assertFalse((self.runtime / "train_watch.py").exists())

    def test_concurrent_changes_prevent_install(self):
        with patch.object(setup, "read_crontab", return_value="changed\n"), \
                patch.object(setup, "write_crontab") as write:
            with self.assertRaises(RuntimeError):
                self.apply()
            write.assert_not_called()
        (self.runtime / "config.json").write_text("{}")
        with patch.object(setup, "write_crontab") as write:
            with self.assertRaises(RuntimeError):
                self.apply()
            write.assert_not_called()

    def test_running_watcher_prevents_install(self):
        import fcntl
        with (self.runtime / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.apply()
        self.assertEqual((self.runtime / "config.json").read_bytes(), self.old_config)

    def test_runtime_inside_repository_or_symlink_is_rejected(self):
        (self.folder / ".git").mkdir()
        with self.assertRaises(ValueError):
            setup.validate_runtime(self.runtime)
        link = self.folder / "link"
        link.symlink_to(self.runtime)
        with self.assertRaises(ValueError):
            setup.validate_runtime(link)

    def test_cancel_has_no_side_effects_or_token_output(self):
        log = self.folder / "job.log"
        log.write_text("healthy\n")
        destination = self.folder / "new-runtime"
        answers = [str(destination), "example/private", "example", "", "job",
                   str(log), "60", "", "N", "48", "5", "N"]
        output = io.StringIO()
        with patch.object(setup.sys, "argv", ["install.py"]), \
                patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.shutil, "which", return_value="/usr/bin/crontab"), \
                patch.object(setup, "read_crontab", return_value=""), \
                patch.object(setup, "verify_github"), \
                patch.object(setup.getpass, "getpass", return_value="hidden-dummy-value"), \
                patch("builtins.input", side_effect=answers), \
                patch.object(setup, "apply_install") as apply, contextlib.redirect_stdout(output):
            setup.main()
        apply.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertNotIn("hidden-dummy-value", output.getvalue())

    def test_complete_prompt_flow_installs_configuration_and_cron(self):
        log = self.folder / "job.log"
        log.write_text("healthy\n")
        answers = [str(self.runtime), "example/private", "example", "-", "new-job",
                   str(log), "60", str(self.folder / "DONE"), str(self.folder / "FAILED"),
                   "N", "48", "5", "y"]
        output, cron = io.StringIO(), [self.cron]
        apply = setup.apply_install
        with patch.object(setup.sys, "argv", ["install.py"]), \
                patch.object(setup.sys.stdin, "isatty", return_value=True), \
                patch.object(setup.shutil, "which", return_value="/usr/bin/crontab"), \
                patch.object(setup, "read_crontab", side_effect=lambda: cron[0]), \
                patch.object(setup, "write_crontab", side_effect=lambda text: cron.__setitem__(0, text)), \
                patch.object(setup, "verify_github"), \
                patch.object(setup.getpass, "getpass", return_value="hidden-dummy-value"), \
                patch("builtins.input", side_effect=answers), \
                patch.object(setup, "apply_install", side_effect=lambda *args: apply(*args, source=self.source)), \
                contextlib.redirect_stdout(output):
            setup.main()
        config = json.loads((self.runtime / "config.json").read_text())
        self.assertEqual([item["name"] for item in config["logs"]], ["existing", "new-job"])
        self.assertEqual(config["github"]["proxy"], "")
        self.assertTrue(config["logs"][-1]["completion_notification"]["enabled"])
        self.assertFalse(config["logs"][-1]["completion_notification"]["notify_existing"])
        self.assertEqual(config["logs"][-1]["completion_notification"]["success_files"],
                         [str(self.folder / "DONE")])
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.state)
        self.assertIn("train_watch.py", cron[0])
        self.assertIn("0 3 * * * backup", cron[0])
        self.assertNotIn("hidden-dummy-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
