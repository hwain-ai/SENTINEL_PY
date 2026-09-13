from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAMES = ("toolchain.py", "toolchain_lock.py", "uv.sh")
PLATFORM_KEYS = ("linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-aarch64")


def _launcher_source_root() -> Path:
    pristine_value = os.environ.get("SENTINEL_MUTMUT_PRISTINE_ROOT")
    return REPOSITORY_ROOT if pristine_value is None else Path(pristine_value)


sys.path.insert(0, str(_launcher_source_root() / "scripts"))
import toolchain  # noqa: E402
import toolchain_lock  # noqa: E402


class PlatformSelectionTests(unittest.TestCase):
    def test_platform_keys_cover_linux_and_macos_on_both_architectures(self):
        expected = {
            ("Linux", "x86_64"): "linux-x86_64",
            ("Linux", "amd64"): "linux-x86_64",
            ("Linux", "aarch64"): "linux-aarch64",
            ("Darwin", "arm64"): "darwin-aarch64",
            ("Darwin", "x86_64"): "darwin-x86_64",
        }
        for (system, machine), key in expected.items():
            with self.subTest(system=system, machine=machine):
                self.assertEqual(key, toolchain_lock.platform_key(system, machine))
        with self.assertRaises(toolchain.ToolchainError) as stopped:
            toolchain.platform_key("Windows", "AMD64")
        self.assertIn("WSL", str(stopped.exception))
        with self.assertRaises(toolchain_lock.LockError):
            toolchain_lock.platform_key("Linux", "riscv64")

    def test_lock_has_a_complete_verified_entry_for_every_platform(self):
        document = toolchain_lock._load(_launcher_source_root() / "toolchain.lock.json")
        for tool in ("python", "uv"):
            for key in PLATFORM_KEYS:
                with self.subTest(tool=tool, platform=key):
                    entry = toolchain_lock._select(document, tool, True, key)
                    self.assertTrue(entry["archiveUrl"].startswith("https://"))
                    self.assertNotEqual("0" * 64, entry["installedTreeSha256"])
                    self.assertNotEqual("0" * 64, entry["binarySha256"])
                    self.assertIn(key.split("-")[1], entry["archiveUrl"].replace("arm64", "aarch64"))
        with self.assertRaises(toolchain_lock.LockError):
            toolchain_lock._select(document, "python", True, "plan9-mips")


class ExtractionTests(unittest.TestCase):
    def _archive(self, members) -> Path:
        temporary = Path(tempfile.mkdtemp(prefix="sentinel-py-archive-"))
        self.addCleanup(shutil.rmtree, temporary, True)
        archive = temporary / "tool.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, kind, payload in members:
                info = tarfile.TarInfo(name)
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                elif kind == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = payload
                    tar.addfile(info)
                else:
                    data = payload.encode()
                    info.size = len(data)
                    info.mode = 0o755 if kind == "exe" else 0o644
                    tar.addfile(info, io.BytesIO(data))
        return archive

    def test_extraction_normalizes_modes_and_keeps_inside_symlinks(self):
        archive = self._archive([
            ("tool", "dir", None),
            ("tool/bin", "dir", None),
            ("tool/bin/run", "exe", "#!/bin/sh\n"),
            ("tool/lib/data.txt", "file", "x"),
            ("tool/bin/alias", "link", "run"),
        ])
        destination = archive.parent / "tree"
        toolchain.extract(archive, "tool", destination)
        self.assertEqual(0o700, (destination / "bin" / "run").stat().st_mode & 0o777)
        self.assertEqual(0o600, (destination / "lib" / "data.txt").stat().st_mode & 0o777)
        self.assertEqual(0o700, (destination / "lib").stat().st_mode & 0o777)
        self.assertEqual("run", os.readlink(destination / "bin" / "alias"))
        digest = toolchain_lock._tree_digest(destination)
        self.assertEqual(64, len(digest))

    def test_extraction_rejects_escaping_entries_and_links(self):
        cases = {
            "parent path": [("tool", "dir", None), ("tool/../evil", "file", "x")],
            "other root": [("other/file", "file", "x")],
            "absolute link": [("tool", "dir", None), ("tool/link", "link", "/etc/passwd")],
            "escaping link": [("tool", "dir", None), ("tool/link", "link", "../../outside")],
        }
        for label, members in cases.items():
            with self.subTest(label=label):
                archive = self._archive(members)
                with self.assertRaises(toolchain.ToolchainError):
                    toolchain.extract(archive, "tool", archive.parent / "tree")


