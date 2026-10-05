"""Unit tests for the unified speech validation command dispatcher."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from scripts import validate_speech


class ValidateSpeechCliTests(unittest.TestCase):
    def test_every_workflow_maps_to_an_existing_runner(self):
        for workflow, (relative_path, _description) in validate_speech.COMMANDS.items():
            with self.subTest(workflow=workflow):
                runner = validate_speech.ROOT / "scripts" / relative_path
                self.assertTrue(runner.is_file(), f"{workflow} points to missing runner: {runner}")

    def test_dispatch_passes_runner_arguments_and_project_root(self):
        workflow, (relative_path, _description) = next(iter(validate_speech.COMMANDS.items()))
        completed = Mock(returncode=0)

        with patch.object(validate_speech.subprocess, "run", return_value=completed) as run:
            result = validate_speech.main([workflow, "--sample-only", "--report", "result.json"])

        expected_runner = validate_speech.ROOT / "scripts" / relative_path
        run.assert_called_once_with(
            [
                validate_speech.sys.executable,
                str(expected_runner),
                "--sample-only",
                "--report",
                "result.json",
            ],
            cwd=validate_speech.ROOT,
            check=False,
        )
        self.assertEqual(result, 0)

    def test_dispatch_returns_child_exit_code(self):
        workflow = next(iter(validate_speech.COMMANDS))
        with patch.object(validate_speech.subprocess, "run", return_value=Mock(returncode=7)):
            self.assertEqual(validate_speech.main([workflow]), 7)

    def test_unknown_workflow_returns_usage_error_without_spawning(self):
        stderr = io.StringIO()
        with patch.object(validate_speech.subprocess, "run") as run, contextlib.redirect_stderr(stderr):
            result = validate_speech.main(["not-a-workflow"])

        self.assertEqual(result, 2)
        self.assertIn("Unknown speech validation workflow", stderr.getvalue())
        run.assert_not_called()

    def test_missing_runner_returns_usage_error_without_spawning(self):
        workflow = next(iter(validate_speech.COMMANDS))
        with patch.dict(validate_speech.COMMANDS, {workflow: ("validation/missing.py", "missing runner")}), \
             patch.object(validate_speech.subprocess, "run") as run, \
             patch("pathlib.Path.is_file", return_value=False), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = validate_speech.main([workflow])

        self.assertEqual(result, 2)
        self.assertIn("Validation runner is missing", stderr.getvalue())
        run.assert_not_called()

    def test_help_lists_all_workflows(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = validate_speech.main(["--help"])

        self.assertEqual(result, 0)
        for workflow in validate_speech.COMMANDS:
            self.assertIn(workflow, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
