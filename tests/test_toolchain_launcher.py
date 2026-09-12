from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PYTHON = "/usr/bin/python3"
SCRIPT_NAMES = (
    "bootstrap-python.sh",
    "python.sh",
    "toolchain_lock.py",
    "uv.sh",
)


class ToolchainLauncherTests(unittest.TestCase):
    def test_required_launchers_exist(self):
        for name in SCRIPT_NAMES:
            with self.subTest(name=name):
                self.assertTrue((REPOSITORY_ROOT / "scripts" / name).is_file())

    def test_pending_lock_stops_bootstrap_before_creating_toolchain(self):
        repository = self._copy_launcher_repository()
        lock_path = repository / "toolchain.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["status"] = "bootstrap-pending"
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self._run_direct(repository, "bootstrap-python.sh", "--offline")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("pending", result.stderr)
        self.assertFalse((repository / ".toolchain").exists())

    def test_direct_python_launcher_ignores_hostile_startup_environment(self):
        repository = self._copy_launcher_repository()
        canary = self._bash_environment_canary(repository)

        result = self._run_direct(
            repository,
            "python.sh",
            "--version",
            environment={
                "BASH_ENV": str(canary),
                "PYTHONHOME": "/hostile/python",
                "PYTHONPATH": "/hostile/imports",
                "VIRTUAL_ENV": "/hostile/venv",
            },
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("verified local Python", result.stderr)
        self.assertFalse((repository / "bash-env-executed").exists())

    def test_explicit_bash_invocation_is_rejected_even_if_marker_is_spoofed(self):
        repository = self._copy_launcher_repository()
        command = [
            "/usr/bin/bash",
            str(repository / "scripts" / "python.sh"),
            "--version",
        ]
        environment = os.environ.copy()
        environment["SENTINEL_PY_SEALED_ENTRY"] = "direct-v1"

        result = subprocess.run(
            command,
            cwd=repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("must be executed directly", result.stderr)

    def test_lock_reader_rejects_duplicate_json_keys(self):
        repository = self._copy_launcher_repository()
        lock_path = repository / "toolchain.lock.json"
        lock_path.write_text(
            '{"repository":"SENTINEL_PY","repository":"SENTINEL_PY",'
            '"status":"locked","toolchains":{}}\n',
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                SYSTEM_PYTHON,
                "-I",
                str(repository / "scripts" / "toolchain_lock.py"),
                str(lock_path),
                "python",
                "--require-locked",
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("duplicate JSON key", result.stderr)

    def test_locked_metadata_rejects_placeholder_digest(self):
        repository = self._copy_launcher_repository()
        lock_path = repository / "toolchain.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["toolchains"]["python"]["binarySha256"] = "0" * 64
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                SYSTEM_PYTHON,
                "-I",
                str(repository / "scripts" / "toolchain_lock.py"),
                str(lock_path),
                "python",
                "--require-locked",
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("placeholder", result.stderr)

    def test_python_import_does_not_mutate_verified_runtime_tree(self):
        repository = self._copy_launcher_repository()
        python_root = self._install_fake_python(repository)
        before = self._tree_digest(repository, python_root)

        result = self._run_direct(
            repository,
            "python.sh",
            "-c",
            "import unittest",
        )

        after = self._tree_digest(repository, python_root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, after)

    def test_bootstrap_is_idempotent_when_both_toolchains_are_verified(self):
        repository = self._copy_launcher_repository()
        self._install_fake_python(repository)
        self._install_fake_uv(repository)

        result = self._run_direct(repository, "bootstrap-python.sh", "--offline")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("verified Python", result.stdout)

    def _copy_launcher_repository(self) -> Path:
        temporary = Path(tempfile.mkdtemp(prefix="sentinel-py-launcher-"))
        self.addCleanup(shutil.rmtree, temporary, True)
        repository = temporary / "repository"
        scripts = repository / "scripts"
        scripts.mkdir(parents=True)
        source_root = self._launcher_source_root()
        shutil.copy2(source_root / "toolchain.lock.json", repository)
        for name in SCRIPT_NAMES:
            source = source_root / "scripts" / name
            self.assertTrue(source.is_file(), f"required launcher is missing: {name}")
            destination = scripts / name
            shutil.copy2(source, destination)
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        return repository

    def _launcher_source_root(self) -> Path:
        pristine_value = os.environ.get("SENTINEL_MUTMUT_PRISTINE_ROOT")
        if pristine_value is None:
            return REPOSITORY_ROOT
        pristine_root = Path(pristine_value)
        self.assertEqual("mutants", REPOSITORY_ROOT.name)
        self.assertEqual(
            REPOSITORY_ROOT.parent.resolve(strict=True),
            pristine_root.resolve(strict=True),
        )
        return pristine_root

    def _install_fake_python(self, repository: Path) -> Path:
        python_root = repository / ".toolchain" / "python-3.12.13"
        binary = python_root / "bin" / "python3.12"
        binary.parent.mkdir(parents=True)
        (repository / ".toolchain" / "home").mkdir()
        binary.write_text(
            "#!/usr/bin/bash\n"
            "saw_bytecode_flag=false\n"
            "for argument in \"$@\"; do\n"
            "  if [[ \"$argument\" == '-B' ]]; then saw_bytecode_flag=true; fi\n"
            "done\n"
            "if [[ \"$saw_bytecode_flag\" != true ]]; then\n"
            "  /usr/bin/touch -- \"${BASH_SOURCE[0]}.runtime-mutated\"\n"
            "fi\n",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        lock_path = repository / "toolchain.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["toolchains"]["python"]["binarySha256"] = hashlib.sha256(
            binary.read_bytes()
        ).hexdigest()
        lock["toolchains"]["python"]["installedTreeSha256"] = self._tree_digest(
            repository, python_root
        ).strip()
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return python_root

    def _install_fake_uv(self, repository: Path) -> Path:
        uv_root = repository / ".toolchain" / "uv-0.12.9-linux-x86_64"
        binary = uv_root / "uv"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o700)
        lock_path = repository / "toolchain.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["toolchains"]["uv"]["binarySha256"] = hashlib.sha256(
            binary.read_bytes()
        ).hexdigest()
        lock["toolchains"]["uv"]["installedTreeSha256"] = self._tree_digest(
            repository, uv_root
        ).strip()
        lock_path.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return uv_root

    @staticmethod
    def _tree_digest(repository: Path, tree: Path) -> str:
        result = subprocess.run(
            [
                SYSTEM_PYTHON,
                "-I",
                str(repository / "scripts" / "toolchain_lock.py"),
                str(repository / "toolchain.lock.json"),
                "python",
                "--digest-tree",
                str(tree),
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout

    @staticmethod
    def _bash_environment_canary(repository: Path) -> Path:
        canary = repository / "hostile-bash-env.sh"
        canary.write_text(
            f"/usr/bin/touch -- {repository / 'bash-env-executed'}\n",
            encoding="utf-8",
        )
        return canary

    @staticmethod
    def _run_direct(
        repository: Path,
        script_name: str,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        child_environment = os.environ.copy()
        if environment is not None:
            child_environment.update(environment)
        return subprocess.run(
            [str(repository / "scripts" / script_name), *arguments],
            cwd=repository,
            env=child_environment,
            text=True,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
