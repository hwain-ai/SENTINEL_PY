from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


class BackendLockTests(unittest.TestCase):
    def test_backend_error_and_json_constant_preserve_the_exact_code(self):
        from sentinel_py.backend_lock import BackendLockError, _reject_constant

        error = BackendLockError("exactCode")
        self.assertEqual("exactCode", error.code)
        self.assertEqual(("exactCode",), error.args)
        with self.assertRaises(BackendLockError) as stopped:
            _reject_constant("NaN")
        self.assertEqual("backendLockInvalid", stopped.exception.code)
        self.assertEqual(("backendLockInvalid",), stopped.exception.args)

    def test_repository_and_packaged_backend_locks_are_identical_and_verified(self):
        from sentinel_py.backend_lock import packaged_lock_path, verify_backend_lock

        repository_lock = REPOSITORY_ROOT / "backend.lock.json"

        self.assertEqual(repository_lock.read_bytes(), packaged_lock_path().read_bytes())
        self.assertEqual(
            "mutmut-3.7.0-raw-state-v2",
            json.loads(repository_lock.read_text(encoding="utf-8"))["rawStateMapVersion"],
        )
        identity = verify_backend_lock()
        self.assertEqual("mutmut", identity.name)
        self.assertEqual("3.7.0", identity.version)
        self.assertEqual(20, identity.module_file_count)

    def test_tampered_backend_digest_is_rejected(self):
        from sentinel_py.backend_lock import BackendLockError, verify_backend_lock

        document = json.loads((REPOSITORY_ROOT / "backend.lock.json").read_text())
        document["moduleTreeSha256"] = "0" * 64
        with tempfile.TemporaryDirectory(prefix="sentinel-backend-lock-") as directory:
            path = Path(directory) / "backend.lock.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(BackendLockError) as stopped:
                verify_backend_lock(path)

            self.assertEqual("backendModuleTreeMismatch", stopped.exception.code)

    def test_unavailable_and_malformed_backend_locks_are_rejected(self):
        from sentinel_py.backend_lock import BackendLockError, verify_backend_lock

        cases = (
            (None, "backendLockUnavailable"),
            (b"\xff", "backendLockInvalid"),
            (
                b'{"schemaVersion":"sentinel-backend-lock-v1","x":NaN}',
                "backendLockInvalid",
            ),
            (
                b'{"schemaVersion":"first","schemaVersion":"duplicate"}',
                "backendLockInvalid",
            ),
            (b"{}", "backendLockInvalid"),
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-backend-lock-") as directory:
            path = Path(directory) / "backend.lock.json"
            for payload, expected in cases:
                with self.subTest(expected=expected, payload=payload):
                    if payload is not None:
                        path.write_bytes(payload)
                    elif path.exists():
                        path.unlink()

                    with self.assertRaises(BackendLockError) as stopped:
                        verify_backend_lock(path)

                    self.assertEqual(expected, stopped.exception.code)

    def test_fixed_identity_and_file_count_failures_have_exact_codes(self):
        from sentinel_py.backend_lock import (
            BackendLockError,
            _validate_fixed_identity,
            verify_backend_lock,
        )

        document = json.loads(
            (REPOSITORY_ROOT / "backend.lock.json").read_text(encoding="utf-8")
        )
        invalid = {**document, "backendName": "other"}
        with self.assertRaises(BackendLockError) as stopped:
            _validate_fixed_identity(invalid)
        self.assertEqual("backendLockIdentityMismatch", stopped.exception.code)
        self.assertEqual(("backendLockIdentityMismatch",), stopped.exception.args)

        path = Path("/approved/backend.lock.json")
        with (
            patch("sentinel_py.backend_lock._load_lock", return_value=document),
            patch("sentinel_py.backend_lock._validate_fixed_identity"),
            patch("sentinel_py.backend_lock._installed_distribution"),
            patch(
                "sentinel_py.backend_lock._module_tree_identity",
                return_value=(document["moduleFileCount"] + 1, document["moduleTreeSha256"]),
            ),
            self.assertRaises(BackendLockError) as stopped,
        ):
            verify_backend_lock(path)
        self.assertEqual("backendModuleFileCountMismatch", stopped.exception.code)
        self.assertEqual(
            ("backendModuleFileCountMismatch",),
            stopped.exception.args,
        )

    def test_lock_parser_keeps_duplicate_and_nonstandard_number_guards_enabled(self):
        from sentinel_py import backend_lock

        document = {name: None for name in backend_lock._FIELDS}
        path = Mock()
        path.is_symlink.return_value = False
        path.is_file.return_value = True
        path.read_bytes.return_value = b"{}"
        with patch.object(
            backend_lock.json,
            "loads",
            return_value=document,
        ) as loads:
            self.assertIs(document, backend_lock._load_lock(path))

        loads.assert_called_once_with(
            "{}",
            object_pairs_hook=backend_lock._unique_object,
            parse_constant=backend_lock._reject_constant,
        )

    def test_mutation_refuses_to_start_before_backend_admission_passes(self):
        from sentinel_py.backend_lock import BackendLockError
        from sentinel_py.runner.mutation_backend import run_mutmut

        with patch(
            "sentinel_py.runner.mutation_backend.verify_backend_lock",
            side_effect=BackendLockError("backendModuleTreeMismatch"),
        ), patch("sentinel_py.runner.mutation_backend._protected_inventory") as inventory:
            with self.assertRaises(BackendLockError):
                run_mutmut(object())

        inventory.assert_not_called()

    def test_lock_number_validation_checks_minimum_type_and_both_digests(self):
        from sentinel_py.backend_lock import BackendLockError, _validate_lock_numbers

        valid = {
            "moduleFileCount": 1,
            "moduleTreeSha256": "a" * 64,
            "operatorInventorySha256": "b" * 64,
        }
        self.assertIsNone(_validate_lock_numbers(valid))
        invalid = (
            {**valid, "moduleFileCount": True},
            {**valid, "moduleFileCount": 0},
            {**valid, "moduleTreeSha256": None},
            {**valid, "operatorInventorySha256": "B" * 64},
        )
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(BackendLockError) as stopped:
                _validate_lock_numbers(document)
            self.assertEqual("backendLockInvalid", stopped.exception.code)

    def test_installed_distribution_uses_exact_name_version_and_errors(self):
        import importlib.metadata

        from sentinel_py.backend_lock import BackendLockError, _installed_distribution

        installed = SimpleNamespace(version="3.7.0")
        with patch(
            "sentinel_py.backend_lock.importlib.metadata.distribution",
            return_value=installed,
        ) as distribution:
            self.assertIs(installed, _installed_distribution("3.7.0"))
        distribution.assert_called_once_with("mutmut")

        with patch(
            "sentinel_py.backend_lock.importlib.metadata.distribution",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            with self.assertRaises(BackendLockError) as missing:
                _installed_distribution("3.7.0")
        self.assertEqual("backendNotInstalled", missing.exception.code)

        with patch(
            "sentinel_py.backend_lock.importlib.metadata.distribution",
            return_value=SimpleNamespace(version="3.6.0"),
        ):
            with self.assertRaises(BackendLockError) as mismatched:
                _installed_distribution("3.7.0")
        self.assertEqual("backendVersionMismatch", mismatched.exception.code)

    def test_operator_inventory_validates_shape_order_and_canonical_digest(self):
        from sentinel_py.backend_lock import BackendLockError, _verify_operator_inventory

        operators = (object(), object())
        names = ("operator-a", "operator-b")
        payload = json.dumps(names, separators=(",", ":")).encode() + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        valid = {
            "operatorInventory": list(names),
            "operatorInventorySha256": digest,
        }
        with (
            patch("sentinel_py.backend_lock.mutation_operators", operators),
            patch(
                "sentinel_py.backend_lock._operator_name",
                side_effect=names,
            ) as operator_name,
        ):
            self.assertEqual(digest, _verify_operator_inventory(valid))
        self.assertEqual([call(operators[0]), call(operators[1])], operator_name.call_args_list)

        cases = (
            ({**valid, "operatorInventory": tuple(names)}, "backendOperatorInventoryInvalid"),
            ({**valid, "operatorInventory": [names[0], 1]}, "backendOperatorInventoryInvalid"),
            ({**valid, "operatorInventory": list(reversed(names))}, "backendOperatorInventoryMismatch"),
            ({**valid, "operatorInventorySha256": "0" * 64}, "backendOperatorDigestMismatch"),
        )
        for document, expected in cases:
            with (
                self.subTest(expected=expected),
                patch("sentinel_py.backend_lock.mutation_operators", operators),
                patch("sentinel_py.backend_lock._operator_name", side_effect=names),
                self.assertRaises(BackendLockError) as stopped,
            ):
                _verify_operator_inventory(document)
            self.assertEqual(expected, stopped.exception.code)

    def test_module_digest_frames_relative_name_and_payload_and_rejects_paths(self):
        from sentinel_py.backend_lock import BackendLockError, _add_module_file

        with tempfile.TemporaryDirectory(prefix="sentinel-module-frame-") as directory:
            root = Path(directory)
            path = root / "module.py"
            path.write_bytes(b"payload")
            digest = Mock()

            _add_module_file(digest, "mutmut/a.py", path)

            self.assertEqual(
                [
                    call((11).to_bytes(8, "big")),
                    call(b"mutmut/a.py"),
                    call((7).to_bytes(8, "big")),
                    call(b"payload"),
                ],
                digest.update.call_args_list,
            )

            symlink = root / "linked.py"
            symlink.symlink_to(path.name)
            for invalid in (root / "missing.py", symlink):
                with self.subTest(path=invalid), self.assertRaises(BackendLockError) as stopped:
                    _add_module_file(Mock(), "mutmut/a.py", invalid)
                self.assertEqual("backendModuleFileInvalid", stopped.exception.code)

            with (
                patch.object(Path, "read_bytes", side_effect=OSError("unavailable")),
                self.assertRaises(BackendLockError) as stopped,
            ):
                _add_module_file(Mock(), "mutmut/a.py", path)
            self.assertEqual("backendModuleFileInvalid", stopped.exception.code)

    def test_module_digest_requests_unsigned_big_endian_length_frames(self):
        from sentinel_py import backend_lock

        path = Mock()
        path.is_symlink.return_value = False
        path.is_file.return_value = True
        path.read_bytes.return_value = b"payload"
        digest = Mock()
        with patch.object(
            backend_lock.struct,
            "pack",
            return_value=b"frame",
        ) as pack:
            backend_lock._add_module_file(digest, "mutmut/a.py", path)

        self.assertEqual(
            [call(">Q", 11), call(">Q", 7)],
            pack.call_args_list,
        )

    def test_verify_backend_lock_joins_every_verified_identity_value(self):
        from sentinel_py.backend_lock import BackendIdentity, verify_backend_lock

        path = Path("/approved/backend.lock.json")
        document = {
            "backendName": "mutmut",
            "backendVersion": "3.7.0",
            "moduleFileCount": 2,
            "moduleTreeSha256": "a" * 64,
        }
        distribution = object()
        with (
            patch("sentinel_py.backend_lock._load_lock", return_value=document) as load,
            patch("sentinel_py.backend_lock._validate_fixed_identity") as validate,
            patch(
                "sentinel_py.backend_lock._installed_distribution",
                return_value=distribution,
            ) as installed,
            patch(
                "sentinel_py.backend_lock._module_tree_identity",
                return_value=(2, "a" * 64),
            ) as tree,
            patch(
                "sentinel_py.backend_lock._verify_operator_inventory",
                return_value="b" * 64,
            ) as operators,
        ):
            identity = verify_backend_lock(path)

        self.assertEqual(
            BackendIdentity("mutmut", "3.7.0", 2, "a" * 64, "b" * 64),
            identity,
        )
        load.assert_called_once_with(path)
        validate.assert_called_once_with(document)
        installed.assert_called_once_with("3.7.0")
        tree.assert_called_once_with(distribution)
        operators.assert_called_once_with(document)


if __name__ == "__main__":
    unittest.main()
