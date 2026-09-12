import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.coverage import load_coverage_json  # noqa: E402
from sentinel_py.provenance import (  # noqa: E402
    CoverageReceiptError,
    load_coverage_receipt,
    verify_coverage_receipt,
)


SELECTION_DIGEST = "sha256:" + "1" * 64


def coverage_payload() -> bytes:
    return json.dumps(
        {
            "meta": {
                "format": 3,
                "version": "7.16.0",
                "timestamp": "2026-09-03T00:00:00",
                "branch_coverage": False,
                "show_contexts": False,
            },
            "files": {
                "src/alpha.py": {
                    "executed_lines": [2],
                    "missing_lines": [],
                    "excluded_lines": [],
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def receipt_payload(report: bytes, source: bytes, **overrides) -> bytes:
    document = {
        "schemaVersion": "sentinel-python-coverage-receipt-v1",
        "coverageTool": {"name": "coverage.py", "version": "7.16.0", "format": 3},
        "coverageReportDigest": "sha256:" + hashlib.sha256(report).hexdigest(),
        "testSelectionDigest": SELECTION_DIGEST,
        "sourceDigests": {
            "src/alpha.py": "sha256:" + hashlib.sha256(source).hexdigest()
        },
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


class CoverageReceiptTests(unittest.TestCase):
    def setUp(self):
        self.source = b"def alpha():\n    return 1\n"
        self.report_payload = coverage_payload()
        self.report = load_coverage_json(self.report_payload)

    def assert_error_code(self, expected, operation):
        with self.assertRaises(CoverageReceiptError) as raised:
            operation()
        self.assertEqual(expected, raised.exception.code)
        self.assertEqual((expected,), raised.exception.args)

    def test_exact_receipt_binds_report_source_and_test_selection(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )

        verified = verify_coverage_receipt(
            receipt,
            self.report,
            {"src/alpha.py": self.source},
            SELECTION_DIGEST,
        )

        self.assertEqual(self.report.report_digest, verified.coverage_report_digest)
        self.assertEqual(SELECTION_DIGEST, verified.test_selection_digest)
        self.assertEqual(("src/alpha.py",), tuple(verified.source_digests))

    def test_changed_report_is_rejected(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )
        changed_report = load_coverage_json(
            self.report_payload.replace(b'"executed_lines":[2]', b'"executed_lines":[]')
        )

        self.assert_error_code(
            "reportDigestMismatch",
            lambda: verify_coverage_receipt(
                receipt,
                changed_report,
                {"src/alpha.py": self.source},
                SELECTION_DIGEST,
            ),
        )

    def test_changed_or_incomplete_source_inventory_is_rejected(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )
        cases = (
            (
                "sourceDigestSetMismatch",
                {"src/alpha.py": b"def alpha():\n    return 2\n"},
            ),
            ("sourceInventoryInvalid", {}),
            (
                "sourceDigestSetMismatch",
                {
                    "src/alpha.py": self.source,
                    "src/extra.py": b"def extra():\n    return 1\n",
                },
            ),
        )

        for expected, sources in cases:
            with self.subTest(expected=expected, sources=tuple(sources)):
                self.assert_error_code(
                    expected,
                    lambda sources=sources: verify_coverage_receipt(
                        receipt,
                        self.report,
                        sources,
                        SELECTION_DIGEST,
                    ),
                )

    def test_changed_test_selection_is_rejected(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )

        self.assert_error_code(
            "selectionDigestMismatch",
            lambda: verify_coverage_receipt(
                receipt,
                self.report,
                {"src/alpha.py": self.source},
                "sha256:" + "2" * 64,
            ),
        )

    def test_unknown_extra_or_duplicate_receipt_fields_are_rejected(self):
        valid = json.loads(receipt_payload(self.report_payload, self.source))
        extra = dict(valid, unexpected=True)
        duplicate = receipt_payload(self.report_payload, self.source).replace(
            b'{"schemaVersion"',
            b'{"schemaVersion":"sentinel-python-coverage-receipt-v1","schemaVersion"',
            1,
        )

        for expected, payload in (
            ("receiptFieldsInvalid", json.dumps(extra).encode("utf-8")),
            ("duplicateReceiptField", duplicate),
        ):
            with self.subTest(expected=expected):
                self.assert_error_code(
                    expected,
                    lambda payload=payload: load_coverage_receipt(payload),
                )

    def test_document_envelope_failures_have_exact_codes(self):
        nonstandard = (
            receipt_payload(self.report_payload, self.source)[:-1] + b',"x":NaN}'
        )
        cases = (
            ("receiptBytesRequired", "{}"),
            ("receiptJsonInvalid", b"\xff"),
            ("receiptJsonInvalid", b"{"),
            ("receiptObjectRequired", b"[]"),
            ("receiptJsonInvalid", nonstandard),
        )

        for expected, payload in cases:
            with self.subTest(expected=expected, payload_type=type(payload).__name__):
                self.assert_error_code(
                    expected,
                    lambda payload=payload: load_coverage_receipt(payload),
                )

    def test_schema_failure_has_exact_code(self):
        payload = receipt_payload(
            self.report_payload,
            self.source,
            schemaVersion="sentinel-python-coverage-receipt-v2",
        )

        self.assert_error_code(
            "receiptSchemaInvalid",
            lambda: load_coverage_receipt(payload),
        )

    def test_invalid_digest_and_tool_metadata_are_rejected(self):
        invalid_documents = (
            (
                "coverageToolInvalid",
                {"coverageTool": None},
            ),
            (
                "coverageToolInvalid",
                {
                    "coverageTool": {
                        "name": "other",
                        "version": "7.16.0",
                        "format": 3,
                    }
                },
            ),
            (
                "coverageToolInvalid",
                {
                    "coverageTool": {
                        "name": "coverage.py",
                        "version": "7.15.0",
                        "format": 3,
                    }
                },
            ),
            (
                "coverageToolInvalid",
                {
                    "coverageTool": {
                        "name": "coverage.py",
                        "version": "7.16.0",
                        "format": 3.0,
                    }
                },
            ),
            (
                "coverageToolInvalid",
                {
                    "coverageTool": {
                        "name": "coverage.py",
                        "version": "7.16.0",
                        "format": 2,
                    }
                },
            ),
        )

        for expected, override in invalid_documents:
            with self.subTest(override=override):
                payload = receipt_payload(
                    self.report_payload,
                    self.source,
                    **override,
                )
                self.assert_error_code(
                    expected,
                    lambda payload=payload: load_coverage_receipt(payload),
                )

    def test_digest_failures_have_exact_codes(self):
        digest_cases = (
            (
                "reportDigestInvalid",
                {"coverageReportDigest": "sha512:" + "1" * 64},
            ),
            (
                "reportDigestInvalid",
                {"coverageReportDigest": "sha256:" + "1" * 63},
            ),
            (
                "reportDigestInvalid",
                {"coverageReportDigest": "sha256:X" + "1" * 63},
            ),
            (
                "reportDigestInvalid",
                {"coverageReportDigest": "sha256:" + "0" * 64},
            ),
            (
                "selectionDigestInvalid",
                {"testSelectionDigest": "sha256:" + "1" * 63},
            ),
        )

        for expected, override in digest_cases:
            with self.subTest(expected=expected, override=override):
                payload = receipt_payload(
                    self.report_payload,
                    self.source,
                    **override,
                )
                self.assert_error_code(
                    expected,
                    lambda payload=payload: load_coverage_receipt(payload),
                )

    def test_source_digest_document_failures_have_exact_codes(self):
        valid_digest = "sha256:" + hashlib.sha256(self.source).hexdigest()
        cases = (
            ("sourceDigestsInvalid", {}),
            ("sourceDigestsInvalid", [["src/alpha.py", valid_digest]]),
            ("sourcePathInvalid", {"../escape.py": valid_digest}),
            ("sourceDigestInvalid", {"src/alpha.py": "sha512:" + "1" * 64}),
        )

        for expected, source_digests in cases:
            with self.subTest(expected=expected, source_digests=source_digests):
                payload = receipt_payload(
                    self.report_payload,
                    self.source,
                    sourceDigests=source_digests,
                )
                self.assert_error_code(
                    expected,
                    lambda payload=payload: load_coverage_receipt(payload),
                )

    def test_nonstandard_number_and_invalid_source_path_are_rejected(self):
        nonstandard = receipt_payload(self.report_payload, self.source)[:-1] + b',"x":NaN}'
        invalid_path = json.loads(receipt_payload(self.report_payload, self.source))
        invalid_path["sourceDigests"] = {
            "../escape.py": "sha256:" + hashlib.sha256(self.source).hexdigest()
        }

        for expected, payload in (
            ("receiptJsonInvalid", nonstandard),
            ("sourcePathInvalid", json.dumps(invalid_path).encode("utf-8")),
        ):
            with self.subTest(expected=expected):
                self.assert_error_code(
                    expected,
                    lambda payload=payload: load_coverage_receipt(payload),
                )

    def test_verifier_requires_both_receipt_and_report_types(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )
        cases = (
            (None, self.report),
            (receipt, None),
        )

        for candidate_receipt, candidate_report in cases:
            with self.subTest(
                receipt_type=type(candidate_receipt).__name__,
                report_type=type(candidate_report).__name__,
            ):
                self.assert_error_code(
                    "receiptTypeInvalid",
                    lambda candidate_receipt=candidate_receipt,
                    candidate_report=candidate_report: verify_coverage_receipt(
                        candidate_receipt,
                        candidate_report,
                        {"src/alpha.py": self.source},
                        SELECTION_DIGEST,
                    ),
                )

    def test_invalid_expected_selection_digest_has_exact_code(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )

        self.assert_error_code(
            "selectionDigestInvalid",
            lambda: verify_coverage_receipt(
                receipt,
                self.report,
                {"src/alpha.py": self.source},
                "sha512:" + "1" * 64,
            ),
        )

    def test_each_report_tool_metadata_field_must_match(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )
        reports = (
            replace(
                self.report,
                metadata=replace(self.report.metadata, version="7.15.0"),
            ),
            replace(
                self.report,
                metadata=replace(self.report.metadata, format=2),
            ),
        )

        for report in reports:
            with self.subTest(metadata=report.metadata):
                self.assert_error_code(
                    "reportToolMismatch",
                    lambda report=report: verify_coverage_receipt(
                        receipt,
                        report,
                        {"src/alpha.py": self.source},
                        SELECTION_DIGEST,
                    ),
                )

    def test_actual_source_inventory_failures_have_exact_codes(self):
        receipt = load_coverage_receipt(
            receipt_payload(self.report_payload, self.source)
        )
        cases = (
            ("sourceInventoryInvalid", [("src/alpha.py", self.source)]),
            ("sourcePathInvalid", {"../escape.py": self.source}),
            ("sourceBytesRequired", {"src/alpha.py": "not bytes"}),
        )

        for expected, sources in cases:
            with self.subTest(expected=expected):
                self.assert_error_code(
                    expected,
                    lambda sources=sources: verify_coverage_receipt(
                        receipt,
                        self.report,
                        sources,
                        SELECTION_DIGEST,
                    ),
                )

    def test_coverage_report_must_contain_every_receipted_source(self):
        second_source = b"def beta():\n    return 2\n"
        document = json.loads(receipt_payload(self.report_payload, self.source))
        document["sourceDigests"]["src/beta.py"] = (
            "sha256:" + hashlib.sha256(second_source).hexdigest()
        )
        receipt = load_coverage_receipt(json.dumps(document).encode("utf-8"))

        self.assert_error_code(
            "coverageFileSetMismatch",
            lambda: verify_coverage_receipt(
                receipt,
                self.report,
                {"src/alpha.py": self.source, "src/beta.py": second_source},
                SELECTION_DIGEST,
            ),
        )


if __name__ == "__main__":
    unittest.main()
