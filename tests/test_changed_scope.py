from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.config import UsageConfigError, load_project, restrict_production  # noqa: E402


def _write_project(project: Path) -> None:
    (project / "app").mkdir()
    (project / "tests").mkdir()
    (project / "app" / "first.py").write_bytes(b"def first():\n    return 1\n")
    (project / "app" / "second.py").write_bytes(b"def second():\n    return 2\n")
    (project / "tests" / "test_all.py").write_bytes(b"def test_all():\n    assert True\n")
    document = {
        "specVersion": "1.0.0",
        "modules": [
            {
                "id": "api",
                "language": "python",
                "root": ".",
                "production": ["app/**/*.py"],
                "testCommand": ["python", "-m", "pytest"],
                "coverage": {
                    "command": ["python", "-m", "coverage", "json"],
                    "format": "coverage-py-json",
                    "report": "coverage.json",
                },
                "testRoots": ["tests"],
                "testPatterns": ["test_*.py"],
            }
        ],
    }
    (project / "sentinel.config.json").write_text(json.dumps(document), encoding="utf-8")


class ChangedScopeTests(unittest.TestCase):
    def test_restriction_keeps_only_changed_production_sources(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-py-changed-") as directory:
            project_root = Path(directory)
            _write_project(project_root)
            project = load_project(str(project_root), None, None)
            self.assertEqual(2, len(project.production_sources))

            restricted = restrict_production(project, ["app/second.py", "tests/test_all.py", "README.md"])
            self.assertEqual(["app/second.py"], [s.module_relative_path for s in restricted.production_sources])
            self.assertIs(project.module, restricted.module)
            self.assertIsNone(restrict_production(project, ["tests/test_all.py"]))

    def test_invalid_changed_paths_are_usage_errors(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-py-changed-") as directory:
            project_root = Path(directory)
            _write_project(project_root)
            project = load_project(str(project_root), None, None)
            for value in ("/etc/passwd", "app/../app/first.py", "", "app//first.py", "./app/first.py"):
                with self.subTest(value=value):
                    with self.assertRaises(UsageConfigError) as stopped:
                        restrict_production(project, [value])
                    self.assertEqual("changedPathInvalid", stopped.exception.code)

    def test_cli_passes_without_a_run_when_no_changed_production_source(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-changed-") as directory:
            project_root = Path(directory)
            _write_project(project_root)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main([
                    "check", "--strict", "--project", str(project_root), "--format", "json",
                    "--changed-file", "tests/test_all.py",
                ])
            self.assertEqual(0, exit_code)
            result = json.loads(stdout.getvalue())
            self.assertEqual("empty", result["changedScope"])
            self.assertEqual("passed", result["run"]["terminalStatus"])
            self.assertFalse((project_root / ".sentinel").exists())


if __name__ == "__main__":
    unittest.main()
