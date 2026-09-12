import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.coverage import (  # noqa: E402
    CallableMetric,
    CoverageFormatError,
    CoverageMetadata,
    CrapGateResult,
    MetricOrderingError,
    evaluate_crap_gate,
    load_coverage_json,
    measure_source,
    sort_callable_metrics,
)
from sentinel_py.crap import SourceRange, analyze_source, calculate_crap  # noqa: E402


SOURCE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "callables.py"
COVERAGE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "coverage.json"
MODULE_PATH = "tests/fixtures/callables.py"
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def coverage_payload(files, metadata=None):
    meta = {
        "format": 3,
        "version": "7.16.0",
        "timestamp": "2026-09-03T00:00:00",
        "branch_coverage": False,
        "show_contexts": False,
    }
    if metadata is not None:
        meta.update(metadata)
    return json.dumps({"meta": meta, "files": files}).encode("utf-8")


def file_coverage(executed=(), missing=(), excluded=()):
    return {
        "executed_lines": list(executed),
        "missing_lines": list(missing),
        "excluded_lines": list(excluded),
    }


class CoverageParsingTests(unittest.TestCase):
    def test_loads_exact_normalized_file_path(self):
        report = load_coverage_json(COVERAGE_PATH.read_bytes())

        self.assertEqual((2, 3, 8, 9, 10, 15, 16, 22, 23, 25, 27), report.files[MODULE_PATH].executed_lines)
        self.assertEqual((4, 17, 18, 24), report.files[MODULE_PATH].missing_lines)
        self.assertEqual((), report.files[MODULE_PATH].excluded_lines)
        self.assertEqual(
            CoverageMetadata(
                format=3,
                version="7.16.0",
                timestamp="2026-09-03T00:00:00",
                branch_coverage=False,
                show_contexts=False,
            ),
            report.metadata,
        )
        self.assertEqual(
            "sha256:" + hashlib.sha256(COVERAGE_PATH.read_bytes()).hexdigest(),
            report.report_digest,
        )

    def test_requires_exact_coverage_json_metadata(self):
        invalid_metadata = (
            {"format": 2},
            {"version": "7.15.3"},
            {"timestamp": ""},
            {"timestamp": "not-a-timestamp"},
            {"timestamp": "2026-09-03T00:00:00+00:00"},
            {"branch_coverage": 0},
            {"show_contexts": 0},
        )

        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata):
                with self.assertRaises(CoverageFormatError):
                    load_coverage_json(
                        coverage_payload({"module.py": file_coverage()}, metadata)
                    )

    def test_reports_exact_coverage_metadata_errors(self):
        cases = (
            (
                json.dumps({"meta": None, "files": {}}).encode("utf-8"),
                "coverage report meta must be a JSON object",
            ),
            (
                coverage_payload({"module.py": file_coverage()}, {"format": 2}),
                "coverage report meta format must be 3",
            ),
            (
                coverage_payload(
                    {"module.py": file_coverage()}, {"version": "7.15.3"}
                ),
                "coverage report meta version must be '7.16.0'",
            ),
            (
                coverage_payload({"module.py": file_coverage()}, {"timestamp": None}),
                "coverage report meta timestamp must be non-empty",
            ),
            (
                coverage_payload({"module.py": file_coverage()}, {"timestamp": ""}),
                "coverage report meta timestamp must be non-empty",
            ),
            (
                coverage_payload(
                    {"module.py": file_coverage()}, {"timestamp": "not-a-timestamp"}
                ),
                "coverage report meta timestamp is invalid",
            ),
            (
                coverage_payload(
                    {"module.py": file_coverage()},
                    {"timestamp": "2026-09-03T00:00:00+00:00"},
                ),
                "coverage report meta timestamp is not canonical local time",
            ),
            (
                coverage_payload(
                    {"module.py": file_coverage()}, {"branch_coverage": 0}
                ),
                "coverage report meta branch_coverage must be boolean",
            ),
            (
                coverage_payload({"module.py": file_coverage()}, {"show_contexts": 0}),
                "coverage report meta show_contexts must be boolean",
            ),
        )

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(CoverageFormatError) as raised:
                    load_coverage_json(payload)
                self.assertEqual(message, str(raised.exception))

    def test_reports_exact_payload_and_document_errors(self):
        cases = (
            ("not-bytes", "coverage JSON must be UTF-8 bytes"),
            (b"\xff", "coverage JSON is not valid UTF-8"),
            (b"{", "coverage report is not valid JSON"),
            (b"[]", "coverage report must be a JSON object"),
            (
                coverage_payload([]),
                "coverage report files must be a JSON object",
            ),
        )

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(CoverageFormatError) as raised:
                    load_coverage_json(payload)
                self.assertEqual(message, str(raised.exception))

    def test_rejects_invalid_line_arrays(self):
        invalid_arrays = (
            {"executed_lines": [True], "missing_lines": [], "excluded_lines": []},
            {"executed_lines": [0], "missing_lines": [], "excluded_lines": []},
            {"executed_lines": [1, 1], "missing_lines": [], "excluded_lines": []},
            {"executed_lines": [1], "missing_lines": [1], "excluded_lines": []},
            {"executed_lines": "1", "missing_lines": [], "excluded_lines": []},
            {"executed_lines": [9_007_199_254_740_992], "missing_lines": [], "excluded_lines": []},
            {"executed_lines": [], "missing_lines": [], "excluded_lines": [1]},
        )

        for file_data in invalid_arrays:
            with self.subTest(file_data=file_data):
                with self.assertRaises(CoverageFormatError):
                    load_coverage_json(coverage_payload({"module.py": file_data}))

    def test_reports_exact_file_entry_and_line_array_errors(self):
        cases = (
            (
                {"module.py": []},
                "coverage file entry must be an object: module.py",
            ),
            (
                {"module.py": {}},
                "executed_lines must be an array for module.py",
            ),
            (
                {
                    "module.py": {
                        "executed_lines": [],
                        "missing_lines": "invalid",
                        "excluded_lines": [],
                    }
                },
                "missing_lines must be an array for module.py",
            ),
            (
                {
                    "module.py": {
                        "executed_lines": [],
                        "missing_lines": [],
                        "excluded_lines": "invalid",
                    }
                },
                "excluded_lines must be an array for module.py",
            ),
            (
                {"module.py": file_coverage(executed=(0,))},
                "executed_lines must contain positive JSON-safe integer lines for module.py",
            ),
            (
                {"module.py": file_coverage(executed=(1, 1))},
                "executed_lines must not contain duplicate lines for module.py",
            ),
            (
                {"module.py": file_coverage(executed=(1,), missing=(1,))},
                "executed_lines and missing_lines overlap for module.py: 1",
            ),
            (
                {"module.py": file_coverage(executed=(2,), excluded=(2,))},
                "executed_lines and excluded_lines overlap for module.py: 2",
            ),
            (
                {"module.py": file_coverage(missing=(3,), excluded=(3,))},
                "missing_lines and excluded_lines overlap for module.py: 3",
            ),
            (
                {"module.py": file_coverage(excluded=(4,))},
                "excluded production lines are not allowed for module.py",
            ),
        )

        for files, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(CoverageFormatError) as raised:
                    load_coverage_json(coverage_payload(files))
                self.assertEqual(message, str(raised.exception))

    def test_accepts_maximum_json_safe_line_number(self):
        report = load_coverage_json(
            coverage_payload(
                {"module.py": file_coverage(executed=(MAX_SAFE_INTEGER,))}
            )
        )

        self.assertEqual(
            (MAX_SAFE_INTEGER,), report.files["module.py"].executed_lines
        )

    def test_rejects_missing_or_invalid_files_object(self):
        invalid_payloads = (
            {},
            {"meta": {}, "files": []},
            json.loads(coverage_payload({"./module.py": file_coverage()})),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(CoverageFormatError):
                    load_coverage_json(json.dumps(payload).encode("utf-8"))

    def test_rejects_duplicate_json_object_key(self):
        payload = (
            b'{"meta":{"format":3,"version":"7.16.0",'
            b'"timestamp":"2026-09-03T00:00:00","branch_coverage":false,'
            b'"show_contexts":false},"files":{"module.py":{"executed_lines":[],'
            b'"executed_lines":[],"missing_lines":[],"excluded_lines":[]}}}'
        )

        with self.assertRaises(CoverageFormatError) as raised:
            load_coverage_json(payload)
        self.assertEqual(
            "duplicate JSON object key: executed_lines", str(raised.exception)
        )

    def test_rejects_non_standard_json_number(self):
        payload = coverage_payload({"module.py": file_coverage()})
        payload = payload[:-1] + b', "invalid": NaN}'

        with self.assertRaises(CoverageFormatError) as raised:
            load_coverage_json(payload)
        self.assertEqual(
            "non-standard JSON number is not allowed: NaN", str(raised.exception)
        )

    def test_rejects_dot_as_a_coverage_file_path(self):
        payload = coverage_payload({".": file_coverage()})

        with self.assertRaises(CoverageFormatError) as raised:
            load_coverage_json(payload)
        self.assertEqual(
            "invalid coverage file path: '.'", str(raised.exception)
        )


class CoverageJoinTests(unittest.TestCase):
    def test_joins_callable_body_lines_and_excludes_nested_ranges(self):
        report = load_coverage_json(COVERAGE_PATH.read_bytes())

        metrics = measure_source(SOURCE_PATH.read_bytes(), MODULE_PATH, report)
        actual = {
            metric.callable.qualified_name: (
                metric.crap.covered,
                metric.crap.total,
                metric.crap.unknown_reason,
            )
            for metric in metrics
        }

        self.assertEqual(
            {
                "plain": (2, 3, None),
                "async_work": (3, 3, None),
                "Worker.run": (2, 4, None),
                "outer": (2, 2, None),
                "outer.<locals>.inner": (2, 3, None),
            },
            actual,
        )

    def test_does_not_use_basename_or_suffix_fallback(self):
        report = load_coverage_json(COVERAGE_PATH.read_bytes())

        metrics = measure_source(SOURCE_PATH.read_bytes(), "callables.py", report)

        self.assertTrue(metrics)
        self.assertTrue(all(metric.crap.covered == 0 for metric in metrics))
        self.assertTrue(all(metric.crap.total == 0 for metric in metrics))
        self.assertTrue(all(metric.crap.unknown_reason == "file-entry-missing" for metric in metrics))
        self.assertTrue(all(metric.crap.passed is None for metric in metrics))

    def test_requires_a_coverage_report_with_exact_error(self):
        with self.assertRaises(TypeError) as raised:
            measure_source(b"def sample():\n    return 1\n", "sample.py", None)

        self.assertEqual("report must be a CoverageReport", str(raised.exception))

    def test_marks_top_level_one_line_callable_coverage_ambiguous(self):
        source = b"def top(): return 1\n"
        report = load_coverage_json(
            coverage_payload({"one_line.py": file_coverage(executed=(1,))})
        )

        metric = measure_source(source, "one_line.py", report)[0]

        self.assertEqual(0, metric.crap.covered)
        self.assertEqual(0, metric.crap.total)
        self.assertEqual("ambiguous-line-coverage", metric.crap.unknown_reason)
        self.assertIsNone(metric.crap.passed)

    def test_does_not_attribute_nested_one_line_to_parent_or_child_twice(self):
        source = (
            b"def outer():\n"
            b"    def inner(): return 1\n"
            b"    return 0\n"
        )
        report = load_coverage_json(
            coverage_payload({"nested.py": file_coverage(executed=(2, 3))})
        )

        metrics = {
            metric.callable.qualified_name: metric
            for metric in measure_source(source, "nested.py", report)
        }

        outer = metrics["outer"].crap
        inner = metrics["outer.<locals>.inner"].crap
        self.assertEqual((2, 2, None), (outer.covered, outer.total, outer.unknown_reason))
        self.assertEqual(
            (0, 0, "ambiguous-line-coverage"),
            (inner.covered, inner.total, inner.unknown_reason),
        )
        self.assertIsNone(inner.passed)

    def test_nested_declaration_belongs_to_parent_and_body_belongs_to_child(self):
        source = (
            b"def outer():\n"
            b"    def inner():\n"
            b"        return 1\n"
            b"    return 0\n"
        )
        report = load_coverage_json(
            coverage_payload(
                {"nested.py": file_coverage(executed=(2, 4), missing=(3,))}
            )
        )

        metrics = {
            metric.callable.qualified_name: metric
            for metric in measure_source(source, "nested.py", report)
        }

        outer = metrics["outer"].crap
        inner = metrics["outer.<locals>.inner"].crap
        self.assertEqual((2, 2, None), (outer.covered, outer.total, outer.unknown_reason))
        self.assertEqual((0, 1, None), (inner.covered, inner.total, inner.unknown_reason))

    def test_multiline_lambda_body_belongs_only_to_the_lambda(self):
        source = (
            b"def outer(flag):\n"
            b"    operation = lambda value: (\n"
            b"        value if flag else 0\n"
            b"    )\n"
            b"    return operation(1)\n"
        )
        report = load_coverage_json(
            coverage_payload({"lambda.py": file_coverage(executed=(2, 3, 5))})
        )

        metrics = measure_source(source, "lambda.py", report)
        outer = next(metric.crap for metric in metrics if metric.callable.kind == "function")
        child = next(metric.crap for metric in metrics if metric.callable.kind == "lambda")

        self.assertEqual((2, 2, None), (outer.covered, outer.total, outer.unknown_reason))
        self.assertEqual((1, 1, None), (child.covered, child.total, child.unknown_reason))

    def test_marks_zero_executable_units_unknown(self):
        source = b"def documented():\n    \"\"\"No executable coverage unit.\"\"\"\n"
        report = load_coverage_json(
            coverage_payload({"documented.py": file_coverage()})
        )

        metric = measure_source(source, "documented.py", report)[0]

        self.assertEqual(0, metric.crap.covered)
        self.assertEqual(0, metric.crap.total)
        self.assertEqual("no-executable-lines", metric.crap.unknown_reason)
        self.assertIsNone(metric.crap.numerator)
        self.assertIsNone(metric.crap.denominator)
        self.assertIsNone(metric.crap.decimal)
        self.assertIsNone(metric.crap.passed)

    def test_analysis_does_not_modify_source_or_coverage_fixtures(self):
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (SOURCE_PATH, COVERAGE_PATH)
        }
        report = load_coverage_json(COVERAGE_PATH.read_bytes())

        measure_source(SOURCE_PATH.read_bytes(), MODULE_PATH, report)

        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (SOURCE_PATH, COVERAGE_PATH)
        }
        self.assertEqual(before, after)

    def test_rejects_reported_line_outside_current_source(self):
        source = b"def sample():\n    return 1\n"
        report = load_coverage_json(
            coverage_payload({"sample.py": file_coverage(executed=(3,))})
        )

        with self.assertRaises(CoverageFormatError) as raised:
            measure_source(source, "sample.py", report)
        self.assertEqual(
            "coverage line is outside current source for sample.py",
            str(raised.exception),
        )

    def test_rejects_outside_lines_for_empty_and_non_lf_source(self):
        cases = (
            (b"", "empty.py", 1),
            (b"def sample():\r\n    return 1\r\n", "crlf.py", 3),
            (b"def sample():\r    return 1\r", "cr.py", 3),
        )

        for source, path, outside_line in cases:
            report = load_coverage_json(
                coverage_payload(
                    {path: file_coverage(executed=(outside_line,))}
                )
            )
            with self.subTest(path=path):
                with self.assertRaises(CoverageFormatError) as raised:
                    measure_source(source, path, report)
                self.assertEqual(
                    f"coverage line is outside current source for {path}",
                    str(raised.exception),
                )

    def test_accepts_last_line_when_source_has_no_final_newline(self):
        source = b"def sample():\n    return 1"
        report = load_coverage_json(
            coverage_payload({"sample.py": file_coverage(executed=(2,))})
        )

        metric = measure_source(source, "sample.py", report)[0]

        self.assertEqual((1, 1, None), (
            metric.crap.covered,
            metric.crap.total,
            metric.crap.unknown_reason,
        ))

    def test_accepts_last_line_with_bare_carriage_return_newline(self):
        source = b"def sample():\r    return 1\r"
        report = load_coverage_json(
            coverage_payload({"sample.py": file_coverage(executed=(2,))})
        )

        metric = measure_source(source, "sample.py", report)[0]

        self.assertEqual((1, 1, None), (
            metric.crap.covered,
            metric.crap.total,
            metric.crap.unknown_reason,
        ))


