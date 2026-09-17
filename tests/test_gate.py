from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.coverage import CallableMetric, CrapGateResult, evaluate_crap_gate  # noqa: E402
from sentinel_py.crap import analyze_source, calculate_crap  # noqa: E402
from sentinel_py.gate import (  # noqa: E402
    DEFAULT_GATE,
    GateInputError,
    GateThresholds,
    load_gate,
    parse_crap_max,
    parse_mutation_min,
)
from sentinel_py.mutation import MUTATION_STATES, MutantRecord, evaluate_mutation_gate  # noqa: E402


GOLDEN = json.loads((REPOSITORY_ROOT / "tests" / "fixtures" / "spec" / "threshold-v1.json").read_text())


def _metric(path, name, complexity, covered, total, crap_max=DEFAULT_GATE.crap_max):
    source = f"def {name}():\n    return 1\n".encode("utf-8")
    definition = replace(analyze_source(source, path)[0], complexity=complexity)
    return CallableMetric(definition, calculate_crap(complexity, covered, total, crap_max=crap_max))


def _records(**counts):
    records = []
    for state, count in counts.items():
        for index in range(count):
            records.append(MutantRecord(f"{state}-{index}", state))
    return records


class GateParsingTests(unittest.TestCase):
    def test_defaults_match_the_shared_golden_vector(self):
        self.assertEqual(GOLDEN["defaults"], DEFAULT_GATE.as_json())
        self.assertEqual(GateThresholds(Fraction(8), Fraction(90)), load_gate(None, None))

    def test_text_is_read_as_an_exact_fraction_and_rendered_back(self):
        gate = load_gate("8.50", "99.9")
        self.assertEqual((Fraction(17, 2), Fraction(999, 10)), (gate.crap_max, gate.mutation_min))
        self.assertEqual({"crapMax": "8.5", "mutationMin": "99.9"}, gate.as_json())

    def test_invalid_strings_and_ranges_use_the_shared_codes(self):
        for value in GOLDEN["invalidThresholds"]:
            with self.subTest(value=value):
                with self.assertRaises(GateInputError) as raised:
                    parse_crap_max(value)
                self.assertEqual("crapMaxInvalid", raised.exception.code)
        for value in GOLDEN["invalidCrapMaxes"]:
            with self.assertRaises(GateInputError) as raised:
                parse_crap_max(value)
            self.assertEqual("crapMaxOutOfRange", raised.exception.code)
        for value in GOLDEN["invalidMutationMins"]:
            with self.assertRaises(GateInputError) as raised:
                parse_mutation_min(value)
            self.assertEqual("mutationMinOutOfRange", raised.exception.code)


class CrapThresholdTests(unittest.TestCase):
    def test_golden_crap_cases_pass_or_fail_against_the_given_limit(self):
        for case in GOLDEN["crapCases"]:
            with self.subTest(case=case["id"]):
                inputs = case["input"]
                crap_max = parse_crap_max(case["crapMax"])
                result = calculate_crap(
                    inputs["cyclomaticComplexity"],
                    inputs["coveredUnits"],
                    inputs["totalUnits"],
                    crap_max=crap_max,
                )
                self.assertEqual(case["expected"]["pass"], result.passed)

    def test_gate_recomputes_every_metric_with_the_same_limit(self):
        limit = parse_crap_max("9")
        risky = _metric("src/risky.py", "risky", 9, 1, 1, crap_max=limit)
        self.assertEqual(CrapGateResult(True, "passed", (risky,)), evaluate_crap_gate((risky,), limit))
        self.assertEqual("crapThresholdExceeded", evaluate_crap_gate((_metric("src/risky.py", "risky", 9, 1, 1),)).reason)


class MutationThresholdTests(unittest.TestCase):
    def test_default_ninety_percent_boundary_and_explicit_full_kill_rate(self):
        for killed, survived, passed in ((9, 1, True), (8999, 1001, False)):
            records = _records(killed=killed, survived=survived)
            candidates = tuple(r.candidate_id for r in records)
            with self.subTest(killed=killed, survived=survived):
                self.assertEqual(passed, evaluate_mutation_gate(candidates, records).passed)
                self.assertFalse(evaluate_mutation_gate(candidates, records, 0, Fraction(100)).passed)

    def test_golden_mutation_cases_follow_the_minimum_kill_rate(self):
        for case in GOLDEN["mutationCases"]:
            with self.subTest(case=case["id"]):
                records = _records(**{state: case["counts"][state] for state in MUTATION_STATES})
                result = evaluate_mutation_gate(
                    tuple(record.candidate_id for record in records),
                    records,
                    case["unauthorizedExclusion"],
                    parse_mutation_min(case["mutationMin"]),
                )
                self.assertEqual(case["expected"]["pass"], result.passed)

    def test_below_default_minimum_keeps_the_non_killed_reason(self):
        records = _records(killed=3, survived=1)
        result = evaluate_mutation_gate(tuple(r.candidate_id for r in records), records)
        self.assertEqual(("nonKilledMutant", "75"), (result.reason, result.kill_rate_percent))
        lowered = evaluate_mutation_gate(tuple(r.candidate_id for r in records), records, 0, Fraction(75))
        self.assertEqual("passed", lowered.reason)


if __name__ == "__main__":
    unittest.main()
