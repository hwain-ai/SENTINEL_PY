from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def assert_bridge_error(test_case, expected_code, operation, *arguments):
    from sentinel_py.mutmut_adapter import MutmutBridgeError

    with test_case.assertRaises(MutmutBridgeError) as caught:
        operation(*arguments)
    test_case.assertEqual(expected_code, caught.exception.code)
    test_case.assertEqual(expected_code, str(caught.exception))


class MutmutCandidatePlanTests(unittest.TestCase):
    def test_plan_uses_the_pinned_mutmut_generator_for_every_source_file(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        candidates = enumerate_candidates(
            {
                "src/pkg/sample.py": (
                    b"def plus(value):\n"
                    b"    return value + 1\n"
                )
            }
        )

        self.assertEqual(
            (
                "pkg.sample.x_plus__mutmut_1",
                "pkg.sample.x_plus__mutmut_2",
            ),
            candidates,
        )

    def test_plan_rejects_mutation_exclusion_pragmas(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        assert_bridge_error(
            self,
            "unauthorizedExclusion",
            enumerate_candidates,
            {"src/pkg/sample.py": b"def f():  # pragma: no mutate\n    return 1\n"},
        )

    def test_plan_only_treats_a_comment_with_both_markers_as_an_exclusion(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        for source in (
            b"label = '# pragma: no mutate'\ndef f():\n    return 1\n",
            b"# pragma: another directive\ndef f():\n    return 1\n",
            b"# no mutate by itself\ndef f():\n    return 1\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(enumerate_candidates({"src/pkg/sample.py": source}))

    def test_plan_rejects_invalid_source_inventory_bytes_and_encoding(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        cases = (
            ("sourceInventoryInvalid", []),
            ("sourceBytesInvalid", {"src/pkg/sample.py": "not bytes"}),
            ("sourceEncodingInvalid", {"src/pkg/sample.py": b"\xff"}),
        )
        for code, sources in cases:
            with self.subTest(code=code):
                assert_bridge_error(self, code, enumerate_candidates, sources)

    def test_plan_rejects_invalid_or_noncanonical_source_paths(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        for path in (
            "",
            ".",
            "/src/sample.py",
            "src//sample.py",
            "src/../sample.py",
            "src\\sample.py",
            "src/sample\x00.py",
            "src/\ud800.py",
            "sample.ts",
            7,
        ):
            with self.subTest(path=repr(path)):
                assert_bridge_error(
                    self,
                    "sourcePathInvalid",
                    enumerate_candidates,
                    {path: b"def f():\n    return 1\n"},
                )

    def test_plan_orders_files_by_their_canonical_paths(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        sources = {
            "src/pkg/z.py": b"def f():\n    return 1\n",
            "src/pkg/a.py": b"def f():\n    return 1\n",
            "src/pkg/\u00e9.py": b"def f():\n    return 1\n",
        }
        with patch(
            "sentinel_py.mutmut_adapter._generate_file_candidates",
            side_effect=lambda path, source: [path],
        ):
            self.assertEqual(
                ("src/pkg/a.py", "src/pkg/z.py", "src/pkg/\u00e9.py"),
                enumerate_candidates(sources),
            )

    def test_plan_rejects_duplicate_generated_candidate_ids(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        with patch(
            "sentinel_py.mutmut_adapter._generate_file_candidates",
            return_value=["same-candidate"],
        ):
            assert_bridge_error(
                self,
                "duplicateCandidateId",
                enumerate_candidates,
                {
                    "src/pkg/a.py": b"def f():\n    return 1\n",
                    "src/pkg/b.py": b"def f():\n    return 1\n",
                },
            )

    def test_plan_wraps_parser_or_generator_failures(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        assert_bridge_error(
            self,
            "generatorError",
            enumerate_candidates,
            {"src/pkg/sample.py": b"def broken():\n    return = 1\n"},
        )

    def test_plan_reports_tokenization_failures_separately(self):
        from sentinel_py.mutmut_adapter import enumerate_candidates

        assert_bridge_error(
            self,
            "sourceParseError",
            enumerate_candidates,
            {"src/pkg/sample.py": b"unterminated = '''\n"},
        )


class MutmutResultTests(unittest.TestCase):
    def test_raw_result_inventory_is_available_before_kill_replay(self):
        from sentinel_py.mutmut_adapter import read_raw_exit_codes

        with tempfile.TemporaryDirectory(prefix="sentinel-py-raw-results-") as directory:
            meta = Path(directory) / "sample.py.meta"
            meta.write_text(
                json.dumps(
                    {
                        "exit_code_by_key": {
                            "pkg.sample.x_f__mutmut_1": 1,
                            "pkg.sample.x_f__mutmut_2": 0,
                        },
                        "hash_by_function_name": {},
                        "type_check_error_by_key": {},
                        "durations_by_key": {},
                        "estimated_durations_by_key": {},
                    }
                ),
                encoding="utf-8",
            )

            results = read_raw_exit_codes((meta,))

            self.assertEqual(
                {
                    "pkg.sample.x_f__mutmut_1": 1,
                    "pkg.sample.x_f__mutmut_2": 0,
                },
                dict(results),
            )
            with self.assertRaises(TypeError):
                results["pkg.sample.x_f__mutmut_1"] = 0

    def test_meta_requires_exact_candidate_set_and_validated_failure_state(self):
        from sentinel_py.mutmut_adapter import load_results

        with tempfile.TemporaryDirectory(prefix="sentinel-py-meta-") as directory:
            root = Path(directory)
            meta = root / "sample.py.meta"
            meta.write_text(
                json.dumps(
                    {
                        "exit_code_by_key": {"pkg.sample.x_f__mutmut_1": 1},
                        "hash_by_function_name": {},
                        "type_check_error_by_key": {},
                        "durations_by_key": {},
                        "estimated_durations_by_key": {},
                    }
                ),
                encoding="utf-8",
            )

            records = load_results(
                ("pkg.sample.x_f__mutmut_1",),
                (meta,),
                {"pkg.sample.x_f__mutmut_1": "killed"},
            )

            self.assertEqual("killed", records[0].status)
            self.assertEqual("pkg.sample.x_f__mutmut_1", records[0].candidate_id)

    def test_candidate_plan_rejects_text_invalid_ids_and_duplicates(self):
        invalid_plans = (
            ("candidatePlanInvalid", "one-candidate"),
            ("candidatePlanInvalid", b"one-candidate"),
            ("candidatePlanInvalid", ("",)),
            ("candidatePlanInvalid", (7,)),
            ("duplicateCandidateId", ("same", "same")),
        )
        for code, plan in invalid_plans:
            with self.subTest(code=code, plan=repr(plan)):
                assert_bridge_error(self, code, self._load_single, 0, plan)

    def test_failure_state_inventory_rejects_invalid_shapes_and_unexpected_ids(self):
        valid_id = "pkg.sample.x_f__mutmut_1"
        cases = (
            ("failureStateInvalid", 0, []),
            ("unexpectedFailureState", 0, {"other": "killed"}),
            ("failureStateInvalid", 1, {valid_id: object()}),
            ("failureStateInvalid", 1, {valid_id: "survived"}),
            ("failureStateInvalid", 1, {valid_id: ""}),
        )
        for code, exit_code, states in cases:
            with self.subTest(code=code, states=repr(states)):
                assert_bridge_error(
                    self,
                    code,
                    self._load_single,
                    exit_code,
                    (valid_id,),
                    states,
                )

    def test_validated_failure_state_preserves_runtime_error(self):
        candidate = "pkg.sample.x_f__mutmut_1"
        records = self._load_single(1, (candidate,), {candidate: "runtimeError"})
        self.assertEqual("runtimeError", records[0].status)

    def test_upstream_internal_error_is_never_counted_as_killed(self):
        assert_bridge_error(self, "runnerInternalError", self._load_single, 3)

    def test_unclassified_test_failure_is_rejected_instead_of_guessed(self):
        assert_bridge_error(self, "failureStateMissing", self._load_single, 1)

    def test_each_known_nonpassing_raw_state_is_preserved(self):
        expected = {
            0: "survived",
            5: "uncovered",
            33: "uncovered",
            34: "ignored",
            36: "timedOut",
            24: "timedOut",
            152: "timedOut",
            255: "timedOut",
            -24: "timedOut",
            37: "compileError",
            2: "pending",
            -11: "runtimeError",
            -9: "runtimeError",
            None: "pending",
        }
        for exit_code, status in expected.items():
            with self.subTest(exit_code=exit_code):
                self.assertEqual(status, self._load_single(exit_code=exit_code)[0].status)

    def test_unknown_exit_code_and_meta_shape_fail_closed(self):
        assert_bridge_error(self, "unknownRawState", self._load_single, 99)
        assert_bridge_error(
            self,
            "metaDuplicateKey",
            self._load_raw,
            '{"exit_code_by_key":{},"exit_code_by_key":{},'
            '"hash_by_function_name":{},"type_check_error_by_key":{},'
            '"durations_by_key":{},"estimated_durations_by_key":{}}',
        )

    def test_meta_requires_exact_fields_and_object_valued_sections(self):
        wrong_fields = json.dumps({})
        non_object_section = json.dumps(
            {
                "exit_code_by_key": [],
                "hash_by_function_name": {},
                "type_check_error_by_key": {},
                "durations_by_key": {},
                "estimated_durations_by_key": {},
            }
        )
        for payload in (wrong_fields, non_object_section):
            with self.subTest(payload=payload):
                assert_bridge_error(self, "metaShapeInvalid", self._load_raw, payload)

    def test_meta_inventory_rejects_text_and_bytes(self):
        from sentinel_py.mutmut_adapter import load_results

        for inventory in ("sample.py.meta", b"sample.py.meta"):
            with self.subTest(inventory=repr(inventory)):
                assert_bridge_error(
                    self,
                    "metaInventoryInvalid",
                    load_results,
                    (),
                    inventory,
                    {},
                )

    def test_meta_read_and_decode_failures_are_wrapped(self):
        from sentinel_py.mutmut_adapter import load_results

        with tempfile.TemporaryDirectory(prefix="sentinel-py-meta-") as directory:
            root = Path(directory)
            missing = root / "missing.py.meta"
            assert_bridge_error(self, "metaInvalid", load_results, (), (missing,), {})

            invalid_utf8 = root / "invalid.py.meta"
            invalid_utf8.write_bytes(b"\xff")
            assert_bridge_error(self, "metaInvalid", load_results, (), (invalid_utf8,), {})

            malformed = root / "malformed.py.meta"
            malformed.write_text("{", encoding="utf-8")
            assert_bridge_error(self, "metaInvalid", load_results, (), (malformed,), {})

    def test_duplicate_results_across_meta_files_are_rejected(self):
        from sentinel_py.mutmut_adapter import load_results

        payload = json.dumps(self._meta_payload(0))
        with tempfile.TemporaryDirectory(prefix="sentinel-py-meta-") as directory:
            root = Path(directory)
            first = root / "first.py.meta"
            second = root / "second.py.meta"
            first.write_text(payload, encoding="utf-8")
            second.write_text(payload, encoding="utf-8")
            assert_bridge_error(
                self,
                "duplicateMutationResult",
                load_results,
                ("pkg.sample.x_f__mutmut_1",),
                (first, second),
                {},
            )

    def test_raw_candidate_ids_and_states_are_validated(self):
        invalid_payloads = (
            (
                "candidateIdInvalid",
                {
                    "exit_code_by_key": {"": 0},
                    "hash_by_function_name": {},
                    "type_check_error_by_key": {},
                    "durations_by_key": {},
                    "estimated_durations_by_key": {},
                },
                ("pkg.sample.x_f__mutmut_1",),
            ),
            ("rawStateInvalid", self._meta_payload(True), ("pkg.sample.x_f__mutmut_1",)),
            ("rawStateInvalid", self._meta_payload(1.5), ("pkg.sample.x_f__mutmut_1",)),
            ("rawStateInvalid", self._meta_payload("1"), ("pkg.sample.x_f__mutmut_1",)),
        )
        for code, payload, plan in invalid_payloads:
            with self.subTest(code=code, payload=payload):
                assert_bridge_error(
                    self,
                    code,
                    self._load_raw,
                    json.dumps(payload),
                    plan,
                )

    def test_result_set_must_match_the_generator_plan_exactly(self):
        assert_bridge_error(
            self,
            "candidateResultSetMismatch",
            self._load_single,
            0,
            ("pkg.sample.x_f__mutmut_1", "pkg.sample.x_f__mutmut_2"),
        )

    def test_results_preserve_the_generator_plan_order(self):
        first = "pkg.sample.x_f__mutmut_1"
        second = "pkg.sample.x_f__mutmut_2"
        payload = self._meta_payload(0)
        payload["exit_code_by_key"] = {first: 0, second: 0}

        records = self._load_raw(json.dumps(payload), (second, first))

        self.assertEqual((second, first), tuple(record.candidate_id for record in records))

    @staticmethod
    def _meta_payload(exit_code):
        return {
            "exit_code_by_key": {"pkg.sample.x_f__mutmut_1": exit_code},
            "hash_by_function_name": {},
            "type_check_error_by_key": {},
            "durations_by_key": {},
            "estimated_durations_by_key": {},
        }

    @staticmethod
    def _load_single(
        exit_code,
        planned=("pkg.sample.x_f__mutmut_1",),
        failure_states=None,
    ):
        payload = MutmutResultTests._meta_payload(exit_code)
        return MutmutResultTests._load_raw(
            json.dumps(payload),
            planned,
            {} if failure_states is None else failure_states,
        )

    @staticmethod
    def _load_raw(payload, planned=("pkg.sample.x_f__mutmut_1",), failure_states=None):
        from sentinel_py.mutmut_adapter import load_results

        with tempfile.TemporaryDirectory(prefix="sentinel-py-meta-") as directory:
            meta = Path(directory) / "sample.py.meta"
            meta.write_text(payload, encoding="utf-8")
            return load_results(
                planned,
                (meta,),
                {} if failure_states is None else failure_states,
            )


if __name__ == "__main__":
    unittest.main()
