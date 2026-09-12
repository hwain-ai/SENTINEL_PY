"""Project-level doctor and native Python CRAP orchestration."""

from __future__ import annotations

import importlib.metadata
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Sequence

from .backend_lock import verify_backend_lock
from .config import LoadedProject, UsageConfigError
from .coverage import CallableMetric, evaluate_crap_gate, load_coverage_json, measure_source
from .evidence import (
    FindingIdentity,
    commit_check_evidence,
    commit_mutation_evidence,
    commit_quality_evidence,
)
from .evidence.store import _format_utc
from .mutation import MutantRecord, evaluate_mutation_gate
from .rendering import render_canonical_decimal
from .runner import run_fresh_coverage, run_mutmut


class DependencyFailure(RuntimeError):
    """A pinned runtime dependency or required report is unavailable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def doctor_result(project: LoadedProject) -> dict:
    """Return read-only validation of the pinned Python execution environment."""

    backend = verify_backend_lock()
    coverage_version = _required_version("coverage", "7.16.0")
    return {
        "backend": {
            "moduleTreeSha256": backend.module_tree_sha256,
            "name": backend.name,
            "operatorInventorySha256": backend.operator_inventory_sha256,
            "version": backend.version,
        },
        "coverage": {"name": "coverage.py", "version": coverage_version},
        "language": "python",
        "module": project.module.module_id,
        "passed": True,
        "productionFiles": len(project.production_sources),
        "schemaVersion": "sentinel-doctor-v1",
    }


def run_local_crap(project: LoadedProject, correlation_id: str | None) -> tuple[int, dict]:
    """Analyze a configured local coverage report and commit redacted evidence."""

    run_id = str(uuid.uuid4())
    correlation = _correlation_id(correlation_id, run_id)
    report = _coverage_report(project.module.coverage_report)
    metrics = _measure_project(project, report)
    return _complete_crap(project, metrics, "local", correlation, run_id)


def run_strict_crap(project: LoadedProject, correlation_id: str | None) -> tuple[int, dict]:
    """Generate fresh snapshot coverage and commit the native CRAP result."""

    run_id = str(uuid.uuid4())
    correlation = _correlation_id(correlation_id, run_id)
    execution = run_fresh_coverage(project)
    report = load_coverage_json(execution.report)
    metrics = _measure_sources(execution.sources, report)
    return _complete_crap(project, metrics, "strict", correlation, run_id)


def _complete_crap(
    project: LoadedProject,
    metrics: Sequence[CallableMetric],
    mode: str,
    correlation: str,
    run_id: str,
) -> tuple[int, dict]:
    gate = evaluate_crap_gate(metrics)
    summary = _crap_summary(gate.metrics, gate.passed, gate.reason)
    status = "passed" if gate.passed else "qualityFailed"
    run = _run_record("crap", mode, run_id, correlation, status)
    findings = _finding_identities(project, gate.metrics, gate.reason)
    result = {
        "crap": summary,
        "run": run,
        "schemaVersion": "sentinel-quality-result-v1",
    }
    commit_quality_evidence(
        project.project_root,
        run,
        _redacted_summary(summary),
        findings,
    )
    return (0 if gate.passed else 2), result


def run_strict_mutation(
    project: LoadedProject,
    correlation_id: str | None,
) -> tuple[int, dict]:
    """Execute the locked backend and apply SENTINEL's killed-only gate."""

    run_id = str(uuid.uuid4())
    correlation = _correlation_id(correlation_id, run_id)
    execution = run_mutmut(project)
    gate = evaluate_mutation_gate(execution.candidate_ids, execution.records)
    status = "passed" if gate.passed else "qualityFailed"
    run = _run_record("mutation", "strict", run_id, correlation, status)
    summary = _mutation_summary(gate)
    findings = _mutation_findings(project, execution.records, gate.reason)
    result = {
        "mutation": summary,
        "run": run,
        "schemaVersion": "sentinel-quality-result-v1",
    }
    commit_mutation_evidence(
        project.project_root,
        run,
        _redacted_mutation_summary(summary),
        findings,
    )
    return (0 if gate.passed else 2), result


