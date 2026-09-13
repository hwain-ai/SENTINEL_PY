from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def _coverage_payload(files: dict) -> bytes:
    document = {
        "meta": {
            "format": 3,
            "version": "7.16.0",
            "timestamp": "2026-09-11T12:00:00",
            "branch_coverage": True,
            "show_contexts": False,
        },
        "files": files,
    }
    return (json.dumps(document, separators=(",", ":")) + "\n").encode()


def _file_coverage(*, executed=(), missing=()) -> dict:
    return {
        "executed_lines": list(executed),
        "missing_lines": list(missing),
        "excluded_lines": [],
    }


class CoverageEvidenceTests(unittest.TestCase):
    def test_real_coverage_reasons_commit_and_round_trip_through_history(self):
        from sentinel_py.coverage import load_coverage_json, measure_source
        from sentinel_py.evidence import read_history
        from sentinel_py.quality import _complete_crap

        cases = (
            (
                "file-entry-missing",
                "coverageFileMissing",
                b"def missing():\n    return 1\n",
                {},
            ),
            (
                "ambiguous-line-coverage",
                "coverageLineAmbiguous",
                b"def ambiguous(): return 1\n",
                {"subject.py": _file_coverage(executed=(1,))},
            ),
            (
                "no-executable-lines",
                "coverageNoExecutableLines",
                b'def documented():\n    """Documentation only."""\n',
                {"subject.py": _file_coverage()},
            ),
        )
        for raw_reason, evidence_reason, source, coverage_files in cases:
            with (
                self.subTest(reason=raw_reason),
                tempfile.TemporaryDirectory(prefix="sentinel-coverage-evidence-") as directory,
            ):
                project_root = Path(directory)
                project = SimpleNamespace(
                    project_root=project_root,
                    module=SimpleNamespace(module_id="api"),
                )
                report = load_coverage_json(_coverage_payload(coverage_files))
                metrics = measure_source(source, "subject.py", report)

                exit_code, result = _complete_crap(
                    project,
                    metrics,
                    "strict",
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                )
                history = read_history(project_root, repeated_only=False)

                self.assertEqual(2, exit_code)
                self.assertEqual("qualityFailed", result["run"]["terminalStatus"])
                self.assertFalse(result["crap"]["pass"])
                self.assertEqual(1, result["crap"]["unknownCount"])
                self.assertEqual(raw_reason, metrics[0].crap.unknown_reason)
                self.assertEqual("coverageUnknown", result["crap"]["callables"][0]["status"])
                self.assertEqual(1, history["completedRuns"])
                self.assertEqual(evidence_reason, history["findings"][0]["reason"])

    def test_strict_check_cli_keeps_all_unknowns_and_runtime_error_failed(self):
        from sentinel_py.cli import main
        from sentinel_py.evidence import read_history
        from sentinel_py.mutation import MutantRecord
        from sentinel_py.runner.coverage_backend import CoverageExecution
        from sentinel_py.runner.mutation_backend import MutationExecution

        sources = {
            "app/ambiguous.py": b"def ambiguous(): return 1\n",
            "app/documented.py": b'def documented():\n    """Documentation only."""\n',
            "app/missing.py": b"def missing():\n    return 1\n",
        }
        coverage = _coverage_payload(
            {
                "app/ambiguous.py": _file_coverage(executed=(1,)),
                "app/documented.py": _file_coverage(),
            }
        )
        coverage_execution = CoverageExecution(MappingProxyType(sources), coverage)
        mutation_execution = MutationExecution(
            ("candidate-runtime",),
            (MutantRecord("candidate-runtime", "runtimeError"),),
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-check-") as directory:
            project = Path(directory)
            self._write_project(project, sources)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("sentinel_py.quality.run_fresh_coverage", return_value=coverage_execution),
                patch("sentinel_py.quality.run_mutmut", return_value=mutation_execution),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    ["check", "--strict", "--project", str(project), "--format", "json"]
                )

            self.assertEqual(2, exit_code, stderr.getvalue() + stdout.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertEqual("qualityFailed", result["run"]["terminalStatus"])
            self.assertFalse(result["crap"]["pass"])
            self.assertEqual(3, result["crap"]["unknownCount"])
            self.assertTrue(
                all(
                    callable_result["status"] == "coverageUnknown"
                    for callable_result in result["crap"]["callables"]
                )
            )
            self.assertFalse(result["mutation"]["pass"])
            self.assertEqual("nonKilledMutant", result["mutation"]["reason"])
            self.assertEqual(1, result["mutation"]["counts"]["runtimeError"])
            history = read_history(project, repeated_only=False)
            self.assertEqual(1, history["completedRuns"])
            self.assertEqual(
                {
                    "coverageFileMissing",
                    "coverageLineAmbiguous",
                    "coverageNoExecutableLines",
                    "runtimeError",
                },
                {finding["reason"] for finding in history["findings"]},
            )
            evidence_path = next(
                (project / ".sentinel" / "state-v1" / "runs").glob("*/evidence.json")
            )
            evidence = evidence_path.read_bytes()
            self.assertNotIn(str(project).encode(), evidence)
            self.assertNotIn(b"Documentation only.", evidence)

    def test_existing_reason_is_preserved_and_unknown_hyphen_is_rejected(self):
        from sentinel_py.evidence import EvidenceError, commit_quality_evidence
        from sentinel_py.quality import _metric_finding, _redacted_summary, _run_record

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-invalid-") as directory:
            project = SimpleNamespace(
                project_root=Path(directory),
                module=SimpleNamespace(module_id="api"),
            )
            metric = SimpleNamespace(
                callable=SimpleNamespace(
                    callable_id="python:v1:subject",
                    module_relative_path="subject.py",
                ),
                crap=SimpleNamespace(passed=None, unknown_reason="coverageFileMissing"),
            )
            self.assertEqual(
                "coverageFileMissing",
                _metric_finding(project, metric).reason,
            )

            metric.crap.unknown_reason = "unknown-hyphen-reason"
            finding = _metric_finding(project, metric)
            summary = _redacted_summary(
                {
                    "callables": [{}],
                    "crapMax": "8",
                    "maxDenominator": None,
                    "maxNumerator": None,
                    "pass": False,
                    "reason": "coverageUnknown",
                    "unknownCount": 1,
                }
            )
            run = _run_record(
                "crap",
                "strict",
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                "qualityFailed",
            )

            with self.assertRaises(EvidenceError) as stopped:
                commit_quality_evidence(project.project_root, run, summary, (finding,))

            self.assertEqual("evidenceInvalid", str(stopped.exception))

    @staticmethod
    def _write_project(project: Path, sources: dict[str, bytes]) -> None:
        for relative_path, source in sources.items():
            path = project / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source)
        (project / "tests").mkdir()
        config = {
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
            json.dumps(config, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
