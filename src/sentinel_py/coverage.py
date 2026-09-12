"""Strict coverage.py JSON parsing and callable line attribution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .crap import (
    MAX_SAFE_INTEGER,
    AnalysisError,
    CallableDefinition,
    CrapResult,
    analyze_source,
    calculate_crap,
    normalize_module_path,
)


COVERAGE_JSON_FORMAT = 3
COVERAGE_TOOL_VERSION = "7.16.0"


class CoverageFormatError(ValueError):
    """Raised when a coverage report cannot be interpreted without guessing."""


class MetricOrderingError(ValueError):
    """Raised when callable metrics cannot form the required total order."""


@dataclass(frozen=True)
class FileCoverage:
    executed_lines: Tuple[int, ...]
    missing_lines: Tuple[int, ...]
    excluded_lines: Tuple[int, ...]


@dataclass(frozen=True)
class CoverageMetadata:
    format: int
    version: str
    timestamp: str
    branch_coverage: bool
    show_contexts: bool


@dataclass(frozen=True)
class CoverageReport:
    metadata: CoverageMetadata
    files: Mapping[str, FileCoverage]
    report_digest: str


@dataclass(frozen=True)
class CallableMetric:
    callable: CallableDefinition
    crap: CrapResult


@dataclass(frozen=True)
class CrapGateResult:
    passed: bool
    reason: str
    metrics: Tuple[CallableMetric, ...]


def load_coverage_json(payload: bytes) -> CoverageReport:
    document = _load_json_document(_decode_json_payload(payload))
    metadata = _coverage_metadata(document)
    files_data = _coverage_files(document)
    files: Dict[str, FileCoverage] = {}
    for path, file_data in files_data.items():
        normalized_path, coverage = _parse_file_coverage(path, file_data)
        files[normalized_path] = coverage
    report_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return CoverageReport(metadata, MappingProxyType(files), report_digest)


def _coverage_metadata(document: dict) -> CoverageMetadata:
    metadata = document.get("meta")
    if not isinstance(metadata, dict):
        raise CoverageFormatError("coverage report meta must be a JSON object")
    expected = {
        "format": COVERAGE_JSON_FORMAT,
        "version": COVERAGE_TOOL_VERSION,
    }
    for name, value in expected.items():
        if metadata.get(name) != value:
            raise CoverageFormatError(f"coverage report meta {name} must be {value!r}")
    timestamp = _coverage_timestamp(metadata)
    branch_coverage = _metadata_boolean(metadata, "branch_coverage")
    show_contexts = _metadata_boolean(metadata, "show_contexts")
    return CoverageMetadata(
        format=COVERAGE_JSON_FORMAT,
        version=COVERAGE_TOOL_VERSION,
        timestamp=timestamp,
        branch_coverage=branch_coverage,
        show_contexts=show_contexts,
    )


def _coverage_timestamp(metadata: dict) -> str:
    value = metadata.get("timestamp")
    if not isinstance(value, str) or not value:
        raise CoverageFormatError("coverage report meta timestamp must be non-empty")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CoverageFormatError("coverage report meta timestamp is invalid") from error
    if parsed.tzinfo is not None or parsed.isoformat() != value:
        raise CoverageFormatError("coverage report meta timestamp is not canonical local time")
    return value


def _metadata_boolean(metadata: dict, name: str) -> bool:
    value = metadata.get(name)
    if not isinstance(value, bool):
        raise CoverageFormatError(f"coverage report meta {name} must be boolean")
    return value


def _decode_json_payload(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise CoverageFormatError("coverage JSON must be UTF-8 bytes")
    try:
        return payload.decode()
    except UnicodeDecodeError as error:
        raise CoverageFormatError("coverage JSON is not valid UTF-8") from error


def _load_json_document(text: str) -> dict:
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except CoverageFormatError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise CoverageFormatError("coverage report is not valid JSON") from error

    if not isinstance(document, dict):
        raise CoverageFormatError("coverage report must be a JSON object")
    return document


def _reject_json_constant(value: str) -> None:
    raise CoverageFormatError(f"non-standard JSON number is not allowed: {value}")


def _coverage_files(document: dict) -> dict:
    files_data = document.get("files")
    if not isinstance(files_data, dict):
        raise CoverageFormatError("coverage report files must be a JSON object")
    return files_data


def _parse_file_coverage(path: str, file_data: object) -> Tuple[str, FileCoverage]:
    try:
        normalized_path = normalize_module_path(path)
    except AnalysisError as error:
        raise CoverageFormatError(f"invalid coverage file path: {path!r}") from error
    if not isinstance(file_data, dict):
        raise CoverageFormatError(f"coverage file entry must be an object: {path}")
    executed = _line_array(file_data, "executed_lines", path)
    missing = _line_array(file_data, "missing_lines", path)
    excluded = _line_array(file_data, "excluded_lines", path)
    _require_disjoint_line_sets(path, executed, missing, excluded)
    if excluded:
        raise CoverageFormatError(f"excluded production lines are not allowed for {path}")
    return normalized_path, FileCoverage(executed, missing, excluded)


def _require_disjoint_line_sets(
    path: str,
    executed: Tuple[int, ...],
    missing: Tuple[int, ...],
    excluded: Tuple[int, ...],
) -> None:
    labels = (
        ("executed_lines", set(executed)),
        ("missing_lines", set(missing)),
        ("excluded_lines", set(excluded)),
    )
    for index, (left_name, left) in enumerate(labels):
        for right_name, right in labels[index + 1:]:
            overlap = left.intersection(right)
            if overlap:
                raise CoverageFormatError(
                    f"{left_name} and {right_name} overlap for {path}: {min(overlap)}"
                )


def measure_source(
    source: bytes,
    module_relative_path: str,
    report: CoverageReport,
) -> Tuple[CallableMetric, ...]:
    if not isinstance(report, CoverageReport):
        raise TypeError("report must be a CoverageReport")
    definitions = analyze_source(source, module_relative_path)
    file_coverage = report.files.get(module_relative_path)
    if file_coverage is not None:
        _require_lines_within_source(source, module_relative_path, file_coverage)
    metrics = []
    for definition in definitions:
        covered, total, unknown_reason = _coverage_counts(definition, file_coverage)
        metrics.append(
            CallableMetric(
                callable=definition,
                crap=calculate_crap(
                    complexity=definition.complexity,
                    covered=covered,
                    total=total,
                    unknown_reason=unknown_reason,
                ),
            )
        )
    return tuple(metrics)


def _require_lines_within_source(
    source: bytes,
    module_relative_path: str,
    file_coverage: FileCoverage,
) -> None:
    normalized = source.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    line_count = 0 if not normalized else normalized.count(b"\n") + int(
        not normalized.endswith(b"\n")
    )
    reported = (
        file_coverage.executed_lines
        + file_coverage.missing_lines
        + file_coverage.excluded_lines
    )
    if reported and max(reported) > line_count:
        raise CoverageFormatError(
            f"coverage line is outside current source for {module_relative_path}"
        )


def sort_callable_metrics(
    metrics: Iterable[CallableMetric],
) -> Tuple[CallableMetric, ...]:
    materialized = tuple(metrics)
    seen = set()
    for metric in materialized:
        _validate_metric(metric)
        identity = _validated_metric_identity(metric)
        if identity in seen:
            raise MetricOrderingError("identityAmbiguous")
        seen.add(identity)
    return tuple(sorted(materialized, key=_metric_sort_key))


def evaluate_crap_gate(metrics: Iterable[CallableMetric]) -> CrapGateResult:
    ordered = sort_callable_metrics(metrics)
    if not ordered:
        return CrapGateResult(False, "emptyCallableInventory", ordered)
    if any(metric.crap.unknown_reason is not None for metric in ordered):
        return CrapGateResult(False, "coverageUnknown", ordered)
    if any(metric.crap.passed is not True for metric in ordered):
        return CrapGateResult(False, "crapThresholdExceeded", ordered)
    return CrapGateResult(True, "passed", ordered)


def _validate_metric(metric: CallableMetric) -> None:
    if not isinstance(metric, CallableMetric):
        raise MetricOrderingError("metricTypeInvalid")
    if not isinstance(metric.callable, CallableDefinition):
        raise MetricOrderingError("callableTypeInvalid")
    if not isinstance(metric.crap, CrapResult):
        raise MetricOrderingError("crapTypeInvalid")
    try:
        expected = calculate_crap(
            complexity=metric.callable.complexity,
            covered=metric.crap.covered,
            total=metric.crap.total,
            unknown_reason=metric.crap.unknown_reason,
        )
    except (TypeError, ValueError) as error:
        raise MetricOrderingError("metricInvariantInvalid") from error
    if metric.crap != expected:
        raise MetricOrderingError("metricInvariantInvalid")


def _metric_sort_key(metric: CallableMetric) -> tuple:
    known = metric.crap.unknown_reason is None
    risk = (
        -Fraction(metric.crap.numerator, metric.crap.denominator)
        if known
        else Fraction()
    )
    return known, risk, _metric_identity(metric)


def _metric_identity(metric: CallableMetric) -> tuple:
    definition = metric.callable
    return (
        definition.module_relative_path.encode(),
        definition.source_range.start_byte,
        definition.callable_id.encode(),
    )


def _validated_metric_identity(metric: CallableMetric) -> tuple:
    definition = metric.callable
    try:
        normalize_module_path(definition.module_relative_path)
        _require_source_digest(definition.source_digest)
        identity = _metric_identity(metric)
        start = definition.source_range.start_byte
        end = definition.source_range.end_byte
    except (AnalysisError, AttributeError, TypeError, UnicodeEncodeError) as error:
        raise MetricOrderingError("identityInvalid") from error
    invalid_positions = any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in (start, end)
    )
    if invalid_positions or start < 0 or end <= start or end > MAX_SAFE_INTEGER:
        raise MetricOrderingError("sourceRangeInvalid")
    return identity


def _require_source_digest(value: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise MetricOrderingError("sourceDigestInvalid")
    hexadecimal = value[len("sha256:"):]
    invalid_character = any(
        character not in "0123456789abcdef" for character in hexadecimal
    )
    if len(hexadecimal) != 64 or invalid_character:
        raise MetricOrderingError("sourceDigestInvalid")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CoverageFormatError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _line_array(file_data: dict, name: str, path: str) -> Tuple[int, ...]:
    values = file_data.get(name)
    if not isinstance(values, list):
        raise CoverageFormatError(f"{name} must be an array for {path}")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        or value > MAX_SAFE_INTEGER
        for value in values
    ):
        raise CoverageFormatError(
            f"{name} must contain positive JSON-safe integer lines for {path}"
        )
    if len(values) != len(set(values)):
        raise CoverageFormatError(f"{name} must not contain duplicate lines for {path}")
    return tuple(sorted(values))


def _coverage_counts(
    definition: CallableDefinition,
    file_coverage: Optional[FileCoverage],
) -> Tuple[int, int, Optional[str]]:
    if file_coverage is None:
        return 0, 0, "file-entry-missing"
    if definition.declaration_line == definition.body_start_line:
        return 0, 0, "ambiguous-line-coverage"

    executed = set(file_coverage.executed_lines)
    owned_lines = _owned_lines(definition, executed.union(file_coverage.missing_lines))
    if not owned_lines:
        return 0, 0, "no-executable-lines"
    covered = len(owned_lines.intersection(executed))
    total = len(owned_lines)
    return covered, total, None


def _owned_lines(
    definition: CallableDefinition,
    executable: set,
) -> set:
    owned_lines = {
        line
        for line in executable
        if definition.body_start_line <= line <= definition.body_end_line
        and not any(start <= line <= end for start, end in definition.excluded_line_ranges)
    }
    return owned_lines
