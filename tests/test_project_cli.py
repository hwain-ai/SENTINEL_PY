from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def _config() -> dict:
    return {
        "specVersion": "1.0.0",
        "modules": [
            {
                "id": "api",
                "language": "python",
                "root": ".",
                "production": ["app/**/*.py"],
                "testCommand": ["python", "-m", "pytest"],
                "coverage": {
                    "command": ["python", "-m", "coverage", "json"],
                    "format": "coverage-py-json",
                    "report": "coverage.json",
                },
                "testRoots": ["tests"],
                "testPatterns": ["test_*.py"],
            }
        ],
    }


def _coverage(files: dict) -> dict:
    return {
        "meta": {
            "format": 3,
            "version": "7.16.0",
            "timestamp": "2026-09-03T10:00:00",
            "branch_coverage": True,
            "show_contexts": False,
        },
        "files": files,
    }


def _file_coverage(*, executed=(), missing=()) -> dict:
    return {
        "executed_lines": list(executed),
        "missing_lines": list(missing),
        "excluded_lines": [],
    }


class ProjectCliTests(unittest.TestCase):
    def test_version_validates_the_project_without_creating_history(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-version-") as directory:
            project = Path(directory)
            self._write_project(project, b"def answer():\n    return 42\n")

            exit_code, stdout, stderr = self._run(
                main,
                ["version", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(0, exit_code, stderr)
            result = json.loads(stdout)
            self.assertEqual("sentinel-version-v1", result["schemaVersion"])
            self.assertEqual("python", result["language"])
            self.assertEqual("api", result["module"])
            self.assertEqual(1, result["productionFiles"])
            self.assertEqual("mutmut", result["backend"]["name"])
            self.assertEqual("3.7.0", result["backend"]["version"])
            self.assertEqual(
                "a1ff46257a341fd1b0d4764e2affdf7e8368fb085b5f913de240593d9b7bec44",
                result["backend"]["moduleTreeSha256"],
            )
            self.assertTrue(result["passed"])
            self.assertFalse((project / ".sentinel").exists())

    def test_local_crap_command_analyzes_report_and_commits_redacted_evidence(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-crap-") as directory:
            project = Path(directory)
            self._write_project(
                project,
                b"def answer():\n    value = 40\n    return value + 2\n",
            )
            self._write_json(
                project / "coverage.json",
                _coverage({"app/subject.py": _file_coverage(executed=(2, 3))}),
            )

            exit_code, stdout, stderr = self._run(
                main,
                [
                    "crap",
                    "--local",
                    "--project",
                    str(project),
                    "--format",
                    "json",
                    "--correlation-id",
                    "00000000-0000-4000-8000-000000000000",
                ],
            )

            self.assertEqual(0, exit_code, stderr)
            result = json.loads(stdout)
            self.assertEqual("sentinel-quality-result-v1", result["schemaVersion"])
            self.assertEqual("passed", result["run"]["terminalStatus"])
            self.assertEqual("1", result["crap"]["maxNumerator"])
            self.assertEqual("1", result["crap"]["maxDenominator"])
            self.assertEqual(0, result["crap"]["unknownCount"])
            self.assertTrue(result["crap"]["pass"])
            self.assertEqual(1, len(result["crap"]["callables"]))

            evidence_files = tuple(
                (project / ".sentinel" / "state-v1" / "runs").glob(
                    "*/evidence.json"
                )
            )
            self.assertEqual(1, len(evidence_files))
            evidence = evidence_files[0].read_bytes()
            self.assertNotIn(str(project).encode(), evidence)
            self.assertNotIn(b"value = 40", evidence)
            self.assertIn(b'"terminalStatus":"passed"', evidence)

    def test_same_crap_defect_in_two_runs_is_reported_as_repeated(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-history-") as directory:
            project = Path(directory)
            source = (
                b"def risky(first, second, third):\n"
                b"    if first:\n"
                b"        return 1\n"
                b"    if second:\n"
                b"        return 2\n"
                b"    if third:\n"
                b"        return 3\n"
                b"    return 0\n"
            )
            self._write_project(project, source)
            self._write_json(
                project / "coverage.json",
                _coverage(
                    {
                        "app/subject.py": _file_coverage(
                            missing=(2, 3, 4, 5, 6, 7, 8)
                        )
                    }
                ),
            )

            correlation = "00000000-0000-4000-8000-000000000001"
            first = self._run(
                main,
                [
                    "crap",
                    "--local",
                    "--project",
                    str(project),
                    "--format",
                    "json",
                    "--correlation-id",
                    correlation,
                ],
            )
            second = self._run(
                main,
                [
                    "crap",
                    "--local",
                    "--project",
                    str(project),
                    "--format",
                    "json",
                    "--correlation-id",
                    correlation,
                ],
            )
            evidence_files = tuple(
                sorted(
                    (project / ".sentinel" / "state-v1" / "runs").glob(
                        "*/evidence.json"
                    )
                )
            )
            first_evidence = evidence_files[0].read_bytes()

            history_exit, history_stdout, history_stderr = self._run(
                main,
                [
                    "history",
                    "--project",
                    str(project),
                    "--repeated",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(2, first[0])
            self.assertEqual(2, second[0])
            self.assertEqual(0, history_exit, history_stderr)
            self.assertEqual(2, len(evidence_files))
            self.assertEqual(first_evidence, evidence_files[0].read_bytes())
            history = json.loads(history_stdout)
            self.assertEqual("sentinel-history-v1", history["schemaVersion"])
            self.assertEqual(2, history["completedRuns"])
            self.assertEqual(1, len(history["findings"]))
            self.assertEqual(2, history["findings"][0]["observationCount"])
            self.assertTrue(history["findings"][0]["repeated"])

    def test_invalid_config_fails_before_state_creation_with_exact_code(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-config-") as directory:
            project = Path(directory)
            config = _config()
            config["unexpected"] = True
            self._write_project(project, b"def answer():\n    return 42\n", config)

            exit_code, stdout, stderr = self._run(
                main,
                ["version", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(3, exit_code, stderr)
            self.assertEqual("projectConfigShapeInvalid", json.loads(stdout)["code"])
            self.assertFalse((project / ".sentinel").exists())

    def test_history_on_an_uninitialized_project_is_read_only(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-empty-history-") as directory:
            project = Path(directory)

            exit_code, stdout, stderr = self._run(
                main,
                ["history", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(0, json.loads(stdout)["completedRuns"])
            self.assertEqual([], list(project.iterdir()))

    def test_malformed_coverage_report_is_a_dependency_error_without_state(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-bad-coverage-") as directory:
            project = Path(directory)
            self._write_project(project, b"def answer():\n    return 42\n")
            (project / "coverage.json").write_bytes(b"not-json")

            exit_code, stdout, stderr = self._run(
                main,
                ["crap", "--local", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(5, exit_code, stderr)
            self.assertEqual("coverageReportInvalid", json.loads(stdout)["code"])
            self.assertFalse((project / ".sentinel").exists())

    def test_strict_crap_generates_fresh_coverage_only_inside_a_snapshot(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-strict-crap-") as directory:
            project = Path(directory)
            self._write_project(
                project,
                b"def answer():\n    value = 40\n    return value + 2\n",
            )
            (project / "tests" / "test_subject.py").write_bytes(
                b"from app.subject import answer\n\n"
                b"def test_answer():\n    assert answer() == 42\n"
            )

            exit_code, stdout, stderr = self._run(
                main,
                ["crap", "--strict", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(0, exit_code, stderr + stdout)
            result = json.loads(stdout)
            self.assertEqual("strict", result["run"]["mode"])
            self.assertEqual("passed", result["run"]["terminalStatus"])
            self.assertEqual("1", result["crap"]["maxNumerator"])
            self.assertTrue(result["crap"]["pass"])
            self.assertFalse((project / "coverage.json").exists())
            self.assertFalse((project / ".coverage").exists())

    def test_unclassified_python_source_cannot_be_hidden_by_the_production_glob(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-hidden-source-") as directory:
            project = Path(directory)
            self._write_project(project, b"def answer():\n    return 42\n")
            (project / "forgotten.py").write_bytes(b"def hidden():\n    return False\n")

            exit_code, stdout, stderr = self._run(
                main,
                ["version", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(3, exit_code, stderr)
            self.assertEqual("unclassifiedSource", json.loads(stdout)["code"])
            self.assertFalse((project / ".sentinel").exists())

    def test_partial_pytest_selector_is_rejected_before_any_child_process(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-partial-tests-") as directory:
            project = Path(directory)
            config = _config()
            config["modules"][0]["testCommand"].extend(("-k", "only_one"))
            self._write_project(project, b"def answer():\n    return 42\n", config)

            exit_code, stdout, stderr = self._run(
                main,
                ["version", "--project", str(project), "--format", "json"],
            )

            self.assertEqual(3, exit_code, stderr)
            self.assertEqual("partialTestSelection", json.loads(stdout)["code"])
            self.assertFalse((project / ".sentinel").exists())

    @staticmethod
    def _run(main, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _write_project(
        cls,
        project: Path,
        source: bytes,
        config: dict | None = None,
    ) -> None:
        (project / "app").mkdir(parents=True)
        (project / "tests").mkdir()
        (project / "app" / "subject.py").write_bytes(source)
        cls._write_json(project / "sentinel.config.json", config or _config())


if __name__ == "__main__":
    unittest.main()
