"""Recovered warnings stay quiet; separate fatal errors must still alert."""
import re
import unittest

import train_watch as watch


class WarningTests(unittest.TestCase):
    def hits(self, lines, context=None, patterns=None, rule_patterns=None):
        mask, context = watch.nonfatal_mask(lines, context, patterns)
        hits = watch.find_hits(lines, [re.compile(p) for p in
                                      (rule_patterns or watch.DEFAULT_CRASH_PATTERNS)],
                               10, skip_indices=mask)
        return hits, context

    def test_warning_lines_are_quiet(self):
        lines = ["2026-11-06 12:00:00 WARNING RuntimeError: fallback used",
                 "[WARN] CUDA out of memory; retrying with a smaller batch",
                 "/example/code.py:12: UserWarning: ValueError fallback"]
        self.assertFalse(self.hits(lines)[0])

    def test_skipped_data_with_repeated_tracebacks_is_quiet(self):
        lines = ["[skip data 123 sample]", "Traceback (most recent call last):",
                 '  File "/example/data.py", line 10, in read', "    assert valid",
                 "AssertionError:", "Traceback (most recent call last):",
                 '  File "/example/data.py", line 10, in read', "    assert valid",
                 "AssertionError:", 'TRAIN_STEP {"step": 2, "loss": 1.0}']
        hits, context = self.hits(lines)
        self.assertFalse(hits)
        self.assertEqual(context["remaining"], 0)

    def test_context_survives_poll_boundary(self):
        _, context = self.hits(["[WARNING] rejected sample", "Traceback (most recent call last):"])
        hits, _ = self.hits(['  File "/example/data.py", line 1', "ValueError: invalid sample"], context)
        self.assertFalse(hits)

    def test_new_error_header_is_not_suppressed(self):
        hits, _ = self.hits(["[skip data 123 sample]", "ValueError: bad sample",
                             "[ERROR] RuntimeError: actual failure"])
        self.assertEqual(hits[0][0], 2)

    def test_progress_ends_warning_context(self):
        hits, _ = self.hits(["[WARNING] bad sample", "ValueError: ignored", "step=2 loss=1",
                             "Traceback (most recent call last):", "RuntimeError: actual failure"])
        self.assertEqual(hits[0][0], 3)

    def test_process_exit_overrides_inherited_warning(self):
        hits, _ = self.hits(["[skip data 123 sample]", "ChildFailedError: worker failed"])
        self.assertEqual(hits[0][0], 1)

    def test_unhandled_traceback_remains_an_alert(self):
        self.assertTrue(self.hits(["Traceback (most recent call last):", "ValueError: invalid"])[0])

    def test_fatal_python_error_and_nonzero_exit_are_alerts(self):
        for text in ("Fatal Python error: Aborted", "exitcode: 1", "exitcode: -9", "exit code = 2"):
            with self.subTest(text=text):
                self.assertTrue(self.hits([text])[0])
                self.assertEqual(self.hits(["[WARNING] an earlier warning", text])[0][0][0], 1)

    def test_successful_exit_and_recovered_warning_remain_quiet(self):
        for text in ("exitcode: 0", "exit code = 0", "[WARNING] exitcode: 1; recovered"):
            with self.subTest(text=text):
                self.assertFalse(self.hits([text])[0])

    def test_custom_failure_marker_is_preserved(self):
        self.assertTrue(self.hits(['{"error_type": "RuntimeError"}'],
                                  rule_patterns=[r'"error_type"\s*:'])[0])

    def test_real_nan_remains_an_alert_but_warning_is_quiet(self):
        self.assertFalse(self.hits(["[WARNING] loss: nan; sample skipped"],
                                   rule_patterns=watch.DEFAULT_NAN_PATTERNS)[0])
        self.assertTrue(self.hits(["train loss: nan"], rule_patterns=watch.DEFAULT_NAN_PATTERNS)[0])

    def test_inherited_context_is_bounded(self):
        _, context = self.hits(["[WARNING] a warning"])
        lines = ["    detail"] * 256 + ["RuntimeError: outside warning block"]
        self.assertEqual(self.hits(lines, context)[0][0][0], 256)


if __name__ == "__main__":
    unittest.main()
