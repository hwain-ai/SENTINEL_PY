#!/usr/bin/env python3
"""Cross-platform launcher for the checker's locked Python and uv.

Replaces the Linux-only shell launchers. Runs on Linux and macOS (x86_64 and
arm64) with the host's Python 3.9+ and the standard library only. Windows is
served through WSL, because the pinned mutation backend (mutmut) refuses to run
natively there.

    scripts/toolchain.py bootstrap            download, verify and install the locked Python and uv
    scripts/toolchain.py setup                bootstrap, sync the locked dependencies, run version
    scripts/toolchain.py run ARGS...          uv run inside the locked environment
    scripts/toolchain.py sync [--offline]     uv sync from uv.lock
    scripts/toolchain.py lock | lock-check    maintain / check uv.lock
    scripts/toolchain.py deps TARGET REQ [--offline]
                                              install a checked project's test requirements into TARGET
    scripts/toolchain.py --version            uv --version
    scripts/toolchain.py platform             print the detected platform key
    scripts/toolchain.py describe PLATFORM    (maintenance) compute lock fields for another platform

Every child process gets a minimal environment: no inherited variables, a
private HOME and cache under .toolchain, and PATH limited to the locked tools.
"""
from __future__ import annotations

import sys

# The launcher may run under the locked interpreter itself; never write bytecode into its tree.
# Callers pass -I -B as well: -I makes Python ignore PYTHONDONTWRITEBYTECODE, so only -B covers startup imports.
sys.dont_write_bytecode = True

import os  # noqa: E402

# Everything the launcher creates or lets a child create is private, as the shell launchers had it.
os.umask(0o077)

import argparse  # noqa: E402
import hashlib  # noqa: E402
import platform as platform_module
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import toolchain_lock  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = REPOSITORY_ROOT / "toolchain.lock.json"
TOOLCHAIN_ROOT = REPOSITORY_ROOT / ".toolchain"
TOOLS = ("python", "uv")
PRIVATE_DIRECTORIES = ("home", "cache", "venv", "pytest-cache", "downloads")
WINDOWS_HINT = (
    "toolchain error: native Windows is not supported (the pinned mutmut backend needs WSL); "
    "run SENTINEL inside WSL2"
)


class ToolchainError(RuntimeError):
    pass


def fail(message: str) -> "ToolchainError":
    return ToolchainError(f"toolchain error: {message}")


# ---------------------------------------------------------------- platform

def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """linux-x86_64 | linux-aarch64 | darwin-x86_64 | darwin-aarch64; Windows is refused with a WSL hint."""

    if (system or platform_module.system()).lower() == "windows":
        raise ToolchainError(WINDOWS_HINT)
    try:
        return toolchain_lock.platform_key(system, machine)
    except toolchain_lock.LockError as error:
        raise fail(str(error)) from error


def select(tool: str, key: Optional[str] = None) -> Dict[str, Any]:
    document = toolchain_lock._load(LOCK_FILE)
    return toolchain_lock._select(document, tool, True, key or platform_key())


