from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


@unittest.skipIf(
    "SENTINEL_MUTMUT_PRISTINE_ROOT" in os.environ,
    "real backend acceptance is verified before self-mutant execution",
)
class MutationCommandTests(unittest.TestCase):
    def test_strict_mutation_runs_every_generated_mutant_in_an_isolated_copy(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-mutation-") as directory:
            project = Path(directory)
            self._write_project(project)
            original = self._source_manifest(project)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    ["mutation", "--strict", "--project", str(project), "--format", "json"]
                )

            self.assertEqual(
                0,
                exit_code,
                stderr.getvalue() + stdout.getvalue(),
            )
            result = json.loads(stdout.getvalue())
            self.assertEqual("sentinel-quality-result-v1", result["schemaVersion"])
            self.assertEqual("passed", result["run"]["terminalStatus"])
            self.assertGreaterEqual(result["mutation"]["inScope"], 1)
            self.assertEqual(
                result["mutation"]["inScope"],
                result["mutation"]["killed"],
            )
            self.assertEqual("100", result["mutation"]["killRatePercent"])
            self.assertTrue(result["mutation"]["pass"])
            self.assertEqual(original, self._source_manifest(project))
            self.assertFalse((project / "mutants").exists())
            evidence = tuple(
                (project / ".sentinel" / "state-v1" / "runs").glob("*/evidence.json")
            )
            self.assertEqual(1, len(evidence))
            self.assertNotIn(str(project).encode(), evidence[0].read_bytes())

    def test_surviving_mutant_is_a_quality_failure_and_is_recorded(self):
        from sentinel_py.cli import main

        weak_test = (
            b"from app.subject import add_one\n\n"
            b"def test_add_one_is_nonnegative():\n    assert add_one(1) >= 0\n"
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-py-survivor-") as directory:
            project = Path(directory)
            self._write_project(project, test_source=weak_test)

            exit_code, result = self._run_json(main, project)

            self.assertEqual(2, exit_code, result)
            self.assertEqual("qualityFailed", result["run"]["terminalStatus"])
            self.assertFalse(result["mutation"]["pass"])
            self.assertEqual("nonKilledMutant", result["mutation"]["reason"])
            self.assertGreater(result["mutation"]["counts"]["survived"], 0)
            evidence = tuple(
                (project / ".sentinel" / "state-v1" / "runs").glob("*/evidence.json")
            )
            self.assertEqual(1, len(evidence))
            self.assertIn(b'"terminalStatus":"qualityFailed"', evidence[0].read_bytes())

    def test_non_assertion_pytest_failure_is_a_runtime_quality_failure(self):
        from sentinel_py.cli import main

        infrastructure_failure = (
            b"import os\n"
            b"from app.subject import add_one\n\n"
            b"def test_add_one():\n"
            b"    if '__mutmut_' in os.environ.get('MUTANT_UNDER_TEST', ''):\n"
            b"        raise RuntimeError('runner infrastructure failed')\n"
            b"    assert add_one(1) == 2\n"
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-py-false-kill-") as directory:
            project = Path(directory)
            self._write_project(project, test_source=infrastructure_failure)

            exit_code, result = self._run_json(main, project)

            self.assertEqual(2, exit_code, result)
            self.assertEqual("qualityFailed", result["run"]["terminalStatus"])
            self.assertGreater(result["mutation"]["inScope"], 0)
            self.assertEqual(
                result["mutation"]["inScope"],
                result["mutation"]["counts"]["runtimeError"],
            )
            self.assertEqual(0, result["mutation"]["killed"])
            self.assertFalse(result["mutation"]["pass"])
            evidence = tuple(
                (project / ".sentinel" / "state-v1" / "runs").glob("*/evidence.json")
            )
            self.assertEqual(1, len(evidence))
            evidence_document = json.loads(evidence[0].read_text(encoding="utf-8"))
            self.assertEqual(
                result["mutation"]["inScope"],
                evidence_document["components"]["mutation"]["runtimeError"],
            )
            self.assertIn("runtimeError", evidence_document["diagnosticCodes"])

    def test_failing_original_test_stops_before_backend_mutants(self):
        from sentinel_py.cli import main

        failing_test = (
            b"from app.subject import add_one\n\n"
            b"def test_wrong_expectation():\n    assert add_one(1) == 999\n"
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-py-baseline-") as directory:
            project = Path(directory)
            self._write_project(project, test_source=failing_test)
            original = self._source_manifest(project)

            exit_code, result = self._run_json(main, project)

            self.assertEqual(4, exit_code, result)
            self.assertEqual("baselineFailed", result["terminalStatus"])
            self.assertEqual("baselineTestsFailed", result["code"])
            self.assertEqual(original, self._source_manifest(project))
            self.assertFalse((project / "mutants").exists())

    @staticmethod
    def _write_project(project: Path, *, test_source: bytes | None = None) -> None:
        (project / "app").mkdir()
        (project / "tests").mkdir()
        (project / "app" / "subject.py").write_bytes(
            b"def add_one(value):\n    return value + 1\n"
        )
        default_test = (
            b"from app.subject import add_one\n\n"
            b"def test_add_one():\n    assert add_one(1) == 2\n"
        )
        (project / "tests" / "test_subject.py").write_bytes(
            default_test if test_source is None else test_source
        )
        document = {
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
        (project / "sentinel.config.json").write_text(
            json.dumps(document, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _source_manifest(project: Path) -> dict[str, bytes]:
        return {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in sorted((project / "app").rglob("*.py"))
        }

    @staticmethod
    def _run_json(main, project: Path) -> tuple[int, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                ["mutation", "--strict", "--project", str(project), "--format", "json"]
            )
        payload = json.loads(stdout.getvalue())
        if stderr.getvalue():
            payload["capturedStderr"] = stderr.getvalue()
        return exit_code, payload


if __name__ == "__main__":
    unittest.main()