class LauncherProcessTests(unittest.TestCase):
    """The launcher copied into a scratch repository with fake locked tools."""

    def test_required_launchers_exist(self):
        for name in SCRIPT_NAMES:
            with self.subTest(name=name):
                self.assertTrue((_launcher_source_root() / "scripts" / name).is_file())

    def test_pending_lock_stops_bootstrap_before_creating_toolchain(self):
        repository = self._copy_launcher_repository()
        lock_path = repository / "toolchain.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["status"] = "bootstrap-pending"
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        result = self._run(repository, "bootstrap")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("pending", result.stderr)
        self.assertFalse((repository / ".toolchain" / "python-3.12.13").exists())

    def test_run_gives_the_child_a_minimal_environment_despite_hostile_variables(self):
        repository = self._copy_launcher_repository()
        self._install_fake_python(repository)
        self._install_fake_uv(repository, record_environment=True)
        result = self._run(
            repository,
            "run", "python", "-c", "pass",
            environment={
                "PYTHONHOME": "/hostile/python",
                "PYTHONPATH": "/hostile/imports",
                "VIRTUAL_ENV": "/hostile/venv",
                "UV_INDEX_URL": "https://hostile.example",
            },
        )
        self.assertEqual(0, result.returncode, result.stderr)
        seen = json.loads((repository / ".toolchain" / "seen-environment.json").read_text(encoding="utf-8"))
        self.assertNotIn("PYTHONHOME", seen)
        self.assertNotIn("PYTHONPATH", seen)
        self.assertNotIn("VIRTUAL_ENV", seen)
        self.assertNotIn("UV_INDEX_URL", seen)
        self.assertEqual(str(repository / ".toolchain" / "home"), seen["HOME"])
        self.assertEqual("1", seen["PYTHONDONTWRITEBYTECODE"])
        self.assertEqual("never", seen["UV_PYTHON_DOWNLOADS"])
        self.assertTrue(seen["PATH"].startswith(str(repository / ".toolchain" / "python-3.12.13" / "bin")))

    def test_run_refuses_a_modified_runtime_tree(self):
        repository = self._copy_launcher_repository()
        python_root = self._install_fake_python(repository)
        self._install_fake_uv(repository)
        (python_root / "bin" / "extra").write_text("x", encoding="utf-8")
        result = self._run(repository, "run", "python", "-c", "pass")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("manifest mismatch", result.stderr)

    def test_bootstrap_is_idempotent_when_both_toolchains_are_verified(self):
        repository = self._copy_launcher_repository()
        self._install_fake_python(repository)
        self._install_fake_uv(repository)
        first = self._run(repository, "bootstrap")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn("verified python", first.stdout)
        second = self._run(repository, "bootstrap")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual([], list((repository / ".toolchain" / "downloads").glob("*")))

    def test_usage_errors_are_reported_without_starting_a_child(self):
        repository = self._copy_launcher_repository()
        self._install_fake_python(repository)
        self._install_fake_uv(repository, record_environment=True)
        for arguments in (("deps", "only-target"), ("lock", "extra"), ("nonsense",)):
            with self.subTest(arguments=arguments):
                result = self._run(repository, *arguments)
                self.assertEqual(2, result.returncode)
                self.assertIn("usage", result.stderr)
        self.assertFalse((repository / ".toolchain" / "seen-environment.json").exists())

    # ---------------------------------------------------------------- helpers

    def _copy_launcher_repository(self) -> Path:
        temporary = Path(tempfile.mkdtemp(prefix="sentinel-py-launcher-")).resolve()
        self.addCleanup(shutil.rmtree, temporary, True)
        repository = temporary / "repository"
        scripts = repository / "scripts"
        scripts.mkdir(parents=True)
        source_root = _launcher_source_root()
        shutil.copy2(source_root / "toolchain.lock.json", repository)
        for name in SCRIPT_NAMES:
            shutil.copy2(source_root / "scripts" / name, scripts / name)
        return repository

    def _run(self, repository: Path, *arguments: str, environment=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", "-B", str(repository / "scripts" / "toolchain.py"), *arguments],
            cwd=repository,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", **(environment or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def _current_platform(self) -> str:
        return toolchain_lock.platform_key()

    def _install_fake_tool(self, repository: Path, tool: str, relative_binary: str, script: str) -> Path:
        lock_path = repository / "toolchain.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        entry = lock["toolchains"][tool]["platforms"][self._current_platform()]
        root = repository / ".toolchain" / entry["installDirectory"]
        binary = root / relative_binary
        binary.parent.mkdir(parents=True, exist_ok=True)
        for name in ("home", "cache", "venv"):
            (repository / ".toolchain" / name).mkdir(parents=True, exist_ok=True)
        version_line = f'if [ "$1" = "--version" ]; then echo "{entry["versionOutput"]}"; exit 0; fi\n'
        binary.write_text(script.replace("#!/bin/sh\n", "#!/bin/sh\n" + version_line, 1), encoding="utf-8")
        binary.chmod(0o700)
        for current, directories, files in os.walk(root):
            for name in directories:
                os.chmod(Path(current) / name, 0o700)
        os.chmod(root, 0o700)
        entry["binaryRelativePath"] = relative_binary
        entry["binarySha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        entry["installedTreeSha256"] = toolchain_lock._tree_digest(root)
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        return root

    def _install_fake_python(self, repository: Path) -> Path:
        return self._install_fake_tool(repository, "python", "bin/python3.12", "#!/bin/sh\nexit 0\n")

    def _install_fake_uv(self, repository: Path, record_environment: bool = False) -> Path:
        script = "#!/bin/sh\nexit 0\n"
        if record_environment:
            record = repository / ".toolchain" / "seen-environment.json"
            script = (
                "#!/bin/sh\n"
                f"{sys.executable} -c 'import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], \"w\"))' "
                f"'{record}'\n"
                "exit 0\n"
            )
        return self._install_fake_tool(repository, "uv", "uv", script)


if __name__ == "__main__":
    unittest.main()
