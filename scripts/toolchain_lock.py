#!/usr/bin/python3
"""Validate the repository-owned Python toolchain lock and installed trees."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import platform as platform_module
import re
import stat
import struct
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple


EXPECTED_REPOSITORY = "SENTINEL_PY"
LOCKED_STATUS = "locked"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SAFE_PATH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_/-]*")


class LockError(ValueError):
    """Raised when lock data cannot identify one exact trusted toolchain."""


PLATFORM_KEYS = ("linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-aarch64")


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """linux-x86_64 | linux-aarch64 | darwin-x86_64 | darwin-aarch64 for this host."""

    system = (system or platform_module.system()).lower()
    machine = (machine or platform_module.machine()).lower()
    if system not in ("linux", "darwin"):
        raise LockError(f"unsupported operating system: {system}")
    if machine in ("x86_64", "amd64"):
        architecture = "x86_64"
    elif machine in ("aarch64", "arm64"):
        architecture = "aarch64"
    else:
        raise LockError(f"unsupported architecture: {machine}")
    return f"{system}-{architecture}"


def _unique_object(pairs: Iterable[Tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LockError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LockError("toolchain lock must be a regular, non-symlink file")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except LockError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LockError(f"cannot read toolchain lock: {error}") from error
    if not isinstance(document, dict):
        raise LockError("toolchain lock must be a JSON object")
    if document.get("repository") != EXPECTED_REPOSITORY:
        raise LockError("toolchain lock repository identity is invalid")
    if not isinstance(document.get("toolchains"), dict):
        raise LockError("toolchain lock has no toolchains object")
    return document


def _reject_constant(value: str) -> None:
    raise LockError(f"non-standard JSON number is not allowed: {value}")


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        raise LockError(f"toolchain lock field {key!r} is invalid")
    return value


def _required_sha256(mapping: dict[str, Any], key: str) -> str:
    value = _required_text(mapping, key)
    if SHA256_PATTERN.fullmatch(value) is None:
        raise LockError(f"toolchain lock field {key!r} is not a SHA-256 digest")
    if value == "0" * 64:
        raise LockError(f"toolchain lock field {key!r} uses a placeholder digest")
    return value


def _required_positive_integer(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LockError(f"toolchain lock field {key!r} is invalid")
    return value


def _required_safe_relative_path(mapping: dict[str, Any], key: str) -> str:
    value = _required_text(mapping, key)
    if (
        SAFE_PATH_PATTERN.fullmatch(value) is None
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise LockError(f"toolchain lock field {key!r} is unsafe")
    return value


def _for_platform(tool: str, toolchain: dict[str, Any], platform: Optional[str]) -> dict[str, Any]:
    """Merge the platforms[...] entry for this (or the given) platform into the common fields."""

    platforms = toolchain.get("platforms")
    if platforms is None:
        return toolchain
    if not isinstance(platforms, dict):
        raise LockError(f"{tool} platforms must be an object")
    key = platform or platform_key()
    entry = platforms.get(key)
    if not isinstance(entry, dict):
        raise LockError(f"{tool} toolchain has no entry for platform {key}")
    merged = {name: value for name, value in toolchain.items() if name != "platforms"}
    merged.update(entry)
    return merged


def _select(
    document: dict[str, Any], tool: str, require_locked: bool, platform: Optional[str] = None
) -> dict[str, Any]:
    """The tool's lock entry for one platform: common fields merged with its platforms[...] entry."""

    toolchain = document["toolchains"].get(tool)
    if not isinstance(toolchain, dict):
        raise LockError(f"{tool} toolchain is missing")
    toolchain = _for_platform(tool, toolchain, platform)
    repository_status = _required_text(document, "status")
    tool_status = _required_text(toolchain, "status")
    if require_locked and (
        repository_status != LOCKED_STATUS or tool_status != LOCKED_STATUS
    ):
        raise LockError(
            f"{tool} toolchain is pending: "
            f"repository={repository_status}, tool={tool_status}"
        )
    _required_text(toolchain, "version")
    url = _required_text(toolchain, "archiveUrl")
    if not url.startswith("https://"):
        raise LockError(f"{tool} archive URL must use HTTPS")
    _required_positive_integer(toolchain, "archiveSize")
    _required_sha256(toolchain, "archiveSha256")
    _required_safe_relative_path(toolchain, "archiveRoot")
    _required_safe_relative_path(toolchain, "installDirectory")
    _required_safe_relative_path(toolchain, "binaryRelativePath")
    _required_sha256(toolchain, "binarySha256")
    _required_text(toolchain, "versionOutput")
    _required_sha256(toolchain, "installedTreeSha256")
    return toolchain


