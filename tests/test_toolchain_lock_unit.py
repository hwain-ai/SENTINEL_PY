from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import toolchain_lock  # noqa: E402


class ToolchainLockUnitTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(
            (REPOSITORY_ROOT / "toolchain.lock.json").read_text(encoding="utf-8")
        )

    def test_parser_preserves_the_exact_public_argument_contract(self):
        lock = REPOSITORY_ROOT / "toolchain.lock.json"
        verify_tree = REPOSITORY_ROOT / ".toolchain" / "python-3.12.13"
        arguments = toolchain_lock._parser().parse_args(
            [
                str(lock),
                "uv",
                "--require-locked",
                "--verify-tree",
                str(verify_tree),
            ]
        )

        self.assertEqual(lock, arguments.lock)
        self.assertEqual("uv", arguments.tool)
        self.assertIs(True, arguments.require_locked)
        self.assertEqual(verify_tree, arguments.verify_tree)
        self.assertIsNone(arguments.digest_tree)

        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            toolchain_lock._parser().parse_args([str(lock), "ruby"])
        self.assertEqual(2, stopped.exception.code)

    def test_load_accepts_the_lock_and_rejects_every_envelope_failure(self):
        self.assertEqual(
            "SENTINEL_PY",
            toolchain_lock._load(REPOSITORY_ROOT / "toolchain.lock.json")["repository"],
        )
        cases = (
            (None, "regular, non-symlink"),
            (b"\xff", "cannot read"),
            (b"{", "cannot read"),
            (b'{"repository":"first","repository":"second"}', "duplicate JSON key"),
            (b'{"repository":"SENTINEL_PY","toolchains":{},"x":NaN}', "non-standard"),
            (b"[]", "JSON object"),
            (b'{"repository":"OTHER","toolchains":{}}', "repository identity"),
            (b'{"repository":"SENTINEL_PY"}', "no toolchains"),
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-lock-load-") as directory:
            path = Path(directory) / "lock.json"
            for payload, message in cases:
                with self.subTest(message=message):
                    if payload is None:
                        if path.exists():
                            path.unlink()
                    else:
                        path.write_bytes(payload)
                    with self.assertRaisesRegex(toolchain_lock.LockError, message):
                        toolchain_lock._load(path)

    def test_load_rejects_a_symlink_without_reading_the_target(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-lock-link-") as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text(json.dumps(self.document), encoding="utf-8")
            link = root / "lock.json"
            link.symlink_to(target.name)

            with self.assertRaisesRegex(toolchain_lock.LockError, "non-symlink"):
                toolchain_lock._load(link)

    def test_load_uses_utf8_and_preserves_every_exact_envelope_error(self):
        valid_document = {
            "repository": "SENTINEL_PY",
            "toolchains": {},
        }
        path = Mock()
        path.is_symlink.return_value = False
        path.is_file.return_value = True
        path.read_text.return_value = json.dumps(valid_document)

        self.assertEqual(valid_document, toolchain_lock._load(path))
        path.read_text.assert_called_once_with(encoding="utf-8")

        cases = (
            (None, "toolchain lock must be a regular, non-symlink file"),
            (b"[]", "toolchain lock must be a JSON object"),
            (
                b'{"repository":"OTHER","toolchains":{}}',
                "toolchain lock repository identity is invalid",
            ),
            (
                b'{"repository":"SENTINEL_PY"}',
                "toolchain lock has no toolchains object",
            ),
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-lock-exact-") as directory:
            lock = Path(directory) / "lock.json"
            for payload, expected in cases:
                with self.subTest(expected=expected):
                    if payload is None:
                        if lock.exists():
                            lock.unlink()
                    else:
                        lock.write_bytes(payload)
                    with self.assertRaises(toolchain_lock.LockError) as stopped:
                        toolchain_lock._load(lock)

                    self.assertEqual(expected, str(stopped.exception))

    def test_field_validators_reject_invalid_values(self):
        self.assertEqual("value", toolchain_lock._required_text({"key": "value"}, "key"))
        for value in (None, "", "nul\0value", "two\nlines"):
            with self.subTest(text=value):
                with self.assertRaises(toolchain_lock.LockError):
                    toolchain_lock._required_text({"key": value}, "key")

        valid_digest = "1" * 64
        self.assertEqual(
            valid_digest,
            toolchain_lock._required_sha256({"key": valid_digest}, "key"),
        )
        for value in ("1" * 63, "X" * 64, "0" * 64):
            with self.subTest(digest=value):
                with self.assertRaises(toolchain_lock.LockError):
                    toolchain_lock._required_sha256({"key": value}, "key")

        self.assertEqual(
            1,
            toolchain_lock._required_positive_integer({"key": 1}, "key"),
        )
        for value in (True, 0, -1, "1"):
            with self.subTest(integer=value):
                with self.assertRaises(toolchain_lock.LockError):
                    toolchain_lock._required_positive_integer({"key": value}, "key")

        self.assertEqual(
            "safe/path",
            toolchain_lock._required_safe_relative_path(
                {"key": "safe/path"},
                "key",
            ),
        )
        for value in ("?bad", "/absolute", "two//parts", "a/../b", "a/./b"):
            with self.subTest(path=value):
                with self.assertRaises(toolchain_lock.LockError):
                    toolchain_lock._required_safe_relative_path({"key": value}, "key")

    def test_field_validators_preserve_exact_error_messages(self):
        cases = (
            (
                toolchain_lock._required_text,
                {"field": ""},
                "toolchain lock field 'field' is invalid",
            ),
            (
                toolchain_lock._required_sha256,
                {"field": "1" * 63},
                "toolchain lock field 'field' is not a SHA-256 digest",
            ),
            (
                toolchain_lock._required_sha256,
                {"field": "0" * 64},
                "toolchain lock field 'field' uses a placeholder digest",
            ),
            (
                toolchain_lock._required_positive_integer,
                {"field": 0},
                "toolchain lock field 'field' is invalid",
            ),
            (
                toolchain_lock._required_safe_relative_path,
                {"field": "two//parts"},
                "toolchain lock field 'field' is unsafe",
            ),
        )
        for validator, mapping, expected in cases:
            with self.subTest(validator=validator.__name__, expected=expected):
                with self.assertRaises(toolchain_lock.LockError) as stopped:
                    validator(mapping, "field")

                self.assertEqual(expected, str(stopped.exception))

    def test_select_validates_status_url_and_complete_tool_identity(self):
        selected = toolchain_lock._select(self.document, "python", True)
        self.assertEqual("3.12.13", selected["version"])
        self.assertEqual(10, len(toolchain_lock._fields(selected)))

        missing = copy.deepcopy(self.document)
        missing["toolchains"].pop("python")
        pending = copy.deepcopy(self.document)
        pending["status"] = "pending"
        insecure = copy.deepcopy(self.document)
        insecure["toolchains"]["python"]["archiveUrl"] = "http://example.invalid/x"
        invalid_size = copy.deepcopy(self.document)
        invalid_size["toolchains"]["python"]["archiveSize"] = 0
        for document in (missing, pending, insecure, invalid_size):
            with self.subTest(document=document):
                with self.assertRaises(toolchain_lock.LockError):
                    toolchain_lock._select(document, "python", True)

        self.assertEqual(
            "3.12.13",
            toolchain_lock._select(pending, "python", False)["version"],
        )

    def test_select_and_fields_preserve_exact_public_values(self):
        selected = toolchain_lock._select(self.document, "python", True)
        field_names = (
            "version",
            "archiveUrl",
            "archiveSize",
            "archiveSha256",
            "archiveRoot",
            "installDirectory",
            "binaryRelativePath",
            "binarySha256",
            "versionOutput",
            "installedTreeSha256",
        )
        self.assertEqual(
            tuple(str(selected[name]) for name in field_names),
            toolchain_lock._fields(selected),
        )

        cases = []
        missing = copy.deepcopy(self.document)
        missing["toolchains"].pop("python")
        cases.append((missing, "python toolchain is missing"))
        pending = copy.deepcopy(self.document)
        pending["status"] = "pending"
        cases.append(
            (
                pending,
                "python toolchain is pending: repository=pending, tool=locked",
            )
        )
        insecure = copy.deepcopy(self.document)
        insecure["toolchains"]["python"]["archiveUrl"] = "http://invalid"
        cases.append((insecure, "python archive URL must use HTTPS"))
        for document, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(toolchain_lock.LockError) as stopped:
                    toolchain_lock._select(document, "python", True)

                self.assertEqual(expected, str(stopped.exception))

    def test_record_uses_exact_length_prefixes_and_payload_order(self):
        digest = Mock()
        values = (b"file", b"nested/payload", b"754", b"payload")

        toolchain_lock._record(
            digest,
            values[0],
            values[1],
            0o754,
            values[3],
        )

        expected_calls = []
        for value in values:
            expected_calls.extend((call(len(value).to_bytes(8)), call(value)))
        self.assertEqual(expected_calls, digest.update.call_args_list)

    def test_record_requests_unsigned_big_endian_length_frames(self):
        digest = Mock()
        values = (b"file", b"nested/payload", b"754", b"payload")
        with patch.object(
            toolchain_lock.struct,
            "pack",
            return_value=b"frame",
        ) as pack:
            toolchain_lock._record(digest, values[0], values[1], 0o754, values[3])

        self.assertEqual(
            [call(">Q", len(value)) for value in values],
            pack.call_args_list,
        )

    def test_file_and_tree_digests_are_stable_and_reject_unsafe_entries(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-tree-digest-") as directory:
            parent = Path(directory)
            tree = parent / "tree"
            (tree / "nested").mkdir(parents=True)
            payload = tree / "nested" / "payload.txt"
            payload.write_bytes(b"payload")
            (tree / "link").symlink_to("nested/payload.txt")

            self.assertEqual(hashlib.sha256(b"payload").digest(), toolchain_lock._file_digest(payload))
            entries = toolchain_lock._tree_entries(tree)
            self.assertEqual(
                sorted(entries, key=lambda item: item.relative_to(tree).as_posix().encode()),
                list(entries),
            )
            first = toolchain_lock._tree_digest(tree)
            second = toolchain_lock._tree_digest(tree)
            self.assertEqual(first, second)
            self.assertTrue(toolchain_lock._inside(tree.resolve(), payload.resolve()))
            self.assertFalse(toolchain_lock._inside(tree.resolve(), parent.resolve()))
            self.assertFalse(toolchain_lock._inside(Path("relative"), Path("/absolute")))

            escaping = parent / "escaping"
            escaping.mkdir()
            (escaping / "link").symlink_to(payload)
            with self.assertRaisesRegex(toolchain_lock.LockError, "escapes"):
                toolchain_lock._tree_digest(escaping)

            special = parent / "special"
            special.mkdir()
            os.mkfifo(special / "pipe")
            with self.assertRaisesRegex(toolchain_lock.LockError, "special file"):
                toolchain_lock._tree_digest(special)

            missing = parent / "missing"
            tree_link = parent / "tree-link"
            tree_link.symlink_to(tree, target_is_directory=True)
            for invalid_root in (missing, tree_link):
                with self.subTest(root=invalid_root.name):
                    with self.assertRaises(toolchain_lock.LockError) as stopped:
                        toolchain_lock._tree_digest(invalid_root)
                    self.assertEqual(
                        "installed tree is missing or is a symlink",
                        str(stopped.exception),
                    )

    def test_file_digest_uses_safe_flags_fixed_blocks_and_always_closes(self):
        path = Path("payload.bin")
        stream = Mock()
        stream.read.side_effect = (b"first", b"second", b"")
        stream_context = Mock()
        stream_context.__enter__ = Mock(return_value=stream)
        stream_context.__exit__ = Mock(return_value=False)
        digest = Mock()
        digest.digest.return_value = b"digest"
        flags = os.O_RDONLY | os.O_NOFOLLOW
        with (
            patch.object(toolchain_lock.hashlib, "sha256", return_value=digest),
            patch.object(toolchain_lock.os, "open", return_value=41) as open_file,
            patch.object(
                toolchain_lock.os,
                "fdopen",
                return_value=stream_context,
            ) as fdopen,
            patch.object(toolchain_lock.os, "close") as close,
        ):
            self.assertEqual(b"digest", toolchain_lock._file_digest(path))

        open_file.assert_called_once_with(path, flags)
        fdopen.assert_called_once_with(41, "rb", closefd=False)
        self.assertEqual([call(1024 * 1024)] * 3, stream.read.call_args_list)
        self.assertEqual([call(b"first"), call(b"second")], digest.update.call_args_list)
        digest.digest.assert_called_once_with()
        close.assert_called_once_with(41)

        failure = OSError("read failed")
        failed_stream = Mock()
        failed_stream.read.side_effect = failure
        failed_context = Mock()
        failed_context.__enter__ = Mock(return_value=failed_stream)
        failed_context.__exit__ = Mock(return_value=False)
        with (
            patch.object(toolchain_lock.os, "open", return_value=42),
            patch.object(toolchain_lock.os, "fdopen", return_value=failed_context),
            patch.object(toolchain_lock.os, "close") as close,
        ):
            with self.assertRaises(OSError) as stopped:
                toolchain_lock._file_digest(path)
        self.assertIs(failure, stopped.exception)
        close.assert_called_once_with(42)

    def test_tree_entries_use_utf8_relative_path_order(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-tree-order-") as directory:
            tree = Path(directory) / "tree"
            (tree / "a").mkdir(parents=True)
            (tree / "a" / "x").write_bytes(b"x")
            (tree / "a.b").write_bytes(b"dot")

            relative = tuple(
                path.relative_to(tree).as_posix()
                for path in toolchain_lock._tree_entries(tree)
            )

        self.assertEqual(("a", "a.b", "a/x"), relative)

    def test_tree_digest_requires_strict_root_and_exact_enumeration_error(self):
        root = Mock()
        root.is_symlink.return_value = False
        root.is_dir.return_value = True
        resolved = Path("/resolved")
        root.resolve.return_value = resolved

        with patch.object(toolchain_lock, "_tree_entries", return_value=()):
            self.assertEqual(hashlib.sha256().hexdigest(), toolchain_lock._tree_digest(root))

        root.resolve.assert_called_once_with(strict=True)

        enumeration_error = OSError("denied")
        root.reset_mock()
        root.is_symlink.return_value = False
        root.is_dir.return_value = True
        root.resolve.return_value = resolved
        with (
            patch.object(
                toolchain_lock,
                "_tree_entries",
                side_effect=enumeration_error,
            ),
            self.assertRaises(toolchain_lock.LockError) as stopped,
        ):
            toolchain_lock._tree_digest(root)

        self.assertEqual(
            "cannot enumerate installed tree: denied",
            str(stopped.exception),
        )
        self.assertIs(enumeration_error, stopped.exception.__cause__)

    def test_add_tree_entry_records_exact_kinds_and_strict_symlink_target(self):
        digest = Mock()
        root = Path("/tree")
        resolved_root = Path("/tree")
        metadata = SimpleNamespace(st_mode=0o100600)
        file_path = Mock()
        file_path.relative_to.return_value.as_posix.return_value = "payload"
        file_path.lstat.return_value = metadata
        with (
            patch.object(toolchain_lock, "_file_digest", return_value=b"digest"),
            patch.object(toolchain_lock, "_record") as record,
        ):
            toolchain_lock._add_tree_entry(
                digest,
                root,
                resolved_root,
                file_path,
            )
        record.assert_called_once_with(digest, b"file", b"payload", 0o600, b"digest")

        directory_path = Mock()
        directory_path.relative_to.return_value.as_posix.return_value = "nested"
        directory_path.lstat.return_value = SimpleNamespace(st_mode=0o040700)
        with patch.object(toolchain_lock, "_record") as record:
            toolchain_lock._add_tree_entry(
                digest,
                root,
                resolved_root,
                directory_path,
            )
        record.assert_called_once_with(digest, b"directory", b"nested", 0o700, b"")

        symlink_path = Mock()
        symlink_path.relative_to.return_value.as_posix.return_value = "link"
        symlink_path.lstat.return_value = SimpleNamespace(st_mode=0o120777)
        symlink_path.resolve.return_value = Path("/tree/payload")
        with (
            patch.object(toolchain_lock.os, "readlink", return_value="payload"),
            patch.object(toolchain_lock, "_record") as record,
        ):
            toolchain_lock._add_tree_entry(
                digest,
                root,
                resolved_root,
                symlink_path,
            )
        symlink_path.resolve.assert_called_once_with(strict=True)
        record.assert_called_once_with(
            digest,
            b"symlink",
            b"link",
            0o777,
            os.fsencode("payload"),
        )

    def test_add_tree_entry_maps_metadata_failure_to_exact_error(self):
        digest = Mock()
        root = Path("/tree")
        path = Mock()
        path.relative_to.return_value.as_posix.return_value = "payload"
        metadata_error = OSError("denied")
        path.lstat.side_effect = metadata_error

        with self.assertRaises(toolchain_lock.LockError) as stopped:
            toolchain_lock._add_tree_entry(
                digest,
                root,
                root,
                path,
            )

        self.assertEqual(
            "invalid installed tree entry: 'payload'",
            str(stopped.exception),
        )
        self.assertIs(metadata_error, stopped.exception.__cause__)

    def test_verify_tree_and_main_cover_all_command_modes(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-tree-main-") as directory:
            tree = Path(directory) / "tree"
            tree.mkdir()
            (tree / "payload").write_text("payload", encoding="utf-8")
            digest = toolchain_lock._tree_digest(tree)
            selected = {"installedTreeSha256": digest}
            toolchain_lock._verify_tree(selected, tree)
            with self.assertRaisesRegex(toolchain_lock.LockError, "manifest mismatch"):
                toolchain_lock._verify_tree({"installedTreeSha256": "1" * 64}, tree)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    0,
                    toolchain_lock.main(
                        [str(REPOSITORY_ROOT / "toolchain.lock.json"), "python"]
                    ),
                )
            selected_toolchain = toolchain_lock._select(self.document, "python", False)
            self.assertEqual(
                toolchain_lock._fields(selected_toolchain),
                tuple(stdout.getvalue().splitlines()),
            )

            with patch.object(toolchain_lock, "_verify_tree") as verify_tree:
                self.assertEqual(
                    0,
                    toolchain_lock.main(
                        [
                            str(REPOSITORY_ROOT / "toolchain.lock.json"),
                            "python",
                            "--verify-tree",
                            str(tree),
                        ]
                    ),
                )
            verify_tree.assert_called_once_with(selected_toolchain, tree)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    0,
                    toolchain_lock.main(
                        [
                            str(REPOSITORY_ROOT / "toolchain.lock.json"),
                            "python",
                            "--digest-tree",
                            str(tree),
                        ]
                    ),
                )
            self.assertEqual(digest, stdout.getvalue().strip())

            with self.assertRaises(toolchain_lock.LockError) as stopped:
                toolchain_lock.main(
                    [
                        str(REPOSITORY_ROOT / "toolchain.lock.json"),
                        "python",
                        "--verify-tree",
                        str(tree),
                        "--digest-tree",
                        str(tree),
                    ]
                )
            self.assertEqual(
                "verify-tree and digest-tree are mutually exclusive",
                str(stopped.exception),
            )

    def test_main_forwards_the_require_locked_flag_exactly(self):
        lock = REPOSITORY_ROOT / "toolchain.lock.json"
        selected = toolchain_lock._select(self.document, "python", True)
        with (
            patch.object(toolchain_lock, "_load", return_value=self.document),
            patch.object(
                toolchain_lock,
                "_select",
                return_value=selected,
            ) as select,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                0,
                toolchain_lock.main(
                    [str(lock), "python", "--require-locked"]
                ),
            )

        select.assert_called_once_with(self.document, "python", True)


if __name__ == "__main__":
    unittest.main()
