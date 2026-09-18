"""Strict bridge for the pinned mutmut 3.7.0 generator and raw metadata."""

from __future__ import annotations

import io
import json
import token
import tokenize
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Optional

import libcst as cst
from mutmut.mutation.file_mutation import MutationVisitor, combine_mutations_to_source, group_by_top_level_node
from mutmut.mutation.trampoline_templates import mangle_function_name
from mutmut.mutation.mutators import mutation_operators
from mutmut.mutation.pragma_handling import IgnoredCode
from mutmut.utils.format_utils import get_mutant_name

from .mutation import MutantRecord


_META_FIELDS = frozenset(
    (
        "exit_code_by_key",
        "hash_by_function_name",
        "type_check_error_by_key",
        "durations_by_key",
        "estimated_durations_by_key",
    )
)
_RAW_STATE_MAP = {
    0: "survived",
    2: "pending",
    5: "uncovered",
    24: "timedOut",
    33: "uncovered",
    34: "ignored",
    36: "timedOut",
    37: "compileError",
    152: "timedOut",
    255: "timedOut",
    -24: "timedOut",
    -11: "runtimeError",
    -9: "runtimeError",
    None: "pending",
}


class MutmutBridgeError(ValueError):
    """Raised when mutmut evidence cannot support a strict result."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def enumerate_candidates(sources: Mapping[str, bytes]) -> tuple[str, ...]:
    if not isinstance(sources, Mapping):
        raise MutmutBridgeError("sourceInventoryInvalid")
    candidates = []
    for path in sorted(sources, key=_utf8_sort_key):
        _validate_source_path(path)
        source = _decode_source(sources[path])
        _reject_exclusion_pragma(source)
        candidates.extend(_generate_file_candidates(path, source))
    if len(candidates) != len(set(candidates)):
        raise MutmutBridgeError("duplicateCandidateId")
    return tuple(candidates)


def _utf8_sort_key(value: object) -> bytes:
    if not isinstance(value, str):
        raise MutmutBridgeError("sourcePathInvalid")
    try:
        return value.encode()
    except UnicodeEncodeError as error:
        raise MutmutBridgeError("sourcePathInvalid") from error


def _validate_source_path(path: str) -> None:
    normalized = PurePosixPath(path)
    invalid = (
        not path
        or "\\" in path
        or "\x00" in path
        or normalized.is_absolute()
        or str(normalized) != path
        or ".." in normalized.parts
        or normalized.suffix != ".py"
    )
    if invalid:
        raise MutmutBridgeError("sourcePathInvalid")


def _decode_source(source: object) -> str:
    if not isinstance(source, bytes):
        raise MutmutBridgeError("sourceBytesInvalid")
    try:
        return source.decode()
    except UnicodeDecodeError as error:
        raise MutmutBridgeError("sourceEncodingInvalid") from error


def _reject_exclusion_pragma(source: str) -> None:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for source_token in tokens:
            if source_token.type == token.COMMENT and _is_exclusion_comment(source_token.string):
                raise MutmutBridgeError("unauthorizedExclusion")
    except (IndentationError, tokenize.TokenError) as error:
        raise MutmutBridgeError("sourceParseError") from error


def _is_exclusion_comment(comment: str) -> bool:
    return "# pragma:" in comment and "no mutate" in comment


def _generate_file_candidates(path: str, source: str) -> list[str]:
    return list(_generate_file_plan(path, source))


def candidate_details(sources: Mapping[str, bytes]) -> dict[str, dict]:
    details = {}
    for path in sorted(sources):
        _validate_source_path(path)
        source = _decode_source(sources[path])
        _reject_exclusion_pragma(source)
        details.update(_generate_file_plan(path, source))
    return details


def _generate_file_plan(path: str, source: str) -> dict[str, dict]:
    try:
        module = cst.parse_module(source)
        wrapper = cst.MetadataWrapper(module)
        visitor = MutationVisitor(
            mutation_operators,
            IgnoredCode(set(), set(), set()),
        )
        visited = wrapper.visit(visitor)
        mutated = combine_mutations_to_source(visited, visitor.mutations)
    except Exception as error:
        if isinstance(error, MutmutBridgeError):
            raise
        raise MutmutBridgeError("generatorError") from error
    details = _located_mutations(path, source, wrapper, visitor.mutations)
    expected = [get_mutant_name(Path(path), name) for name in mutated.mutant_names]
    if set(expected) != set(details):
        raise MutmutBridgeError("candidateLocationMismatch")
    return {identifier: details[identifier] for identifier in expected}


def _function_owners(module):
    owners = []
    for node in module.body:
        if isinstance(node, cst.FunctionDef):
            owners.append((node, None))
        elif isinstance(node, cst.ClassDef) and isinstance(node.body, cst.IndentedBlock):
            owners.extend((method, node.name.value) for method in node.body.body if isinstance(method, cst.FunctionDef))
    return owners


def _located_mutations(path, source, wrapper, mutations):
    positions = wrapper.resolve(cst.metadata.PositionProvider)
    details = {}
    groups = group_by_top_level_node(mutations)
    for function, class_name in _function_owners(wrapper.module):
        mangled = mangle_function_name(name=function.name.value, class_name=class_name)
        for index, mutation in enumerate(groups.get(function, ())):
            identifier = get_mutant_name(Path(path), f"{mangled}__mutmut_{index + 1}")
            position = positions[mutation.original_node]
            source_lines = source.splitlines(keepends=True)
            start_byte = len("".join(source_lines[:position.start.line - 1]).encode()) + len(source_lines[position.start.line - 1][:position.start.column].encode())
            details[identifier] = {
                "file": path,
                "function": f"{class_name}.{function.name.value}" if class_name else function.name.value,
                "line": position.start.line,
                "column": position.start.column + 1,
                "sourceStartByte": start_byte,
                "original": wrapper.module.code_for_node(mutation.original_node),
                "replacement": wrapper.module.code_for_node(mutation.mutated_node),
            }
    return details


def load_results(
    candidate_ids: Sequence[str],
    meta_paths: Sequence[Path],
    failure_states: Mapping[str, str],
) -> tuple[MutantRecord, ...]:
    candidates = _validate_candidates(candidate_ids)
    raw_results = _read_raw_results(meta_paths)
    if frozenset(raw_results) != frozenset(candidates):
        raise MutmutBridgeError("candidateResultSetMismatch")
    expected_failures = frozenset(
        candidate_id for candidate_id, exit_code in raw_results.items() if exit_code == 1
    )
    _validate_failure_states(failure_states, expected_failures)
    return tuple(
        MutantRecord(
            candidate_id,
            _normalize_status(candidate_id, raw_results[candidate_id], failure_states),
        )
        for candidate_id in candidates
    )


def read_raw_exit_codes(meta_paths: Sequence[Path]) -> Mapping[str, Optional[int]]:
    """Expose the validated raw inventory so only claimed kills need replay."""

    return MappingProxyType(_read_raw_results(meta_paths))


def _validate_candidates(candidate_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(candidate_ids, (str, bytes)):
        raise MutmutBridgeError("candidatePlanInvalid")
    candidates = tuple(candidate_ids)
    if any(not isinstance(value, str) or not value for value in candidates):
        raise MutmutBridgeError("candidatePlanInvalid")
    if len(candidates) != len(set(candidates)):
        raise MutmutBridgeError("duplicateCandidateId")
    return candidates


def _validate_failure_states(
    states: Mapping[str, str],
    expected_candidates: frozenset[str],
) -> None:
    if not isinstance(states, Mapping):
        raise MutmutBridgeError("failureStateInvalid")
    actual_candidates = frozenset(states)
    if not actual_candidates.issubset(expected_candidates):
        raise MutmutBridgeError("unexpectedFailureState")
    if actual_candidates != expected_candidates:
        raise MutmutBridgeError("failureStateMissing")
    if any(value not in ("killed", "runtimeError") for value in states.values()):
        raise MutmutBridgeError("failureStateInvalid")


def _read_raw_results(meta_paths: Sequence[Path]) -> dict[str, Optional[int]]:
    if isinstance(meta_paths, (str, bytes)):
        raise MutmutBridgeError("metaInventoryInvalid")
    raw_results = {}
    for path in meta_paths:
        meta = _load_meta(path)
        for candidate_id, exit_code in meta["exit_code_by_key"].items():
            if candidate_id in raw_results:
                raise MutmutBridgeError("duplicateMutationResult")
            _validate_raw_outcome(candidate_id, exit_code)
            raw_results[candidate_id] = exit_code
    return raw_results


def _load_meta(path: Path) -> dict:
    try:
        payload = path.read_bytes().decode()
        meta = json.loads(payload, object_pairs_hook=_unique_object)
    except MutmutBridgeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MutmutBridgeError("metaInvalid") from error
    if not isinstance(meta, dict) or frozenset(meta) != _META_FIELDS:
        raise MutmutBridgeError("metaShapeInvalid")
    if any(not isinstance(meta[field], dict) for field in _META_FIELDS):
        raise MutmutBridgeError("metaShapeInvalid")
    return meta


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MutmutBridgeError("metaDuplicateKey")
        result[key] = value
    return result


def _validate_raw_outcome(candidate_id: object, exit_code: object) -> None:
    if not isinstance(candidate_id, str) or not candidate_id:
        raise MutmutBridgeError("candidateIdInvalid")
    if exit_code is not None and type(exit_code) is not int:
        raise MutmutBridgeError("rawStateInvalid")


def _normalize_status(
    candidate_id: str,
    exit_code: Optional[int],
    failure_states: Mapping[str, str],
) -> str:
    # RISK(breaking): mutmut exit 1은 v2에서 검증된 killed 또는 runtimeError로만 해석한다.
    if exit_code == 1:
        return _validated_failure_state(candidate_id, failure_states)
    if exit_code == 3:
        raise MutmutBridgeError("runnerInternalError")
    try:
        return _RAW_STATE_MAP[exit_code]
    except KeyError as error:
        raise MutmutBridgeError("unknownRawState") from error


def _validated_failure_state(candidate_id: str, states: Mapping[str, str]) -> str:
    try:
        return states[candidate_id]
    except KeyError as error:
        raise MutmutBridgeError("failureStateMissing") from error