def _fields(toolchain: dict[str, Any]) -> Tuple[str, ...]:
    names = (
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
    return tuple(str(toolchain[name]) for name in names)


def _record(
    digest: Any, kind: bytes, relative_path: bytes, mode: int, payload: bytes
) -> None:
    for value in (kind, relative_path, f"{mode:03o}".encode(), payload):
        digest.update(struct.pack(">Q", len(value)))
        digest.update(value)


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    finally:
        os.close(descriptor)
    return digest.digest()


def _tree_entries(root: Path) -> Tuple[Path, ...]:
    entries = []
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        entries.extend(current_path / name for name in directories)
        entries.extend(current_path / name for name in files)
    return tuple(
        sorted(
            entries,
            key=functools.partial(_tree_entry_sort_key, root),
        )
    )


def _tree_entry_sort_key(root: Path, item: Path) -> bytes:
    return item.relative_to(root).as_posix().encode()


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        return False


def _tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise LockError("installed tree is missing or is a symlink")
    resolved_root = root.resolve(strict=True)
    digest = hashlib.sha256()
    try:
        entries = _tree_entries(root)
    except (OSError, UnicodeError) as error:
        raise LockError(f"cannot enumerate installed tree: {error}") from error
    for path in entries:
        _add_tree_entry(digest, root, resolved_root, path)
    return digest.hexdigest()


def _add_tree_entry(digest: Any, root: Path, resolved_root: Path, path: Path) -> None:
    relative = path.relative_to(root).as_posix()
    try:
        relative_bytes = relative.encode()
        metadata = path.lstat()
    except (OSError, UnicodeError) as error:
        raise LockError(f"invalid installed tree entry: {relative!r}") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode):
        _record(digest, b"file", relative_bytes, mode, _file_digest(path))
        return
    if stat.S_ISDIR(metadata.st_mode):
        _record(digest, b"directory", relative_bytes, mode, b"")
        return
    if not stat.S_ISLNK(metadata.st_mode):
        raise LockError(f"installed tree contains special file: {relative}")
    target = os.readlink(path)
    resolved_target = path.resolve(strict=True)
    if not _inside(resolved_root, resolved_target):
        raise LockError(f"installed tree symlink escapes its root: {relative}")
    # Symlink modes differ between Linux (777) and macOS (umask-dependent); only the target matters.
    _record(digest, b"symlink", relative_bytes, 0o777, os.fsencode(target))


def _verify_tree(toolchain: dict[str, Any], path: Path) -> None:
    expected = _required_sha256(toolchain, "installedTreeSha256")
    actual = _tree_digest(path)
    if actual != expected:
        raise LockError(
            f"installed tree manifest mismatch: expected {expected}, got {actual}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", type=Path)
    parser.add_argument("tool", choices=("python", "uv"))
    parser.add_argument("--require-locked", action="store_true")
    parser.add_argument("--platform", choices=PLATFORM_KEYS)
    parser.add_argument("--verify-tree", type=Path)
    parser.add_argument("--digest-tree", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    document = _load(arguments.lock)
    toolchain = _select(document, arguments.tool, arguments.require_locked, arguments.platform)
    if arguments.verify_tree is not None and arguments.digest_tree is not None:
        raise LockError("verify-tree and digest-tree are mutually exclusive")
    if arguments.verify_tree is not None:
        _verify_tree(toolchain, arguments.verify_tree)
        return 0
    if arguments.digest_tree is not None:
        print(_tree_digest(arguments.digest_tree))
        return 0
    print("\n".join(_fields(toolchain)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LockError as error:
        print(f"toolchain error: {error}", file=sys.stderr)
        raise SystemExit(2)
