"""Backend-neutral mutation records and the strict killed-only gate."""

from __future__ import annotations

import math
from fractions import Fraction
from collections.abc import Sized
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

from .crap import MAX_SAFE_INTEGER
from .gate import DEFAULT_GATE, kill_rate_passes
from .rendering import render_canonical_decimal


MUTATION_STATES = (
    "killed",
    "survived",
    "uncovered",
    "timedOut",
    "compileError",
    "runtimeError",
    "pending",
    "ignored",
    "toolError",
)


class MutationGateError(ValueError):
    """Raised when a mutation plan or result set is not trustworthy."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MutantRecord:
    candidate_id: str
    status: str


@dataclass(frozen=True)
class MutationGateResult:
    passed: bool
    reason: str
    in_scope: int
    killed: int
    counts: Mapping[str, int]
    kill_rate_numerator: Optional[int]
    kill_rate_denominator: Optional[int]
    kill_rate_percent: Optional[str]


def evaluate_mutation_gate(
    candidate_ids: Iterable[str],
    records: Iterable[MutantRecord],
    unauthorized_exclusion: int = 0,
    mutation_min: Fraction = DEFAULT_GATE.mutation_min,
) -> MutationGateResult:
    candidates = _materialize_candidates(candidate_ids)
    outcomes = _materialize_records(records)
    _require_safe_count(unauthorized_exclusion, "unauthorizedExclusion")
    _require_exact_result_set(candidates, outcomes)
    counts = _state_counts(outcomes)
    in_scope = len(candidates)
    killed = counts["killed"]
    fraction = _kill_rate_fraction(killed, in_scope)
    reason = _gate_reason(in_scope, killed, unauthorized_exclusion, mutation_min)
    return MutationGateResult(
        passed=reason == "passed",
        reason=reason,
        in_scope=in_scope,
        killed=killed,
        counts=MappingProxyType(counts),
        kill_rate_numerator=None if fraction is None else fraction[0],
        kill_rate_denominator=None if fraction is None else fraction[1],
        kill_rate_percent=None
        if fraction is None
        else render_canonical_decimal(100 * fraction[0], fraction[1]),
    )


def _materialize_candidates(candidate_ids: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(candidate_ids, (str, bytes)):
        raise MutationGateError("candidatePlanNotIterable")
    if isinstance(candidate_ids, Sized):
        _require_in_scope_count(len(candidate_ids))
    try:
        candidates = tuple(candidate_ids)
    except TypeError as error:
        raise MutationGateError("candidatePlanNotIterable") from error
    _require_in_scope_count(len(candidates))
    for candidate_id in candidates:
        _validate_candidate_id(candidate_id)
    if len(candidates) != len(set(candidates)):
        raise MutationGateError("duplicateCandidateId")
    return candidates


def _materialize_records(records: Iterable[MutantRecord]) -> Tuple[MutantRecord, ...]:
    if isinstance(records, (str, bytes)):
        raise MutationGateError("mutationRecordsNotIterable")
    try:
        outcomes = tuple(records)
    except TypeError as error:
        raise MutationGateError("mutationRecordsNotIterable") from error
    for record in outcomes:
        _validate_record(record)
    identifiers = [record.candidate_id for record in outcomes]
    if len(identifiers) != len(set(identifiers)):
        raise MutationGateError("duplicateMutationResult")
    return outcomes


def _validate_record(record: MutantRecord) -> None:
    if not isinstance(record, MutantRecord):
        raise MutationGateError("mutationRecordTypeInvalid")
    _validate_candidate_id(record.candidate_id)
    if record.status not in MUTATION_STATES:
        raise MutationGateError("unknownMutationState")


def _validate_candidate_id(candidate_id: str) -> None:
    if not isinstance(candidate_id, str) or not candidate_id or "\x00" in candidate_id:
        raise MutationGateError("candidateIdInvalid")
    try:
        candidate_id.encode()
    except UnicodeEncodeError as error:
        raise MutationGateError("candidateIdInvalid") from error


def _require_safe_count(value: int, field_name: str) -> None:
    invalid_type = not isinstance(value, int) or isinstance(value, bool)
    if invalid_type:
        raise MutationGateError(field_name + "NotInteger")
    if value < 0 or value > MAX_SAFE_INTEGER:
        raise MutationGateError(field_name + "OutOfRange")


def _require_in_scope_count(value: int) -> None:
    _require_safe_count(value, "inScope")


def _require_exact_result_set(
    candidates: Tuple[str, ...],
    outcomes: Tuple[MutantRecord, ...],
) -> None:
    candidate_set = set(candidates)
    result_set = {record.candidate_id for record in outcomes}
    if candidate_set != result_set:
        raise MutationGateError("candidateResultSetMismatch")


def _state_counts(outcomes: Tuple[MutantRecord, ...]) -> dict:
    counts = {state: 0 for state in MUTATION_STATES}
    for record in outcomes:
        counts[record.status] += 1
    return counts


def _kill_rate_fraction(killed: int, in_scope: int) -> Optional[Tuple[int, int]]:
    if in_scope == 0:
        return None
    divisor = math.gcd(killed, in_scope)
    return killed // divisor, in_scope // divisor


def _gate_reason(
    in_scope: int,
    killed: int,
    unauthorized_exclusion: int,
    mutation_min: Fraction,
) -> str:
    if in_scope == 0:
        return "zeroMutants"
    if unauthorized_exclusion != 0:
        return "unauthorizedExclusion"
    # With an explicit 100 percent this is exactly killed != in_scope.
    if not kill_rate_passes(killed, in_scope, mutation_min):
        return "nonKilledMutant"
    return "passed"
