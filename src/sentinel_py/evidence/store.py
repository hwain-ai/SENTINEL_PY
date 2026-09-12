"""Immutable, project-local evidence with keyed defect identities."""

from __future__ import annotations

import base64
import calendar
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence


_STATE_VERSION = "state-v1"
_PROJECT_SCHEMA = "sentinel-project-state-v1"
_EVIDENCE_SCHEMA = "sentinel-evidence-v1"
_STARTED_SCHEMA = "sentinel-run-started-v1"
_EVENT_SCHEMA = "sentinel-finding-event-v1"
_HISTORY_SCHEMA = "sentinel-history-v1"
_SPEC_VERSION = "1.0.0"
_FINGERPRINT_VERSION = "sentinel-fingerprint-v1"
_SEQUENCE_VERSION = "commit-sequence-v1"
_SEQUENCE_KEY_NAMESPACE = b"SENTINEL\0commit-sequence-key\0v1\0"
_SEQUENCE_MAC_NAMESPACE = b"SENTINEL\0commit-sequence\0v1\0"
_EVIDENCE_KEY_NAMESPACE = b"SENTINEL\0evidence-key\0v1\0"
_EVIDENCE_MAC_NAMESPACE = b"SENTINEL\0evidence\0v1\0"
_EVENT_KEY_NAMESPACE = b"SENTINEL\0finding-event-key\0v1\0"
_EVENT_MAC_NAMESPACE = b"SENTINEL\0finding-event\0v1\0"
_STATE_BIND_NAMESPACE = b"SENTINEL\0project-state-binding\0v1\0"
_LOWER_HEX_256 = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE = re.compile(r"[a-z][A-Za-z0-9]{0,63}")
_FINDING_FINGERPRINT = re.compile(r"hmac-sha256:[0-9a-f]{64}")
_EVENT_ID = re.compile(r"[0-9a-f]{32}")
_EVENT_FILENAME = re.compile(r"[0-9a-f]{32}\.json")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{0,8}[1-9])?Z"
)
_UINT64_MAX = (1 << 64) - 1
_JSON_SAFE_INTEGER_MAX = (1 << 53) - 1
_EXIT_CODES = {
    "passed": 0,
    "toolError": 1,
    "qualityFailed": 2,
    "baselineFailed": 4,
    "dependencyError": 5,
    "backendError": 6,
    "evidenceError": 7,
    "cancelled": 8,
}
_MUTATION_COMPONENT_COUNTS = (
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
_EVIDENCE_FIELDS = {
    "certification",
    "command",
    "commitSequence",
    "committedAtUtc",
    "completedAtUtc",
    "components",
    "correlationId",
    "diagnosticCodes",
    "eventCount",
    "events",
    "exitCode",
    "fingerprintVersion",
    "hmacSha256",
    "keyEpoch",
    "language",
    "mode",
    "observationSource",
    "projectStateHmac",
    "runId",
    "schemaVersion",
    "sourceRunId",
    "specVersion",
    "startedAtUtc",
    "startedSha256",
    "terminalStatus",
}


class EvidenceError(RuntimeError):
    """Project evidence could not be committed or read safely."""


@dataclass(frozen=True)
class FindingIdentity:
    """Private values used only to derive one stable project-local fingerprint."""

    category: str
    reason: str
    language: str
    module: str
    module_relative_path: str
    subject_id: str


@dataclass(frozen=True)
class _ProjectKeys:
    project_identifier: bytes
    fingerprint_key: bytes
    cleanup_lease_key: bytes
    key_epoch: int


def commit_quality_evidence(
    project_root: Path,
    run: Mapping[str, object],
    crap_summary: Mapping[str, object],
    findings: Sequence[FindingIdentity],
) -> dict:
    """Commit one completed evidence document without retaining source identities."""

    state_root = project_root / ".sentinel" / _STATE_VERSION
    with _exclusive_state(state_root):
        keys = _project_keys(state_root)
        sequence = _next_sequence(state_root, keys)
        evidence = _commit_completed_run(
            state_root,
            run,
            {"crap": crap_summary},
            findings,
            keys,
            sequence,
        )
    return evidence


def commit_mutation_evidence(
    project_root: Path,
    run: Mapping[str, object],
    mutation_summary: Mapping[str, object],
    findings: Sequence[FindingIdentity],
) -> dict:
    """Commit a redacted mutation result through the same sequence and lock."""

    state_root = project_root / ".sentinel" / _STATE_VERSION
    with _exclusive_state(state_root):
        keys = _project_keys(state_root)
        sequence = _next_sequence(state_root, keys)
        evidence = _commit_completed_run(
            state_root,
            run,
            {"mutation": mutation_summary},
            findings,
            keys,
            sequence,
        )
    return evidence


def commit_check_evidence(
    project_root: Path,
    run: Mapping[str, object],
    crap_summary: Mapping[str, object],
    mutation_summary: Mapping[str, object],
    findings: Sequence[FindingIdentity],
) -> dict:
    """Commit both strict components under one run and sequence."""

    state_root = project_root / ".sentinel" / _STATE_VERSION
    with _exclusive_state(state_root):
        keys = _project_keys(state_root)
        sequence = _next_sequence(state_root, keys)
        components = {"crap": crap_summary, "mutation": mutation_summary}
        evidence = _commit_completed_run(
            state_root,
            run,
            components,
            findings,
            keys,
            sequence,
        )
    return evidence


def read_history(project_root: Path, *, repeated_only: bool) -> dict:
    """Read completed immutable runs without creating project state."""

    state_root = project_root / ".sentinel" / _STATE_VERSION
    if state_root.is_symlink():
        raise EvidenceError("statePathInvalid")
    if not state_root.exists():
        return _history_document(0, ())
    if not state_root.is_dir():
        raise EvidenceError("statePathInvalid")
    with _shared_state(state_root):
        return _read_history_state(state_root, repeated_only)


def _read_history_state(state_root: Path, repeated_only: bool) -> dict:
    runs_root = state_root / "runs"
    if runs_root.is_symlink():
        raise EvidenceError("runStateInvalid")
    if not runs_root.exists():
        return _history_document(0, ())
    if not runs_root.is_dir():
        raise EvidenceError("runStateInvalid")
    keys = _read_project_keys(state_root / "project.json")
    evidence = tuple(
        _read_evidence(path, keys) for path in _evidence_paths(runs_root)
    )
    _validate_sequence_state(state_root, evidence, keys.cleanup_lease_key)
    findings = _history_findings(evidence, repeated_only=repeated_only)
    return _history_document(len(evidence), findings)


@contextmanager
def _shared_state(state_root: Path) -> Iterator[None]:
    lock_path = state_root / "lock"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags)
        _require_regular_file(descriptor)
        fcntl.lockf(descriptor, fcntl.LOCK_SH, 1, 0, os.SEEK_SET)
    except (OSError, EvidenceError) as error:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("evidenceLockFailed") from error
    try:
        yield
    finally:
        fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
        os.close(descriptor)