def run_strict_check(project: LoadedProject, correlation_id: str | None) -> tuple[int, dict]:
    """Run fresh CRAP and mutation gates and commit exactly one combined run."""

    run_id = str(uuid.uuid4())
    correlation = _correlation_id(correlation_id, run_id)
    coverage_execution = run_fresh_coverage(project)
    report = load_coverage_json(coverage_execution.report)
    metrics = _measure_sources(coverage_execution.sources, report)
    crap_gate = evaluate_crap_gate(metrics)
    mutation_execution = run_mutmut(project)
    mutation_gate = evaluate_mutation_gate(
        mutation_execution.candidate_ids,
        mutation_execution.records,
    )
    passed = crap_gate.passed and mutation_gate.passed
    status = "passed" if passed else "qualityFailed"
    run = _run_record("check", "strict", run_id, correlation, status)
    crap = _crap_summary(crap_gate.metrics, crap_gate.passed, crap_gate.reason)
    mutation = _mutation_summary(mutation_gate)
    findings = (
        *_finding_identities(project, crap_gate.metrics, crap_gate.reason),
        *_mutation_findings(project, mutation_execution.records, mutation_gate.reason),
    )
    result = {
        "crap": crap,
        "mutation": mutation,
        "run": run,
        "schemaVersion": "sentinel-quality-result-v1",
    }
    commit_check_evidence(
        project.project_root,
        run,
        _redacted_summary(crap),
        _redacted_mutation_summary(mutation),
        findings,
    )
    return (0 if passed else 2), result


def _required_version(distribution: str, expected: str) -> str:
    try:
        installed = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as error:
        raise DependencyFailure(distribution + "NotInstalled") from error
    if installed != expected:
        raise DependencyFailure(distribution + "VersionMismatch")
    return installed


def _correlation_id(value: str | None, run_id: str) -> str:
    if value is None:
        return run_id
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise UsageConfigError("correlationIdInvalid") from error
    if str(parsed) != value:
        raise UsageConfigError("correlationIdInvalid")
    return value


def _coverage_report(path: Path):
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise DependencyFailure("coverageReportUnavailable") from error
    return load_coverage_json(payload)


def _measure_project(project: LoadedProject, report) -> tuple[CallableMetric, ...]:
    sources = {}
    for source in project.production_sources:
        try:
            payload = source.path.read_bytes()
        except OSError as error:
            raise DependencyFailure("productionSourceUnavailable") from error
        sources[source.module_relative_path] = payload
    return _measure_sources(sources, report)


def _measure_sources(sources, report) -> tuple[CallableMetric, ...]:
    metrics = []
    for path, payload in sources.items():
        metrics.extend(measure_source(payload, path, report))
    return tuple(metrics)


def _crap_summary(
    metrics: Sequence[CallableMetric],
    passed: bool,
    reason: str,
) -> dict:
    known = tuple(metric for metric in metrics if metric.crap.numerator is not None)
    maximum = max(known, key=_crap_fraction) if known else None
    numerator, denominator = _maximum_fraction(maximum)
    return {
        "callables": [_callable_record(metric) for metric in metrics],
        "maxDenominator": denominator,
        "maxNumerator": numerator,
        "pass": passed,
        "reason": reason,
        "unknownCount": sum(metric.crap.unknown_reason is not None for metric in metrics),
    }


def _crap_fraction(metric: CallableMetric) -> Fraction:
    crap = metric.crap
    return Fraction(crap.numerator, crap.denominator)


def _maximum_fraction(metric: CallableMetric | None) -> tuple[str | None, str | None]:
    if metric is None:
        return None, None
    return str(metric.crap.numerator), str(metric.crap.denominator)


