from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.config import UsageConfigError, load_project  # noqa: E402
from sentinel_py.project_files import DEPENDENCY_DIRECTORY, is_tool_owned_path  # noqa: E402
from sentinel_py.runner.mutation_backend import (  # noqa: E402
    _copy_project,
    _mutation_test_environment,
    _observer_test_environment,
)


def _config(excluded=None) -> dict:
    module = {
        "id": "api",
        "language": "python",
        "root": ".",
        "production": ["src/**/*.py"],
        "testCommand": ["python", "-m", "pytest"],
        "coverage": {"command": ["python", "-m", "coverage", "json"], "format": "coverage-py-json", "report": "coverage.json"},
        "testRoots": ["tests"],
        "testPatterns": ["test_*.py"],
    }
    if excluded is not None:
        module["excluded"] = excluded
    return {"specVersion": "1.0.0", "modules": [module]}


def _write(project: Path, excluded=None) -> None:
    (project / "src").mkdir()
    (project / "tests").mkdir()
    (project / "docs").mkdir()
    (project / "src" / "core.py").write_bytes(b"def core():\n    return 1\n")
    (project / "tests" / "test_core.py").write_bytes(b"def test_core():\n    assert True\n")
    (project / "docs" / "conf.py").write_bytes(b"project = 'x'\n")
    (project / "sentinel.config.json").write_text(json.dumps(_config(excluded)), encoding="utf-8")


class ExcludedSourcesTests(unittest.TestCase):
    def test_undeclared_helper_file_is_rejected_and_excluded_glob_admits_it(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-py-excluded-") as directory:
            project = Path(directory)
            _write(project)
            with self.assertRaises(UsageConfigError) as stopped:
                load_project(str(project), None, None)
            self.assertEqual("unclassifiedSource", stopped.exception.code)
            (project / "sentinel.config.json").write_text(json.dumps(_config(["docs/**/*.py"])), encoding="utf-8")
            loaded = load_project(str(project), None, None)
            self.assertEqual(["src/core.py"], [s.module_relative_path for s in loaded.production_sources])
            self.assertEqual((Path("docs/**/*.py"),), loaded.module.excluded)


class DependencyDirectoryTests(unittest.TestCase):
    def test_dependency_directory_is_tool_owned_but_copied_and_on_pythonpath(self):
        self.assertTrue(is_tool_owned_path(Path(DEPENDENCY_DIRECTORY) / "pkg" / "__init__.py"))
        with tempfile.TemporaryDirectory(prefix="sentinel-py-deps-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src" / "core.py").write_bytes(b"x = 1\n")
            (project / DEPENDENCY_DIRECTORY / "helper").mkdir(parents=True)
            (project / DEPENDENCY_DIRECTORY / "helper" / "__init__.py").write_bytes(b"VALUE = 7\n")
            (project / ".git").mkdir()
            (project / ".git" / "HEAD").write_bytes(b"ref\n")
            snapshot = base / "snapshot"
            _copy_project(project, snapshot)
            self.assertTrue((snapshot / DEPENDENCY_DIRECTORY / "helper" / "__init__.py").is_file())
            self.assertFalse((snapshot / ".git").exists())
            environment = _observer_test_environment(base, snapshot)
            paths = environment["PYTHONPATH"].split(":")
            self.assertEqual(str(snapshot / DEPENDENCY_DIRECTORY), paths[-1])
            self.assertIn(str(snapshot / "src"), paths)
            without = _observer_test_environment(base, base / "missing")
            self.assertNotIn(DEPENDENCY_DIRECTORY, without["PYTHONPATH"])

    def test_mutmut_run_and_mutant_replay_see_the_snapshot_dependencies(self):
        with tempfile.TemporaryDirectory(prefix="sentinel-py-deps-") as directory:
            base = Path(directory)
            snapshot = base / "project"
            (snapshot / DEPENDENCY_DIRECTORY).mkdir(parents=True)
            (snapshot / "mutants").mkdir()
            dependencies = str(snapshot / DEPENDENCY_DIRECTORY)
            run_environment = _mutation_test_environment(base, snapshot)
            self.assertEqual([str(base / "observer"), dependencies], run_environment["PYTHONPATH"].split(":"))
            replay_paths = _mutation_test_environment(base, snapshot, snapshot / "mutants")["PYTHONPATH"].split(":")
            self.assertEqual(dependencies, replay_paths[-1])
            self.assertNotIn(str(snapshot / "mutants" / DEPENDENCY_DIRECTORY), replay_paths)
            baseline_paths = _mutation_test_environment(base, snapshot, snapshot)["PYTHONPATH"].split(":")
            self.assertEqual(1, baseline_paths.count(dependencies))


if __name__ == "__main__":
    unittest.main()
