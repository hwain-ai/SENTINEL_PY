from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"

sys.path.insert(0, str(SOURCE_ROOT))

from sentinel_py.gate import DEFAULT_GATE  # noqa: E402


class CliHelpTests(unittest.TestCase):
    def test_error_emission_builds_one_exact_diagnostic(self):
        from sentinel_py import cli

        arguments = object()
        with patch.object(cli, "_emit_result", return_value=41) as emit_result:
            self.assertEqual(
                41,
                cli._emit_error(arguments, 7, "exactCode", "evidenceError"),
            )
        emit_result.assert_called_once_with(
            arguments,
            7,
            {"code": "exactCode", "terminalStatus": "evidenceError"},
            error_text=True,
        )

    def test_history_project_root_requires_one_resolved_directory(self):
        from sentinel_py import cli

        with tempfile.TemporaryDirectory(prefix="sentinel-history-root-") as directory:
            root = Path(directory)
            child = root / "child"
            child.mkdir()
            self.assertEqual(child.resolve(), cli._history_project_root(str(child)))

            with patch.object(Path, "cwd", return_value=child):
                self.assertEqual(child.resolve(), cli._history_project_root(None))

            file_path = root / "file.txt"
            file_path.write_text("file", encoding="utf-8")
            for invalid in (root / "missing", file_path):
                with self.subTest(path=invalid.name):
                    with self.assertRaises(cli.UsageConfigError) as stopped:
                        cli._history_project_root(str(invalid))
                    self.assertEqual("projectRootInvalid", stopped.exception.code)

        for failure in (OSError("resolve"), RuntimeError("resolve")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(Path, "resolve", side_effect=failure):
                    with self.assertRaises(cli.UsageConfigError) as stopped:
                        cli._history_project_root("project")
                self.assertEqual("projectRootInvalid", stopped.exception.code)
                self.assertIs(failure, stopped.exception.__cause__)

    def test_main_dispatches_and_maps_every_supported_failure_exactly(self):
        from sentinel_py import cli
        from sentinel_py.backend_lock import BackendLockError
        from sentinel_py.config import UsageConfigError
        from sentinel_py.coverage import CoverageFormatError, MetricOrderingError
        from sentinel_py.crap import AnalysisError
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.mutation import MutationGateError
        from sentinel_py.mutmut_adapter import MutmutBridgeError
        from sentinel_py.quality import DependencyFailure
        from sentinel_py.runner import (
            BaselineFailure,
            CoverageRunnerError,
            MutationBackendError,
        )

        arguments = SimpleNamespace(command="doctor")
        argv = ("doctor", "--format", "json")
        parser = SimpleNamespace(parse_args=lambda value: arguments)
        with (
            patch.object(cli, "build_parser", return_value=parser) as build_parser,
            patch.object(cli, "_dispatch", return_value=29) as dispatch,
        ):
            self.assertEqual(29, cli.main(argv))
        build_parser.assert_called_once_with()
        dispatch.assert_called_once_with(arguments)

        cases = (
            (
                UsageConfigError("usage-code"),
                cli.USAGE_CONFIG_ERROR,
                "usage-code",
                "usageConfigError",
            ),
            (
                BackendLockError("lock-code"),
                cli.DEPENDENCY_ERROR,
                "lock-code",
                "dependencyError",
            ),
            (
                DependencyFailure("dependency-code"),
                cli.DEPENDENCY_ERROR,
                "dependency-code",
                "dependencyError",
            ),
            (
                CoverageFormatError("bad coverage"),
                cli.DEPENDENCY_ERROR,
                "coverageReportInvalid",
                "dependencyError",
            ),
            (
                CoverageRunnerError("coverage-code"),
                cli.DEPENDENCY_ERROR,
                "coverage-code",
                "dependencyError",
            ),
            (
                BaselineFailure("baseline-code"),
                cli.BASELINE_FAILED,
                "baseline-code",
                "baselineFailed",
            ),
            (
                MutationBackendError("backend-code"),
                cli.BACKEND_ERROR,
                "backend-code",
                "backendError",
            ),
            (
                MutmutBridgeError("bridge-code"),
                cli.BACKEND_ERROR,
                "bridge-code",
                "backendError",
            ),
            (
                MutationGateError("gate-code"),
                cli.BACKEND_ERROR,
                "gate-code",
                "backendError",
            ),
            (
                EvidenceError("evidence-code"),
                cli.EVIDENCE_ERROR,
                "evidence-code",
                "evidenceError",
            ),
            (
                AnalysisError("analysis-code"),
                cli.TOOL_ERROR,
                "analysis-code",
                "toolError",
            ),
            (
                MetricOrderingError("ordering-code"),
                cli.TOOL_ERROR,
                "ordering-code",
                "toolError",
            ),
        )
        for error, exit_code, code, terminal_status in cases:
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(cli, "build_parser", return_value=parser),
                    patch.object(cli, "_dispatch", side_effect=error),
                    patch.object(cli, "_emit_error", return_value=31) as emit_error,
                ):
                    self.assertEqual(31, cli.main(argv))
                emit_error.assert_called_once_with(
                    arguments,
                    exit_code,
                    code,
                    terminal_status,
                )

    def test_project_dispatch_routes_every_command_to_one_exact_runner(self):
        from sentinel_py import cli

        project = object()
        doctor_document = {"runner": "doctor"}
        check_document = {"runner": "check"}
        mutation_document = {"runner": "mutation"}
        local_document = {"runner": "local-crap"}
        strict_document = {"runner": "strict-crap"}
        arguments = (
            SimpleNamespace(command="doctor"),
            SimpleNamespace(command="check", correlation_id="check-id"),
            SimpleNamespace(command="mutation", correlation_id="mutation-id"),
            SimpleNamespace(command="crap", mode="local", correlation_id="local-id"),
            SimpleNamespace(command="crap", mode="strict", correlation_id="strict-id"),
        )
        with (
            patch.object(cli, "doctor_result", return_value=doctor_document) as doctor,
            patch.object(
                cli,
                "run_strict_check",
                return_value=(12, check_document),
            ) as check,
            patch.object(
                cli,
                "run_strict_mutation",
                return_value=(13, mutation_document),
            ) as mutation,
            patch.object(
                cli,
                "run_local_crap",
                return_value=(14, local_document),
            ) as local_crap,
            patch.object(
                cli,
                "run_strict_crap",
                return_value=(15, strict_document),
            ) as strict_crap,
            patch.object(
                cli,
                "_emit_result",
                side_effect=(101, 102, 103, 104, 105),
            ) as emit,
        ):
            results = tuple(
                cli._dispatch_project_command(argument, project, DEFAULT_GATE)
                for argument in arguments
            )

        self.assertEqual((101, 102, 103, 104, 105), results)
        doctor.assert_called_once_with(project)
        check.assert_called_once_with(project, "check-id", DEFAULT_GATE)
        mutation.assert_called_once_with(project, "mutation-id", DEFAULT_GATE)
        local_crap.assert_called_once_with(project, "local-id", DEFAULT_GATE)
        strict_crap.assert_called_once_with(project, "strict-id", DEFAULT_GATE)
        self.assertEqual(
            [
                call(arguments[0], 0, doctor_document),
                call(arguments[1], 12, check_document),
                call(arguments[2], 13, mutation_document),
                call(arguments[3], 14, local_document),
                call(arguments[4], 15, strict_document),
            ],
            emit.call_args_list,
        )

    def test_dispatch_preserves_explicit_config_and_module_selection(self):
        from sentinel_py import cli

        arguments = SimpleNamespace(
            command="doctor",
            project="project",
            config="chosen.json",
            module="api",
            mode="strict",
        )
        project = object()
        with (
            patch.object(cli, "load_project", return_value=project) as load_project,
            patch.object(
                cli,
                "_dispatch_project_command",
                return_value=27,
            ) as dispatch_project,
        ):
            self.assertEqual(27, cli._dispatch(arguments))

        load_project.assert_called_once_with("project", "chosen.json", "api")
        dispatch_project.assert_called_once_with(arguments, project, DEFAULT_GATE)

    def test_pending_quality_commands_cover_check_and_mutation_only(self):
        from sentinel_py import cli

        cases = (
            ("check", "local", True),
            ("mutation", "local", True),
            ("check", "strict", False),
            ("mutation", "strict", False),
            ("crap", "local", False),
        )
        for command, mode, expected in cases:
            with self.subTest(command=command, mode=mode):
                self.assertIs(
                    expected,
                    cli._quality_command_pending(
                        SimpleNamespace(command=command, mode=mode)
                    ),
                )

    def test_history_preserves_pending_arguments_and_repeated_filter(self):
        from sentinel_py import cli

        pending = SimpleNamespace(
            command="history",
            history_command="prune",
            run_id=None,
            local_details=False,
            export=None,
            before=None,
            incomplete=False,
            confirm=False,
        )
        with patch.object(cli, "_pending", return_value=31) as pending_result:
            self.assertEqual(31, cli._history(pending))
        pending_result.assert_called_once_with(pending)

        query = SimpleNamespace(
            command="history",
            history_command=None,
            project="project",
            run_id=None,
            repeated=True,
            local_details=False,
            export=None,
            before=None,
            incomplete=False,
            confirm=False,
        )
        root = Path("/project")
        result = {"history": True}
        with (
            patch.object(cli, "_history_project_root", return_value=root),
            patch.object(cli, "read_history", return_value=result) as read_history,
            patch.object(cli, "_emit_result", return_value=32) as emit_result,
        ):
            self.assertEqual(32, cli._history(query))
        read_history.assert_called_once_with(root, repeated_only=True)
        emit_result.assert_called_once_with(query, 0, result)

    def test_result_emission_preserves_output_json_error_and_text_contracts(self):
        from sentinel_py import cli

        document = {"z": "한글", "a": 1}
        compact = '{"z":"한글","a":1}\n'
        pretty = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

        output_arguments = SimpleNamespace(
            output="result.json",
            format="text",
        )
        with (
            patch.object(cli, "_write_output") as write_output,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(
                17,
                cli._emit_result(output_arguments, 17, document),
            )
        write_output.assert_called_once_with(Path("result.json"), compact)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

        for output_format in ("json", "jsonl"):
            with self.subTest(output_format=output_format):
                arguments = SimpleNamespace(output=None, format=output_format)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(18, cli._emit_result(arguments, 18, document))
                self.assertEqual(compact, stdout.getvalue())

        error_arguments = SimpleNamespace(output=None, format="text")
        error = {"terminalStatus": "backendError", "code": "backendFailed"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                19,
                cli._emit_result(error_arguments, 19, error, error_text=True),
            )
        self.assertEqual("backendError: backendFailed\n", stderr.getvalue())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(20, cli._emit_result(error_arguments, 20, document))
        self.assertEqual(pretty, stdout.getvalue())

    def test_output_writer_preserves_utf8_and_reports_the_exact_failure(self):
        from sentinel_py.cli import _write_output
        from sentinel_py.evidence import EvidenceError

        with tempfile.TemporaryDirectory(prefix="sentinel-py-output-") as directory:
            root = Path(directory)
            output = root / "result.json"
            _write_output(output, '{"한글":true}\n')
            self.assertEqual(b'{"\xed\x95\x9c\xea\xb8\x80":true}\n', output.read_bytes())

            with self.assertRaises(EvidenceError) as stopped:
                _write_output(root, "payload")

        self.assertEqual("outputWriteFailed", str(stopped.exception))

        with patch.object(Path, "write_text") as writer:
            _write_output(Path("result.json"), "payload")
        writer.assert_called_once_with("payload", encoding="utf-8")

    def test_result_emission_explicitly_disables_ascii_escaping(self):
        from sentinel_py import cli

        document = {"한글": True}
        arguments = SimpleNamespace(output="result.json", format="json")
        with (
            patch.object(cli.json, "dumps", return_value="{}") as dumps,
            patch.object(cli, "_write_output"),
        ):
            self.assertEqual(17, cli._emit_result(arguments, 17, document))
        dumps.assert_called_once_with(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        arguments = SimpleNamespace(output=None, format="text")
        with (
            patch.object(cli.json, "dumps", return_value="{}") as dumps,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(18, cli._emit_result(arguments, 18, document))
        self.assertEqual(
            [
                call(document, ensure_ascii=False, separators=(",", ":")),
                call(document, ensure_ascii=False, indent=2),
            ],
            dumps.call_args_list,
        )

    def test_root_help_preserves_program_description_and_requires_a_command(self):
        from sentinel_py.cli import build_parser

        parser = build_parser()
        help_text = parser.format_help()

        self.assertTrue(help_text.startswith("usage: sentinel-py "))
        self.assertEqual(
            "Strict Python CRAP and mutation quality gates.",
            parser.description,
        )
        self.assertIn(parser.description, help_text)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                parser.parse_args([])
        self.assertEqual(2, stopped.exception.code)

    def test_help_lists_the_complete_command_surface_without_writing_state(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-help-") as directory:
            project = Path(directory)
            stdout = io.StringIO()
            previous = Path.cwd()
            os.chdir(project)
            try:
                with contextlib.redirect_stdout(stdout):
                    with self.assertRaises(SystemExit) as stopped:
                        main(["--help"])
            finally:
                os.chdir(previous)

            self.assertEqual(0, stopped.exception.code)
            for command in ("crap", "mutation", "check", "doctor", "history"):
                with self.subTest(command=command):
                    self.assertIn(command, stdout.getvalue())
            self.assertEqual([], list(project.iterdir()))

    def test_quality_commands_default_to_strict_and_local_is_explicit(self):
        from sentinel_py.cli import build_parser

        for command in ("crap", "mutation", "check"):
            with self.subTest(command=command, mode="default"):
                arguments = build_parser().parse_args([command])
                self.assertEqual("strict", arguments.mode)
            with self.subTest(command=command, mode="local"):
                arguments = build_parser().parse_args([command, "--local"])
                self.assertEqual("local", arguments.mode)

    def test_quality_command_rejects_two_execution_modes(self):
        from sentinel_py.cli import build_parser

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                build_parser().parse_args(["check", "--strict", "--local"])

        self.assertEqual(2, stopped.exception.code)

    def test_parser_preserves_every_quality_option_without_shell_parsing(self):
        from sentinel_py.cli import build_parser

        arguments = build_parser().parse_args(
            [
                "mutation",
                "--project",
                "project path",
                "--config",
                "sentinel.json",
                "--module",
                "api",
                "--strict",
                "--format",
                "json",
                "--output",
                "result.json",
                "--correlation-id",
                "00000000-0000-4000-8000-000000000000",
            ]
        )

        self.assertEqual(
            {
                "command": "mutation",
                "project": "project path",
                "config": "sentinel.json",
                "module": "api",
                "mode": "strict",
                "format": "json",
                "output": "result.json",
                "correlation_id": "00000000-0000-4000-8000-000000000000",
                "crap_max": None,
                "mutation_min": None,
                "changed_file": [],
            },
            vars(arguments),
        )

    def test_history_parser_preserves_query_and_prune_options(self):
        from sentinel_py.cli import build_parser

        arguments = build_parser().parse_args(
            [
                "history",
                "prune",
                "--project",
                "project",
                "--run-id",
                "run",
                "--repeated",
                "--local-details",
                "--export",
                "export.json",
                "--before",
                "2026-09-03",
                "--incomplete",
                "--confirm",
                "--format",
                "jsonl",
            ]
        )

        self.assertEqual(
            {
                "command": "history",
                "history_command": "prune",
                "project": "project",
                "run_id": "run",
                "repeated": True,
                "local_details": True,
                "export": "export.json",
                "before": "2026-09-03",
                "incomplete": True,
                "confirm": True,
                "format": "jsonl",
            },
            vars(arguments),
        )

    def test_history_command_is_optional_and_only_prune_is_valid(self):
        from sentinel_py.cli import build_parser

        query = build_parser().parse_args(["history"])
        prune = build_parser().parse_args(["history", "prune"])

        self.assertIsNone(query.history_command)
        self.assertEqual("prune", prune.history_command)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                build_parser().parse_args(["history", "show"])
        self.assertEqual(2, stopped.exception.code)

    def test_output_formats_are_exact_for_quality_doctor_and_history(self):
        from sentinel_py.cli import build_parser

        commands = (["check"], ["doctor"], ["history"])
        for prefix in commands:
            with self.subTest(command=prefix[0], mode="default"):
                self.assertEqual(
                    "text",
                    build_parser().parse_args(prefix).format,
                )
            for output_format in ("text", "json", "jsonl"):
                with self.subTest(command=prefix[0], output_format=output_format):
                    arguments = build_parser().parse_args(
                        [*prefix, "--format", output_format]
                    )
                    self.assertEqual(output_format, arguments.format)
            for invalid_format in ("TEXT", "JSON", "JSONL", "yaml"):
                with self.subTest(command=prefix[0], invalid_format=invalid_format):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as stopped:
                            build_parser().parse_args(
                                [*prefix, "--format", invalid_format]
                            )
                    self.assertEqual(2, stopped.exception.code)

    def test_pending_check_command_fails_closed_in_text_and_json(self):
        from sentinel_py.cli import DEPENDENCY_ERROR, main

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(DEPENDENCY_ERROR, main(["check", "--local"]))
        self.assertEqual("dependencyError: commandPending\n", stderr.getvalue())

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(
                DEPENDENCY_ERROR,
                main(["check", "--local", "--format", "json"]),
            )
        self.assertEqual(
            '{"code":"commandPending","command":"check",'
            '"terminalStatus":"dependencyError"}\n',
            stdout.getvalue(),
        )

    def test_installed_module_help_has_no_project_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-py-module-help-") as directory:
            project = Path(directory)
            result = subprocess.run(
                [sys.executable, "-m", "sentinel_py", "--help"],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("sentinel-py", result.stdout)
            self.assertEqual([], list(project.iterdir()))


if __name__ == "__main__":
    unittest.main()