def _callable_record(metric: CallableMetric) -> dict:
    definition = metric.callable
    crap = metric.crap
    return {
        "callableId": definition.callable_id,
        "coverageBasis": "executable-line",
        "coverageFraction": _coverage_fraction(crap.covered, crap.total),
        "coveredUnits": crap.covered,
        "crapDenominator": _optional_decimal(crap.denominator),
        "crapNumerator": _optional_decimal(crap.numerator),
        "crapRaw": crap.decimal,
        "cyclomaticComplexity": crap.complexity,
        "kind": definition.kind,
        "moduleRelativePath": definition.module_relative_path,
        "qualifiedName": definition.qualified_name,
        "sourceRange": {
            "endByte": definition.source_range.end_byte,
            "startByte": definition.source_range.start_byte,
        },
        "status": _metric_status(metric),
        "totalUnits": crap.total,
    }


def _coverage_fraction(covered: int, total: int) -> str | None:
    if total == 0:
        return None
    return render_canonical_decimal(covered, total)


def _optional_decimal(value: int | None) -> str | None:
    return None if value is None else str(value)


def _metric_status(metric: CallableMetric) -> str:
    if metric.crap.unknown_reason is not None:
        return "coverageUnknown"
    if metric.crap.passed is not True:
        return "crapThresholdExceeded"
    return "passed"


def _run_record(
    command: str,
    mode: str,
    run_id: str,
    correlation_id: str,
    terminal_status: str,
) -> dict:
    completed_at = _format_utc(datetime.now(timezone.utc))
    return {
        "command": command,
        "completedAt": completed_at,
        "correlationId": correlation_id,
        "mode": mode,
        "runId": run_id,
        "terminalStatus": terminal_status,
    }


def _finding_identities(
    project: LoadedProject,
    metrics: Sequence[CallableMetric],
    gate_reason: str,
) -> tuple[FindingIdentity, ...]:
    findings = tuple(
        _metric_finding(project, metric)
        for metric in metrics
        if metric.crap.passed is not True
    )
    if findings or gate_reason != "emptyCallableInventory":
        return findings
    return (
        FindingIdentity(
            category="crap",
            reason=gate_reason,
            language="python",
            module=project.module.module_id,
            module_relative_path="",
            subject_id=project.module.module_id,
        ),
    )


def _metric_finding(project: LoadedProject, metric: CallableMetric) -> FindingIdentity:
    reason = metric.crap.unknown_reason or "crapThresholdExceeded"
    reason = {
        "file-entry-missing": "coverageFileMissing",
        "ambiguous-line-coverage": "coverageLineAmbiguous",
        "no-executable-lines": "coverageNoExecutableLines",
    }.get(reason, reason)
    return FindingIdentity(
        category="crap",
        reason=reason,
        language="python",
        module=project.module.module_id,
        module_relative_path=metric.callable.module_relative_path,
        subject_id=metric.callable.callable_id,
    )


def _redacted_summary(summary: dict) -> dict:
    return {
        "callableCount": len(summary["callables"]),
        "maxDenominator": summary["maxDenominator"],
        "maxNumerator": summary["maxNumerator"],
        "pass": summary["pass"],
        "reason": summary["reason"],
        "unknownCount": summary["unknownCount"],
    }


def _mutation_summary(gate) -> dict:
    return {
        "counts": dict(gate.counts),
        "inScope": gate.in_scope,
        "killRateDenominator": _optional_decimal(gate.kill_rate_denominator),
        "killRateNumerator": _optional_decimal(gate.kill_rate_numerator),
        "killRatePercent": gate.kill_rate_percent,
        "killed": gate.killed,
        "pass": gate.passed,
        "reason": gate.reason,
    }


def _mutation_findings(
    project: LoadedProject,
    records: Sequence[MutantRecord],
    gate_reason: str,
) -> tuple[FindingIdentity, ...]:
    findings = tuple(
        FindingIdentity(
            category="mutation",
            reason=record.status,
            language="python",
            module=project.module.module_id,
            module_relative_path="",
            subject_id=record.candidate_id,
        )
        for record in records
        if record.status != "killed"
    )
    if findings or gate_reason != "zeroMutants":
        return findings
    return (
        FindingIdentity(
            category="mutation",
            reason=gate_reason,
            language="python",
            module=project.module.module_id,
            module_relative_path="",
            subject_id=project.module.module_id,
        ),
    )


def _redacted_mutation_summary(summary: dict) -> dict:
    return dict(summary)
