"""Minimal structured pytest observation used to confirm mutation kills."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _ObservationState:
    collected: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


_states: dict[tuple[str, str, bytes], _ObservationState] = {}
_MUTMUT_CONTROL_IDS = frozenset(
    ("", "fail", "list_all_tests", "mutant_generation", "stats")
)
_HMAC_KEY_ENVIRONMENT = "SENTINEL_PYTEST_HMAC_KEY"
# RISK(breaking): 순서를 보존하는 v3 기록은 정렬된 v2 기록과 호환되지 않는다.
_OBSERVATION_SCHEMA = "sentinel-pytest-observation-v3"


def pytest_configure(config) -> None:
    del config
    contract = _report_contract()
    if contract is not None:
        _states[contract] = _ObservationState()


def pytest_collection_finish(session) -> None:
    state = _current_state()
    if state is not None:
        state.collected.extend(item.nodeid for item in session.items)


def pytest_runtest_logstart(nodeid, location) -> None:
    del location
    state = _current_state()
    if state is not None:
        state.started.append(nodeid)


def pytest_runtest_makereport(item, call) -> None:
    contract = _report_contract()
    state = None if contract is None else _states.get(contract)
    if state is None or call.excinfo is None:
        return
    exception_type = call.excinfo.type
    type_name = exception_type.__module__ + "." + exception_type.__qualname__
    assertion = issubclass(exception_type, AssertionError)
    location = _failure_location(item, call.excinfo.value)
    failure = {
        "assertion": assertion,
        "exceptionType": type_name,
        "location": location,
        "nodeId": item.nodeid,
        "phase": call.when,
    }
    failure["signature"] = _failure_signature(
        contract[2],
        assertion,
        type_name,
        item.nodeid,
        call.when,
        location,
    )
    state.failures.append(failure)


def pytest_sessionfinish(session, exitstatus) -> None:
    del session
    contract = _report_contract()
    if contract is None:
        return
    output, nonce, assertion_key = contract
    state = _states.pop(contract, None)
    if state is None:
        return
    document = {
        "collected": state.collected,
        "exitCode": int(exitstatus),
        "failures": sorted(state.failures, key=_failure_sort_key),
        "nonce": nonce,
        "schemaVersion": _OBSERVATION_SCHEMA,
        "started": state.started,
    }
    document["reportSignature"] = _report_signature(assertion_key, document)
    Path(output).write_text(
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _current_state() -> _ObservationState | None:
    contract = _report_contract()
    return None if contract is None else _states.get(contract)


def _report_contract() -> tuple[str, str, bytes] | None:
    output = os.environ.get("SENTINEL_PYTEST_REPORT")
    nonce = os.environ.get("SENTINEL_PYTEST_NONCE")
    if output is not None or nonce is not None:
        if not output or not nonce:
            raise RuntimeError("sentinelPytestContractInvalid")
        return output, nonce, _hmac_key()
    return _mutmut_report_contract()


def _mutmut_report_contract(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str, bytes] | None:
    source = os.environ if environment is None else environment
    root = source.get("SENTINEL_MUTMUT_REPORT_ROOT")
    run_nonce = source.get("SENTINEL_MUTMUT_RUN_NONCE")
    mutant = source.get("MUTANT_UNDER_TEST", "")
    if root is None and run_nonce is None:
        return None
    if not root or not run_nonce:
        raise RuntimeError("sentinelPytestContractInvalid")
    if mutant in _MUTMUT_CONTROL_IDS:
        return None
    return (
        str(mutmut_report_path(Path(root), mutant)),
        mutmut_report_nonce(run_nonce, mutant),
        _hmac_key(source),
    )


def mutmut_report_path(root: Path, mutant: str) -> Path:
    digest = hashlib.sha256(mutant.encode()).hexdigest()
    return root / (digest + ".json")


def mutmut_report_nonce(run_nonce: str, mutant: str) -> str:
    digest = hashlib.sha256(mutant.encode()).hexdigest()
    return run_nonce + ":" + digest


def _hmac_key(environment: Mapping[str, str] | None = None) -> bytes:
    source = os.environ if environment is None else environment
    value = source.get(_HMAC_KEY_ENVIRONMENT)
    if value is None:
        raise RuntimeError("sentinelPytestHmacKeyInvalid")
    try:
        key = bytes.fromhex(value)
    except ValueError as error:
        raise RuntimeError("sentinelPytestHmacKeyInvalid") from error
    if value != key.hex() or len(key) != 32:
        raise RuntimeError("sentinelPytestHmacKeyInvalid")
    return key


def _failure_location(item, exception: BaseException) -> dict[str, object]:
    root = _item_root(item)
    try:
        frames = traceback.extract_tb(exception.__traceback__)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise RuntimeError("sentinelPytestFailureLocationInvalid") from error
    for frame in reversed(frames):
        try:
            relative = Path(frame.filename).resolve(strict=True).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        column = frame.colno if type(frame.colno) is int else -1
        return {
            "column": column,
            "line": frame.lineno,
            "path": relative.as_posix(),
        }
    raise RuntimeError("sentinelPytestFailureLocationInvalid")


def _item_root(item) -> Path:
    parts = _node_path_parts(item.nodeid)
    item_path = _resolved_item_path(item)
    _require_item_suffix(item_path, parts)
    return item_path.parents[len(parts) - 1]


def _node_path_parts(node_id: str) -> tuple[str, ...]:
    parts = tuple(node_id.partition("::")[0].split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise RuntimeError("sentinelPytestFailureLocationInvalid")
    return parts


def _resolved_item_path(item) -> Path:
    try:
        return Path(item.path).resolve(strict=True)
    except (AttributeError, OSError, RuntimeError, TypeError) as error:
        raise RuntimeError("sentinelPytestFailureLocationInvalid") from error


def _require_item_suffix(item_path: Path, parts: tuple[str, ...]) -> None:
    if len(item_path.parts) < len(parts):
        raise RuntimeError("sentinelPytestFailureLocationInvalid")
    if item_path.parts[-len(parts) :] != parts:
        raise RuntimeError("sentinelPytestFailureLocationInvalid")


def _failure_signature(
    key: bytes,
    assertion: bool,
    type_name: str,
    node_id: str,
    phase: str,
    location: dict[str, object],
) -> str:
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("assertion HMAC key must contain exactly 256 bits")
    payload = json.dumps(
        {
            "assertion": assertion,
            "exceptionType": type_name,
            "location": location,
            "nodeId": node_id,
            "phase": phase,
            "signatureVersion": "sentinel-pytest-failure-v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


def _report_signature(key: bytes, document: dict) -> str:
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("report HMAC key must contain exactly 256 bits")
    payload = json.dumps(
        {
            "observation": document,
            "signatureVersion": "sentinel-pytest-report-v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


def _failure_sort_key(failure: dict) -> tuple[bytes, bytes, bytes, int, int, bytes]:
    return (
        failure["nodeId"].encode(),
        failure["phase"].encode(),
        failure["location"]["path"].encode(),
        failure["location"]["line"],
        failure["location"]["column"],
        failure["signature"].encode(),
    )