@contextmanager
def _exclusive_state(state_root: Path) -> Iterator[None]:
    _private_directory(state_root.parent)
    _private_directory(state_root)
    lock_path = state_root / "lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        _require_private_regular_file(descriptor)
        fcntl.lockf(descriptor, fcntl.LOCK_EX, 1, 0, os.SEEK_SET)
    except (OSError, EvidenceError) as error:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("evidenceLockFailed") from error
    try:
        yield
    finally:
        fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
        os.close(descriptor)


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise EvidenceError("statePathInvalid")
        path.chmod(0o700)
    except OSError as error:
        raise EvidenceError("stateDirectoryFailed") from error


def _require_private_regular_file(descriptor: int) -> None:
    _require_regular_file(descriptor)
    os.fchmod(descriptor, 0o600)


def _require_regular_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise EvidenceError("stateFileInvalid")


def _project_keys(state_root: Path) -> _ProjectKeys:
    path = state_root / "project.json"
    if not path.exists():
        _atomic_json(path, _new_project_document())
    return _read_project_keys(path)


def _new_project_document() -> dict:
    project_identifier = os.urandom(16)
    fingerprint_key = os.urandom(32)
    cleanup_lease_key = os.urandom(32)
    if fingerprint_key == cleanup_lease_key:
        raise EvidenceError("projectStateInvalid")
    return {
        "cleanupLeaseKey": _encode_base64url(cleanup_lease_key),
        "fingerprintHmacKey": _encode_base64url(fingerprint_key),
        "keyEpoch": 1,
        "projectIdentifier": _encode_base64url(project_identifier),
        "schemaVersion": _PROJECT_SCHEMA,
        "stateVersion": _STATE_VERSION,
    }


def _read_project_keys(path: Path) -> _ProjectKeys:
    document = _read_json(path, "projectStateInvalid")
    required = {
        "cleanupLeaseKey",
        "fingerprintHmacKey",
        "keyEpoch",
        "projectIdentifier",
        "schemaVersion",
        "stateVersion",
    }
    if set(document) != required:
        raise EvidenceError("projectStateInvalid")
    if (
        document["schemaVersion"] != _PROJECT_SCHEMA
        or document["stateVersion"] != _STATE_VERSION
        or type(document["keyEpoch"]) is not int
        or not 1 <= document["keyEpoch"] <= _JSON_SAFE_INTEGER_MAX
    ):
        raise EvidenceError("projectStateInvalid")
    keys = _ProjectKeys(
        project_identifier=_decode_base64url(document["projectIdentifier"], 16),
        fingerprint_key=_decode_base64url(document["fingerprintHmacKey"], 32),
        cleanup_lease_key=_decode_base64url(document["cleanupLeaseKey"], 32),
        key_epoch=document["keyEpoch"],
    )
    if keys.fingerprint_key == keys.cleanup_lease_key:
        raise EvidenceError("projectStateInvalid")
    return keys


def _encode_base64url(value: bytes) -> str:
    encoded = base64.urlsafe_b64encode(value).decode()
    return encoded.removesuffix("==").removesuffix("=")


