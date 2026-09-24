from __future__ import annotations

import importlib.metadata
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sentinel_py.gate import DEFAULT_GATE


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


class MutationQualityTests(unittest.TestCase):
    def test_local_and_strict_crap_preserve_correlation_and_run_identity(self):
        from sentinel_py.quality import run_local_crap, run_strict_crap

        project = SimpleNamespace(
            module=SimpleNamespace(coverage_report=Path("/project/coverage.json"))
        )
        local_report = object()
        strict_report = object()
        local_metrics = (object(),)
        strict_metrics = (object(), object())
        execution = SimpleNamespace(report=b"fresh", sources={"app.py": b"source"})
        with (
            patch("sentinel_py.quality.uuid.uuid4", return_value="local-run"),
            patch(
                "sentinel_py.quality._correlation_id",
                return_value="local-correlation",
            ) as correlation,
            patch(
                "sentinel_py.quality._coverage_report",
                return_value=local_report,
            ) as coverage_report,
            patch(
                "sentinel_py.quality._measure_project",
                return_value=local_metrics,
            ) as measure_project,
            patch(
                "sentinel_py.quality._complete_crap",
                return_value=(0, {"local": True}),
            ) as complete,
        ):
            self.assertEqual(
                (0, {"local": True}),
                run_local_crap(project, "requested-correlation"),
            )
        correlation.assert_called_once_with("requested-correlation", "local-run")
        coverage_report.assert_called_once_with(project.module.coverage_report)
        measure_project.assert_called_once_with(project, local_report, DEFAULT_GATE)
        complete.assert_called_once_with(
            project,
            local_metrics,
            "local",
            "local-correlation",
            "local-run",
            DEFAULT_GATE,
        )

        with (
            patch("sentinel_py.quality.uuid.uuid4", return_value="strict-run"),
            patch(
                "sentinel_py.quality._correlation_id",
                return_value="strict-correlation",
            ) as correlation,
            patch(
                "sentinel_py.quality.run_fresh_coverage",
                return_value=execution,
            ) as fresh_coverage,
            patch(
                "sentinel_py.quality.load_coverage_json",
                return_value=strict_report,
            ) as load_coverage,
            patch(
                "sentinel_py.quality._measure_sources",
                return_value=strict_metrics,
            ) as measure_sources,
            patch(
                "sentinel_py.quality._complete_crap",
                return_value=(2, {"strict": True}),
            ) as complete,
        ):
            self.assertEqual(
                (2, {"strict": True}),
                run_strict_crap(project, "requested-correlation"),
            )
        correlation.assert_called_once_with("requested-correlation", "strict-run")
        fresh_coverage.assert_called_once_with(project)
        load_coverage.assert_called_once_with(b"fresh")
        measure_sources.assert_called_once_with(execution.sources, strict_report, DEFAULT_GATE)
        complete.assert_called_once_with(
            project,
            strict_metrics,
            "strict",
            "strict-correlation",
            "strict-run",
            DEFAULT_GATE,
        )

    def test_complete_crap_joins_gate_reason_findings_and_redacted_evidence(self):
        from sentinel_py.quality import _complete_crap

        project = self._project()
        metrics = (object(), object())
        gate_metrics = (object(),)
        gate = SimpleNamespace(
            metrics=gate_metrics,
            passed=False,
            reason="crapThresholdExceeded",
        )
        summary = {"callables": [], "pass": False}
        run = {"terminalStatus": "qualityFailed"}
        redacted = {"redacted": True}
        findings = (object(),)
        with (
            patch(
                "sentinel_py.quality.evaluate_crap_gate",
                return_value=gate,
            ) as evaluate,
            patch(
                "sentinel_py.quality._crap_summary",
                return_value=summary,
            ) as summarize,
            patch(
                "sentinel_py.quality._run_record",
                return_value=run,
            ) as run_record,
            patch(
                "sentinel_py.quality._finding_identities",
                return_value=findings,
            ) as finding_identities,
            patch(
                "sentinel_py.quality._redacted_summary",
                return_value=redacted,
            ) as redact,
            patch("sentinel_py.quality.commit_quality_evidence") as commit,
        ):
            exit_code, result = _complete_crap(
                project,
                metrics,
                "strict",
                "correlation",
                "run-id",
                DEFAULT_GATE,
            )

        self.assertEqual(2, exit_code)
        self.assertEqual(
            {
                "crap": summary,
                "run": run,
                "schemaVersion": "sentinel-quality-result-v1",
            },
            result,
        )
        evaluate.assert_called_once_with(metrics, DEFAULT_GATE.crap_max)
        summarize.assert_called_once_with(
            gate_metrics,
            False,
            "crapThresholdExceeded",
            "8",
        )
        run_record.assert_called_once_with(
            "crap",
            "strict",
            "run-id",
            "correlation",
            "qualityFailed",
        )
        finding_identities.assert_called_once_with(
            project,
            gate_metrics,
            "crapThresholdExceeded",
        )
        redact.assert_called_once_with(summary)
        commit.assert_called_once_with(
            project.project_root,
            run,
            redacted,
            findings,
        )

    def test_crap_summary_uses_exact_fraction_and_redaction_contract(self):
        from fractions import Fraction

        from sentinel_py.quality import (
            _coverage_fraction,
            _crap_fraction,
            _crap_summary,
            _redacted_summary,
        )

        lower = SimpleNamespace(
            callable=SimpleNamespace(callable_id="lower"),
            crap=SimpleNamespace(
                numerator=15,
                denominator=2,
                unknown_reason=None,
            ),
        )
        higher = SimpleNamespace(
            callable=SimpleNamespace(callable_id="higher"),
            crap=SimpleNamespace(
                numerator=31,
                denominator=4,
                unknown_reason=None,
            ),
        )
        unknown = SimpleNamespace(
            callable=SimpleNamespace(callable_id="unknown"),
            crap=SimpleNamespace(
                numerator=None,
                denominator=None,
                unknown_reason="coverageUnknown",
            ),
        )
        with patch(
            "sentinel_py.quality._callable_record",
            side_effect=lambda metric: {"id": metric.callable.callable_id},
        ):
            summary = _crap_summary(
                (lower, higher, unknown),
                False,
                "coverageUnknown",
            )

        self.assertEqual(Fraction(15, 2), _crap_fraction(lower))
        self.assertEqual(
            {
                "callables": [
                    {"id": "lower"},
                    {"id": "higher"},
                    {"id": "unknown"},
                ],
                "maxDenominator": "4",
                "crapMax": "8",
                "maxNumerator": "31",
                "pass": False,
                "reason": "coverageUnknown",
                "unknownCount": 1,
            },
            summary,
        )
        self.assertEqual(
            {
                "callableCount": 3,
                "maxDenominator": "4",
                "crapMax": "8",
                "maxNumerator": "31",
                "pass": False,
                "reason": "coverageUnknown",
                "unknownCount": 1,
            },
            _redacted_summary(summary),
        )
        self.assertIsNone(_coverage_fraction(4, 0))
        self.assertEqual("0.5", _coverage_fraction(2, 4))

    def test_run_record_uses_utc_and_preserves_every_public_field(self):
        from datetime import timezone

        from sentinel_py.quality import _run_record

        instant = object()
        with (
            patch("sentinel_py.quality.datetime") as clock,
            patch(
                "sentinel_py.quality._format_utc",
                return_value="2026-09-04T12:34:56.000Z",
            ) as format_utc,
        ):
            clock.now.return_value = instant
            record = _run_record(
                "crap",
                "strict",
                "run-id",
                "correlation-id",
                "passed",
            )

        clock.now.assert_called_once_with(timezone.utc)
        format_utc.assert_called_once_with(instant)
        self.assertEqual(
            {
                "command": "crap",
                "completedAt": "2026-09-04T12:34:56.000Z",
                "correlationId": "correlation-id",
                "mode": "strict",
                "runId": "run-id",
                "terminalStatus": "passed",
            },
            record,
        )

    def test_dependency_version_and_correlation_identity_fail_closed(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.quality import (
            DependencyFailure,
            _correlation_id,
            _required_version,
        )

        with patch("sentinel_py.quality.importlib.metadata.version", return_value="1.2.3"):
            self.assertEqual("1.2.3", _required_version("sample", "1.2.3"))

        with patch("sentinel_py.quality.importlib.metadata.version", return_value="9.9.9"):
            with self.assertRaises(DependencyFailure) as stopped:
                _required_version("sample", "1.2.3")
        self.assertEqual("sampleVersionMismatch", stopped.exception.code)

        missing = importlib.metadata.PackageNotFoundError("sample")
        with patch(
            "sentinel_py.quality.importlib.metadata.version",
            side_effect=missing,
        ):
            with self.assertRaises(DependencyFailure) as stopped:
                _required_version("sample", "1.2.3")
        self.assertEqual("sampleNotInstalled", stopped.exception.code)
        self.assertIs(missing, stopped.exception.__cause__)

        run_id = "run-id"
        correlation = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.assertEqual(run_id, _correlation_id(None, run_id))
        self.assertEqual(correlation, _correlation_id(correlation, run_id))
        for invalid in (
            "not-a-uuid",
            correlation.upper(),
            123,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(UsageConfigError) as stopped:
                    _correlation_id(invalid, run_id)
                self.assertEqual("correlationIdInvalid", stopped.exception.code)

    def test_coverage_report_and_project_measurement_preserve_exact_boundaries(self):
        from sentinel_py.quality import (
            DependencyFailure,
            _coverage_report,
            _measure_project,
        )

        parsed_report = object()
        with tempfile.TemporaryDirectory(prefix="sentinel-quality-input-") as directory:
            root = Path(directory)
            coverage_path = root / "coverage.json"
            coverage_path.write_bytes(b"coverage-bytes")
            with patch(
                "sentinel_py.quality.load_coverage_json",
                return_value=parsed_report,
            ) as load_coverage:
                self.assertIs(parsed_report, _coverage_report(coverage_path))
            load_coverage.assert_called_once_with(b"coverage-bytes")

            first = root / "first.py"
            second = root / "second.py"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            project = SimpleNamespace(
                project_root=root,
                production_sources=(
                    SimpleNamespace(path=first, module_relative_path="src/first.py"),
                    SimpleNamespace(path=second, module_relative_path="src/second.py"),
                ),
            )
            expected_metrics = (object(), object())
            with patch(
                "sentinel_py.quality._measure_sources",
                return_value=expected_metrics,
            ) as measure_sources:
                self.assertEqual(expected_metrics, _measure_project(project, parsed_report))
            measure_sources.assert_called_once_with(
                {"src/first.py": b"first", "src/second.py": b"second"},
                parsed_report,
                DEFAULT_GATE,
            )

            with self.assertRaises(DependencyFailure) as stopped:
                _coverage_report(root / "missing-coverage.json")
            self.assertEqual("coverageReportUnavailable", stopped.exception.code)

            missing_project = SimpleNamespace(
                production_sources=(
                    SimpleNamespace(
                        path=root / "missing-source.py",
                        module_relative_path="src/missing.py",
                    ),
                )
            )
            with self.assertRaises(DependencyFailure) as stopped:
                _measure_project(missing_project, parsed_report)
            self.assertEqual("productionSourceUnavailable", stopped.exception.code)

    def test_version_result_preserves_the_exact_public_contract(self):
        from sentinel_py.quality import version_result

        project = SimpleNamespace(
            module=SimpleNamespace(module_id="api"),
            production_sources=(object(), object()),
        )
        backend = SimpleNamespace(
            module_tree_sha256="module-tree",
            name="mutmut",
            operator_inventory_sha256="operator-inventory",
            version="3.7.0",
        )
        with (
            patch("sentinel_py.quality.verify_backend_lock", return_value=backend) as verify,
            patch(
                "sentinel_py.quality._required_version",
                return_value="7.16.0",
            ) as required_version,
        ):
            result = version_result(project)

        self.assertEqual(
            {
                "backend": {
                    "moduleTreeSha256": "module-tree",
                    "name": "mutmut",
                    "operatorInventorySha256": "operator-inventory",
                    "version": "3.7.0",
                },
                "coverage": {"name": "coverage.py", "version": "7.16.0"},
                "language": "python",
                "module": "api",
                "passed": True,
                "productionFiles": 2,
                "schemaVersion": "sentinel-version-v1",
            },
            result,
        )
        verify.assert_called_once_with()
        required_version.assert_called_once_with("coverage", "7.16.0")

    def test_callable_record_and_metric_status_preserve_exact_public_values(self):
        from sentinel_py.quality import _callable_record, _metric_status

        definition = SimpleNamespace(
            callable_id="python:v1:callable",
            kind="function",
            module_relative_path="src/sample.py",
            qualified_name="sample",
            declaration_line=1,
            source_range=SimpleNamespace(start_byte=11, end_byte=29),
        )
        crap = SimpleNamespace(
            complexity=5,
            covered=2,
            total=4,
            numerator=65,
            denominator=8,
            decimal="8.125",
            passed=False,
            unknown_reason=None,
        )
        metric = SimpleNamespace(callable=definition, crap=crap)

        self.assertEqual(
            {
                "callableId": "python:v1:callable",
                "coverageBasis": "executable-line",
                "coverageFraction": "0.5",
                "coveredUnits": 2,
                "crapDenominator": "8",
                "crapNumerator": "65",
                "crapRaw": "8.125",
                "cyclomaticComplexity": 5,
                "kind": "function",
                "moduleRelativePath": "src/sample.py",
                "qualifiedName": "sample",
                "line": 1,
                "sourceRange": {"endByte": 29, "startByte": 11},
                "status": "crapThresholdExceeded",
                "totalUnits": 4,
            },
            _callable_record(metric),
        )
        self.assertEqual("crapThresholdExceeded", _metric_status(metric))
        self.assertEqual(
            "coverageUnknown",
            _metric_status(
                SimpleNamespace(
                    crap=SimpleNamespace(
                        unknown_reason="coverageFileMissing",
                        passed=None,
                    )
                )
            ),
        )
        self.assertEqual(
            "passed",
            _metric_status(
                SimpleNamespace(crap=SimpleNamespace(unknown_reason=None, passed=True))
            ),
        )

    def test_crap_findings_preserve_exact_failed_and_empty_identities(self):
        from sentinel_py.evidence import FindingIdentity
        from sentinel_py.quality import _finding_identities

        project = self._project()
        passing = SimpleNamespace(
            callable=SimpleNamespace(
                callable_id="passing-id",
                module_relative_path="src/pass.py",
            ),
            crap=SimpleNamespace(passed=True, unknown_reason=None),
        )
        exceeded = SimpleNamespace(
            callable=SimpleNamespace(
                callable_id="exceeded-id",
                module_relative_path="src/exceeded.py",
            ),
            crap=SimpleNamespace(passed=False, unknown_reason=None),
        )
        unknown = SimpleNamespace(
            callable=SimpleNamespace(
                callable_id="unknown-id",
                module_relative_path="src/unknown.py",
            ),
            crap=SimpleNamespace(passed=None, unknown_reason="coverageFileMissing"),
        )

        self.assertEqual(
            (
                FindingIdentity(
                    category="crap",
                    reason="crapThresholdExceeded",
                    language="python",
                    module="api",
                    module_relative_path="src/exceeded.py",
                    subject_id="exceeded-id",
                ),
                FindingIdentity(
                    category="crap",
                    reason="coverageFileMissing",
                    language="python",
                    module="api",
                    module_relative_path="src/unknown.py",
                    subject_id="unknown-id",
                ),
            ),
            _finding_identities(
                project,
                (passing, exceeded, unknown),
                "crapThresholdExceeded",
            ),
        )
        self.assertEqual(
            (
                FindingIdentity(
                    category="crap",
                    reason="emptyCallableInventory",
                    language="python",
                    module="api",
                    module_relative_path="",
                    subject_id="api",
                ),
            ),
            _finding_identities(project, (), "emptyCallableInventory"),
        )
        self.assertEqual((), _finding_identities(project, (), "passed"))

    def test_mutation_summary_preserves_every_public_result_field(self):
        from sentinel_py.quality import _mutation_summary

        gate = SimpleNamespace(
            counts={"killed": 3, "survived": 1},
            in_scope=4,
            kill_rate_denominator=4,
            kill_rate_numerator=3,
            kill_rate_percent="75.000000",
            killed=3,
            passed=False,
            reason="nonKilledMutant",
        )

        self.assertEqual(
            {
                "counts": {"killed": 3, "survived": 1},
                "inScope": 4,
                "killRateDenominator": "4",
                "mutationMin": "90",
                "killRateNumerator": "3",
                "killRatePercent": "75.000000",
                "killed": 3,
                "pass": False,
                "reason": "nonKilledMutant",
            },
            _mutation_summary(gate),
        )

    def test_mutation_findings_preserve_exact_non_killed_and_zero_identities(self):
        from sentinel_py.evidence import FindingIdentity
        from sentinel_py.mutation import MutantRecord
        from sentinel_py.quality import _mutation_findings

        project = self._project()
        records = (
            MutantRecord("candidate-killed", "killed"),
            MutantRecord("candidate-survived", "survived"),
            MutantRecord("candidate-timeout", "timedOut"),
        )

        self.assertEqual(
            (
                FindingIdentity(
                    category="mutation",
                    reason="survived",
                    language="python",
                    module="api",
                    module_relative_path="",
                    subject_id="candidate-survived",
                ),
                FindingIdentity(
                    category="mutation",
                    reason="timedOut",
                    language="python",
                    module="api",
                    module_relative_path="",
                    subject_id="candidate-timeout",
                ),
            ),
            _mutation_findings(project, records, "nonKilledMutant"),
        )
        self.assertEqual(
            (
                FindingIdentity(
                    category="mutation",
                    reason="zeroMutants",
                    language="python",
                    module="api",
                    module_relative_path="",
                    subject_id="api",
                ),
            ),
            _mutation_findings(project, (), "zeroMutants"),
        )
        self.assertEqual((), _mutation_findings(project, (), "passed"))

    def test_strict_mutation_commits_killed_survived_and_zero_mutant_results(self):
        from sentinel_py.mutation import MutantRecord
        from sentinel_py.quality import run_strict_mutation
        from sentinel_py.runner.mutation_backend import MutationExecution

        cases = (
            ((MutantRecord("candidate", "killed"),), 0, "passed", "passed"),
            (
                (MutantRecord("candidate", "survived"),),
                2,
                "qualityFailed",
                "nonKilledMutant",
            ),
            ((), 2, "qualityFailed", "zeroMutants"),
        )
        for records, expected_exit, expected_status, expected_reason in cases:
            candidates = tuple(record.candidate_id for record in records)
            execution = MutationExecution(candidates, records)
            with (
                self.subTest(reason=expected_reason),
                patch("sentinel_py.quality.run_mutmut", return_value=execution),
                patch("sentinel_py.quality.commit_mutation_evidence") as commit,
            ):
                exit_code, result = run_strict_mutation(self._project(), None)

            self.assertEqual(expected_exit, exit_code)
            self.assertEqual(expected_status, result["run"]["terminalStatus"])
            self.assertEqual(expected_reason, result["mutation"]["reason"])
            self.assertEqual(len(records), result["mutation"]["inScope"])
            self.assertEqual(
                sum(record.status == "killed" for record in records),
                result["mutation"]["killed"],
            )
            committed_summary = commit.call_args.args[2]
            self.assertEqual(result["mutation"], committed_summary)
            findings = commit.call_args.args[3]
            if expected_reason == "passed":
                self.assertEqual((), findings)
            else:
                self.assertEqual(expected_reason if not records else "survived", findings[0].reason)

    def test_strict_mutation_binds_every_result_and_evidence_boundary(self):
        from sentinel_py.quality import run_strict_mutation
        from sentinel_py.runner.mutation_backend import MutationExecution

        project = self._project()
        correlation_id = "requested-correlation"
        candidates = ("candidate-a", "candidate-b")
        records = (object(), object())
        execution = MutationExecution(candidates, records)
        gate = SimpleNamespace(passed=True, reason="passed")
        run = {"terminalStatus": "passed"}
        summary = {"component": "mutation"}
        redacted = {"redacted": "mutation"}
        findings = (object(), object())

        with (
            patch("sentinel_py.quality.uuid.uuid4", return_value="run-uuid") as uuid4,
            patch(
                "sentinel_py.quality._correlation_id",
                return_value="approved-correlation",
            ) as correlation,
            patch("sentinel_py.quality.run_mutmut", return_value=execution) as mutation_run,
            patch(
                "sentinel_py.quality.evaluate_mutation_gate",
                return_value=gate,
            ) as evaluate_gate,
            patch("sentinel_py.quality._run_record", return_value=run) as run_record,
            patch(
                "sentinel_py.quality._mutation_summary",
                return_value=summary,
            ) as summarize,
            patch(
                "sentinel_py.quality._mutation_findings",
                return_value=findings,
            ) as find,
            patch(
                "sentinel_py.quality._redacted_mutation_summary",
                return_value=redacted,
            ) as redact,
            patch("sentinel_py.quality.commit_mutation_evidence") as commit,
        ):
            exit_code, result = run_strict_mutation(project, correlation_id)

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                "mutation": summary,
                "run": run,
                "schemaVersion": "sentinel-quality-result-v1",
            },
            result,
        )
        uuid4.assert_called_once_with()
        correlation.assert_called_once_with(correlation_id, "run-uuid")
        mutation_run.assert_called_once_with(project)
        evaluate_gate.assert_called_once_with(candidates, records, mutation_min=DEFAULT_GATE.mutation_min)
        run_record.assert_called_once_with(
            "mutation",
            "strict",
            "run-uuid",
            "approved-correlation",
            "passed",
        )
        summarize.assert_called_once_with(gate, "90")
        find.assert_called_once_with(project, records, "passed")
        redact.assert_called_once_with(summary)
        commit.assert_called_once_with(project.project_root, run, redacted, findings)

    def test_strict_check_commits_both_components_once_for_pass_and_failure(self):
        from sentinel_py.quality import run_strict_check
        from sentinel_py.runner.mutation_backend import MutationExecution

        project = self._project()
        correlation_id = "requested-correlation"
        coverage_sources = {"src/subject.py": b"value = 1\n"}
        coverage_execution = SimpleNamespace(
            report=b"coverage-report",
            sources=coverage_sources,
        )
        parsed_report = object()
        metrics = (object(), object())
        candidates = ("candidate-a", "candidate-b")
        records = (object(), object())
        mutation_execution = MutationExecution(candidates, records)
        crap_summary = {"component": "crap"}
        mutation_summary = {"component": "mutation"}
        crap_findings = (object(),)
        mutation_findings = (object(), object())
        redacted_crap = {"redacted": "crap"}
        redacted_mutation = {"redacted": "mutation"}

        cases = (
            (True, True, 0, "passed"),
            (False, True, 2, "qualityFailed"),
            (True, False, 2, "qualityFailed"),
            (False, False, 2, "qualityFailed"),
        )
        for crap_passed, mutation_passed, expected_exit, expected_status in cases:
            crap_gate = SimpleNamespace(
                metrics=metrics,
                passed=crap_passed,
                reason="crap-reason",
            )
            mutation_gate = SimpleNamespace(
                passed=mutation_passed,
                reason="mutation-reason",
            )
            run = {"terminalStatus": expected_status}
            with (
                self.subTest(crap=crap_passed, mutation=mutation_passed),
                patch("sentinel_py.quality.uuid.uuid4", return_value="run-uuid") as uuid4,
                patch(
                    "sentinel_py.quality._correlation_id",
                    return_value="approved-correlation",
                ) as correlation,
                patch(
                    "sentinel_py.quality.run_fresh_coverage",
                    return_value=coverage_execution,
                ) as fresh_coverage,
                patch(
                    "sentinel_py.quality.load_coverage_json",
                    return_value=parsed_report,
                ) as load_coverage,
                patch(
                    "sentinel_py.quality._measure_sources",
                    return_value=metrics,
                ) as measure_sources,
                patch(
                    "sentinel_py.quality.evaluate_crap_gate",
                    return_value=crap_gate,
                ) as crap_gate_evaluation,
                patch(
                    "sentinel_py.quality.run_mutmut",
                    return_value=mutation_execution,
                ) as mutation_run,
                patch(
                    "sentinel_py.quality.evaluate_mutation_gate",
                    return_value=mutation_gate,
                ) as mutation_gate_evaluation,
                patch("sentinel_py.quality._run_record", return_value=run) as run_record,
                patch(
                    "sentinel_py.quality._crap_summary",
                    return_value=crap_summary,
                ) as summarize_crap,
                patch(
                    "sentinel_py.quality._mutation_summary",
                    return_value=mutation_summary,
                ) as summarize_mutation,
                patch(
                    "sentinel_py.quality._finding_identities",
                    return_value=crap_findings,
                ) as find_crap,
                patch(
                    "sentinel_py.quality._mutation_findings",
                    return_value=mutation_findings,
                ) as find_mutation,
                patch(
                    "sentinel_py.quality._redacted_summary",
                    return_value=redacted_crap,
                ) as redact_crap,
                patch(
                    "sentinel_py.quality._redacted_mutation_summary",
                    return_value=redacted_mutation,
                ) as redact_mutation,
                patch("sentinel_py.quality.commit_check_evidence") as commit,
                patch("sentinel_py.quality._protected_inventory", return_value=()),
            ):
                exit_code, result = run_strict_check(project, correlation_id, execution_mode="sequential")

            self.assertEqual(expected_exit, exit_code)
            self.assertEqual(
                {
                    "crap": crap_summary,
                    "mutation": mutation_summary,
                    "run": run,
                    "schemaVersion": "sentinel-quality-result-v1",
                },
                result,
            )
            uuid4.assert_called_once_with()
            correlation.assert_called_once_with(correlation_id, "run-uuid")
            fresh_coverage.assert_called_once_with(project)
            load_coverage.assert_called_once_with(coverage_execution.report)
            measure_sources.assert_called_once_with(coverage_sources, parsed_report, DEFAULT_GATE)
            crap_gate_evaluation.assert_called_once_with(metrics, DEFAULT_GATE.crap_max)
            mutation_run.assert_called_once_with(project)
            mutation_gate_evaluation.assert_called_once_with(candidates, records, mutation_min=DEFAULT_GATE.mutation_min)
            run_record.assert_called_once_with(
                "check",
                "strict",
                "run-uuid",
                "approved-correlation",
                expected_status,
            )
            summarize_crap.assert_called_once_with(metrics, crap_passed, "crap-reason", "8")
            summarize_mutation.assert_called_once_with(mutation_gate, "90")
            find_crap.assert_called_once_with(project, metrics, "crap-reason")
            find_mutation.assert_called_once_with(project, records, "mutation-reason")
            redact_crap.assert_called_once_with(crap_summary)
            redact_mutation.assert_called_once_with(mutation_summary)
            commit.assert_called_once_with(
                project.project_root,
                run,
                redacted_crap,
                redacted_mutation,
                (*crap_findings, *mutation_findings),
            )

    @staticmethod
    def _project() -> SimpleNamespace:
        return SimpleNamespace(
            project_root=Path("/project"),
            module=SimpleNamespace(module_id="api"),
        )


if __name__ == "__main__":
    unittest.main()