class MetricOrderingTests(unittest.TestCase):
    def test_orders_unknown_then_exact_risk_then_utf8_identity(self):
        rows = (
            self._metric("src/low.py", "low", 1, 1, 1),
            self._metric("src/high.py", "high", 9, 1, 1),
            self._metric("src/𐀀.py", "astral", 1, 0, 0, "coverageUnitsMissing"),
            self._metric("src/.py", "private", 1, 0, 0, "coverageFileMissing"),
        )

        ordered = sort_callable_metrics(rows)

        self.assertEqual(
            ("private", "astral", "high", "low"),
            tuple(metric.callable.qualified_name for metric in ordered),
        )

    def test_rejects_duplicate_final_identity(self):
        metric = self._metric("src/same.py", "same", 1, 1, 1)

        with self.assertRaises(MetricOrderingError) as raised:
            sort_callable_metrics((metric, metric))
        self.assertEqual("identityAmbiguous", str(raised.exception))

    def test_orders_exact_risk_before_conflicting_identity(self):
        low = self._metric("src/a.py", "low", 1, 1, 1)
        high = self._metric("src/z.py", "high", 9, 1, 1)

        ordered = sort_callable_metrics((low, high))

        self.assertEqual(
            ("high", "low"),
            tuple(metric.callable.qualified_name for metric in ordered),
        )

    def test_orders_exact_fraction_instead_of_fraction_numerator(self):
        risk_two = self._metric("src/z.py", "risk_two", 2, 1, 1)
        large_numerator_lower_risk = self._metric(
            "src/a.py", "almost_full", 1, 99, 100
        )

        ordered = sort_callable_metrics((large_numerator_lower_risk, risk_two))

        self.assertEqual(
            ("risk_two", "almost_full"),
            tuple(metric.callable.qualified_name for metric in ordered),
        )

    def test_rejects_internally_inconsistent_metric(self):
        metric = self._metric("src/a.py", "sample", 1, 1, 1)
        invalid = replace(metric, crap=replace(metric.crap, passed=False))

        with self.assertRaises(MetricOrderingError) as raised:
            sort_callable_metrics((invalid,))
        self.assertEqual("metricInvariantInvalid", str(raised.exception))

    def test_rejects_invalid_source_digest(self):
        metric = self._metric("src/a.py", "sample", 1, 1, 1)
        invalid_callable = replace(metric.callable, source_digest="sha256:not-a-digest")

        with self.assertRaises(MetricOrderingError) as raised:
            sort_callable_metrics((replace(metric, callable=invalid_callable),))
        self.assertEqual("sourceDigestInvalid", str(raised.exception))

    def test_rejects_every_invalid_source_digest_shape(self):
        metric = self._metric("src/a.py", "sample", 1, 1, 1)
        invalid_digests = (
            None,
            "sha512:" + "0" * 64,
            "sha256:" + "0" * 63,
            "sha256:" + "0" * 63 + "G",
            "sha256:" + "0" * 63 + "X",
        )

        for digest in invalid_digests:
            invalid_callable = replace(metric.callable, source_digest=digest)
            with self.subTest(digest=digest):
                with self.assertRaises(MetricOrderingError) as raised:
                    sort_callable_metrics((replace(metric, callable=invalid_callable),))
                self.assertEqual("sourceDigestInvalid", str(raised.exception))

    def test_rejects_invalid_metric_component_types(self):
        metric = self._metric("src/a.py", "sample", 1, 1, 1)
        invalid_rows = (
            ("not-a-metric", "metricTypeInvalid"),
            (replace(metric, callable="not-a-callable"), "callableTypeInvalid"),
            (replace(metric, crap="not-a-crap-result"), "crapTypeInvalid"),
            (
                replace(metric, callable=replace(metric.callable, complexity=0)),
                "metricInvariantInvalid",
            ),
        )

        for invalid, message in invalid_rows:
            with self.subTest(message=message):
                with self.assertRaises(MetricOrderingError) as raised:
                    sort_callable_metrics((invalid,))
                self.assertEqual(message, str(raised.exception))

    def test_rejects_invalid_metric_identity_and_source_range(self):
        metric = self._metric("src/a.py", "sample", 1, 1, 1)
        invalid_callables = (
            (
                replace(metric.callable, module_relative_path="../escape.py"),
                "identityInvalid",
            ),
            (
                replace(metric.callable, source_range=SourceRange(-1, 1)),
                "sourceRangeInvalid",
            ),
            (
                replace(metric.callable, source_range=SourceRange(1, 1)),
                "sourceRangeInvalid",
            ),
            (
                replace(metric.callable, source_range=SourceRange(True, 2)),
                "sourceRangeInvalid",
            ),
            (
                replace(
                    metric.callable,
                    source_range=SourceRange(1, MAX_SAFE_INTEGER + 1),
                ),
                "sourceRangeInvalid",
            ),
        )

        for invalid_callable, message in invalid_callables:
            with self.subTest(message=message):
                with self.assertRaises(MetricOrderingError) as raised:
                    sort_callable_metrics((replace(metric, callable=invalid_callable),))
                self.assertEqual(message, str(raised.exception))

    def test_accepts_maximum_json_safe_source_end(self):
        metric = self._metric("src/a.py", "sample", 1, 1, 1)
        boundary_callable = replace(
            metric.callable,
            source_range=SourceRange(MAX_SAFE_INTEGER - 1, MAX_SAFE_INTEGER),
        )
        boundary_metric = replace(metric, callable=boundary_callable)

        self.assertEqual((boundary_metric,), sort_callable_metrics((boundary_metric,)))

    def test_gate_requires_nonempty_known_rows_at_or_below_eight(self):
        passing = self._metric("src/pass.py", "passing", 8, 1, 1)
        failing = self._metric("src/fail.py", "failing", 9, 1, 1)
        unknown = self._metric(
            "src/unknown.py",
            "unknown",
            1,
            0,
            0,
            "coverageFileMissing",
        )

        self.assertEqual(
            CrapGateResult(False, "emptyCallableInventory", ()),
            evaluate_crap_gate(()),
        )
        self.assertEqual(
            CrapGateResult(True, "passed", (passing,)),
            evaluate_crap_gate((passing,)),
        )
        self.assertEqual(
            CrapGateResult(
                False,
                "crapThresholdExceeded",
                (failing, passing),
            ),
            evaluate_crap_gate((passing, failing)),
        )
        self.assertEqual(
            CrapGateResult(False, "coverageUnknown", (unknown, passing)),
            evaluate_crap_gate((passing, unknown)),
        )

    @staticmethod
    def _metric(path, name, complexity, covered, total, unknown_reason=None):
        source = f"def {name}():\n    return 1\n".encode("utf-8")
        definition = replace(analyze_source(source, path)[0], complexity=complexity)
        crap = calculate_crap(complexity, covered, total, unknown_reason)
        return CallableMetric(definition, crap)


if __name__ == "__main__":
    unittest.main()
