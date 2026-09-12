"""Cryptographic joins for coverage reports, source bytes, and test selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Tuple

from .coverage import COVERAGE_JSON_FORMAT, COVERAGE_TOOL_VERSION, CoverageReport
from .crap import AnalysisError, normalize_module_path


_SCHEMA_VERSION = "sentinel-python-coverage-receipt-v1"
_TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "coverageTool",
    "coverageReportDigest",
    "testSelectionDigest",
    "sourceDigests",
}
_TOOL_FIELDS = {"name", "version", "format"}


class CoverageReceiptError(ValueError):
    """Raised when coverage artifacts do not form one exact run."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CoverageReceipt:
    coverage_report_digest: str
    test_selection_digest: str
    source_digests: Mapping[str, str]


@dataclass(frozen=True)
class VerifiedCoverage:
    coverage_report_digest: str
    test_selection_digest: str
    source_digests: Mapping[str, str]


def load_coverage_receipt(payload: bytes) -> CoverageReceipt:
    document = _load_document(payload)
    if set(document) != _TOP_LEVEL_FIELDS:
        raise CoverageReceiptError("receiptFieldsInvalid")
    if document.get("schemaVersion") != _SCHEMA_VERSION:
        raise CoverageReceiptError("receiptSchemaInvalid")
    _require_tool_metadata(document.get("coverageTool"))
    report_digest = _require_digest(
        document.get("coverageReportDigest"), "reportDigestInvalid"
    )
    selection_digest = _require_digest(
        document.get("testSelectionDigest"), "selectionDigestInvalid"
    )
    source_digests = _source_digests(document.get("sourceDigests"))
    return CoverageReceipt(report_digest, selection_digest, source_digests)


def verify_coverage_receipt(
    receipt: CoverageReceipt,
    report: CoverageReport,
    sources: Mapping[str, bytes],
    expected_selection_digest: str,
) -> VerifiedCoverage:
    if not isinstance(receipt, CoverageReceipt) or not isinstance(report, CoverageReport):
        raise CoverageReceiptError("receiptTypeInvalid")
    expected_selection = _require_digest(
        expected_selection_digest, "selectionDigestInvalid"
    )
    if receipt.coverage_report_digest != report.report_digest:
        raise CoverageReceiptError("reportDigestMismatch")
    if receipt.test_selection_digest != expected_selection:
        raise CoverageReceiptError("selectionDigestMismatch")
    _require_report_tool_metadata(report)
    actual_digests = _actual_source_digests(sources)
    if actual_digests != dict(receipt.source_digests):
        raise CoverageReceiptError("sourceDigestSetMismatch")
    if not set(actual_digests).issubset(report.files):
        raise CoverageReceiptError("coverageFileSetMismatch")
    return VerifiedCoverage(
        receipt.coverage_report_digest,
        receipt.test_selection_digest,
        MappingProxyType(actual_digests),
    )


def _load_document(payload: bytes) -> dict:
    if not isinstance(payload, bytes):
        raise CoverageReceiptError("receiptBytesRequired")
    try:
        text = payload.decode()
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except CoverageReceiptError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoverageReceiptError("receiptJsonInvalid") from error
    if not isinstance(document, dict):
        raise CoverageReceiptError("receiptObjectRequired")
    return document


def _unique_object(pairs: Tuple[Tuple[str, object], ...]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CoverageReceiptError("duplicateReceiptField")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CoverageReceiptError("receiptJsonInvalid")


def _require_tool_metadata(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _TOOL_FIELDS:
        raise CoverageReceiptError("coverageToolInvalid")
    if value.get("name") != "coverage.py":
        raise CoverageReceiptError("coverageToolInvalid")
    if value.get("version") != COVERAGE_TOOL_VERSION:
        raise CoverageReceiptError("coverageToolInvalid")
    file_format = value.get("format")
    if not isinstance(file_format, int) or isinstance(file_format, bool):
        raise CoverageReceiptError("coverageToolInvalid")
    if file_format != COVERAGE_JSON_FORMAT:
        raise CoverageReceiptError("coverageToolInvalid")


def _require_digest(value: object, error_code: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise CoverageReceiptError(error_code)
    hexadecimal = value[7:]
    if len(hexadecimal) != 64 or any(
        character not in "0123456789abcdef" for character in hexadecimal
    ):
        raise CoverageReceiptError(error_code)
    if hexadecimal == "0" * 64:
        raise CoverageReceiptError(error_code)
    return value


def _source_digests(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or not value:
        raise CoverageReceiptError("sourceDigestsInvalid")
    result = {}
    for path, digest in value.items():
        normalized_path = _normalize_path(path)
        result[normalized_path] = _require_digest(digest, "sourceDigestInvalid")
    return MappingProxyType(result)


def _normalize_path(path: object) -> str:
    try:
        return normalize_module_path(path)
    except (AnalysisError, TypeError) as error:
        raise CoverageReceiptError("sourcePathInvalid") from error


def _require_report_tool_metadata(report: CoverageReport) -> None:
    metadata = report.metadata
    if (
        metadata.version != COVERAGE_TOOL_VERSION
        or metadata.format != COVERAGE_JSON_FORMAT
    ):
        raise CoverageReceiptError("reportToolMismatch")


def _actual_source_digests(sources: Mapping[str, bytes]) -> dict[str, str]:
    if not isinstance(sources, Mapping) or not sources:
        raise CoverageReceiptError("sourceInventoryInvalid")
    result = {}
    for path, source in sources.items():
        normalized_path = _normalize_path(path)
        if not isinstance(source, bytes):
            raise CoverageReceiptError("sourceBytesRequired")
        result[normalized_path] = "sha256:" + hashlib.sha256(source).hexdigest()
    return result
