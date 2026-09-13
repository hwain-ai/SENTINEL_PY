from __future__ import annotations

import json
import base64
import hashlib
import multiprocessing
import stat
import sys
import tempfile
import unittest
import uuid
import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, call, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def _commit_worker(project_root: str, index: int) -> None:
    from sentinel_py.evidence import commit_quality_evidence

    run_id = str(uuid.UUID(int=index + 1))
    run = {
        "command": "crap",
        "completedAt": "2026-09-03T10:00:00Z",
        "correlationId": run_id,
        "mode": "strict",
        "runId": run_id,
        "terminalStatus": "passed",
    }
    summary = {
        "callableCount": 1,
        "crapMax": "8",
        "maxDenominator": "1",
        "maxNumerator": "1",
        "pass": True,
        "reason": "passed",
        "unknownCount": 0,
    }
    commit_quality_evidence(Path(project_root), run, summary, ())


class EvidenceStoreTests(unittest.TestCase):
    def test_started_document_preserves_the_exact_wire_contract(self):
        from sentinel_py.evidence.store import _started_document

        run = {
            "command": "mutation",
            "correlationId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "mode": "strict",
            "runId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "startedAtUtc": "2026-09-04T07:00:00Z",
        }

        self.assertEqual(
            {
                "command": "mutation",
                "correlationId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "language": "python",
                "mode": "strict",
                "runId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "schemaVersion": "sentinel-run-started-v1",
                "specVersion": "1.0.0",
                "startedAtUtc": "2026-09-04T07:00:00Z",
            },
            _started_document(run),
        )

    def test_atomic_bytes_uses_exclusive_private_durable_publish_contract(self):
        from sentinel_py.evidence import store

        path = Path("/state/evidence.json")
        temporary = Path("/state/.evidence.json.fixed-nonce.tmp")
        payload = b"evidence\n"
        stream = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = stream
        context.__exit__.return_value = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

        with (
            patch.object(store.uuid, "uuid4", return_value="fixed-nonce"),
            patch.object(store.os, "open", return_value=7) as opened,
            patch.object(store.os, "fdopen", return_value=context) as fdopen,
            patch.object(store.os, "fsync") as fsync,
            patch.object(store.os, "replace") as replace,
            patch.object(store.os, "close") as close,
            patch.object(store, "_require_private_regular_file") as require_private,
            patch.object(store, "_sync_directory") as sync_directory,
        ):
            store._atomic_bytes(path, payload)

        opened.assert_called_once_with(temporary, flags, 0o600)
        require_private.assert_called_once_with(7)
        fdopen.assert_called_once_with(7, "wb", closefd=True)
        stream.write.assert_called_once_with(payload)
        stream.flush.assert_called_once_with()
        stream.fileno.assert_called_once_with()
        fsync.assert_called_once_with(stream.fileno.return_value)
        replace.assert_called_once_with(temporary, path)
        sync_directory.assert_called_once_with(path.parent)
        close.assert_not_called()

    def test_atomic_bytes_open_failure_cleans_up_and_has_exact_error(self):
        from sentinel_py.evidence import EvidenceError, store

        path = Path("/state/evidence.json")
        failure = OSError("open failed")
        with (
            patch.object(store.uuid, "uuid4", return_value="fixed-nonce"),
            patch.object(store.os, "open", side_effect=failure),
            patch.object(store.os, "close") as close,
            patch.object(Path, "unlink") as unlink,
            self.assertRaises(EvidenceError) as stopped,
        ):
            store._atomic_bytes(path, b"payload")

        self.assertEqual("evidenceCommitFailed", str(stopped.exception))
        self.assertIs(failure, stopped.exception.__cause__)
        close.assert_not_called()
        unlink.assert_called_once_with(missing_ok=True)

    def test_atomic_bytes_validation_failure_closes_descriptor_zero_and_reraises(self):
        from sentinel_py.evidence import EvidenceError, store

        failure = EvidenceError("privateFileInvalid")
        with (
            patch.object(store.uuid, "uuid4", return_value="fixed-nonce"),
            patch.object(store.os, "open", return_value=0),
            patch.object(store.os, "close") as close,
            patch.object(Path, "unlink") as unlink,
            patch.object(
                store,
                "_require_private_regular_file",
                side_effect=failure,
            ),
            self.assertRaises(EvidenceError) as stopped,
        ):
            store._atomic_bytes(Path("/state/evidence.json"), b"payload")

        self.assertIs(failure, stopped.exception)
        close.assert_called_once_with(0)
        unlink.assert_called_once_with(missing_ok=True)

    def test_atomic_bytes_stream_failure_does_not_double_close_owned_descriptor(self):
        from sentinel_py.evidence import EvidenceError, store

        failure = OSError("write failed")
        stream = MagicMock()
        stream.write.side_effect = failure
        context = MagicMock()
        context.__enter__.return_value = stream
        context.__exit__.return_value = False
        with (
            patch.object(store.uuid, "uuid4", return_value="fixed-nonce"),
            patch.object(store.os, "open", return_value=7),
            patch.object(store.os, "fdopen", return_value=context),
            patch.object(store.os, "close") as close,
            patch.object(Path, "unlink") as unlink,
            patch.object(store, "_require_private_regular_file"),
            self.assertRaises(EvidenceError) as stopped,
        ):
            store._atomic_bytes(Path("/state/evidence.json"), b"payload")

        self.assertEqual("evidenceCommitFailed", str(stopped.exception))
        self.assertIs(failure, stopped.exception.__cause__)
        close.assert_not_called()
        unlink.assert_called_once_with(missing_ok=True)

    def test_run_values_preserve_current_and_legacy_time_contracts(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _validated_run_values

        run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        correlation_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        current = {
            "command": "mutation",
            "completedAt": "2020-01-01T00:00:00Z",
            "completedAtUtc": "2026-09-04T01:02:03Z",
            "correlationId": correlation_id,
            "mode": "strict",
            "observationSource": "fresh",
            "runId": run_id,
            "startedAt": "2020-01-01T00:00:00Z",
            "startedAtUtc": "2026-09-04T01:00:00Z",
            "terminalStatus": "passed",
        }
        self.assertEqual(
            {
                "command": "mutation",
                "completedAtUtc": "2026-09-04T01:02:03Z",
                "correlationId": correlation_id,
                "mode": "strict",
                "observationSource": "fresh",
                "runId": run_id,
                "startedAtUtc": "2026-09-04T01:00:00Z",
                "terminalStatus": "passed",
            },
            _validated_run_values(current),
        )

        legacy = {
            "command": "crap",
            "completedAt": "2026-09-04T02:00:00Z",
            "correlationId": correlation_id,
            "mode": "local",
            "runId": run_id,
            "terminalStatus": "qualityFailed",
        }
        self.assertEqual(
            {
                "command": "crap",
                "completedAtUtc": "2026-09-04T02:00:00Z",
                "correlationId": correlation_id,
                "mode": "local",
                "observationSource": "fresh",
                "runId": run_id,
                "startedAtUtc": "2026-09-04T02:00:00Z",
                "terminalStatus": "qualityFailed",
            },
            _validated_run_values(legacy),
        )

        legacy_with_distinct_start = {
            **legacy,
            "startedAt": "2026-09-04T01:59:00Z",
        }
        self.assertEqual(
            "2026-09-04T01:59:00Z",
            _validated_run_values(legacy_with_distinct_start)["startedAtUtc"],
        )

        invalid_changes = (
            ("command", "unknown"),
            ("mode", "automatic"),
            ("observationSource", "cached"),
            ("terminalStatus", "unknown"),
            ("runId", "not-a-uuid"),
            ("correlationId", "not-a-uuid"),
            ("startedAtUtc", "not-a-time"),
            ("completedAtUtc", "not-a-time"),
        )
        for name, value in invalid_changes:
            with self.subTest(name=name):
                invalid = {**current, name: value}
                with self.assertRaises(EvidenceError) as stopped:
                    _validated_run_values(invalid)
                self.assertEqual("runIdentityInvalid", str(stopped.exception))

    def test_source_run_rules_require_a_distinct_valid_local_cache_origin(self):
        from sentinel_py.evidence.store import _valid_source_run

        run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        fresh = {
            "mode": "strict",
            "observationSource": "fresh",
            "runId": run_id,
            "sourceRunId": None,
        }
        cached = {
            "mode": "local",
            "observationSource": "cache",
            "runId": run_id,
            "sourceRunId": source_id,
        }
        cases = (
            (fresh, True),
            ({**fresh, "sourceRunId": source_id}, False),
            (cached, True),
            ({**cached, "mode": "strict"}, False),
            ({**cached, "sourceRunId": "not-a-uuid"}, False),
            ({**cached, "sourceRunId": run_id}, False),
        )

        for document, expected in cases:
            with self.subTest(document=document):
                self.assertIs(expected, _valid_source_run(document))

    def test_terminal_status_matches_component_and_backend_semantics(self):
        from sentinel_py.evidence.store import _valid_terminal_semantics

        passing = {"crap": {"pass": True}}
        failing = {"crap": {"pass": False}}
        backend_failure = {
            "mutation": {
                "pass": False,
                "toolError": 1,
            }
        }
        cases = (
            ({"terminalStatus": "passed", "components": passing}, True),
            ({"terminalStatus": "passed", "components": failing}, False),
            ({"terminalStatus": "qualityFailed", "components": passing}, False),
            ({"terminalStatus": "qualityFailed", "components": failing}, True),
            ({"terminalStatus": "usageError", "components": failing}, True),
            ({"terminalStatus": "backendError", "components": backend_failure}, True),
            ({"terminalStatus": "qualityFailed", "components": backend_failure}, False),
        )

        for document, expected in cases:
            with self.subTest(document=document):
                self.assertIs(expected, _valid_terminal_semantics(document))

    def test_mutation_tool_error_requires_a_positive_declared_counter(self):
        from sentinel_py.evidence.store import _mutation_tool_error

        cases = (
            ({}, False),
            ({"mutation": None}, False),
            ({"mutation": []}, False),
            ({"mutation": {}}, False),
            ({"mutation": {"toolError": -1}}, False),
            ({"mutation": {"toolError": 0}}, False),
            ({"mutation": {"toolError": 1}}, True),
        )
        for components, expected in cases:
            with self.subTest(components=components):
                self.assertIs(expected, _mutation_tool_error(components))

    def test_evidence_value_validation_maps_each_semantic_failure_exactly(self):
        from sentinel_py.evidence import EvidenceError, store

        document = {"certification": True, "components": {"crap": {"pass": True}}}
        validators = (
            "_valid_evidence_result",
            "_valid_evidence_lists",
            "_valid_terminal_semantics",
        )
        for invalid_validator in validators:
            with self.subTest(invalid_validator=invalid_validator):
                results = {
                    "_valid_evidence_identity": True,
                    "_valid_evidence_result": True,
                    "_valid_evidence_lists": True,
                    "_valid_time_order": True,
                    "_valid_terminal_semantics": True,
                }
                results[invalid_validator] = False
                with (
                    patch.object(
                        store,
                        "_valid_evidence_identity",
                        return_value=results["_valid_evidence_identity"],
                    ),
                    patch.object(
                        store,
                        "_valid_evidence_result",
                        return_value=results["_valid_evidence_result"],
                    ),
                    patch.object(
                        store,
                        "_valid_evidence_lists",
                        return_value=results["_valid_evidence_lists"],
                    ),
                    patch.object(
                        store,
                        "_valid_time_order",
                        return_value=results["_valid_time_order"],
                    ),
                    patch.object(
                        store,
                        "_valid_terminal_semantics",
                        return_value=results["_valid_terminal_semantics"],
                    ),
                    patch.object(store, "_is_certified", return_value=True),
                    self.assertRaises(EvidenceError) as stopped,
                ):
                    store._validate_evidence_values(document)
                self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_evidence_result_requires_every_identity_time_and_digest_field(self):
        from sentinel_py.evidence.store import _valid_evidence_result

        valid = {
            "terminalStatus": "passed",
            "exitCode": 0,
            "startedAtUtc": "2026-09-04T01:00:00Z",
            "completedAtUtc": "2026-09-04T01:01:00Z",
            "committedAtUtc": "2026-09-04T01:02:00Z",
            "startedSha256": "a" * 64,
        }
        self.assertTrue(_valid_evidence_result(valid))
        invalid = (
            {**valid, "terminalStatus": "unknown", "exitCode": None},
            {**valid, "exitCode": 1},
            {**valid, "startedAtUtc": "invalid"},
            {**valid, "completedAtUtc": "invalid"},
            {**valid, "committedAtUtc": "invalid"},
            {**valid, "startedSha256": "invalid"},
        )
        for document in invalid:
            with self.subTest(document=document):
                self.assertFalse(_valid_evidence_result(document))

    def test_evidence_lists_require_safe_sorted_codes_and_exact_event_contract(self):
        from sentinel_py.evidence.store import (
            _valid_evidence_lists,
            _valid_event_list,
        )

        valid = {
            "diagnosticCodes": ["survived", "timedOut"],
            "eventCount": 0,
            "events": [],
            "observationSource": "fresh",
        }
        self.assertTrue(_valid_evidence_lists(valid))
        invalid_lists = (
            {**valid, "diagnosticCodes": ["bad-code"]},
            {**valid, "diagnosticCodes": ["timedOut", "survived"]},
            {**valid, "diagnosticCodes": ["survived", "survived"]},
            {**valid, "eventCount": 1},
        )
        for document in invalid_lists:
            with self.subTest(document=document):
                self.assertFalse(_valid_evidence_lists(document))

        invalid_events = (
            {**valid, "events": ()},
            {**valid, "eventCount": 0.0},
            {**valid, "eventCount": 1},
            {
                **valid,
                "eventCount": 1,
                "events": [{"event": True}],
                "observationSource": "cache",
            },
        )
        for document in invalid_events:
            with self.subTest(document=document):
                self.assertFalse(_valid_event_list(document))

    def test_time_order_accepts_equal_completion_and_commit_instants(self):
        from sentinel_py.evidence.store import _valid_time_order

        document = {
            "startedAtUtc": "2026-09-04T01:00:00Z",
            "completedAtUtc": "2026-09-04T01:01:00Z",
            "committedAtUtc": "2026-09-04T01:01:00Z",
        }

        self.assertTrue(_valid_time_order(document))

    def test_event_validation_requires_fields_hmac_join_and_all_value_groups(self):
        from sentinel_py.evidence import EvidenceError, store

        event = {"hmacSha256": "a" * 64}
        keys = SimpleNamespace(cleanup_lease_key=b"key")
        path = Path("events") / ("0" * 31 + "1.json")
        evidence = {"runId": "run"}
        cases = (
            (False, True, True),
            (True, False, True),
            (True, True, False),
        )
        for fields_valid, hmac_valid, join_valid in cases:
            with (
                self.subTest(
                    fields=fields_valid,
                    hmac=hmac_valid,
                    join=join_valid,
                ),
                patch.object(
                    store,
                    "_namespaced_document_hmac",
                    return_value="expected",
                ),
                patch.object(
                    store,
                    "_valid_event_fields",
                    return_value=fields_valid,
                ),
                patch.object(store, "_valid_hmac", return_value=hmac_valid),
                patch.object(
                    store,
                    "_event_joins_evidence",
                    return_value=join_valid,
                ),
                self.assertRaises(EvidenceError) as stopped,
            ):
                store._validate_event(event, path, evidence, keys)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

        event_fields = {
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
        complete_event = {name: object() for name in event_fields}
        with patch.object(store, "_valid_event_values", return_value=True):
            self.assertTrue(store._valid_event_fields(complete_event))
            self.assertFalse(
                store._valid_event_fields(
                    {
                        name: value
                        for name, value in complete_event.items()
                        if name != "value"
                    }
                )
            )

        validator_names = (
            "_valid_event_contract",
            "_valid_event_classification",
            "_valid_event_fingerprints",
            "_valid_utc",
        )
        for invalid_name in validator_names:
            results = {name: True for name in validator_names}
            results[invalid_name] = False
            with (
                self.subTest(invalid=invalid_name),
                patch.object(
                    store,
                    "_valid_event_contract",
                    return_value=results["_valid_event_contract"],
                ),
                patch.object(
                    store,
                    "_valid_event_classification",
                    return_value=results["_valid_event_classification"],
                ),
                patch.object(
                    store,
                    "_valid_event_fingerprints",
                    return_value=results["_valid_event_fingerprints"],
                ),
                patch.object(
                    store,
                    "_valid_utc",
                    return_value=results["_valid_utc"],
                ),
            ):
                self.assertFalse(store._valid_event_values({"observedAtUtc": "time"}))

    def test_sequence_document_round_trip_and_invalid_contracts(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _canonical_json,
            _read_sequence,
            _sequence_document,
        )

        key = bytes(range(32))
        with tempfile.TemporaryDirectory(prefix="sentinel-sequence-read-") as directory:
            path = Path(directory) / "commit-sequence.json"
            valid = _sequence_document(17, key)
            path.write_bytes(_canonical_json(valid) + b"\n")
            self.assertEqual(17, _read_sequence(path, key))

            path.write_bytes(b"not-json\n")
            with self.assertRaises(EvidenceError) as stopped:
                _read_sequence(path, key)
            self.assertEqual("sequenceStateInvalid", str(stopped.exception))

            invalid_documents = (
                {name: value for name, value in valid.items() if name != "hmacSha256"},
                {**valid, "extra": True},
                {**valid, "version": "wrong"},
                {**valid, "lastAllocated": "0"},
                {**valid, "hmacSha256": "0" * 64},
            )
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_bytes(_canonical_json(document) + b"\n")
                    with self.assertRaises(EvidenceError) as stopped:
                        _read_sequence(path, key)
                    self.assertEqual("sequenceStateInvalid", str(stopped.exception))

    def test_started_bundle_verification_binds_digest_document_and_error_code(self):
        from sentinel_py.evidence import EvidenceError, store

        path = Path("/state/runs/run/started.json")
        payload = b"started payload\n"
        started = {"schemaVersion": "sentinel-run-started-v1"}
        evidence = {
            "startedSha256": hashlib.sha256(payload).hexdigest(),
        }
        with (
            patch.object(store, "_safe_read", return_value=payload) as safe_read,
            patch.object(store, "_read_json", return_value=started) as read_json,
            patch.object(store, "_started_document", return_value=started) as expected,
        ):
            self.assertIsNone(store._verify_started(path, evidence))
        safe_read.assert_called_once_with(path)
        read_json.assert_called_once_with(path, "evidenceInvalid")
        expected.assert_called_once_with(evidence)

        invalid_cases = (
            (b"different\n", started, started),
            (payload, {"schemaVersion": "wrong"}, started),
        )
        for actual_payload, actual_started, expected_started in invalid_cases:
            with self.subTest(
                actual_payload=actual_payload,
                actual_started=actual_started,
            ):
                with (
                    patch.object(store, "_safe_read", return_value=actual_payload),
                    patch.object(store, "_read_json", return_value=actual_started),
                    patch.object(
                        store,
                        "_started_document",
                        return_value=expected_started,
                    ),
                    self.assertRaises(EvidenceError) as stopped,
                ):
                    store._verify_started(path, evidence)
                self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_private_directory_uses_exact_owner_only_creation_contract(self):
        from sentinel_py.evidence import store

        path = MagicMock()
        path.is_symlink.return_value = False
        path.is_dir.return_value = True

        store._private_directory(path)

        path.mkdir.assert_called_once_with(mode=0o700, parents=True, exist_ok=True)
        path.is_symlink.assert_called_once_with()
        path.is_dir.assert_called_once_with()
        path.chmod.assert_called_once_with(0o700)

    def test_private_directory_rejects_wrong_type_and_maps_os_error_exactly(self):
        from sentinel_py.evidence import EvidenceError, store

        for symlink, directory in ((True, True), (False, False)):
            with self.subTest(symlink=symlink, directory=directory):
                path = MagicMock()
                path.is_symlink.return_value = symlink
                path.is_dir.return_value = directory
                with self.assertRaises(EvidenceError) as stopped:
                    store._private_directory(path)
                self.assertEqual("statePathInvalid", str(stopped.exception))
                path.chmod.assert_not_called()

        failure = OSError("mkdir failed")
        path = MagicMock()
        path.mkdir.side_effect = failure
        with self.assertRaises(EvidenceError) as stopped:
            store._private_directory(path)
        self.assertEqual("stateDirectoryFailed", str(stopped.exception))
        self.assertIs(failure, stopped.exception.__cause__)

    def test_read_history_state_preserves_dependency_flow_and_repeat_filter(self):
        from sentinel_py.evidence import store

        key = b"k" * 32
        keys = MagicMock(cleanup_lease_key=key)
        paths = (Path("/runs/a/evidence.json"), Path("/runs/b/evidence.json"))
        evidence = ({"runId": "a"}, {"runId": "b"})
        findings = ({"fingerprint": "one"},)
        expected = {"completedRuns": 2, "findings": list(findings)}

        with tempfile.TemporaryDirectory(prefix="sentinel-history-state-") as directory:
            state_root = Path(directory)
            (state_root / "runs").mkdir()
            with (
                patch.object(store, "_read_project_keys", return_value=keys) as read_keys,
                patch.object(store, "_evidence_paths", return_value=paths) as evidence_paths,
                patch.object(store, "_read_evidence", side_effect=evidence) as read_evidence,
                patch.object(store, "_validate_sequence_state") as validate_sequence,
                patch.object(store, "_history_findings", return_value=findings) as history_findings,
                patch.object(store, "_history_document", return_value=expected) as history_document,
            ):
                actual = store._read_history_state(state_root, True)

        self.assertIs(expected, actual)
        read_keys.assert_called_once_with(state_root / "project.json")
        evidence_paths.assert_called_once_with(state_root / "runs")
        self.assertEqual(
            [call(paths[0], keys), call(paths[1], keys)],
            read_evidence.call_args_list,
        )
        validate_sequence.assert_called_once_with(state_root, evidence, key)
        history_findings.assert_called_once_with(evidence, repeated_only=True)
        history_document.assert_called_once_with(2, findings)

    def test_read_history_state_handles_missing_and_invalid_run_roots_exactly(self):
        from sentinel_py.evidence import EvidenceError, store

        with tempfile.TemporaryDirectory(prefix="sentinel-history-roots-") as directory:
            state_root = Path(directory)
            expected = {"completedRuns": 0}
            with patch.object(store, "_history_document", return_value=expected) as document:
                self.assertIs(expected, store._read_history_state(state_root, False))
            document.assert_called_once_with(0, ())

            runs_root = state_root / "runs"
            runs_root.write_bytes(b"not-a-directory")
            with self.assertRaises(EvidenceError) as stopped:
                store._read_history_state(state_root, False)
            self.assertEqual("runStateInvalid", str(stopped.exception))

            runs_root.unlink()
            target = state_root / "target"
            target.mkdir()
            runs_root.symlink_to(target, target_is_directory=True)
            with self.assertRaises(EvidenceError) as stopped:
                store._read_history_state(state_root, False)
            self.assertEqual("runStateInvalid", str(stopped.exception))

    def test_read_history_validates_state_path_and_forwards_repeat_filter(self):
        from sentinel_py.evidence import EvidenceError, store

        with tempfile.TemporaryDirectory(prefix="sentinel-read-history-") as directory:
            project = Path(directory)
            expected = {"completedRuns": 3}
            with patch.object(store, "_history_document", return_value=expected) as document:
                self.assertIs(expected, store.read_history(project, repeated_only=False))
            document.assert_called_once_with(0, ())

            state_root = project / ".sentinel" / "state-v1"
            state_root.mkdir(parents=True)
            shared = MagicMock()
            shared.return_value.__enter__.return_value = None
            shared.return_value.__exit__.return_value = False
            with (
                patch.object(store, "_shared_state", shared),
                patch.object(store, "_read_history_state", return_value=expected) as read_state,
            ):
                self.assertIs(expected, store.read_history(project, repeated_only=True))
            shared.assert_called_once_with(state_root)
            read_state.assert_called_once_with(state_root, True)

            state_root.rmdir()
            state_root.write_bytes(b"not-a-directory")
            with self.assertRaises(EvidenceError) as stopped:
                store.read_history(project, repeated_only=False)
            self.assertEqual("statePathInvalid", str(stopped.exception))

            state_root.unlink()
            target = project / "state-target"
            target.mkdir()
            state_root.symlink_to(target, target_is_directory=True)
            with self.assertRaises(EvidenceError) as stopped:
                store.read_history(project, repeated_only=False)
            self.assertEqual("statePathInvalid", str(stopped.exception))

    def test_timestamp_key_preserves_seconds_and_nanoseconds_exactly(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _timestamp_key

        cases = (
            ("1970-01-01T00:00:00Z", (0, 0)),
            ("1970-01-01T00:00:00.1Z", (0, 100_000_000)),
            ("1970-01-01T00:00:00.000000001Z", (0, 1)),
            ("2026-09-04T07:08:09.123456789Z", (1_788_505_689, 123_456_789)),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, _timestamp_key(value))

        with self.assertRaises(EvidenceError) as stopped:
            _timestamp_key("not-a-time")
        self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_add_run_observations_accumulates_exact_runs_and_sequences(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _add_run_observations

        first_run = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_run = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        finding = {
            "category": "mutation",
            "fingerprint": "hmac-sha256:" + "1" * 64,
            "reason": "survived",
        }
        observations = {}
        _add_run_observations(
            observations,
            {"runId": first_run, "commitSequence": "2", "_eventFindings": [finding]},
        )
        _add_run_observations(
            observations,
            {"runId": second_run, "commitSequence": "5", "_eventFindings": [finding]},
        )
        self.assertEqual(
            {
                finding["fingerprint"]: {
                    "finding": finding,
                    "runs": {first_run, second_run},
                    "sequences": [2, 5],
                }
            },
            observations,
        )

        empty_observations = {}
        _add_run_observations(
            empty_observations,
            {"runId": first_run, "commitSequence": "6"},
        )
        self.assertEqual({}, empty_observations)

        for invalid in (
            {"runId": "not-a-uuid", "commitSequence": "1"},
            {"runId": first_run, "commitSequence": "0"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(EvidenceError) as stopped:
                _add_run_observations({}, invalid)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_evidence_sequence_requires_exact_run_and_uint64_identity(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _evidence_sequence

        run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.assertEqual(
            17,
            _evidence_sequence({"commitSequence": "17", "runId": run_id}),
        )
        for document in (
            {"commitSequence": "0", "runId": run_id},
            {"commitSequence": "17", "runId": "not-a-uuid"},
        ):
            with self.subTest(document=document), self.assertRaises(EvidenceError) as stopped:
                _evidence_sequence(document)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_evidence_paths_are_complete_sorted_and_reject_invalid_run_roots(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _evidence_paths

        with tempfile.TemporaryDirectory(prefix="sentinel-evidence-paths-") as directory:
            runs_root = Path(directory)
            expected = []
            for name in ("b", "a"):
                run_root = runs_root / name
                run_root.mkdir()
                evidence_path = run_root / "evidence.json"
                evidence_path.write_bytes(b"evidence")
                expected.append(evidence_path)
            (runs_root / "incomplete").mkdir()

            self.assertEqual(tuple(sorted(expected)), _evidence_paths(runs_root))

            invalid = runs_root / "invalid"
            invalid.write_bytes(b"not-a-directory")
            with self.assertRaises(EvidenceError) as stopped:
                _evidence_paths(runs_root)
            self.assertEqual("runStateInvalid", str(stopped.exception))

            invalid.unlink()
            invalid.symlink_to(runs_root / "a", target_is_directory=True)
            with self.assertRaises(EvidenceError) as stopped:
                _evidence_paths(runs_root)
            self.assertEqual("runStateInvalid", str(stopped.exception))

    def test_evidence_components_enforces_exact_fields_names_and_validation(self):
        from sentinel_py.evidence import EvidenceError, store

        components = {"crap": {}, "mutation": {}}
        document = {"command": "check", "components": components, "marker": True}
        with (
            patch.object(store, "_EVIDENCE_FIELDS", frozenset(document)),
            patch.object(
                store,
                "_expected_component_names",
                return_value={"crap", "mutation"},
            ) as expected_names,
            patch.object(store, "_validate_component_values") as validate,
        ):
            self.assertEqual({"crap", "mutation"}, store._evidence_components(document))
        expected_names.assert_called_once_with("check")
        validate.assert_called_once_with(components)

        invalid_documents = (
            {"command": "check", "components": components},
            {**document, "components": []},
            {**document, "components": {"crap": {}}},
        )
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with (
                    patch.object(store, "_EVIDENCE_FIELDS", frozenset(document)),
                    patch.object(
                        store,
                        "_expected_component_names",
                        return_value={"crap", "mutation"},
                    ),
                    self.assertRaises(EvidenceError) as stopped,
                ):
                    store._evidence_components(invalid)
                self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_crap_component_preserves_exact_values_and_defaults(self):
        from sentinel_py.evidence.store import _crap_component

        self.assertEqual(
            {
                "callableCount": 3,
                "crapMax": "8",
                "maxDenominator": "7",
                "maxNumerator": "11",
                "pass": False,
                "unknownCount": 2,
            },
            _crap_component(
                {
                    "callableCount": 3,
                    "crapMax": "8",
                    "maxDenominator": "7",
                    "maxNumerator": "11",
                    "pass": False,
                    "unknownCount": 2,
                }
            ),
        )
        self.assertEqual("1", _crap_component({
            "callableCount": 0,
            "crapMax": "8",
            "pass": False,
            "unknownCount": 0,
        })["maxDenominator"])
        self.assertEqual("0", _crap_component({
            "callableCount": 0,
            "crapMax": "8",
            "pass": False,
            "unknownCount": 0,
        })["maxNumerator"])

    def test_safe_read_uses_nofollow_and_closes_the_descriptor(self):
        from sentinel_py.evidence import EvidenceError, store

        path = Path("/state/evidence.json")
        stream = MagicMock()
        stream.read.return_value = b"payload"
        context = MagicMock()
        context.__enter__.return_value = stream
        context.__exit__.return_value = False
        flags = os.O_RDONLY | os.O_NOFOLLOW
        with (
            patch.object(store.os, "open", return_value=7) as opened,
            patch.object(store.os, "fdopen", return_value=context) as fdopen,
            patch.object(store.os, "close") as close,
            patch.object(store, "_require_regular_file") as require_regular,
        ):
            self.assertEqual(b"payload", store._safe_read(path))
        opened.assert_called_once_with(path, flags)
        require_regular.assert_called_once_with(7)
        fdopen.assert_called_once_with(7, "rb", closefd=False)
        stream.read.assert_called_once_with()
        close.assert_called_once_with(7)

        failure = EvidenceError("stateFileInvalid")
        with (
            patch.object(store.os, "open", return_value=8),
            patch.object(store.os, "close") as close,
            patch.object(store, "_require_regular_file", side_effect=failure),
            self.assertRaises(EvidenceError) as stopped,
        ):
            store._safe_read(path)
        self.assertIs(failure, stopped.exception)
        close.assert_called_once_with(8)

    def test_sync_directory_uses_directory_flag_and_always_closes(self):
        from sentinel_py.evidence import store

        path = Path("/state/runs")
        flags = os.O_RDONLY | os.O_DIRECTORY
        with (
            patch.object(store.os, "open", return_value=9) as opened,
            patch.object(store.os, "fsync") as fsync,
            patch.object(store.os, "close") as close,
        ):
            store._sync_directory(path)
        opened.assert_called_once_with(path, flags)
        fsync.assert_called_once_with(9)
        close.assert_called_once_with(9)

        failure = OSError("sync failed")
        with (
            patch.object(store.os, "open", return_value=10),
            patch.object(store.os, "fsync", side_effect=failure),
            patch.object(store.os, "close") as close,
            self.assertRaises(OSError) as stopped,
        ):
            store._sync_directory(path)
        self.assertIs(failure, stopped.exception)
        close.assert_called_once_with(10)

    def test_event_value_validators_accept_only_the_declared_vocabulary(self):
        from sentinel_py.evidence.store import (
            _valid_event_classification,
            _valid_event_contract,
            _valid_event_fingerprints,
        )

        event = {
            "schemaVersion": "sentinel-finding-event-v1",
            "event": "detected",
            "fingerprintVersion": "sentinel-fingerprint-v1",
            "observationSource": "fresh",
            "findingClass": "projectCode",
            "defectKind": "mutation",
            "diagnosticCode": "survived",
            "fingerprintKind": "occurrence",
            "findingToken": "1" * 64,
            "value": "2" * 64,
        }

        for value in ("detected", "persisted", "resolved", "reopened"):
            with self.subTest(event=value):
                self.assertTrue(_valid_event_contract({**event, "event": value}))
        for name, value in (
            ("schemaVersion", "wrong"),
            ("event", "created"),
            ("fingerprintVersion", "wrong"),
            ("observationSource", "cached"),
        ):
            with self.subTest(contract=name):
                self.assertFalse(_valid_event_contract({**event, name: value}))

        for value in (
            "projectCode",
            "projectTest",
            "projectCodeOrTest",
            "backend",
            "sentinel",
            "environment",
        ):
            with self.subTest(finding_class=value):
                self.assertTrue(
                    _valid_event_classification({**event, "findingClass": value})
                )
        for name, value in (
            ("findingClass", "project"),
            ("defectKind", "Bad-Kind"),
            ("diagnosticCode", "bad-code"),
        ):
            with self.subTest(classification=name):
                self.assertFalse(_valid_event_classification({**event, name: value}))

        for value in ("occurrence", "context", "family"):
            with self.subTest(fingerprint_kind=value):
                self.assertTrue(
                    _valid_event_fingerprints({**event, "fingerprintKind": value})
                )
        for name, value in (
            ("fingerprintKind", "identity"),
            ("findingToken", "A" * 64),
            ("value", "short"),
        ):
            with self.subTest(fingerprint=name):
                self.assertFalse(_valid_event_fingerprints({**event, name: value}))

    def test_evidence_components_and_diagnostics_keep_exact_public_values(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _diagnostic_codes, _mutation_component

        count_names = (
            "compileError",
            "ignored",
            "killed",
            "pending",
            "runtimeError",
            "survived",
            "timedOut",
            "toolError",
            "uncovered",
        )
        counts = {name: index for index, name in enumerate(count_names)}
        summary = {
            "counts": counts,
            "inScope": 36,
            "mutationMin": "100",
            "pass": False,
        }
        self.assertEqual(
            {
                **counts,
                "inScope": 36,
                "mutationMin": "100",
                "pass": False,
                "unauthorizedExclusion": 0,
            },
            _mutation_component(summary),
        )
        self.assertEqual(
            3,
            _mutation_component({**summary, "unauthorizedExclusion": 3})[
                "unauthorizedExclusion"
            ],
        )
        for invalid in (None, (), "counts"):
            with self.subTest(counts=invalid):
                with self.assertRaises(EvidenceError) as stopped:
                    _mutation_component({**summary, "counts": invalid})
                self.assertEqual("evidenceInvalid", str(stopped.exception))

        components = {
            "crap": {"pass": False, "reason": "crapThresholdExceeded"},
            "mutation": {"pass": True, "reason": "passed"},
        }
        findings = (
            {"reason": "timedOut"},
            {"reason": "survived"},
            {"reason": "survived"},
        )
        self.assertEqual(
            ["crapThresholdExceeded", "survived", "timedOut"],
            _diagnostic_codes(components, findings),
        )
        with self.assertRaises(EvidenceError) as stopped:
            _diagnostic_codes(
                {"crap": {"pass": False, "reason": "bad-code"}},
                (),
            )
        self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_new_run_root_is_private_unique_and_requires_a_canonical_uuid(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _new_run_root

        run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with tempfile.TemporaryDirectory(prefix="sentinel-new-run-") as directory:
            state_root = Path(directory) / "state-v1"
            state_root.mkdir()
            run_root = _new_run_root(state_root, run_id)
            self.assertEqual(state_root / "runs" / run_id, run_root)
            self.assertTrue(run_root.is_dir())
            self.assertEqual(0o700, run_root.stat().st_mode & 0o777)
            self.assertEqual(0o700, run_root.parent.stat().st_mode & 0o777)

            with self.assertRaises(EvidenceError) as stopped:
                _new_run_root(state_root, run_id)
            self.assertEqual("runAlreadyExists", str(stopped.exception))
            self.assertIsInstance(stopped.exception.__cause__, OSError)

            for invalid in (None, "not-a-uuid", run_id.upper()):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(EvidenceError) as stopped:
                        _new_run_root(state_root, invalid)
                    self.assertEqual("runIdentityInvalid", str(stopped.exception))

        with (
            patch("sentinel_py.evidence.store._private_directory"),
            patch.object(Path, "mkdir") as mkdir,
        ):
            self.assertEqual(
                Path("/state/runs") / run_id,
                _new_run_root(Path("/state"), run_id),
            )
        mkdir.assert_called_once_with(mode=0o700)

    def test_event_verification_binds_manifest_payload_and_public_finding(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence import store

        path = Path("events") / ("0" * 31 + "1.json")
        payload = b'{"event":"payload"}\n'
        manifest = {
            "filename": path.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        evidence = {"runId": "run"}
        keys = object()
        event = {
            "defectKind": "mutation",
            "diagnosticCode": "survived",
            "findingToken": "a" * 64,
        }
        with (
            patch.object(store, "_safe_read", return_value=payload) as safe_read,
            patch.object(store, "_read_json", return_value=event) as read_json,
            patch.object(store, "_validate_event") as validate_event,
        ):
            self.assertEqual(
                {
                    "category": "mutation",
                    "fingerprint": "hmac-sha256:" + "a" * 64,
                    "reason": "survived",
                },
                store._verify_event(path, manifest, evidence, keys),
            )
        safe_read.assert_called_once_with(path)
        read_json.assert_called_once_with(path, "evidenceInvalid")
        validate_event.assert_called_once_with(event, path, evidence, keys)

        for invalid_manifest in (
            None,
            {**manifest, "filename": "bad.json"},
            {**manifest, "sha256": "A" * 64},
            {**manifest, "extra": True},
        ):
            with self.subTest(manifest=invalid_manifest):
                with patch.object(store, "_safe_read") as safe_read:
                    with self.assertRaises(EvidenceError) as stopped:
                        store._verify_event(path, invalid_manifest, evidence, keys)
                self.assertEqual("evidenceInvalid", str(stopped.exception))
                safe_read.assert_not_called()

        with (
            patch.object(store, "_safe_read", return_value=b"tampered"),
            patch.object(store, "_read_json") as read_json,
        ):
            with self.assertRaises(EvidenceError) as stopped:
                store._verify_event(path, manifest, evidence, keys)
        self.assertEqual("evidenceInvalid", str(stopped.exception))
        read_json.assert_not_called()

    def test_read_evidence_binds_error_code_validators_and_event_findings(self):
        from sentinel_py.evidence import store

        path = Path("runs/run/evidence.json")
        keys = object()
        document = {"runId": "run", "value": True}
        findings = ({"fingerprint": "hmac-sha256:" + "a" * 64},)
        with (
            patch.object(store, "_read_json", return_value=document) as read_json,
            patch.object(store, "_validate_evidence_document") as validate,
            patch.object(store, "_validate_run_path") as validate_path,
            patch.object(
                store,
                "_verify_run_bundle",
                return_value=findings,
            ) as verify_bundle,
        ):
            actual = store._read_evidence(path, keys)

        self.assertEqual({**document, "_eventFindings": findings}, actual)
        read_json.assert_called_once_with(path, "evidenceInvalid")
        validate.assert_called_once_with(document, keys)
        validate_path.assert_called_once_with(path, document)
        verify_bundle.assert_called_once_with(path, document, keys)

    def test_evidence_hmac_and_event_join_fail_closed_on_every_mismatch(self):
        from sentinel_py.evidence import EvidenceError, store

        document = {"hmacSha256": "a" * 64, "runId": "run"}
        with (
            patch.object(
                store,
                "_namespaced_document_hmac",
                return_value="b" * 64,
            ),
            patch.object(store, "_valid_hmac", return_value=False),
            self.assertRaises(EvidenceError) as stopped,
        ):
            store._verify_evidence_hmac(document, b"key")
        self.assertEqual("evidenceInvalid", str(stopped.exception))

        event_id = "0" * 31 + "1"
        path = Path("events") / (event_id + ".json")
        event = {
            "eventId": event_id,
            "runId": "run",
            "commitSequence": "7",
            "observedAtUtc": "2026-09-04T01:00:00Z",
            "keyEpoch": 1,
            "findingToken": "a" * 64,
            "value": "a" * 64,
        }
        evidence = {
            "runId": "run",
            "commitSequence": "7",
            "completedAtUtc": "2026-09-04T01:00:00Z",
            "keyEpoch": 1,
        }
        self.assertTrue(store._event_joins_evidence(event, path, evidence))
        changes = (
            ("eventId", "f" * 32),
            ("eventId", "invalid"),
            ("runId", "other"),
            ("commitSequence", "8"),
            ("observedAtUtc", "2026-09-04T02:00:00Z"),
            ("keyEpoch", 2),
            ("value", "b" * 64),
        )
        for name, value in changes:
            with self.subTest(name=name, value=value):
                self.assertFalse(
                    store._event_joins_evidence(
                        {**event, name: value},
                        path,
                        evidence,
                    )
                )

    def test_read_json_rejects_duplicate_constant_non_object_and_noncanonical_data(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _read_json

        cases = (
            b'{"value":1,"value":2}\n',
            b'{"value":NaN}\n',
            b"[]\n",
            b'{"value": 1}\n',
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-read-json-") as directory:
            path = Path(directory) / "document.json"
            for payload in cases:
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(EvidenceError) as stopped:
                        _read_json(path, "exactReadError")

                    self.assertEqual("exactReadError", str(stopped.exception))

    def test_unique_json_and_run_findings_preserve_distinct_values_and_exact_error(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _unique_json_object,
            _unique_run_findings,
        )

        with self.assertRaises(EvidenceError) as stopped:
            _unique_json_object((('name', 1), ('name', 2)))
        self.assertEqual("jsonDuplicateKey", str(stopped.exception))

        first = {
            "category": "mutation",
            "fingerprint": "hmac-sha256:" + "a" * 64,
            "reason": "survived",
        }
        second = {
            "category": "mutation",
            "fingerprint": "hmac-sha256:" + "b" * 64,
            "reason": "timedOut",
        }
        self.assertEqual((first, second), _unique_run_findings((first, second)))
        conflict = {**first, "reason": "timedOut"}
        with self.assertRaises(EvidenceError) as stopped:
            _unique_run_findings((first, conflict))
        self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_read_json_keeps_both_parser_guards_enabled(self):
        from sentinel_py.evidence import store

        with (
            patch.object(store, "_safe_read", return_value=b"{}\n"),
            patch.object(store.json, "loads", return_value={}) as loads,
            patch.object(store, "_canonical_json", return_value=b"{}"),
        ):
            self.assertEqual({}, store._read_json(Path("document.json"), "readError"))

        loads.assert_called_once_with(
            "{}\n",
            object_pairs_hook=store._unique_json_object,
            parse_constant=store._reject_json_constant,
        )

    def test_public_and_history_findings_sort_by_fingerprint_bytes(self):
        from sentinel_py.evidence import FindingIdentity
        from sentinel_py.evidence import store

        findings = (
            FindingIdentity("mutation", "timedOut", "python", "api", "", "b"),
            FindingIdentity("mutation", "survived", "python", "api", "", "a"),
        )
        public = (
            {
                "category": "mutation",
                "fingerprint": "hmac-sha256:" + "b" * 64,
                "reason": "timedOut",
            },
            {
                "category": "mutation",
                "fingerprint": "hmac-sha256:" + "a" * 64,
                "reason": "survived",
            },
        )
        with patch.object(store, "_public_finding", side_effect=public):
            ordered = store._public_findings(findings, b"secret")
        self.assertEqual(tuple(reversed(public)), ordered)

        first_run = "00000000-0000-4000-8000-000000000001"
        second_run = "00000000-0000-4000-8000-000000000002"
        evidence = (
            {
                "runId": first_run,
                "commitSequence": "1",
                "_eventFindings": public,
            },
            {
                "runId": second_run,
                "commitSequence": "2",
                "_eventFindings": (),
            },
        )
        rows = store._history_findings(evidence, repeated_only=False)
        self.assertEqual(
            ("hmac-sha256:" + "a" * 64, "hmac-sha256:" + "b" * 64),
            tuple(row["fingerprint"] for row in rows),
        )

    def test_json_constant_callback_rejects_every_nonstandard_number_token(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _reject_json_constant

        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token), self.assertRaisesRegex(
                EvidenceError,
                "^jsonConstantInvalid$",
            ):
                _reject_json_constant(token)

    def test_project_key_document_and_base64_contracts_are_exact(self):
        from sentinel_py.evidence import EvidenceError, store

        identifier = b"i" * 16
        fingerprint = b"f" * 32
        cleanup = b"c" * 32
        with patch.object(
            store.os,
            "urandom",
            side_effect=(identifier, fingerprint, cleanup),
        ):
            document = store._new_project_document()
        self.assertEqual(
            {
                "cleanupLeaseKey": store._encode_base64url(cleanup),
                "fingerprintHmacKey": store._encode_base64url(fingerprint),
                "keyEpoch": 1,
                "projectIdentifier": store._encode_base64url(identifier),
                "schemaVersion": "sentinel-project-state-v1",
                "stateVersion": "state-v1",
            },
            document,
        )
        self.assertNotIn("=", document["projectIdentifier"])
        self.assertEqual("YQ", store._encode_base64url(b"a"))
        self.assertEqual("YWI", store._encode_base64url(b"ab"))
        self.assertEqual(
            identifier,
            store._decode_base64url(document["projectIdentifier"], 16),
        )
        for invalid in (None, "bad$value", "eA==", "eA"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(EvidenceError) as stopped:
                    store._decode_base64url(invalid, 16)
                self.assertEqual("projectStateInvalid", str(stopped.exception))

        with (
            patch.object(store.base64, "b64decode", return_value=b"a") as decode,
            patch.object(store, "_encode_base64url", return_value="YQ"),
        ):
            self.assertEqual(b"a", store._decode_base64url("YQ", 1))
        decode.assert_called_once_with(
            "YQ==",
            altchars=b"-_",
            validate=True,
        )

        with (
            patch.object(
                store.os,
                "urandom",
                side_effect=(identifier, fingerprint, fingerprint),
            ),
            self.assertRaises(EvidenceError) as stopped,
        ):
            store._new_project_document()
        self.assertEqual("projectStateInvalid", str(stopped.exception))

    def test_numeric_contracts_cover_zero_maximum_and_length_boundaries(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _canonical_uint64,
            _nonnegative_decimal,
            _positive_decimal,
            _safe_uint,
        )

        maximum_safe = (1 << 53) - 1
        maximum_uint64 = (1 << 64) - 1
        for value in (0, maximum_safe):
            self.assertEqual(value, _safe_uint(value))
        for value in (True, -1, maximum_safe + 1):
            with self.subTest(safe_uint=value):
                with self.assertRaises(EvidenceError):
                    _safe_uint(value)

        self.assertEqual("1" * 96, _nonnegative_decimal("1" * 96))
        with self.assertRaises(EvidenceError):
            _nonnegative_decimal("1" * 97)
        self.assertEqual("1" * 48, _positive_decimal("1" * 48))
        with self.assertRaises(EvidenceError):
            _positive_decimal("1" * 49)

        for value in ("1", str(maximum_uint64)):
            self.assertTrue(_canonical_uint64(value))
        for value in (None, 1, "", "0", "01", str(maximum_uint64 + 1)):
            with self.subTest(uint64=value):
                self.assertFalse(_canonical_uint64(value))

    def test_component_routing_calls_each_validator_and_rejects_empty_exactly(self):
        from sentinel_py.evidence import EvidenceError, store

        crap = object()
        mutation = object()
        with (
            patch.object(store, "_validate_crap_component") as validate_crap,
            patch.object(
                store,
                "_validate_mutation_component",
            ) as validate_mutation,
        ):
            store._validate_component_values(
                {"crap": crap, "mutation": mutation}
            )
        validate_crap.assert_called_once_with(crap)
        validate_mutation.assert_called_once_with(mutation)

        self.assertEqual({"crap", "mutation"}, store._expected_component_names("check"))
        self.assertEqual({"crap"}, store._expected_component_names("crap"))
        self.assertEqual({"mutation"}, store._expected_component_names("mutation"))
        with self.assertRaises(EvidenceError) as stopped:
            store._expected_component_names("unknown")
        self.assertEqual("evidenceInvalid", str(stopped.exception))

        with self.assertRaises(EvidenceError) as stopped:
            store._component_document({})
        self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_utc_helpers_convert_offset_time_and_request_utc_clock(self):
        from datetime import timedelta

        from sentinel_py.evidence import store

        offset = timezone(timedelta(hours=9))
        value = datetime(2026, 9, 4, 10, 2, 3, 120000, tzinfo=offset)
        self.assertEqual("2026-09-04T01:02:03.12Z", store._format_utc(value))

        instant = object()
        with (
            patch.object(store, "datetime") as clock,
            patch.object(store, "_format_utc", return_value="now") as format_utc,
        ):
            clock.now.return_value = instant
            self.assertEqual("now", store._utc_now())
        clock.now.assert_called_once_with(timezone.utc)
        format_utc.assert_called_once_with(instant)

        converted = datetime(2026, 9, 4, 1, 2, 3, 123450, tzinfo=timezone.utc)
        input_value = MagicMock(microsecond=123450)
        input_value.astimezone.return_value = converted
        self.assertEqual("2026-09-04T01:02:03.12345Z", store._format_utc(input_value))
        input_value.astimezone.assert_called_once_with(timezone.utc)

    def test_utc_formatter_removes_only_redundant_fractional_zeroes(self):
        from sentinel_py.evidence.store import _format_utc, _valid_utc

        cases = (
            (datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc), "2026-09-03T01:02:03Z"),
            (
                datetime(2026, 9, 3, 1, 2, 3, 120000, tzinfo=timezone.utc),
                "2026-09-03T01:02:03.12Z",
            ),
            (
                datetime(2026, 9, 3, 1, 2, 3, 123456, tzinfo=timezone.utc),
                "2026-09-03T01:02:03.123456Z",
            ),
            (
                datetime(2026, 9, 3, 1, 2, 3, 123450, tzinfo=timezone.utc),
                "2026-09-03T01:02:03.12345Z",
            ),
        )
        for value, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, _format_utc(value))
                self.assertTrue(_valid_utc(expected))

        for value in (
            "2026-09-03T01:02:03.0Z",
            "2026-09-03T01:02:03.120Z",
            "2026-09-03T01:02:03.Z",
            "2026-99-03T01:02:03Z",
        ):
            with self.subTest(invalid=value):
                self.assertFalse(_valid_utc(value))

    def test_private_contract_validators_cover_every_rejected_value_family(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _canonical_json,
            _nonnegative_decimal,
            _positive_decimal,
            _validate_crap_component,
            _validate_mutation_component,
            _validated_finding,
        )

        self.assertEqual(
            b'{"array":[null,true,0,"value"]}',
            _canonical_json({"array": [None, True, 0, "value"]}),
        )
        invalid_canonical = (
            (-1, "canonicalJsonIntegerOutOfRange"),
            (1.0, "canonicalJsonTypeInvalid"),
            ({1: "value"}, "canonicalJsonKeyInvalid"),
            ({"value": "\ud800"}, "canonicalJsonUnicodeScalarInvalid"),
        )
        for value, expected_code in invalid_canonical:
            with self.subTest(canonical=value), self.assertRaises(EvidenceError) as stopped:
                _canonical_json(value)
            self.assertEqual(expected_code, str(stopped.exception))

        self.assertEqual("0", _nonnegative_decimal("0"))
        self.assertEqual("12", _positive_decimal("12"))
        for value in (None, "", "01", "-1", "١"):
            with self.subTest(decimal=value), self.assertRaises(EvidenceError) as stopped:
                _nonnegative_decimal(value)
            self.assertEqual("evidenceInvalid", str(stopped.exception))
        with self.assertRaises(EvidenceError) as stopped:
            _positive_decimal("0")
        self.assertEqual("evidenceInvalid", str(stopped.exception))

        valid_crap = {
            "callableCount": 1,
            "crapMax": "8",
            "maxDenominator": "1",
            "maxNumerator": "8",
            "pass": True,
            "unknownCount": 0,
        }
        _validate_crap_component(valid_crap)
        _validate_crap_component(
            {
                "callableCount": 2,
                "crapMax": "8",
                "maxDenominator": "1",
                "maxNumerator": "0",
                "pass": False,
                "unknownCount": 2,
            }
        )
        _validate_crap_component(
            {
                "callableCount": 0,
                "crapMax": "8",
                "maxDenominator": "1",
                "maxNumerator": "0",
                "pass": False,
                "unknownCount": 0,
            }
        )
        _validate_crap_component(
            {
                "callableCount": 1,
                "crapMax": "8",
                "maxDenominator": "2",
                "maxNumerator": "15",
                "pass": True,
                "unknownCount": 0,
            }
        )
        _validate_crap_component(
            {
                "callableCount": 1,
                "crapMax": "8",
                "maxDenominator": "1",
                "maxNumerator": "9",
                "pass": False,
                "unknownCount": 0,
            }
        )
        invalid_crap = (
            {**valid_crap, "maxDenominator": "2", "maxNumerator": "2"},
            {**valid_crap, "pass": False},
            {**valid_crap, "unknownCount": 1},
            {**valid_crap, "callableCount": 1, "unknownCount": 2, "pass": False},
            {**valid_crap, "callableCount": 1, "unknownCount": 1, "pass": False},
            {**valid_crap, "maxNumerator": "0"},
            {**valid_crap, "maxNumerator": "1" * 97},
            {**valid_crap, "maxDenominator": "1" * 49},
        )
        for value in invalid_crap:
            with self.subTest(crap=value), self.assertRaises(EvidenceError) as stopped:
                _validate_crap_component(value)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

        required_names = [
            "callableCount",
            "crapMax",
            "maxDenominator",
            "maxNumerator",
            "pass",
            "unknownCount",
        ]
        for invalid_shape in ({}, required_names):
            with self.subTest(shape=type(invalid_shape).__name__):
                with self.assertRaises(EvidenceError) as stopped:
                    _validate_crap_component(invalid_shape)
                self.assertEqual(("evidenceInvalid",), stopped.exception.args)

        valid_finding = {
            "category": "mutation",
            "fingerprint": "hmac-sha256:" + "1" * 64,
            "reason": "survived",
        }
        self.assertEqual(valid_finding, _validated_finding(valid_finding))
        invalid_findings = (
            None,
            {**valid_finding, "extra": True},
            {**valid_finding, "category": "Bad"},
            {**valid_finding, "reason": "bad-code"},
            {**valid_finding, "fingerprint": 1},
            {**valid_finding, "fingerprint": "not-a-mac"},
        )
        for value in invalid_findings:
            with self.subTest(finding=value), self.assertRaises(EvidenceError) as stopped:
                _validated_finding(value)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

        valid_mutation = {
            "compileError": 0,
            "ignored": 0,
            "inScope": 1,
            "mutationMin": "100",
            "killed": 1,
            "pass": True,
            "pending": 0,
            "runtimeError": 0,
            "survived": 0,
            "timedOut": 0,
            "toolError": 0,
            "unauthorizedExclusion": 0,
            "uncovered": 0,
        }
        _validate_mutation_component(valid_mutation)
        _validate_mutation_component(
            {
                **valid_mutation,
                "pass": False,
                "unauthorizedExclusion": 3,
            }
        )
        invalid_components = (
            None,
            {name: value for name, value in valid_mutation.items() if name != "ignored"},
            {**valid_mutation, "ignored": True},
            {**valid_mutation, "pass": 1},
            {**valid_mutation, "inScope": 2},
            {**valid_mutation, "pass": False},
        )
        for value in invalid_components:
            with self.subTest(component=value), self.assertRaises(EvidenceError) as stopped:
                _validate_mutation_component(value)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_finding_class_distinguishes_code_test_and_ambiguous_failures(self):
        from sentinel_py.evidence.store import _finding_class

        cases = (
            ({"category": "crap", "reason": "crapThresholdExceeded"}, "projectCode"),
            ({"category": "mutation", "reason": "timedOut"}, "projectCodeOrTest"),
            ({"category": "mutation", "reason": "runtimeError"}, "projectCodeOrTest"),
            ({"category": "mutation", "reason": "survived"}, "projectTest"),
        )
        for finding, expected in cases:
            with self.subTest(finding=finding):
                self.assertEqual(expected, _finding_class(finding))

    def test_sequence_high_water_is_exact_and_rejects_duplicate_sequences(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _sequence_high_water

        self.assertEqual(0, _sequence_high_water(()))
        evidence = (
            {"commitSequence": "2", "runId": "00000000-0000-4000-8000-000000000002"},
            {"commitSequence": "5", "runId": "00000000-0000-4000-8000-000000000005"},
        )
        self.assertEqual(5, _sequence_high_water(evidence))
        with self.assertRaises(EvidenceError) as stopped:
            _sequence_high_water((evidence[0], evidence[0]))
        self.assertEqual("sequenceStateInvalid", str(stopped.exception))

    def test_regular_file_guard_rejects_type_and_link_count_independently(self):
        from sentinel_py.evidence import EvidenceError, store

        cases = (
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_nlink=1),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=2),
        )
        for metadata in cases:
            with (
                self.subTest(metadata=metadata),
                patch.object(store.os, "fstat", return_value=metadata),
                self.assertRaises(EvidenceError) as stopped,
            ):
                store._require_regular_file(17)
            self.assertEqual("stateFileInvalid", str(stopped.exception))

    def test_canonical_character_covers_surrogate_and_scalar_boundaries(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import _canonical_character

        for codepoint in (0xD800, 0xDFFF):
            with self.subTest(codepoint=codepoint):
                with self.assertRaises(EvidenceError) as stopped:
                    _canonical_character(chr(codepoint))
                self.assertEqual(
                    "canonicalJsonUnicodeScalarInvalid",
                    str(stopped.exception),
                )
        self.assertEqual(chr(0xE000).encode(), _canonical_character(chr(0xE000)))

    def test_evidence_identity_accepts_one_valid_cache_origin(self):
        from sentinel_py.evidence.store import _valid_evidence_identity

        document = {
            "command": "crap",
            "mode": "local",
            "observationSource": "cache",
            "sourceRunId": "00000000-0000-4000-8000-000000000001",
            "runId": "00000000-0000-4000-8000-000000000002",
            "correlationId": "00000000-0000-4000-8000-000000000003",
            "commitSequence": "1",
        }

        self.assertTrue(_valid_evidence_identity(document))

    def test_sequence_state_requires_unique_committed_values_within_allocation(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence import store

        key = bytes(range(32))
        first = {
            "commitSequence": "2",
            "runId": "00000000-0000-4000-8000-000000000002",
        }
        second = {
            "commitSequence": "5",
            "runId": "00000000-0000-4000-8000-000000000005",
        }
        with tempfile.TemporaryDirectory(prefix="sentinel-sequence-state-") as directory:
            state_root = Path(directory)
            sequence_path = state_root / "commit-sequence.json"

            self.assertIsNone(store._validate_sequence_state(state_root, (), key))
            with self.assertRaises(EvidenceError) as stopped:
                store._validate_sequence_state(state_root, (first,), key)
            self.assertEqual("sequenceStateInvalid", str(stopped.exception))

            sequence_path.write_text("state", encoding="utf-8")
            with patch.object(store, "_read_sequence", return_value=0):
                self.assertIsNone(
                    store._validate_sequence_state(state_root, (), key)
                )
            with patch.object(store, "_read_sequence", return_value=5) as read_sequence:
                self.assertIsNone(
                    store._validate_sequence_state(
                        state_root,
                        (second, first),
                        key,
                    )
                )
            read_sequence.assert_called_once_with(sequence_path, key)

            for evidence, allocated in (
                ((first, first), 5),
                ((second,), 4),
            ):
                with self.subTest(evidence=evidence, allocated=allocated):
                    with patch.object(store, "_read_sequence", return_value=allocated):
                        with self.assertRaises(EvidenceError) as stopped:
                            store._validate_sequence_state(state_root, evidence, key)
                    self.assertEqual("sequenceStateInvalid", str(stopped.exception))

    def test_mutation_semantics_require_nonzero_all_killed_and_no_exclusion_to_pass(self):
        from sentinel_py.evidence.store import _valid_mutation_semantics

        def component(*, in_scope, killed, survived, unauthorized, passed):
            return {
                "compileError": 0,
                "ignored": 0,
                "inScope": in_scope,
                "mutationMin": "100",
                "killed": killed,
                "pass": passed,
                "pending": 0,
                "runtimeError": 0,
                "survived": survived,
                "timedOut": 0,
                "toolError": 0,
                "unauthorizedExclusion": unauthorized,
                "uncovered": 0,
            }

        cases = (
            (component(in_scope=1, killed=1, survived=0, unauthorized=0, passed=True), True),
            (component(in_scope=0, killed=0, survived=0, unauthorized=0, passed=False), True),
            (component(in_scope=0, killed=0, survived=0, unauthorized=0, passed=True), False),
            (component(in_scope=1, killed=1, survived=0, unauthorized=1, passed=False), True),
            (component(in_scope=1, killed=1, survived=0, unauthorized=1, passed=True), False),
            (component(in_scope=1, killed=0, survived=1, unauthorized=0, passed=False), True),
            (component(in_scope=1, killed=0, survived=1, unauthorized=0, passed=True), False),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertIs(expected, _valid_mutation_semantics(value))

    def test_public_finding_fingerprint_binds_every_identity_field(self):
        from sentinel_py.evidence import FindingIdentity
        from sentinel_py.evidence.store import _public_finding

        finding = FindingIdentity(
            "mutation",
            "survived",
            "python",
            "api",
            "src/a.py",
            "candidate-a",
        )

        self.assertEqual(
            {
                "category": "mutation",
                "fingerprint": (
                    "hmac-sha256:"
                    "26d0653aa9b7e149696ac842b69080ebdd977b53998ffc91d3fb0bdc0a147943"
                ),
                "reason": "survived",
            },
            _public_finding(finding, b"k" * 32),
        )

    def test_canonical_json_matches_the_cross_runtime_control_character_contract(self):
        from sentinel_py.evidence.store import _canonical_json

        document = {
            "z": "quote=\" slash=/ backslash=\\ controls=\b\t\n\f\r\u0000\u001f",
            "가": "한글",
        }

        self.assertEqual(
            (
                b'{"z":"quote=\\\" slash=/ backslash=\\\\ controls='
                b'\\u0008\\u0009\\u000a\\u000c\\u000d\\u0000\\u001f",'
                b'"\xea\xb0\x80":"\xed\x95\x9c\xea\xb8\x80"}'
            ),
            _canonical_json(document),
        )

    def test_completed_evidence_uses_the_common_top_level_wire_contract(self):
        from sentinel_py.evidence import commit_quality_evidence

        with tempfile.TemporaryDirectory(prefix="sentinel-evidence-contract-") as directory:
            project = Path(directory)
            run = {**self._run(), "mode": "strict"}
            evidence = commit_quality_evidence(project, run, self._summary(), ())
            run_root = project / ".sentinel" / "state-v1" / "runs" / evidence["runId"]
            started_payload = (run_root / "started.json").read_bytes()

        self.assertEqual(
            {
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
            },
            set(evidence),
        )
        self.assertEqual("1", evidence["commitSequence"])
        self.assertEqual("sentinel-evidence-v1", evidence["schemaVersion"])
        self.assertEqual("sentinel-fingerprint-v1", evidence["fingerprintVersion"])
        self.assertIsNone(evidence["sourceRunId"])
        self.assertTrue(evidence["certification"])
        self.assertEqual(0, evidence["exitCode"])
        self.assertEqual([], evidence["diagnosticCodes"])
        self.assertEqual(0, evidence["eventCount"])
        self.assertEqual([], evidence["events"])
        self.assertEqual(
            hashlib.sha256(started_payload).hexdigest(),
            evidence["startedSha256"],
        )
        self.assertEqual(
            {
                "callableCount": 1,
                "crapMax": "8",
                "maxDenominator": "1",
                "maxNumerator": "1",
                "pass": True,
                "unknownCount": 0,
            },
            evidence["components"]["crap"],
        )

    def test_evidence_hmac_matches_the_shared_spec_golden_vector(self):
        from sentinel_py.evidence.store import (
            _EVIDENCE_KEY_NAMESPACE,
            _EVIDENCE_MAC_NAMESPACE,
            _ProjectKeys,
            _canonical_json,
            _namespaced_document_hmac,
            _project_state_hmac,
        )

        keys = _ProjectKeys(bytes(range(16)), bytes(range(32)), bytes(range(32, 64)), 1)
        body = {
            "certification": True,
            "command": "check",
            "commitSequence": "1",
            "committedAtUtc": "2026-09-03T12:00:01.2Z",
            "completedAtUtc": "2026-09-03T12:00:01.1Z",
            "components": {
                "crap": {
                    "callableCount": 1,
                    "crapMax": "8",
                    "maxDenominator": "1",
                    "maxNumerator": "8",
                    "pass": True,
                    "unknownCount": 0,
                },
                "mutation": {
                    "compileError": 0,
                    "ignored": 0,
                    "inScope": 1,
                    "mutationMin": "100",
                    "killed": 1,
                    "pass": True,
                    "pending": 0,
                    "runtimeError": 0,
                    "survived": 0,
                    "timedOut": 0,
                    "toolError": 0,
                    "unauthorizedExclusion": 0,
                    "uncovered": 0,
                },
            },
            "correlationId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "diagnosticCodes": [],
            "eventCount": 1,
            "events": [
                {
                    "filename": "00000000000000000000000000000001.json",
                    "sha256": "a" * 64,
                }
            ],
            "exitCode": 0,
            "fingerprintVersion": "sentinel-fingerprint-v1",
            "keyEpoch": 1,
            "language": "python",
            "mode": "strict",
            "observationSource": "fresh",
            "projectStateHmac": "441395de4352207dc696516a31efa8fb34fc5d4df9a9005537342cda21e84354",
            "runId": "11111111-1111-4111-8111-111111111111",
            "schemaVersion": "sentinel-evidence-v1",
            "sourceRunId": None,
            "specVersion": "1.0.0",
            "startedAtUtc": "2026-09-03T12:00:00Z",
            "startedSha256": "b" * 64,
            "terminalStatus": "passed",
        }

        self.assertEqual(
            "441395de4352207dc696516a31efa8fb34fc5d4df9a9005537342cda21e84354",
            _project_state_hmac(keys),
        )
        self.assertEqual(
            "93a268af3b825df203d008c9fb475defad0d85e9db0f3855e27f7266d4527caf",
            hashlib.sha256(_canonical_json(body)).hexdigest(),
        )
        self.assertEqual(
            "1aaaa38f58a49f6d64bcda9f05037e4432c88766f02a3000f8d9ef520fffc66e",
            _namespaced_document_hmac(
                keys.cleanup_lease_key,
                _EVIDENCE_KEY_NAMESPACE,
                _EVIDENCE_MAC_NAMESPACE,
                body,
            ),
        )

    def test_authenticated_evidence_rejects_cross_field_semantic_tampering(self):
        from sentinel_py.evidence import EvidenceError, commit_quality_evidence, read_history

        updates = (
            lambda value: value.__setitem__("completedAtUtc", "2026-09-03T09:59:59Z"),
            lambda value: value.__setitem__("command", "mutation"),
            lambda value: value.__setitem__("certification", True),
            lambda value: value.__setitem__("sourceRunId", str(uuid.uuid4())),
            lambda value: value["components"]["crap"].__setitem__("pass", False),
        )
        for index, update in enumerate(updates):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                prefix="sentinel-cross-field-"
            ) as directory:
                project = Path(directory)
                commit_quality_evidence(project, self._run(), self._summary(), ())
                self._rewrite_evidence(project, update)

                with self.assertRaises(EvidenceError) as stopped:
                    read_history(project, repeated_only=False)

                self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_private_numeric_and_base64_decoders_reject_every_boundary(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _UINT64_MAX,
            _canonical_uint64,
            _decode_base64url,
            _encode_base64url,
        )

        self.assertTrue(_canonical_uint64("1"))
        self.assertTrue(_canonical_uint64(str(_UINT64_MAX)))
        for value in (None, 1, "", "0", "01", "-1", "1.0", "١", str(_UINT64_MAX + 1)):
            with self.subTest(value=value):
                self.assertFalse(_canonical_uint64(value))

        encoded = _encode_base64url(b"a" * 32)
        self.assertEqual(b"a" * 32, _decode_base64url(encoded, 32))
        for value, size in ((None, 32), ("has=padding", 32), ("A", 32), ("AB", 1), (encoded, 16)):
            with self.subTest(encoded=value, size=size):
                with self.assertRaises(EvidenceError) as stopped:
                    _decode_base64url(value, size)
                self.assertEqual("projectStateInvalid", str(stopped.exception))

    def test_project_state_rejects_schema_epoch_and_key_separation_failures(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _JSON_SAFE_INTEGER_MAX,
            _canonical_json,
            _encode_base64url,
            _read_project_keys,
        )

        valid = {
            "cleanupLeaseKey": _encode_base64url(b"c" * 32),
            "fingerprintHmacKey": _encode_base64url(b"f" * 32),
            "keyEpoch": _JSON_SAFE_INTEGER_MAX,
            "projectIdentifier": _encode_base64url(b"p" * 16),
            "schemaVersion": "sentinel-project-state-v1",
            "stateVersion": "state-v1",
        }
        cases = (
            {name: value for name, value in valid.items() if name != "stateVersion"},
            {**valid, "schemaVersion": "project-state-v1"},
            {**valid, "stateVersion": "STATE-V1"},
            {**valid, "keyEpoch": True},
            {**valid, "keyEpoch": 0},
            {**valid, "keyEpoch": _JSON_SAFE_INTEGER_MAX + 1},
            {**valid, "cleanupLeaseKey": valid["fingerprintHmacKey"]},
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-project-state-cases-") as directory:
            path = Path(directory) / "project.json"
            path.write_bytes(_canonical_json(valid) + b"\n")
            self.assertEqual(_JSON_SAFE_INTEGER_MAX, _read_project_keys(path).key_epoch)
            for index, document in enumerate(cases):
                with self.subTest(index=index):
                    if document["keyEpoch"] == _JSON_SAFE_INTEGER_MAX + 1:
                        payload = json.dumps(
                            document,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode() + b"\n"
                    else:
                        payload = _canonical_json(document) + b"\n"
                    path.write_bytes(payload)
                    with self.assertRaises(EvidenceError) as stopped:
                        _read_project_keys(path)
                    self.assertEqual("projectStateInvalid", str(stopped.exception))

    def test_evidence_metadata_checks_every_state_binding_field(self):
        from sentinel_py.evidence import EvidenceError
        from sentinel_py.evidence.store import (
            _ProjectKeys,
            _project_state_hmac,
            _validate_evidence_metadata,
        )

        keys = _ProjectKeys(b"p" * 16, b"f" * 32, b"c" * 32, 7)
        valid = {
            "fingerprintVersion": "sentinel-fingerprint-v1",
            "language": "python",
            "schemaVersion": "sentinel-evidence-v1",
            "specVersion": "1.0.0",
            "keyEpoch": 7,
            "projectStateHmac": _project_state_hmac(keys),
        }
        _validate_evidence_metadata(valid, keys)
        cases = (
            {**valid, "schemaVersion": "evidence-v1"},
            {**valid, "specVersion": "2.0.0"},
            {**valid, "fingerprintVersion": "fingerprint-v1"},
            {**valid, "language": "typescript"},
            {**valid, "keyEpoch": True},
            {**valid, "keyEpoch": 8},
            {**valid, "projectStateHmac": "0" * 64},
        )
        for index, document in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(EvidenceError) as stopped:
                    _validate_evidence_metadata(document, keys)
                self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_project_state_binding_survives_fingerprint_key_rotation(self):
        from sentinel_py.evidence import FindingIdentity, commit_quality_evidence, read_history
        from sentinel_py.evidence.store import (
            _ProjectKeys,
            _canonical_json,
            _encode_base64url,
            _project_state_hmac,
        )

        epoch_one = _ProjectKeys(b"p" * 16, b"a" * 32, b"c" * 32, 1)
        epoch_two = _ProjectKeys(b"p" * 16, b"b" * 32, b"c" * 32, 2)
        another_project = _ProjectKeys(b"q" * 16, b"b" * 32, b"c" * 32, 2)

        self.assertEqual(_project_state_hmac(epoch_one), _project_state_hmac(epoch_two))
        self.assertNotEqual(
            _project_state_hmac(epoch_two),
            _project_state_hmac(another_project),
        )

        finding = FindingIdentity("crap", "crapThresholdExceeded", "python", "api", "a.py", "id")
        with tempfile.TemporaryDirectory(prefix="sentinel-key-rotation-") as directory:
            project = Path(directory)
            commit_quality_evidence(project, self._run(), self._summary(), (finding,))
            project_path = project / ".sentinel" / "state-v1" / "project.json"
            state = json.loads(project_path.read_text(encoding="utf-8"))
            state["fingerprintHmacKey"] = _encode_base64url(b"n" * 32)
            state["keyEpoch"] = 2
            project_path.write_bytes(_canonical_json(state) + b"\n")

            history = read_history(project, repeated_only=False)

        self.assertEqual(1, history["completedRuns"])
        self.assertEqual(1, len(history["findings"]))

    def test_authenticated_run_id_must_match_its_directory(self):
        from sentinel_py.evidence import (
            EvidenceError,
            commit_quality_evidence,
            read_history,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-run-binding-") as directory:
            project = Path(directory)
            commit_quality_evidence(project, self._run(), self._summary(), ())
            self._rewrite_evidence(
                project,
                lambda document: document.__setitem__("runId", str(uuid.uuid4())),
            )

            with self.assertRaises(EvidenceError) as stopped:
                read_history(project, repeated_only=False)

        self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_authenticated_public_findings_reject_unsafe_field_values(self):
        from sentinel_py.evidence import (
            EvidenceError,
            FindingIdentity,
            commit_quality_evidence,
            read_history,
        )

        finding = FindingIdentity("mutation", "survived", "python", "api", "a.py", "id")
        cases = (
            ("defectKind", 1),
            ("findingToken", "not-a-mac"),
            ("diagnosticCode", "bad\x00code"),
        )
        for index, (field, replacement) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory(
                prefix="sentinel-finding-shape-"
            ) as directory:
                project = Path(directory)
                commit_quality_evidence(project, self._run(), self._summary(), (finding,))
                self._rewrite_first_event(
                    project,
                    lambda document, name=field, value=replacement: document.__setitem__(
                        name, value
                    ),
                )

                with self.assertRaises(EvidenceError) as stopped:
                    read_history(project, repeated_only=False)

                self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_event_manifest_rejects_missing_reordered_and_extra_entries(self):
        from sentinel_py.evidence import (
            EvidenceError,
            FindingIdentity,
            commit_quality_evidence,
            read_history,
        )

        finding = FindingIdentity("mutation", "survived", "python", "api", "a.py", "id")

        with tempfile.TemporaryDirectory(prefix="sentinel-event-root-") as directory:
            project = Path(directory)
            evidence = commit_quality_evidence(project, self._run(), self._summary(), (finding,))
            events_root = (
                project
                / ".sentinel"
                / "state-v1"
                / "runs"
                / evidence["runId"]
                / "events"
            )
            moved = events_root.with_name("events-moved")
            events_root.rename(moved)
            events_root.symlink_to(moved.name, target_is_directory=True)
            with self.assertRaises(EvidenceError) as stopped:
                read_history(project, repeated_only=False)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

        with tempfile.TemporaryDirectory(prefix="sentinel-event-order-") as directory:
            project = Path(directory)
            commit_quality_evidence(project, self._run(), self._summary(), (finding,))
            self._rewrite_evidence(
                project,
                lambda document: document["events"][0].__setitem__(
                    "filename", "00000000000000000000000000000002.json"
                ),
            )
            with self.assertRaises(EvidenceError) as stopped:
                read_history(project, repeated_only=False)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

        with tempfile.TemporaryDirectory(prefix="sentinel-event-extra-") as directory:
            project = Path(directory)
            evidence = commit_quality_evidence(project, self._run(), self._summary(), (finding,))
            events_root = (
                project
                / ".sentinel"
                / "state-v1"
                / "runs"
                / evidence["runId"]
                / "events"
            )
            (events_root / "00000000000000000000000000000002.json").write_bytes(b"{}\n")
            with self.assertRaises(EvidenceError) as stopped:
                read_history(project, repeated_only=False)
            self.assertEqual("evidenceInvalid", str(stopped.exception))

    def test_cross_process_commits_allocate_every_sequence_once(self):
        from sentinel_py.evidence import read_history

        with tempfile.TemporaryDirectory(prefix="sentinel-concurrent-commit-") as directory:
            context = multiprocessing.get_context("spawn")
            processes = tuple(
                context.Process(target=_commit_worker, args=(directory, index))
                for index in range(6)
            )
            for process in processes:
                process.start()
            for process in processes:
                process.join(20)
            self.assertEqual([0] * 6, [process.exitcode for process in processes])
            history = read_history(Path(directory), repeated_only=False)
            evidence_paths = tuple(
                (Path(directory) / ".sentinel" / "state-v1" / "runs").glob(
                    "*/evidence.json"
                )
            )
            sequences = sorted(
                int(json.loads(path.read_text(encoding="utf-8"))["commitSequence"])
                for path in evidence_paths
            )

        self.assertEqual(6, history["completedRuns"])
        self.assertEqual(list(range(1, 7)), sequences)

    def test_commit_lock_uses_the_cross_runtime_posix_byte_range(self):
        from sentinel_py.evidence.store import _exclusive_state

        with tempfile.TemporaryDirectory(prefix="sentinel-byte-lock-") as directory:
            state_root = Path(directory) / ".sentinel" / "state-v1"
            with patch("sentinel_py.evidence.store.fcntl.lockf") as lockf:
                with _exclusive_state(state_root):
                    pass

        self.assertEqual(
            [
                call(ANY, fcntl.LOCK_EX, 1, 0, os.SEEK_SET),
                call(ANY, fcntl.LOCK_UN, 1, 0, os.SEEK_SET),
            ],
            lockf.call_args_list,
        )

    def test_completed_evidence_has_an_integrity_mac_and_tampering_is_rejected(self):
        from sentinel_py.evidence import (
            EvidenceError,
            commit_quality_evidence,
            read_history,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-evidence-mac-") as directory:
            project = Path(directory)
            evidence = commit_quality_evidence(project, self._run(), self._summary(), ())
            self.assertRegex(evidence["hmacSha256"], r"^[0-9a-f]{64}$")
            evidence_path = next(
                (project / ".sentinel" / "state-v1" / "runs").glob(
                    "*/evidence.json"
                )
            )
            document = json.loads(evidence_path.read_text(encoding="utf-8"))
            document["components"]["crap"]["pass"] = False
            evidence_path.write_text(
                json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            before = self._tree_snapshot(project / ".sentinel")

            with self.assertRaises(EvidenceError) as stopped:
                read_history(project, repeated_only=False)

            self.assertEqual("evidenceInvalid", str(stopped.exception))
            self.assertEqual(before, self._tree_snapshot(project / ".sentinel"))

    def test_commit_sequence_rollback_is_rejected_without_a_new_run(self):
        from sentinel_py.evidence import (
            EvidenceError,
            commit_quality_evidence,
            read_history,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-sequence-rollback-") as directory:
            project = Path(directory)
            commit_quality_evidence(project, self._run(), self._summary(), ())
            state_root = project / ".sentinel" / "state-v1"
            sequence_path = state_root / "commit-sequence.json"
            self.assertTrue(sequence_path.is_file())
            first_sequence = sequence_path.read_bytes()
            commit_quality_evidence(project, self._run(), self._summary(), ())
            sequence_path.write_bytes(first_sequence)
            run_count = len(tuple((state_root / "runs").iterdir()))

            with self.assertRaises(EvidenceError) as stopped:
                commit_quality_evidence(project, self._run(), self._summary(), ())

            self.assertEqual("sequenceStateInvalid", str(stopped.exception))
            self.assertEqual(run_count, len(tuple((state_root / "runs").iterdir())))
            with self.assertRaises(EvidenceError):
                read_history(project, repeated_only=False)

    def test_project_keys_are_separate_and_state_files_are_canonical_owner_only(self):
        from sentinel_py.evidence import commit_quality_evidence

        with tempfile.TemporaryDirectory(prefix="sentinel-private-state-") as directory:
            project = Path(directory)
            evidence = commit_quality_evidence(project, self._run(), self._summary(), ())
            state_root = project / ".sentinel" / "state-v1"
            project_path = state_root / "project.json"
            sequence_path = state_root / "commit-sequence.json"
            evidence_path = next((state_root / "runs").glob("*/evidence.json"))
            project_state = json.loads(project_path.read_text(encoding="utf-8"))
            fingerprint_key = self._decode_key(project_state["fingerprintHmacKey"])
            cleanup_key = self._decode_key(project_state["cleanupLeaseKey"])

            self.assertEqual(16, len(self._decode_key(project_state["projectIdentifier"])))
            self.assertEqual(32, len(fingerprint_key))
            self.assertEqual(32, len(cleanup_key))
            self.assertNotEqual(fingerprint_key, cleanup_key)
            self.assertEqual(1, project_state["keyEpoch"])
            self.assertEqual("1", json.loads(sequence_path.read_text())["lastAllocated"])
            self.assertRegex(evidence["projectStateHmac"], r"^[0-9a-f]{64}$")
            for path in (project_path, sequence_path, evidence_path):
                with self.subTest(path=path.name):
                    self.assertEqual(0o600, path.stat().st_mode & 0o777)
                    document = json.loads(path.read_text(encoding="utf-8"))
                    expected = json.dumps(
                        document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode() + b"\n"
                    self.assertEqual(expected, path.read_bytes())

    def test_repeated_history_counts_distinct_runs_not_duplicate_rows(self):
        from sentinel_py.evidence import (
            FindingIdentity,
            commit_quality_evidence,
            read_history,
        )

        finding = FindingIdentity(
            category="mutation",
            reason="survived",
            language="python",
            module="api",
            module_relative_path="private/subject.py",
            subject_id="private-callable",
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-repeat-history-") as directory:
            project = Path(directory)
            commit_quality_evidence(
                project,
                self._run(),
                self._summary(),
                (finding, finding),
            )
            commit_quality_evidence(project, self._run(), self._summary(), (finding,))

            history = read_history(project, repeated_only=True)

        self.assertEqual(2, history["completedRuns"])
        self.assertEqual(1, len(history["findings"]))
        row = history["findings"][0]
        self.assertEqual(2, row["observationCount"])
        self.assertEqual(1, row["firstSequence"])
        self.assertEqual(2, row["lastSequence"])
        self.assertTrue(row["repeated"])
        self.assertNotIn("private", json.dumps(history))

    def test_history_reads_completed_evidence_without_chmod_or_state_writes(self):
        from sentinel_py.evidence import commit_quality_evidence, read_history

        with tempfile.TemporaryDirectory(prefix="sentinel-evidence-read-") as directory:
            project = Path(directory)
            commit_quality_evidence(project, self._run(), self._summary(), ())
            before = self._tree_snapshot(project / ".sentinel")

            with (
                patch("sentinel_py.evidence.store.os.fchmod") as chmod,
                patch("sentinel_py.evidence.store.fcntl.lockf") as lockf,
            ):
                history = read_history(project, repeated_only=False)

            self.assertEqual(1, history["completedRuns"])
            chmod.assert_not_called()
            self.assertEqual(
                [
                    call(ANY, fcntl.LOCK_SH, 1, 0, os.SEEK_SET),
                    call(ANY, fcntl.LOCK_UN, 1, 0, os.SEEK_SET),
                ],
                lockf.call_args_list,
            )
            self.assertEqual(before, self._tree_snapshot(project / ".sentinel"))

    def test_consecutive_runs_have_unique_monotonic_sequences(self):
        from sentinel_py.evidence import commit_quality_evidence, read_history

        with tempfile.TemporaryDirectory(prefix="sentinel-evidence-sequence-") as directory:
            project = Path(directory)
            first = commit_quality_evidence(project, self._run(), self._summary(), ())
            second = commit_quality_evidence(project, self._run(), self._summary(), ())

            history = read_history(project, repeated_only=False)

            self.assertEqual("1", first["commitSequence"])
            self.assertEqual("2", second["commitSequence"])
            self.assertNotEqual(first["runId"], second["runId"])
            self.assertEqual(2, history["completedRuns"])

    def test_mutation_and_check_committers_publish_their_exact_component_sets(self):
        from sentinel_py.evidence import commit_check_evidence, commit_mutation_evidence

        counts = {
            "compileError": 0,
            "ignored": 0,
            "killed": 1,
            "pending": 0,
            "runtimeError": 0,
            "survived": 0,
            "timedOut": 0,
            "toolError": 0,
            "uncovered": 0,
        }
        mutation = {
            "counts": counts,
            "inScope": 1,
            "mutationMin": "100",
            "killRateDenominator": "1",
            "killRateNumerator": "1",
            "killRatePercent": "100",
            "killed": 1,
            "pass": True,
            "reason": "passed",
        }
        with tempfile.TemporaryDirectory(prefix="sentinel-component-commit-") as directory:
            project = Path(directory)
            mutation_run = {**self._run(), "command": "mutation"}
            mutation_evidence = commit_mutation_evidence(
                project,
                mutation_run,
                mutation,
                (),
            )
            check_run = {**self._run(), "command": "check"}
            check_evidence = commit_check_evidence(
                project,
                check_run,
                self._summary(),
                mutation,
                (),
            )

        self.assertEqual({"mutation"}, set(mutation_evidence["components"]))
        self.assertEqual({"crap", "mutation"}, set(check_evidence["components"]))
        self.assertEqual(1, mutation_evidence["components"]["mutation"]["inScope"])
        self.assertEqual("2", check_evidence["commitSequence"])

    @staticmethod
    def _run() -> dict:
        run_id = str(uuid.uuid4())
        return {
            "command": "crap",
            "completedAt": "2026-09-03T10:00:00Z",
            "correlationId": run_id,
            "mode": "local",
            "runId": run_id,
            "terminalStatus": "passed",
        }

    @staticmethod
    def _summary() -> dict:
        return {
            "callableCount": 1,
            "crapMax": "8",
            "maxDenominator": "1",
            "maxNumerator": "1",
            "pass": True,
            "reason": "passed",
            "unknownCount": 0,
        }

    @staticmethod
    def _decode_key(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @staticmethod
    def _rewrite_evidence(project: Path, update) -> None:
        from sentinel_py.evidence.store import (
            _EVIDENCE_KEY_NAMESPACE,
            _EVIDENCE_MAC_NAMESPACE,
            _canonical_json,
            _namespaced_document_hmac,
            _read_project_keys,
        )

        state_root = project / ".sentinel" / "state-v1"
        path = next((state_root / "runs").glob("*/evidence.json"))
        document = json.loads(path.read_text(encoding="utf-8"))
        update(document)
        body = {name: value for name, value in document.items() if name != "hmacSha256"}
        keys = _read_project_keys(state_root / "project.json")
        document["hmacSha256"] = _namespaced_document_hmac(
            keys.cleanup_lease_key,
            _EVIDENCE_KEY_NAMESPACE,
            _EVIDENCE_MAC_NAMESPACE,
            body,
        )
        path.write_bytes(_canonical_json(document) + b"\n")

    @staticmethod
    def _rewrite_first_event(project: Path, update) -> None:
        from sentinel_py.evidence.store import (
            _EVENT_KEY_NAMESPACE,
            _EVENT_MAC_NAMESPACE,
            _EVIDENCE_KEY_NAMESPACE,
            _EVIDENCE_MAC_NAMESPACE,
            _canonical_json,
            _namespaced_document_hmac,
            _read_project_keys,
        )

        state_root = project / ".sentinel" / "state-v1"
        evidence_path = next((state_root / "runs").glob("*/evidence.json"))
        event_path = next((evidence_path.parent / "events").glob("*.json"))
        keys = _read_project_keys(state_root / "project.json")
        event = json.loads(event_path.read_text(encoding="utf-8"))
        update(event)
        event_body = {
            name: value for name, value in event.items() if name != "hmacSha256"
        }
        event["hmacSha256"] = _namespaced_document_hmac(
            keys.cleanup_lease_key,
            _EVENT_KEY_NAMESPACE,
            _EVENT_MAC_NAMESPACE,
            event_body,
        )
        event_payload = _canonical_json(event) + b"\n"
        event_path.write_bytes(event_payload)

        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["events"][0]["sha256"] = hashlib.sha256(event_payload).hexdigest()
        evidence_body = {
            name: value for name, value in evidence.items() if name != "hmacSha256"
        }
        evidence["hmacSha256"] = _namespaced_document_hmac(
            keys.cleanup_lease_key,
            _EVIDENCE_KEY_NAMESPACE,
            _EVIDENCE_MAC_NAMESPACE,
            evidence_body,
        )
        evidence_path.write_bytes(_canonical_json(evidence) + b"\n")

    @staticmethod
    def _tree_snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            sorted(
                (
                    path.relative_to(root).as_posix(),
                    path.stat().st_mode,
                    path.stat().st_mtime_ns,
                )
                for path in root.rglob("*")
            )
        )


if __name__ == "__main__":
    unittest.main()
