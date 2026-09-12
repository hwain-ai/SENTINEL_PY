"""Verification of the only mutation backend admitted by this release."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from mutmut.mutation.mutators import mutation_operators


_FIELDS = frozenset(
    (
        "schemaVersion",
        "backendName",
        "backendVersion",
        "wheelSha256",
        "moduleFileCount",
        "moduleTreeSha256",
        "operatorInventory",
        "operatorInventorySha256",
        "mutationDomain",
        "rawStateMapVersion",
        "killConfirmationPolicy",
        "isolationMode",
    )
)
_WHEEL_SHA256 = "1d2f9a1bfa4a474b2213df6b17223150b492bf4a85af0eda4fb322297337fb32"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BackendLockError(RuntimeError):
    """The installed mutmut artifact does not match its sealed identity."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackendIdentity:
    """Verified identity safe to expose through doctor output."""

    name: str
    version: str
    module_file_count: int
    module_tree_sha256: str
    operator_inventory_sha256: str


def packaged_lock_path() -> Path:
    return Path(__file__).with_name("data") / "backend.lock.json"


def verify_backend_lock(path: Path | None = None) -> BackendIdentity:
    """Join the lock to installed module bytes and the operator registry."""

    document = _load_lock(packaged_lock_path() if path is None else path)
    _validate_fixed_identity(document)
    distribution = _installed_distribution(document["backendVersion"])
    file_count, tree_digest = _module_tree_identity(distribution)
    if file_count != document["moduleFileCount"]:
        raise BackendLockError("backendModuleFileCountMismatch")
    if tree_digest != document["moduleTreeSha256"]:
        raise BackendLockError("backendModuleTreeMismatch")
    operator_digest = _verify_operator_inventory(document)
    return BackendIdentity(
        name=document["backendName"],
        version=document["backendVersion"],
        module_file_count=file_count,
        module_tree_sha256=tree_digest,
        operator_inventory_sha256=operator_digest,
    )


def _load_lock(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise BackendLockError("backendLockUnavailable")
    try:
        document = json.loads(
            path.read_bytes().decode(),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except BackendLockError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackendLockError("backendLockInvalid") from error
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise BackendLockError("backendLockInvalid")
    return document


def _unique_object(pairs: Iterable[tuple[str, object]]) -> dict:
    document = {}
    for key, value in pairs:
        if key in document:
            raise BackendLockError("backendLockInvalid")
        document[key] = value
    return document


def _reject_constant(_value: str) -> None:
    raise BackendLockError("backendLockInvalid")


def _validate_fixed_identity(document: dict) -> None:
    expected = {
        "backendName": "mutmut",
        "backendVersion": "3.7.0",
        "isolationMode": "disposable-project-snapshot-v1",
        "killConfirmationPolicy": "clean-control+typed-assertion+matching-replay-v1",
        "mutationDomain": "python-function-and-method-body-v1",
        "rawStateMapVersion": "mutmut-3.7.0-raw-state-v2",
        "schemaVersion": "sentinel-backend-lock-v1",
        "wheelSha256": _WHEEL_SHA256,
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise BackendLockError("backendLockIdentityMismatch")
    _validate_lock_numbers(document)


def _validate_lock_numbers(document: dict) -> None:
    count = document["moduleFileCount"]
    if type(count) is not int or count < 1:
        raise BackendLockError("backendLockInvalid")
    for key in ("moduleTreeSha256", "operatorInventorySha256"):
        value = document[key]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise BackendLockError("backendLockInvalid")


def _installed_distribution(expected_version: str):
    try:
        distribution = importlib.metadata.distribution("mutmut")
    except importlib.metadata.PackageNotFoundError as error:
        raise BackendLockError("backendNotInstalled") from error
    if distribution.version != expected_version:
        raise BackendLockError("backendVersionMismatch")
    return distribution


def _module_tree_identity(distribution) -> tuple[int, str]:
    rows = []
    for item in distribution.files or ():
        relative = PurePosixPath(str(item))
        if _is_backend_module(relative):
            rows.append((relative.as_posix(), distribution.locate_file(item)))
    digest = hashlib.sha256()
    for relative, path in sorted(rows):
        _add_module_file(digest, relative, Path(path))
    return len(rows), digest.hexdigest()


def _is_backend_module(path: PurePosixPath) -> bool:
    return bool(path.parts) and path.parts[0] == "mutmut" and path.suffix == ".py"


def _add_module_file(digest, relative: str, path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BackendLockError("backendModuleFileInvalid")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise BackendLockError("backendModuleFileInvalid") from error
    name = relative.encode()
    digest.update(struct.pack(">Q", len(name)))
    digest.update(name)
    digest.update(struct.pack(">Q", len(payload)))
    digest.update(payload)


def _verify_operator_inventory(document: dict) -> str:
    configured = document["operatorInventory"]
    if not isinstance(configured, list) or any(not isinstance(item, str) for item in configured):
        raise BackendLockError("backendOperatorInventoryInvalid")
    actual = tuple(_operator_name(item) for item in mutation_operators)
    if tuple(configured) != actual:
        raise BackendLockError("backendOperatorInventoryMismatch")
    payload = b"[" + b",".join(json.dumps(name).encode() for name in actual) + b"]\n"
    digest = hashlib.sha256(payload).hexdigest()
    if digest != document["operatorInventorySha256"]:
        raise BackendLockError("backendOperatorDigestMismatch")
    return digest


def _operator_name(operator: tuple[type, object]) -> str:
    node_type, function = operator
    return (
        node_type.__module__
        + "."
        + node_type.__qualname__
        + "|"
        + function.__module__
        + "."
        + function.__qualname__
    )