def _decode_base64url(value: object, size: int) -> bytes:
    if not isinstance(value, str):
        raise EvidenceError("projectStateInvalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError) as error:
        raise EvidenceError("projectStateInvalid") from error
    if len(decoded) != size or _encode_base64url(decoded) != value:
        raise EvidenceError("projectStateInvalid")
    return decoded


def _next_sequence(state_root: Path, keys: _ProjectKeys) -> int:
    path = state_root / "commit-sequence.json"
    if path.exists():
        current = _read_sequence(path, keys.cleanup_lease_key)
    else:
        current = 0
    high_water = _retained_sequence_high_water(state_root, keys)
    if current < high_water or current == _UINT64_MAX:
        raise EvidenceError("sequenceStateInvalid")
    allocated = current + 1
    _atomic_json(path, _sequence_document(allocated, keys.cleanup_lease_key))
    return allocated


def _read_sequence(path: Path, cleanup_key: bytes) -> int:
    document = _read_json(path, "sequenceStateInvalid")
    if set(document) != {"hmacSha256", "lastAllocated", "version"}:
        raise EvidenceError("sequenceStateInvalid")
    value = document["lastAllocated"]
    if document["version"] != _SEQUENCE_VERSION or not _canonical_uint64(value):
        raise EvidenceError("sequenceStateInvalid")
    body = {"lastAllocated": value, "version": _SEQUENCE_VERSION}
    expected = _namespaced_document_hmac(
        cleanup_key,
        _SEQUENCE_KEY_NAMESPACE,
        _SEQUENCE_MAC_NAMESPACE,
        body,
    )
    if not _valid_hmac(document["hmacSha256"], expected):
        raise EvidenceError("sequenceStateInvalid")
    return int(value)


def _sequence_document(value: int, cleanup_key: bytes) -> dict:
    body = {"lastAllocated": str(value), "version": _SEQUENCE_VERSION}
    return {
        "hmacSha256": _namespaced_document_hmac(
            cleanup_key,
            _SEQUENCE_KEY_NAMESPACE,
            _SEQUENCE_MAC_NAMESPACE,
            body,
        ),
        **body,
    }


def _canonical_uint64(value: object) -> bool:
    if not isinstance(value, str) or not value or value[0] == "0":
        return False
    if not value.isascii() or not value.isdecimal():
        return False
    return int(value) <= _UINT64_MAX


def _retained_sequence_high_water(state_root: Path, keys: _ProjectKeys) -> int:
    runs_root = state_root / "runs"
    if not runs_root.is_dir():
        return 0
    evidence = tuple(
        _read_evidence(path, keys) for path in _evidence_paths(runs_root)
    )
    return _sequence_high_water(evidence)


def _validate_sequence_state(
    state_root: Path,
    evidence: Sequence[dict],
    cleanup_key: bytes,
) -> None:
    path = state_root / "commit-sequence.json"
    if not path.is_file():
        if evidence:
            raise EvidenceError("sequenceStateInvalid")
        return
    allocated = _read_sequence(path, cleanup_key)
    sequences = tuple(_evidence_sequence(document) for document in evidence)
    if len(sequences) != len(set(sequences)) or allocated < max(sequences, default=0):
        raise EvidenceError("sequenceStateInvalid")


def _sequence_high_water(evidence: Sequence[dict]) -> int:
    sequences = tuple(_evidence_sequence(document) for document in evidence)
    if len(sequences) != len(set(sequences)):
        raise EvidenceError("sequenceStateInvalid")
    return max(sequences, default=0)


def _evidence_sequence(document: dict) -> int:
    sequence = document.get("commitSequence")
    run_id = document.get("runId")
    if not _canonical_uint64(sequence):
        raise EvidenceError("evidenceInvalid")
    if not _canonical_uuid(run_id):
        raise EvidenceError("evidenceInvalid")
    return int(sequence)


def _commit_completed_run(
    state_root: Path,
    run: Mapping[str, object],
    components: Mapping[str, Mapping[str, object]],
    findings: Sequence[FindingIdentity],
    keys: _ProjectKeys,
    sequence: int,
) -> dict:
    run_values = _validated_run_values(run)
    run_root = _new_run_root(state_root, run_values["runId"])
    started = _started_document(run_values)
    started_payload = _canonical_json(started) + b"\n"
    _atomic_bytes(run_root / "started.json", started_payload)
    public_findings = _public_findings(findings, keys.fingerprint_key)
    event_manifest = _commit_events(
        run_root,
        run_values,
        public_findings,
        keys,
        sequence,
    )
    evidence = _evidence_document(
        run_values,
        components,
        public_findings,
        keys,
        sequence,
        hashlib.sha256(started_payload).hexdigest(),
        event_manifest,
    )
    _validate_evidence_document(evidence, keys)
    _atomic_json(run_root / "evidence.json", evidence)
    return evidence


def _validated_run_values(run: Mapping[str, object]) -> dict:
    completed_at = run.get("completedAtUtc", run.get("completedAt"))
    started_at = run.get("startedAtUtc", run.get("startedAt", completed_at))
    values = {
        "command": run.get("command"),
        "completedAtUtc": completed_at,
        "correlationId": run.get("correlationId"),
        "mode": run.get("mode"),
        "observationSource": run.get("observationSource", "fresh"),
        "runId": run.get("runId"),
        "startedAtUtc": started_at,
        "terminalStatus": run.get("terminalStatus"),
    }
    if not _valid_run_values(values):
        raise EvidenceError("runIdentityInvalid")
    return values


def _valid_run_values(values: Mapping[str, object]) -> bool:
    return (
        _valid_run_kind(values)
        and _valid_run_status(values)
        and _valid_run_identity(values)
        and _valid_run_times(values)
    )


def _valid_run_kind(values: Mapping[str, object]) -> bool:
    return (
        values["command"] in ("crap", "mutation", "check")
        and values["mode"] in ("local", "strict")
        and values["observationSource"] == "fresh"
    )


def _valid_run_status(values: Mapping[str, object]) -> bool:
    status = values["terminalStatus"]
    return isinstance(status, str) and status in _EXIT_CODES


def _valid_run_identity(values: Mapping[str, object]) -> bool:
    return _canonical_uuid(values["runId"]) and _canonical_uuid(
        values["correlationId"]
    )


def _valid_run_times(values: Mapping[str, object]) -> bool:
    return _valid_utc(values["startedAtUtc"]) and _valid_utc(
        values["completedAtUtc"]
    )


def _started_document(run: Mapping[str, object]) -> dict:
    return {
        "command": run["command"],
        "correlationId": run["correlationId"],
        "language": "python",
        "mode": run["mode"],
        "runId": run["runId"],
        "schemaVersion": _STARTED_SCHEMA,
        "specVersion": _SPEC_VERSION,
        "startedAtUtc": run["startedAtUtc"],
    }


def _new_run_root(state_root: Path, run_id: object) -> Path:
    if not _canonical_uuid(run_id):
        raise EvidenceError("runIdentityInvalid")
    runs_root = state_root / "runs"
    _private_directory(runs_root)
    run_root = runs_root / run_id
    try:
        run_root.mkdir(mode=0o700)
    except OSError as error:
        raise EvidenceError("runAlreadyExists") from error
    return run_root


def _public_findings(
    findings: Sequence[FindingIdentity],
    secret: bytes,
) -> tuple[dict, ...]:
    values = tuple(_public_finding(item, secret) for item in findings)
    unique = _unique_run_findings(values)
    return tuple(sorted(unique, key=_finding_fingerprint_sort_key))


def _finding_fingerprint_sort_key(finding: dict) -> bytes:
    return finding["fingerprint"].encode()


def _commit_events(
    run_root: Path,
    run: Mapping[str, object],
    findings: Sequence[dict],
    keys: _ProjectKeys,
    sequence: int,
) -> tuple[dict, ...]:
    events_root = run_root / "events"
    _private_directory(events_root)
    manifest = []
    for ordinal, finding in enumerate(findings, start=1):
        event_id = f"{ordinal:032x}"
        event = _event_document(event_id, run, finding, keys, sequence)
        payload = _canonical_json(event) + b"\n"
        filename = event_id + ".json"
        _atomic_bytes(events_root / filename, payload)
        manifest.append(
            {"filename": filename, "sha256": hashlib.sha256(payload).hexdigest()}
        )
    _sync_directory(events_root)
    return tuple(manifest)


def _event_document(
    event_id: str,
    run: Mapping[str, object],
    finding: Mapping[str, object],
    keys: _ProjectKeys,
    sequence: int,
) -> dict:
    token = finding["fingerprint"].removeprefix("hmac-sha256:")
    body = {
        "commitSequence": str(sequence),
        "defectKind": finding["category"],
        "diagnosticCode": finding["reason"],
        "event": "detected",
        "eventId": event_id,
        "findingClass": _finding_class(finding),
        "findingToken": token,
        "fingerprintKind": "occurrence",
        "fingerprintVersion": _FINGERPRINT_VERSION,
        "keyEpoch": keys.key_epoch,
        "observationSource": "fresh",
        "observedAtUtc": run["completedAtUtc"],
        "runId": run["runId"],
        "schemaVersion": _EVENT_SCHEMA,
        "value": token,
    }
    return {
        **body,
        "hmacSha256": _namespaced_document_hmac(
            keys.cleanup_lease_key,
            _EVENT_KEY_NAMESPACE,
            _EVENT_MAC_NAMESPACE,
            body,
        ),
    }


def _finding_class(finding: Mapping[str, object]) -> str:
    if finding["category"] == "crap":
        return "projectCode"
    if finding["reason"] in ("timedOut", "runtimeError"):
        return "projectCodeOrTest"
    return "projectTest"


def _evidence_document(
    run: Mapping[str, object],
    components: Mapping[str, Mapping[str, object]],
    findings: Sequence[dict],
    keys: _ProjectKeys,
    sequence: int,
    started_sha256: str,
    event_manifest: Sequence[dict],
) -> dict:
    normalized_components = _component_document(components)
    body = {
        "certification": _is_certified(run, normalized_components),
        "command": run["command"],
        "commitSequence": str(sequence),
        "committedAtUtc": _utc_now(),
        "completedAtUtc": run["completedAtUtc"],
        "components": normalized_components,
        "correlationId": run["correlationId"],
        "diagnosticCodes": _diagnostic_codes(components, findings),
        "eventCount": len(event_manifest),
        "events": list(event_manifest),
        "exitCode": _EXIT_CODES[run["terminalStatus"]],
        "fingerprintVersion": _FINGERPRINT_VERSION,
        "keyEpoch": keys.key_epoch,
        "language": "python",
        "mode": run["mode"],
        "observationSource": run["observationSource"],
        "projectStateHmac": _project_state_hmac(keys),
        "runId": run["runId"],
        "schemaVersion": _EVIDENCE_SCHEMA,
        "sourceRunId": None,
        "specVersion": _SPEC_VERSION,
        "startedAtUtc": run["startedAtUtc"],
        "startedSha256": started_sha256,
        "terminalStatus": run["terminalStatus"],
    }
    return {
        **body,
        "hmacSha256": _namespaced_document_hmac(
            keys.cleanup_lease_key,
            _EVIDENCE_KEY_NAMESPACE,
            _EVIDENCE_MAC_NAMESPACE,
            body,
        ),
    }


def _component_document(
    components: Mapping[str, Mapping[str, object]],
) -> dict:
    result = {}
    if "crap" in components:
        result["crap"] = _crap_component(components["crap"])
    if "mutation" in components:
        result["mutation"] = _mutation_component(components["mutation"])
    if set(result) not in ({"crap"}, {"mutation"}, {"crap", "mutation"}):
        raise EvidenceError("evidenceInvalid")
    return result


def _crap_component(summary: Mapping[str, object]) -> dict:
    numerator = summary.get("maxNumerator")
    denominator = summary.get("maxDenominator")
    return {
        "callableCount": _safe_uint(summary.get("callableCount")),
        "maxDenominator": _positive_decimal(denominator or "1"),
        "maxNumerator": _nonnegative_decimal(numerator or "0"),
        "pass": _boolean(summary.get("pass")),
        "unknownCount": _safe_uint(summary.get("unknownCount")),
    }


def _mutation_component(summary: Mapping[str, object]) -> dict:
    counts = summary.get("counts")
    if not isinstance(counts, Mapping):
        raise EvidenceError("evidenceInvalid")
    result = {name: _safe_uint(counts.get(name)) for name in _MUTATION_COMPONENT_COUNTS}
    result.update(
        {
            "inScope": _safe_uint(summary.get("inScope")),
            "pass": _boolean(summary.get("pass")),
            "unauthorizedExclusion": _safe_uint(
                summary.get("unauthorizedExclusion", 0)
            ),
        }
    )
    return result


def _diagnostic_codes(
    components: Mapping[str, Mapping[str, object]],
    findings: Sequence[dict],
) -> list[str]:
    values = {finding["reason"] for finding in findings}
    values.update(
        summary.get("reason")
        for summary in components.values()
        if summary.get("pass") is False
    )
    if not all(_valid_safe_code(value) for value in values):
        raise EvidenceError("evidenceInvalid")
    return sorted(values)


def _is_certified(run: Mapping[str, object], components: Mapping[str, dict]) -> bool:
    return (
        run["mode"] == "strict"
        and run["observationSource"] == "fresh"
        and run["terminalStatus"] == "passed"
        and all(component["pass"] for component in components.values())
    )


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise EvidenceError("evidenceInvalid")
    return value


def _safe_uint(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _JSON_SAFE_INTEGER_MAX:
        raise EvidenceError("evidenceInvalid")
    return value


def _nonnegative_decimal(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise EvidenceError("evidenceInvalid")
    if len(value) > 96 or (len(value) > 1 and value[0] == "0"):
        raise EvidenceError("evidenceInvalid")
    return value


def _positive_decimal(value: object) -> str:
    result = _nonnegative_decimal(value)
    if result == "0" or len(result) > 48:
        raise EvidenceError("evidenceInvalid")
    return result


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _valid_utc(value: object) -> bool:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return _format_utc(datetime.now(timezone.utc))


def _format_utc(value: datetime) -> str:
    prefix = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    fraction = re.sub("0+$", "", f"{value.microsecond:06d}")
    return prefix + (("." + fraction) if fraction else "") + "Z"


def _public_finding(finding: FindingIdentity, secret: bytes) -> dict:
    payload = "\0".join(
        (
            finding.category,
            finding.reason,
            finding.language,
            finding.module,
            finding.module_relative_path,
            finding.subject_id,
        )
    ).encode()
    fingerprint = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return {
        "category": finding.category,
        "fingerprint": "hmac-sha256:" + fingerprint,
        "reason": finding.reason,
    }


def _project_state_hmac(keys: _ProjectKeys) -> str:
    payload = _STATE_BIND_NAMESPACE + keys.project_identifier
    return hmac.new(keys.cleanup_lease_key, payload, hashlib.sha256).hexdigest()


def _namespaced_document_hmac(
    key: bytes,
    key_namespace: bytes,
    mac_namespace: bytes,
    document: Mapping[str, object],
) -> str:
    derived_key = hmac.new(key, key_namespace, hashlib.sha256).digest()
    return hmac.new(
        derived_key,
        mac_namespace + _canonical_json(document),
        hashlib.sha256,
    ).hexdigest()


def _valid_hmac(actual: object, expected: str) -> bool:
    return (
        isinstance(actual, str)
        and _LOWER_HEX_256.fullmatch(actual) is not None
        and hmac.compare_digest(actual, expected)
    )


def _atomic_json(path: Path, document: Mapping[str, object]) -> None:
    _atomic_bytes(path, _canonical_json(document) + b"\n")


def _canonical_json(document: Mapping[str, object]) -> bytes:
    return _canonical_value(document)


def _canonical_value(value: object) -> bytes:
    if value is None:
        return b"null"
    if type(value) is bool:
        return b"true" if value else b"false"
    if type(value) is int:
        return _canonical_integer(value)
    if isinstance(value, str):
        return _canonical_string(value)
    if isinstance(value, list):
        return _canonical_array(value)
    if isinstance(value, Mapping):
        return _canonical_object(value)
    raise EvidenceError("canonicalJsonTypeInvalid")


def _canonical_integer(value: int) -> bytes:
    if not 0 <= value <= _JSON_SAFE_INTEGER_MAX:
        raise EvidenceError("canonicalJsonIntegerOutOfRange")
    return str(value).encode()


def _canonical_string(value: str) -> bytes:
    encoded = bytearray(b'"')
    for character in value:
        encoded.extend(_canonical_character(character))
    encoded.extend(b'"')
    return bytes(encoded)


def _canonical_character(character: str) -> bytes:
    codepoint = ord(character)
    if character == '"':
        return b'\\"'
    if character == "\\":
        return b"\\\\"
    if codepoint < 0x20:
        return f"\\u{codepoint:04x}".encode()
    if 0xD800 <= codepoint <= 0xDFFF:
        raise EvidenceError("canonicalJsonUnicodeScalarInvalid")
    return character.encode()


def _canonical_array(values: Sequence[object]) -> bytes:
    return b"[" + b",".join(_canonical_value(value) for value in values) + b"]"


def _canonical_object(document: Mapping[str, object]) -> bytes:
    if not all(isinstance(name, str) for name in document):
        raise EvidenceError("canonicalJsonKeyInvalid")
    names = sorted(document)
    fields = (
        _canonical_string(name) + b":" + _canonical_value(document[name])
        for name in names
    )
    return b"{" + b",".join(fields) + b"}"


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        _require_private_regular_file(descriptor)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except (OSError, EvidenceError) as error:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("evidenceCommitFailed") from error


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _evidence_paths(runs_root: Path) -> tuple[Path, ...]:
    paths = []
    for run_root in runs_root.iterdir():
        path = run_root / "evidence.json"
        if run_root.is_symlink() or not run_root.is_dir():
            raise EvidenceError("runStateInvalid")
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return tuple(sorted(paths))


def _read_evidence(path: Path, keys: _ProjectKeys) -> dict:
    document = _read_json(path, "evidenceInvalid")
    _validate_evidence_document(document, keys)
    _validate_run_path(path, document)
    findings = _verify_run_bundle(path, document, keys)
    return {**document, "_eventFindings": findings}


def _validate_evidence_document(document: dict, keys: _ProjectKeys) -> None:
    _evidence_components(document)
    _validate_evidence_metadata(document, keys)
    _validate_evidence_values(document)
    _verify_evidence_hmac(document, keys.cleanup_lease_key)


def _evidence_components(document: dict) -> set[str]:
    if set(document) != _EVIDENCE_FIELDS:
        raise EvidenceError("evidenceInvalid")
    components = document["components"]
    if not isinstance(components, dict):
        raise EvidenceError("evidenceInvalid")
    names = set(components)
    expected = _expected_component_names(document["command"])
    if names != expected:
        raise EvidenceError("evidenceInvalid")
    _validate_component_values(components)
    return names


def _expected_component_names(command: object) -> set[str]:
    if command == "check":
        return {"crap", "mutation"}
    if command in ("crap", "mutation"):
        return {command}
    raise EvidenceError("evidenceInvalid")


def _validate_evidence_metadata(document: dict, keys: _ProjectKeys) -> None:
    if (
        document["schemaVersion"] != _EVIDENCE_SCHEMA
        or document["specVersion"] != _SPEC_VERSION
        or document["fingerprintVersion"] != _FINGERPRINT_VERSION
        or document["language"] != "python"
        or type(document["keyEpoch"]) is not int
        or not 1 <= document["keyEpoch"] <= keys.key_epoch
        or document["projectStateHmac"] != _project_state_hmac(keys)
    ):
        raise EvidenceError("evidenceInvalid")


def _validate_evidence_values(document: dict) -> None:
    if not _valid_evidence_identity(document):
        raise EvidenceError("evidenceInvalid")
    if not _valid_evidence_result(document):
        raise EvidenceError("evidenceInvalid")
    if not _valid_evidence_lists(document):
        raise EvidenceError("evidenceInvalid")
    if not _valid_time_order(document):
        raise EvidenceError("evidenceInvalid")
    if not _valid_terminal_semantics(document):
        raise EvidenceError("evidenceInvalid")
    expected_certification = _is_certified(document, document["components"])
    if document["certification"] is not expected_certification:
        raise EvidenceError("evidenceInvalid")


def _valid_evidence_identity(document: Mapping[str, object]) -> bool:
    return (
        document["command"] in ("crap", "mutation", "check")
        and document["mode"] in ("local", "strict")
        and document["observationSource"] in ("fresh", "cache")
        and _valid_source_run(document)
        and _canonical_uuid(document["runId"])
        and _canonical_uuid(document["correlationId"])
        and _canonical_uint64(document["commitSequence"])
    )


def _valid_source_run(document: Mapping[str, object]) -> bool:
    if document["observationSource"] == "fresh":
        return document["sourceRunId"] is None
    return (
        document["mode"] == "local"
        and _canonical_uuid(document["sourceRunId"])
        and document["sourceRunId"] != document["runId"]
    )


def _valid_evidence_result(document: Mapping[str, object]) -> bool:
    status = document["terminalStatus"]
    return (
        isinstance(status, str)
        and status in _EXIT_CODES
        and document["exitCode"] == _EXIT_CODES.get(status)
        and _valid_utc(document["startedAtUtc"])
        and _valid_utc(document["completedAtUtc"])
        and _valid_utc(document["committedAtUtc"])
        and _valid_sha256(document["startedSha256"])
    )


def _valid_evidence_lists(document: Mapping[str, object]) -> bool:
    codes = document["diagnosticCodes"]
    if not isinstance(codes, list) or not all(_valid_safe_code(code) for code in codes):
        return False
    return codes == sorted(set(codes)) and _valid_event_list(
        document
    )


def _valid_event_list(document: Mapping[str, object]) -> bool:
    events = document["events"]
    if not isinstance(events, list) or type(document["eventCount"]) is not int:
        return False
    return document["eventCount"] == len(events) and not (
        document["observationSource"] == "cache" and bool(events)
    )


def _valid_time_order(document: Mapping[str, object]) -> bool:
    started = _timestamp_key(document["startedAtUtc"])
    completed = _timestamp_key(document["completedAtUtc"])
    committed = _timestamp_key(document["committedAtUtc"])
    return started <= completed <= committed


def _timestamp_key(value: object) -> tuple[int, int]:
    if not _valid_utc(value):
        raise EvidenceError("evidenceInvalid")
    without_zone = value[:-1]
    base, separator, fraction = without_zone.partition(".")
    instant = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    nanoseconds = int(fraction.ljust(9, "0")) if separator else 0
    return calendar.timegm(instant.timetuple()), nanoseconds


def _valid_terminal_semantics(document: Mapping[str, object]) -> bool:
    status = document["terminalStatus"]
    components = document["components"]
    components_pass = all(component["pass"] for component in components.values())
    if _mutation_tool_error(components) and status != "backendError":
        return False
    if status == "passed":
        return components_pass
    if status == "qualityFailed":
        return not components_pass
    return True


def _mutation_tool_error(components: Mapping[str, object]) -> bool:
    mutation = components.get("mutation")
    return isinstance(mutation, Mapping) and mutation.get("toolError", 0) > 0


def _validate_component_values(components: Mapping[str, object]) -> None:
    if "crap" in components:
        _validate_crap_component(components["crap"])
    if "mutation" in components:
        _validate_mutation_component(components["mutation"])


def _validate_crap_component(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "callableCount",
        "maxDenominator",
        "maxNumerator",
        "pass",
        "unknownCount",
    }:
        raise EvidenceError("evidenceInvalid")
    expected = _crap_component(value)
    if expected != value or not _valid_crap_semantics(value):
        raise EvidenceError("evidenceInvalid")


def _valid_crap_semantics(value: Mapping[str, object]) -> bool:
    numerator = int(value["maxNumerator"])
    denominator = int(value["maxDenominator"])
    no_known_callables = value["callableCount"] == value["unknownCount"]
    zero_maximum = numerator == 0 and denominator == 1
    expected_pass = (
        value["callableCount"] > 0
        and value["unknownCount"] == 0
        and numerator <= 8 * denominator
    )
    return (
        value["unknownCount"] <= value["callableCount"]
        and no_known_callables is zero_maximum
        and math.gcd(numerator, denominator) == 1
        and value["pass"] is expected_pass
    )


def _validate_mutation_component(value: object) -> None:
    fields = {
        *_MUTATION_COMPONENT_COUNTS,
        "inScope",
        "pass",
        "unauthorizedExclusion",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise EvidenceError("evidenceInvalid")
    for name in (*_MUTATION_COMPONENT_COUNTS, "inScope", "unauthorizedExclusion"):
        _safe_uint(value[name])
    _boolean(value["pass"])
    if not _valid_mutation_semantics(value):
        raise EvidenceError("evidenceInvalid")


def _valid_mutation_semantics(value: Mapping[str, object]) -> bool:
    state_total = sum(value[name] for name in _MUTATION_COMPONENT_COUNTS)
    expected_pass = (
        value["inScope"] > 0
        and value["killed"] == value["inScope"]
        and value["unauthorizedExclusion"] == 0
    )
    return state_total == value["inScope"] and value["pass"] is expected_pass


def _validate_run_path(path: Path, document: dict) -> None:
    if document["runId"] != path.parent.name:
        raise EvidenceError("evidenceInvalid")


def _verify_evidence_hmac(document: dict, cleanup_key: bytes) -> None:
    body = {name: value for name, value in document.items() if name != "hmacSha256"}
    expected = _namespaced_document_hmac(
        cleanup_key,
        _EVIDENCE_KEY_NAMESPACE,
        _EVIDENCE_MAC_NAMESPACE,
        body,
    )
    if not _valid_hmac(document["hmacSha256"], expected):
        raise EvidenceError("evidenceInvalid")


def _verify_run_bundle(
    evidence_path: Path,
    document: dict,
    keys: _ProjectKeys,
) -> tuple[dict, ...]:
    run_root = evidence_path.parent
    _verify_started(run_root / "started.json", document)
    return _verify_events(run_root / "events", document, keys)


def _verify_started(path: Path, evidence: dict) -> None:
    payload = _safe_read(path)
    if hashlib.sha256(payload).hexdigest() != evidence["startedSha256"]:
        raise EvidenceError("evidenceInvalid")
    started = _read_json(path, "evidenceInvalid")
    expected = _started_document(evidence)
    if started != expected:
        raise EvidenceError("evidenceInvalid")


def _verify_events(
    events_root: Path,
    evidence: dict,
    keys: _ProjectKeys,
) -> tuple[dict, ...]:
    manifest = evidence["events"]
    if events_root.is_symlink() or not events_root.is_dir():
        raise EvidenceError("evidenceInvalid")
    if tuple(entry.get("filename") for entry in manifest) != _expected_event_names(
        len(manifest)
    ):
        raise EvidenceError("evidenceInvalid")
    actual_names = tuple(sorted(path.name for path in events_root.iterdir()))
    if actual_names != _expected_event_names(len(manifest)):
        raise EvidenceError("evidenceInvalid")
    return tuple(
        _verify_event(events_root / entry["filename"], entry, evidence, keys)
        for entry in manifest
    )


def _expected_event_names(count: int) -> tuple[str, ...]:
    return tuple(f"{ordinal:032x}.json" for ordinal in range(1, count + 1))


def _verify_event(
    path: Path,
    manifest: object,
    evidence: dict,
    keys: _ProjectKeys,
) -> dict:
    if not _valid_manifest_entry(manifest):
        raise EvidenceError("evidenceInvalid")
    payload = _safe_read(path)
    if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
        raise EvidenceError("evidenceInvalid")
    event = _read_json(path, "evidenceInvalid")
    _validate_event(event, path, evidence, keys)
    return {
        "category": event["defectKind"],
        "fingerprint": "hmac-sha256:" + event["findingToken"],
        "reason": event["diagnosticCode"],
    }


def _valid_manifest_entry(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"filename", "sha256"}
        and isinstance(value["filename"], str)
        and _EVENT_FILENAME.fullmatch(value["filename"]) is not None
        and _valid_sha256(value["sha256"])
    )


def _validate_event(
    event: dict,
    path: Path,
    evidence: dict,
    keys: _ProjectKeys,
) -> None:
    body = {name: value for name, value in event.items() if name != "hmacSha256"}
    expected_hmac = _namespaced_document_hmac(
        keys.cleanup_lease_key,
        _EVENT_KEY_NAMESPACE,
        _EVENT_MAC_NAMESPACE,
        body,
    )
    if not _valid_event_fields(event) or not _valid_hmac(event.get("hmacSha256"), expected_hmac):
        raise EvidenceError("evidenceInvalid")
    if not _event_joins_evidence(event, path, evidence):
        raise EvidenceError("evidenceInvalid")


def _valid_event_fields(event: Mapping[str, object]) -> bool:
    fields = {
        "commitSequence",
        "defectKind",
        "diagnosticCode",
        "event",
        "eventId",
        "findingClass",
        "findingToken",
        "fingerprintKind",
        "fingerprintVersion",
        "hmacSha256",
        "keyEpoch",
        "observationSource",
        "observedAtUtc",
        "runId",
        "schemaVersion",
        "value",
    }
    return set(event) == fields and _valid_event_values(event)


def _valid_event_values(event: Mapping[str, object]) -> bool:
    return (
        _valid_event_contract(event)
        and _valid_event_classification(event)
        and _valid_event_fingerprints(event)
        and _valid_utc(event["observedAtUtc"])
    )


def _valid_event_contract(event: Mapping[str, object]) -> bool:
    return (
        event["schemaVersion"] == _EVENT_SCHEMA
        and event["event"] in ("detected", "persisted", "resolved", "reopened")
        and event["fingerprintVersion"] == _FINGERPRINT_VERSION
        and event["observationSource"] == "fresh"
    )


def _valid_event_classification(event: Mapping[str, object]) -> bool:
    return (
        event["findingClass"]
        in (
            "projectCode",
            "projectTest",
            "projectCodeOrTest",
            "backend",
            "sentinel",
            "environment",
        )
        and _valid_safe_code(event["defectKind"])
        and _valid_safe_code(event["diagnosticCode"])
    )


def _valid_event_fingerprints(event: Mapping[str, object]) -> bool:
    return (
        event["fingerprintKind"] in ("occurrence", "context", "family")
        and _valid_sha256(event["findingToken"])
        and _valid_sha256(event["value"])
    )


def _event_joins_evidence(
    event: Mapping[str, object],
    path: Path,
    evidence: Mapping[str, object],
) -> bool:
    return (
        event["eventId"] == path.stem
        and _EVENT_ID.fullmatch(event["eventId"]) is not None
        and event["runId"] == evidence["runId"]
        and event["commitSequence"] == evidence["commitSequence"]
        and event["observedAtUtc"] == evidence["completedAtUtc"]
        and event["keyEpoch"] == evidence["keyEpoch"]
        and event["findingToken"] == event["value"]
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _LOWER_HEX_256.fullmatch(value) is not None


def _read_json(path: Path, code: str) -> dict:
    try:
        payload = _safe_read(path)
        document = json.loads(
            payload.decode(),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, EvidenceError) as error:
        raise EvidenceError(code) from error
    if not isinstance(document, dict):
        raise EvidenceError(code)
    try:
        canonical = _canonical_json(document) + b"\n"
    except EvidenceError as error:
        raise EvidenceError(code) from error
    if payload != canonical:
        raise EvidenceError(code)
    return document


def _unique_json_object(pairs: Sequence[tuple[str, object]]) -> dict:
    document = {}
    for name, value in pairs:
        if name in document:
            raise EvidenceError("jsonDuplicateKey")
        document[name] = value
    return document


def _reject_json_constant(_value: str) -> None:
    raise EvidenceError("jsonConstantInvalid")


def _safe_read(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        _require_regular_file(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _history_findings(evidence: Sequence[dict], *, repeated_only: bool) -> tuple[dict, ...]:
    observations: dict[str, dict] = {}
    for document in evidence:
        _add_run_observations(observations, document)
    rows = tuple(_history_row(item) for item in observations.values())
    selected = (row for row in rows if not repeated_only or row["repeated"])
    return tuple(sorted(selected, key=_history_finding_sort_key))


def _history_finding_sort_key(row: dict) -> bytes:
    return row["fingerprint"].encode()


def _add_run_observations(observations: dict[str, dict], document: dict) -> None:
    run_id = document.get("runId")
    sequence_value = document.get("commitSequence")
    if not _canonical_uuid(run_id) or not _canonical_uint64(sequence_value):
        raise EvidenceError("evidenceInvalid")
    sequence = int(sequence_value)
    for finding in _unique_run_findings(document.get("_eventFindings", ())):
        fingerprint = finding["fingerprint"]
        bucket = observations.setdefault(
            fingerprint,
            {"finding": finding, "runs": set(), "sequences": []},
        )
        bucket["runs"].add(run_id)
        bucket["sequences"].append(sequence)


def _unique_run_findings(findings: Sequence[object]) -> tuple[dict, ...]:
    unique = {}
    for value in findings:
        finding = _validated_finding(value)
        fingerprint = finding["fingerprint"]
        if fingerprint in unique and unique[fingerprint] != finding:
            raise EvidenceError("evidenceInvalid")
        unique[fingerprint] = finding
    return tuple(unique.values())


def _validated_finding(value: object) -> dict:
    if not isinstance(value, dict):
        raise EvidenceError("evidenceInvalid")
    if set(value) != {"category", "fingerprint", "reason"}:
        raise EvidenceError("evidenceInvalid")
    if not _valid_safe_code(value["category"]) or not _valid_safe_code(value["reason"]):
        raise EvidenceError("evidenceInvalid")
    fingerprint = value["fingerprint"]
    if not isinstance(fingerprint, str) or _FINDING_FINGERPRINT.fullmatch(fingerprint) is None:
        raise EvidenceError("evidenceInvalid")
    return value


def _valid_safe_code(value: object) -> bool:
    return isinstance(value, str) and _SAFE_CODE.fullmatch(value) is not None


def _history_row(bucket: dict) -> dict:
    finding = bucket["finding"]
    count = len(bucket["runs"])
    return {
        **finding,
        "firstSequence": min(bucket["sequences"]),
        "lastSequence": max(bucket["sequences"]),
        "observationCount": count,
        "repeated": count >= 2,
    }


def _history_document(completed_runs: int, findings: Sequence[dict]) -> dict:
    return {
        "completedRuns": completed_runs,
        "findings": list(findings),
        "schemaVersion": _HISTORY_SCHEMA,
    }