# ------------------------------------------------------------------ files

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise fail(f"private directory is a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _download(entry: Dict[str, Any]) -> Path:
    """Fetch the locked archive once into .toolchain/downloads, verifying size and SHA-256."""

    downloads = TOOLCHAIN_ROOT / "downloads"
    _private_directory(downloads)
    archive = downloads / (entry["archiveSha256"] + ".tar.gz")
    if archive.is_file() and archive.stat().st_size == entry["archiveSize"] and _sha256_file(archive) == entry["archiveSha256"]:
        return archive
    partial = archive.with_suffix(".partial")
    if partial.exists():
        partial.unlink()
    url = entry["archiveUrl"]
    if not url.startswith("https://"):
        raise fail("archive URL must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "sentinel-toolchain/1"})
    with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as stream:
        shutil.copyfileobj(response, stream, 1024 * 1024)
    os.chmod(partial, 0o600)
    if partial.stat().st_size != entry["archiveSize"]:
        partial.unlink()
        raise fail("archive size mismatch")
    if _sha256_file(partial) != entry["archiveSha256"]:
        partial.unlink()
        raise fail("archive checksum mismatch")
    os.replace(partial, archive)
    return archive


def _safe_member(member: tarfile.TarInfo, root_name: str) -> Optional[str]:
    """Return the member path relative to the archive root, or None for the root entry itself."""

    parts = Path(member.name).parts
    if not _stays_under_root(member.name, parts, root_name):
        raise fail(f"archive entry escapes or leaves the expected root: {member.name}")
    if len(parts) == 1:
        return None
    if member.type not in (tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.REGTYPE, tarfile.AREGTYPE):
        raise fail(f"archive entry has an unsupported type: {member.name}")
    return "/".join(parts[1:])


def _stays_under_root(name: str, parts: Sequence[str], root_name: str) -> bool:
    absolute = name.startswith("/") or name.startswith("\\")
    return bool(parts) and not absolute and ".." not in parts and parts[0] == root_name


def _normalized_mode(member: tarfile.TarInfo) -> int:
    if member.isdir():
        return 0o700
    return 0o700 if member.mode & 0o111 else 0o600


def extract(archive: Path, root_name: str, destination: Path) -> None:
    """Extract the archive's root directory into destination with normalized private modes.

    Directories become 700, files 600 (700 when any execute bit was set), symlinks are kept
    only when they stay inside the tree. The result is byte-identical across platforms, so
    the lock's tree fingerprint applies to every platform entry.
    """

    destination.mkdir(mode=0o700)
    symlinks: List[tuple] = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            relative = _safe_member(member, root_name)
            if relative is None:
                continue
            if member.issym():
                symlinks.append((destination / relative, member.linkname))
            else:
                _extract_entry(tar, member, destination / relative)
    for target, link in symlinks:
        _create_symlink(destination, target, link)
    _restrict_directories(destination)


def _extract_entry(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    if member.isdir():
        target.mkdir(mode=0o700, exist_ok=True)
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = tar.extractfile(member)
    if source is None:
        raise fail(f"archive entry is unreadable: {member.name}")
    with open(target, "wb") as stream:
        shutil.copyfileobj(source, stream, 1024 * 1024)
    os.chmod(target, _normalized_mode(member))


def _create_symlink(root: Path, target: Path, link: str) -> None:
    if link.startswith("/"):
        raise fail(f"archive symlink is absolute: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.symlink(link, target)
    if not _inside(root.resolve(), target.resolve()):
        target.unlink()
        raise fail(f"archive symlink escapes its root: {target}")


def _restrict_directories(root: Path) -> None:
    for current, directories, _files in os.walk(root):
        for name in directories:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except ValueError:
        return False


# -------------------------------------------------------------- bootstrap

def _child_environment(python_home: Path, uv_home: Path) -> Dict[str, str]:
    return {
        "HOME": str(TOOLCHAIN_ROOT / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join([str(python_home / "bin"), str(uv_home), "/usr/bin", "/bin"]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONSAFEPATH": "1",
        "UV_CACHE_DIR": str(TOOLCHAIN_ROOT / "cache"),
        "UV_KEYRING_PROVIDER": "disabled",
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PROJECT_ENVIRONMENT": str(TOOLCHAIN_ROOT / "venv"),
    }


def _version_output(binary: Path, environment: Dict[str, str]) -> str:
    completed = subprocess.run(
        [str(binary), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise fail(f"{binary.name} --version failed")
    return completed.stdout.splitlines()[0].strip() if completed.stdout else ""


def _verify_installed(tool: str, entry: Dict[str, Any], environment: Dict[str, str], check_version: bool) -> Path:
    home = TOOLCHAIN_ROOT / entry["installDirectory"]
    if home.is_symlink() or not home.is_dir():
        raise fail(f"verified local {tool} tree is missing")
    toolchain_lock._verify_tree(entry, home)
    binary = _verified_binary(tool, home / entry["binaryRelativePath"], entry["binarySha256"])
    if check_version:
        observed = _version_output(binary, environment)
        if observed != entry["versionOutput"]:
            raise fail(f"{tool} version output mismatch: expected {entry['versionOutput']!r}, got {observed!r}")
    return binary


def _verified_binary(tool: str, binary: Path, expected_sha256: str) -> Path:
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise fail(f"verified local {tool} executable is missing")
    if _sha256_file(binary) != expected_sha256:
        raise fail(f"{tool} executable checksum mismatch")
    return binary


def bootstrap(key: Optional[str] = None) -> Dict[str, Path]:
    """Install every locked tool for this platform; already-verified trees are left alone."""

    _private_directory(TOOLCHAIN_ROOT)
    for name in PRIVATE_DIRECTORIES:
        _private_directory(TOOLCHAIN_ROOT / name)
    entries = {tool: select(tool, key) for tool in TOOLS}
    environment = _child_environment(
        TOOLCHAIN_ROOT / entries["python"]["installDirectory"], TOOLCHAIN_ROOT / entries["uv"]["installDirectory"]
    )
    binaries: Dict[str, Path] = {}
    for tool, entry in entries.items():
        home = TOOLCHAIN_ROOT / entry["installDirectory"]
        if home.exists() or home.is_symlink():
            binaries[tool] = _verify_installed(tool, entry, environment, check_version=False)
            continue
        archive = _download(entry)
        staging = Path(tempfile.mkdtemp(prefix=f".staging-{tool}-", dir=TOOLCHAIN_ROOT))
        try:
            extracted = staging / "tree"
            extract(archive, entry["archiveRoot"], extracted)
            toolchain_lock._verify_tree(entry, extracted)
            binary = extracted / entry["binaryRelativePath"]
            if _sha256_file(binary) != entry["binarySha256"]:
                raise fail(f"{tool} executable checksum mismatch after extraction")
            os.rename(extracted, home)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        binaries[tool] = _verify_installed(tool, entry, environment, check_version=True)
    return binaries


def _require_installed(check_version: bool = False) -> Dict[str, Path]:
    entries = {tool: select(tool) for tool in TOOLS}
    environment = _child_environment(
        TOOLCHAIN_ROOT / entries["python"]["installDirectory"], TOOLCHAIN_ROOT / entries["uv"]["installDirectory"]
    )
    for name in ("home", "cache"):
        directory = TOOLCHAIN_ROOT / name
        if directory.is_symlink() or not directory.is_dir():
            raise fail("verified local Python and uv trees are missing; run scripts/toolchain.py bootstrap")
    return {tool: _verify_installed(tool, entry, environment, check_version) for tool, entry in entries.items()}


def _execute(argv: Sequence[str], environment: Dict[str, str]) -> int:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(argv[0], list(argv), environment)
    return 3  # not reached


# ---------------------------------------------------------------- commands

USAGE = "usage: toolchain.py {bootstrap|setup|run|sync|lock|lock-check|deps|--version|platform|describe} ..."


def _exact_arguments(arguments: List[str], count: int, usage: str) -> None:
    if len(arguments) != count:
        raise fail(usage)


def _run_arguments(uv: str, python: str, root: str, arguments: List[str]) -> List[str]:
    return [uv, "--no-config", "run", "--project", root, "--locked", "--offline", "--no-sync", "--python", python, *arguments]


def _sync_arguments(uv: str, python: str, root: str, arguments: List[str]) -> List[str]:
    if arguments not in ([], ["--offline"]):
        raise fail("usage: toolchain.py sync [--offline]")
    return [uv, "--no-config", "sync", "--project", root, "--locked", "--python", python, *arguments]


def _lock_arguments(uv: str, python: str, root: str, arguments: List[str]) -> List[str]:
    _exact_arguments(arguments, 0, "usage: toolchain.py lock")
    return [uv, "--no-config", "lock", "--project", root, "--python", python]


def _lock_check_arguments(uv: str, python: str, root: str, arguments: List[str]) -> List[str]:
    _exact_arguments(arguments, 0, "usage: toolchain.py lock-check")
    return [uv, "--no-config", "lock", "--project", root, "--check", "--offline", "--python", python]


def _deps_arguments(uv: str, python: str, root: str, arguments: List[str]) -> List[str]:
    # deps TARGET REQUIREMENTS [--offline]: a checked project's own test requirements
    # (wheels only) for the pinned Python; the checker puts TARGET on PYTHONPATH.
    offline = arguments[2:] == ["--offline"]
    if len(arguments) not in (2, 3) or (len(arguments) == 3 and not offline):
        raise fail("usage: toolchain.py deps TARGET REQUIREMENTS [--offline]")
    return [
        uv, "--no-config", "pip", "install", "--python", python, "--target", arguments[0],
        "--requirement", arguments[1], "--only-binary", ":all:", "--link-mode=copy", "--reinstall", *arguments[2:],
    ]


def _version_arguments(uv: str, python: str, root: str, arguments: List[str]) -> List[str]:
    _exact_arguments(arguments, 0, "usage: toolchain.py --version")
    return [uv, "--version"]


UV_MODES = {
    "run": _run_arguments,
    "sync": _sync_arguments,
    "lock": _lock_arguments,
    "lock-check": _lock_check_arguments,
    "deps": _deps_arguments,
    "--version": _version_arguments,
}


def _uv_invocation(mode: str, arguments: List[str]) -> List[str]:
    builder = UV_MODES.get(mode)
    if builder is None:
        raise fail(USAGE)
    binaries = _require_installed()
    return builder(str(binaries["uv"]), str(binaries["python"]), str(REPOSITORY_ROOT), arguments)


def _run_child(argv: Sequence[str], quiet: bool = False) -> int:
    entries = {tool: select(tool) for tool in TOOLS}
    environment = _child_environment(
        TOOLCHAIN_ROOT / entries["python"]["installDirectory"], TOOLCHAIN_ROOT / entries["uv"]["installDirectory"]
    )
    completed = subprocess.run(list(argv), env=environment, check=False, stdout=subprocess.DEVNULL if quiet else None)
    return completed.returncode


def command_setup() -> int:
    bootstrap()
    if _run_child(_uv_invocation("sync", ["--offline"])) != 0:
        print("sentinel-tool: locked dependencies are not cached, syncing from the lock file online", file=sys.stderr)
        if _run_child(_uv_invocation("sync", [])) != 0:
            raise fail("dependency sync failed")
    if _run_child(_uv_invocation("run", ["sentinel-py", "version", "--project", str(REPOSITORY_ROOT), "--format", "json"]), quiet=True) != 0:
        raise fail("version failed")
    print("sentinel-tool: python checker ready", file=sys.stderr)
    return 0


def command_describe(key: str) -> int:
    """Maintenance: download and extract another platform's archives and print the lock fields."""

    import json

    output: Dict[str, Any] = {}
    for tool in TOOLS:
        entry = select(tool, key)
        archive = _download(entry)
        with tempfile.TemporaryDirectory(prefix=f"describe-{tool}-") as directory:
            tree = Path(directory) / "tree"
            extract(archive, entry["archiveRoot"], tree)
            output[tool] = {
                "archiveSize": entry["archiveSize"],
                "binarySha256": _sha256_file(tree / entry["binaryRelativePath"]),
                "installedTreeSha256": toolchain_lock._tree_digest(tree),
            }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def command_bootstrap() -> int:
    bootstrap()
    for tool, binary in _require_installed(check_version=True).items():
        print(f"verified {tool}: {binary.relative_to(REPOSITORY_ROOT)}")
    return 0


def command_platform() -> int:
    print(platform_key())
    return 0


def _describe_command(rest: List[str]) -> int:
    _exact_arguments(rest, 1, "usage: toolchain.py describe PLATFORM")
    return command_describe(rest[0])


def _launch(mode: str, rest: List[str]) -> int:
    child = _uv_invocation(mode, rest)
    entries = {tool: select(tool) for tool in TOOLS}
    environment = _child_environment(
        TOOLCHAIN_ROOT / entries["python"]["installDirectory"], TOOLCHAIN_ROOT / entries["uv"]["installDirectory"]
    )
    return _execute(child, environment)


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise fail(USAGE)
    mode, rest = arguments[0], arguments[1:]
    simple = {"platform": command_platform, "bootstrap": command_bootstrap, "setup": command_setup}
    if mode in simple:
        return simple[mode]()
    if mode == "describe":
        return _describe_command(rest)
    return _launch(mode, rest)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolchainError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
