from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


@unittest.skipIf(
    "SENTINEL_MUTMUT_PRISTINE_ROOT" in os.environ,
    "real backend acceptance is verified before self-mutant execution",
)
class CheckCommandTests(unittest.TestCase):
    def test_both_modes_use_project_dependencies_and_return_equal_measurements(self):
        from sentinel_py.cli import main
        with tempfile.TemporaryDirectory(prefix="sentinel-py-project-dependency-") as directory:
            project = Path(directory)
            self._write_project(project)
            package = project / ".sentinel-deps" / "project_helper"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("EXPECTED = 2\n")
            test = project / "tests/test_subject.py"
            test.write_text("from app.subject import add_one\nfrom project_helper import EXPECTED\n"
                            "def test_add_one():\n    assert add_one(1) == EXPECTED\n")
            source = project / "app/subject.py"
            original = source.read_bytes()
            measurements = []
            for mode in ("parallel", "sequential"):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = main(["check", "--project", str(project), "--execution-mode", mode, "--format", "json"])
                self.assertEqual(code, 0, errors.getvalue())
                document = json.loads(output.getvalue())
                measurements.append((document["crap"], document["mutation"]))
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual((package / "__init__.py").read_text(), "EXPECTED = 2\n")
            self.assertEqual(*measurements)

    def test_strict_check_commits_crap_and_mutation_as_one_run(self):
        from sentinel_py.cli import main

        with tempfile.TemporaryDirectory(prefix="sentinel-py-check-") as directory:
            project = Path(directory)
            self._write_project(project)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    ["check", "--strict", "--project", str(project), "--format", "json"]
                )

            self.assertEqual(0, exit_code, stderr.getvalue() + stdout.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertEqual("check", result["run"]["command"])
            self.assertEqual("strict", result["run"]["mode"])
            self.assertEqual("passed", result["run"]["terminalStatus"])
            self.assertTrue(result["crap"]["pass"])
            self.assertTrue(result["mutation"]["pass"])
            evidence_files = tuple(
                (project / ".sentinel" / "state-v1" / "runs").glob("*/evidence.json")
            )
            self.assertEqual(1, len(evidence_files))
            evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
            self.assertEqual(result["run"]["runId"], evidence["runId"])
            self.assertEqual({"crap", "mutation"}, set(evidence["components"]))

    @staticmethod
    def _write_project(project: Path) -> None:
        (project / "app").mkdir()
        (project / "tests").mkdir()
        (project / "app" / "subject.py").write_bytes(
            b"def add_one(value):\n    return value + 1\n"
        )
        (project / "tests" / "test_subject.py").write_bytes(
            b"from app.subject import add_one\n\n"
            b"def test_add_one():\n    assert add_one(1) == 2\n"
        )
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
        (project / "sentinel.config.json").write_text(
            json.dumps(document, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
