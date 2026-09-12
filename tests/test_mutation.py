import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.mutation import (  # noqa: E402
    MUTATION_STATES,
    MutantRecord,
    MutationGateError,
    _require_safe_count,
    evaluate_mutation_gate,
)


class MutationGateTests(unittest.TestCase):
    def assert_gate_error(self, expected, candidates, records, exclusion=0):
        with self.assertRaises(MutationGateError) as raised:
            evaluate_mutation_gate(candidates, records, exclusion)
        self.assertEqual(expected, raised.exception.code)
        self.assertEqual(expected, str(raised.exception))

    def test_all_planned_mutants_killed_is_the_only_pass(self):
        result = evaluate_mutation_gate(
            ("m-1", "m-2"),
            (MutantRecord("m-2", "killed"), MutantRecord("m-1", "killed")),
        )

        self.assertTrue(result.passed)
        self.assertEqual("passed", result.reason)
        self.assertEqual((2, 2), (result.in_scope, result.killed))
        self.assertEqual((1, 1, "100"), (
            result.kill_rate_numerator,
            result.kill_rate_denominator,
            result.kill_rate_percent,
        ))
        self.assertEqual(
            {state: 2 if state == "killed" else 0 for state in MUTATION_STATES},
            dict(result.counts),
        )

    def test_every_non_killed_state_fails(self):
        for status in MUTATION_STATES[1:]:
            with self.subTest(status=status):
                result = evaluate_mutation_gate(
                    ("m-1",),
                    (MutantRecord("m-1", status),),
                )
                self.assertFalse(result.passed)
                self.assertEqual("nonKilledMutant", result.reason)

    def test_zero_mutants_fails_without_fabricated_percentage(self):
        result = evaluate_mutation_gate((), ())

        self.assertFalse(result.passed)
        self.assertEqual("zeroMutants", result.reason)
        self.assertIsNone(result.kill_rate_numerator)
        self.assertIsNone(result.kill_rate_denominator)
        self.assertIsNone(result.kill_rate_percent)

    def test_renders_exact_fraction_and_percentage(self):
        result = evaluate_mutation_gate(
            ("m-1", "m-2", "m-3"),
            (
                MutantRecord("m-1", "killed"),
                MutantRecord("m-2", "survived"),
                MutantRecord("m-3", "timedOut"),
            ),
        )

        self.assertEqual((1, 3), (
            result.kill_rate_numerator,
            result.kill_rate_denominator,
        ))
        self.assertEqual("33.333333333333", result.kill_rate_percent)

    def test_unauthorized_exclusion_fails_even_when_all_are_killed(self):
        result = evaluate_mutation_gate(
            ("m-1",),
            (MutantRecord("m-1", "killed"),),
            unauthorized_exclusion=1,
        )

        self.assertFalse(result.passed)
        self.assertEqual("unauthorizedExclusion", result.reason)

    def test_requires_exact_unique_candidate_and_result_sets(self):
        invalid_cases = (
            (("m-1", "m-1"), (), "duplicateCandidateId"),
            (("m-1",), (), "candidateResultSetMismatch"),
            ((), (MutantRecord("m-1", "killed"),), "candidateResultSetMismatch"),
            (
                ("m-1",),
                (MutantRecord("m-1", "killed"), MutantRecord("m-1", "killed")),
                "duplicateMutationResult",
            ),
        )

        for candidates, records, error in invalid_cases:
            with self.subTest(error=error):
                self.assert_gate_error(error, candidates, records)

    def test_rejects_unknown_state_invalid_id_and_unsafe_exclusion(self):
        invalid_cases = (
            (("m-1",), (MutantRecord("m-1", "unknown"),), 0, "unknownMutationState"),
            (("",), (), 0, "candidateIdInvalid"),
            ((), (), True, "unauthorizedExclusionNotInteger"),
            ((), (), 9_007_199_254_740_992, "unauthorizedExclusionOutOfRange"),
        )

        for candidates, records, exclusion, error in invalid_cases:
            with self.subTest(error=error):
                self.assert_gate_error(error, candidates, records, exclusion)

    def test_rejects_non_iterables_with_exact_stable_codes(self):
        candidate_cases = ("m-1", b"m-1", 1, None)
        record_cases = ("record", b"record", 1, None)

        for candidates in candidate_cases:
            with self.subTest(candidates=candidates):
                self.assert_gate_error(
                    "candidatePlanNotIterable",
                    candidates,
                    (),
                )
        for records in record_cases:
            with self.subTest(records=records):
                self.assert_gate_error(
                    "mutationRecordsNotIterable",
                    (),
                    records,
                )

    def test_rejects_invalid_record_and_candidate_ids_with_exact_codes(self):
        invalid_cases = (
            (("m-1",), ({"candidate_id": "m-1", "status": "killed"},), "mutationRecordTypeInvalid"),
            (("bad\x00id",), (), "candidateIdInvalid"),
            (("bad\ud800id",), (), "candidateIdInvalid"),
            (("m-1",), (MutantRecord("m-1", "unknown"),), "unknownMutationState"),
        )

        for candidates, records, expected in invalid_cases:
            with self.subTest(expected=expected):
                self.assert_gate_error(expected, candidates, records)

    def test_safe_count_accepts_the_json_safe_maximum(self):
        maximum = 9_007_199_254_740_991

        self.assertIsNone(_require_safe_count(maximum, "count"))

    def test_rejects_a_known_oversized_plan_before_iterating_it(self):
        class OversizedPlan:
            def __len__(self):
                return 9_007_199_254_740_992

            def __iter__(self):
                raise AssertionError("an oversized plan must not be iterated")

        self.assert_gate_error("inScopeOutOfRange", OversizedPlan(), ())


if __name__ == "__main__":
    unittest.main()
