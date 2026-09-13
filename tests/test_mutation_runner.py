from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def _process_alive(pid: int) -> bool:
    """Portable liveness probe (macOS has no /proc): signal 0 succeeds while the process exists."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BackendConfigTests(unittest.TestCase):
    def test_backend_copy_names_reject_every_unrepresentable_or_ambiguous_name(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _backend_copy_name,
        )

        for name in ("nul\0name", "line\nname", "carriage\rname", "surrogate\ud800"):
            with self.subTest(name=ascii(name)):
                with self.assertRaises(MutationBackendError) as stopped:
                    _backend_copy_name(Path(name))
                self.assertEqual("backendCopyPathInvalid", stopped.exception.code)

    def test_backend_copy_and_source_inventory_errors_preserve_exact_codes(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _backend_copy_paths,
            _source_bytes,
        )

        with patch.object(Path, "iterdir", side_effect=OSError("unavailable")):
            with self.assertRaises(MutationBackendError) as stopped:
                _backend_copy_paths(Path("snapshot"))
        self.assertEqual("backendCopyInventoryUnavailable", stopped.exception.code)

        project = SimpleNamespace(
            project_root=Path("/project"),
            production_sources=(SimpleNamespace(path=Path("/project/missing.py")),),
        )
        with patch.object(Path, "read_bytes", side_effect=OSError("unavailable")):
            with self.assertRaises(MutationBackendError) as stopped:
                _source_bytes(project)
        self.assertEqual("productionSourceUnavailable", stopped.exception.code)

    def test_pyproject_backend_detection_handles_absent_present_and_invalid_metadata(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _pyproject_has_mutmut,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-pyproject-detect-") as directory:
            path = Path(directory) / "pyproject.toml"
            path.write_text("[project]\nname = 'subject'\n", encoding="utf-8")
            self.assertFalse(_pyproject_has_mutmut(path))
            path.write_text("[tool.mutmut]\npaths_to_mutate = 'src'\n", encoding="utf-8")
            self.assertTrue(_pyproject_has_mutmut(path))
            path.write_bytes(b"\xff")
            with self.assertRaises(MutationBackendError) as stopped:
                _pyproject_has_mutmut(path)
            self.assertEqual("projectMetadataInvalid", stopped.exception.code)

    def test_backend_copy_inventory_keeps_every_safe_root_test_input(self):
        from sentinel_py.runner.mutation_backend import _backend_copy_paths

        with tempfile.TemporaryDirectory(prefix="sentinel-backend-copy-") as directory:
            snapshot = Path(directory)
            for relative_path in (
                "README.md",
                "backend.lock.json",
                "assets/sample.json",
                "scripts/launcher.sh",
                "src/package/subject.py",
                "tests/test_subject.py",
                ".git/config",
                ".sentinel/state-v1/project.json",
                ".toolchain/venv/dependency.py",
                "mutants/generated.py",
            ):
                path = snapshot / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("input\n", encoding="utf-8")

            self.assertEqual(
                (
                    "README.md",
                    "assets",
                    "backend.lock.json",
                    "scripts",
                    "src",
                    "tests",
                ),
                _backend_copy_paths(snapshot),
            )

    def test_setup_config_detection_is_direct_and_case_sensitive(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _setup_has_mutmut,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-setup-detect-") as directory:
            path = Path(directory) / "setup.cfg"
            path.write_text("[project]\nname = subject\n", encoding="utf-8")
            self.assertFalse(_setup_has_mutmut(path))
            path.write_text("[mutmut]\nsource_paths = app\n", encoding="utf-8")
            self.assertTrue(_setup_has_mutmut(path))
            path.write_text("[MUTMUT]\nsource_paths = app\n", encoding="utf-8")
            self.assertFalse(_setup_has_mutmut(path))

        parser = SimpleNamespace()
        parser.read = Mock()
        parser.has_section = Mock(return_value=True)
        with patch(
            "sentinel_py.runner.mutation_backend.configparser.ConfigParser",
            return_value=parser,
        ) as parser_type:
            self.assertTrue(_setup_has_mutmut(Path("project.cfg")))
        parser_type.assert_called_once_with()
        parser.read.assert_called_once_with(Path("project.cfg"), encoding="utf-8")
        parser.has_section.assert_called_once_with("mutmut")

        failure = OSError("unavailable")
        parser.read.side_effect = failure
        with (
            patch(
                "sentinel_py.runner.mutation_backend.configparser.ConfigParser",
                return_value=parser,
            ),
            self.assertRaises(MutationBackendError) as stopped,
        ):
            _setup_has_mutmut(Path("broken.cfg"))
        self.assertEqual("projectMetadataInvalid", stopped.exception.code)
        self.assertIs(failure, stopped.exception.__cause__)

    def test_dependency_and_coverage_errors_preserve_the_exact_code(self):
        from sentinel_py.quality import DependencyFailure
        from sentinel_py.runner.coverage_backend import CoverageRunnerError
        from sentinel_py.runner.mutation_backend import (
            BaselineFailure,
            MutationBackendError,
        )

        for error_type in (
            BaselineFailure,
            MutationBackendError,
            DependencyFailure,
            CoverageRunnerError,
        ):
            with self.subTest(error_type=error_type.__name__):
                error = error_type("exactCode")
                self.assertEqual("exactCode", error.code)
                self.assertEqual(("exactCode",), error.args)

    def test_backend_config_and_source_inventory_use_every_declared_path(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _install_backend_config,
            _source_bytes,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-backend-config-") as directory:
            root = Path(directory)
            source = root / "app" / "subject.py"
            tests = root / "tests"
            source.parent.mkdir()
            tests.mkdir()
            source.write_bytes(b"value = 1\n")
            (tests / "test_subject.py").write_bytes(b"def test_value(): pass\n")
            (root / "fixture.json").write_text("{}\n", encoding="utf-8")
            project = SimpleNamespace(
                project_root=root,
                production_sources=(SimpleNamespace(path=source),),
                module=SimpleNamespace(test_roots=(tests,)),
            )

            _install_backend_config(project, root)

            setup = (root / "setup.cfg").read_text(encoding="utf-8")
            self.assertEqual(
                "\n[mutmut]\n"
                "source_paths =\n"
                "    app/subject.py\n"
                "pytest_add_cli_args =\n"
                "    -p\n"
                "    _sentinel_pytest_observer_v1\n"
                "pytest_add_cli_args_test_selection =\n"
                "    tests\n"
                "also_copy =\n"
                "    app\n"
                "    fixture.json\n"
                "    tests\n"
                "mutate_only_covered_lines = false\n"
                "use_git_change_detection = false\n",
                setup,
            )
            self.assertEqual({"app/subject.py": b"value = 1\n"}, _source_bytes(project))

            failure = OSError("write failed")
            (root / "setup.cfg").unlink()
            with patch.object(Path, "write_bytes", side_effect=failure):
                with self.assertRaises(MutationBackendError) as stopped:
                    _install_backend_config(project, root)
            self.assertEqual("backendConfigWriteFailed", stopped.exception.code)
            self.assertIs(failure, stopped.exception.__cause__)

    def test_optional_backend_metadata_is_exact_utf8_or_absent(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _optional_text,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-backend-metadata-") as directory:
            path = Path(directory) / "setup.cfg"
            self.assertEqual("", _optional_text(path))
            path.write_text("[project]\n", encoding="utf-8")
            self.assertEqual("[project]\n", _optional_text(path))
            path.write_bytes(b"\xff")
            with self.assertRaises(MutationBackendError) as stopped:
                _optional_text(path)
            self.assertEqual("projectMetadataInvalid", stopped.exception.code)

    def test_backend_run_binds_reports_to_one_nonce_and_rejects_nonzero_exit(self):
        from sentinel_py.runner import mutation_backend
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _run_backend,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-backend-run-") as directory:
            temporary_root = Path(directory)
            snapshot = temporary_root / "project"
            snapshot.mkdir()
            assertion_key = bytes(range(32))
            with (
                patch("sentinel_py.runner.mutation_backend.uuid.uuid4", return_value="nonce"),
                patch(
                    "sentinel_py.runner.mutation_backend.secrets.token_bytes",
                    return_value=assertion_key,
                ) as token_bytes,
                patch(
                    "sentinel_py.runner.mutation_backend._run_process_with_progress",
                    return_value=SimpleNamespace(returncode=0),
                ) as run,
                patch.object(
                    mutation_backend,
                    "_mutation_test_environment",
                    return_value={"BASE": "sealed"},
                ) as mutation_environment,
            ):
                reports = _run_backend(snapshot, temporary_root)

            self.assertEqual(temporary_root / "mutmut-reports", reports.root)
            self.assertEqual("nonce", reports.run_nonce)
            self.assertEqual(assertion_key, reports.assertion_key)
            self.assertNotIn(assertion_key.hex(), repr(reports))
            token_bytes.assert_called_once_with(32)
            command, cwd, environment, progress_root = run.call_args.args
            self.assertEqual((sys.executable, "-m", "mutmut", "run", "--max-children", "1"), command)
            self.assertEqual(snapshot, cwd)
            self.assertEqual(reports.root, progress_root)
            self.assertEqual("sealed", environment["BASE"])
            self.assertEqual("nonce", environment["SENTINEL_MUTMUT_RUN_NONCE"])
            self.assertEqual(
                str(reports.root),
                environment["SENTINEL_MUTMUT_REPORT_ROOT"],
            )
            self.assertEqual(
                assertion_key.hex(),
                environment["SENTINEL_PYTEST_HMAC_KEY"],
            )
            self.assertEqual(
                {
                    "startup_timeout_seconds": 15 * 60,
                    "idle_timeout_seconds": 10 * 60,
                    "absolute_timeout_seconds": 24 * 60 * 60,
                    "poll_seconds": 1,
                },
                run.call_args.kwargs,
            )
            self.assertEqual(0o700, reports.root.stat().st_mode & 0o777)
            mutation_environment.assert_called_once_with(temporary_root, snapshot)

        with tempfile.TemporaryDirectory(prefix="sentinel-backend-failed-") as directory:
            temporary_root = Path(directory)
            snapshot = temporary_root / "project"
            snapshot.mkdir()
            with patch(
                "sentinel_py.runner.mutation_backend._run_process_with_progress",
                return_value=SimpleNamespace(returncode=1),
            ):
                with self.assertRaises(MutationBackendError) as stopped:
                    _run_backend(snapshot, temporary_root)
            self.assertEqual("backendProcessFailed", stopped.exception.code)

    def test_backend_run_requests_exact_owner_only_report_mode(self):
        from sentinel_py.runner import mutation_backend

        with (
            patch.object(Path, "mkdir") as mkdir,
            patch.object(mutation_backend.uuid, "uuid4", return_value="nonce"),
            patch.object(mutation_backend.secrets, "token_bytes", return_value=b"k" * 32),
            patch.object(mutation_backend, "_mutation_test_environment", return_value={}),
            patch.object(
                mutation_backend,
                "_run_process_with_progress",
                return_value=SimpleNamespace(returncode=0),
            ),
        ):
            reports = mutation_backend._run_backend(Path("/snapshot"), Path("/temporary"))

        self.assertEqual(Path("/temporary/mutmut-reports"), reports.root)
        mkdir.assert_called_once_with(mode=0o700)

    def test_coverage_backend_runs_the_exact_locked_command_sequence(self):
        from sentinel_py.runner import coverage_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-command-") as directory:
            temporary_root = Path(directory)
            module_root = temporary_root / "project" / "module"
            module_root.mkdir(parents=True)
            report_path = module_root / "reports" / "coverage.json"
            project = SimpleNamespace(
                module=SimpleNamespace(
                    root=module_root,
                    test_roots=(module_root / "tests", module_root / "checks"),
                    coverage_command=("python", "-m", "coverage", "json"),
                )
            )
            environment = {"LOCKED": "environment"}
            coverage_config = temporary_root / "coverage.ini"
            command_environment = {
                **environment,
                "COVERAGE_RCFILE": str(coverage_config),
            }
            commands = (
                (sys.executable, "-m", "coverage", "erase"),
                (
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    "--source=.",
                    "-m",
                    "pytest",
                    "tests",
                    "checks",
                ),
                (
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    "-o",
                    "reports/coverage.json",
                ),
            )

            with (
                patch.object(
                    coverage_backend,
                    "_observer_test_environment",
                    return_value=environment,
                ) as observer_environment,
                patch.object(
                    coverage_backend,
                    "_run_process",
                    side_effect=(
                        SimpleNamespace(returncode=0),
                        SimpleNamespace(returncode=0),
                        SimpleNamespace(returncode=0),
                    ),
                ) as run_process,
            ):
                self.assertIsNone(
                    coverage_backend._execute_coverage(
                        project,
                        module_root,
                        report_path,
                        temporary_root,
                    )
                )

            observer_environment.assert_called_once_with(temporary_root, module_root)
            self.assertEqual(
                "[run]\nomit =\n    .sentinel-deps/*\n[report]\nexclude_lines =\n",
                coverage_config.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [
                    call(command, module_root, command_environment)
                    for command in commands
                ],
                run_process.call_args_list,
            )

            for failed_index in range(3):
                with self.subTest(failed_index=failed_index):
                    results = [SimpleNamespace(returncode=0) for _ in range(3)]
                    results[failed_index] = SimpleNamespace(returncode=17)
                    with (
                        patch.object(
                            coverage_backend,
                            "_observer_test_environment",
                            return_value=environment,
                        ),
                        patch.object(
                            coverage_backend,
                            "_run_process",
                            side_effect=results,
                        ) as run_process,
                    ):
                        with self.assertRaises(
                            coverage_backend.CoverageRunnerError
                        ) as stopped:
                            coverage_backend._execute_coverage(
                                project,
                                module_root,
                                report_path,
                                temporary_root,
                            )
                    self.assertEqual("coverageExecutionFailed", stopped.exception.code)
                    self.assertEqual(failed_index + 1, run_process.call_count)

            project.module.coverage_command = ("coverage", "json")
            coverage_config.unlink()
            with self.assertRaises(coverage_backend.CoverageRunnerError) as stopped:
                coverage_backend._execute_coverage(
                    project,
                    module_root,
                    report_path,
                    temporary_root,
                )
            self.assertEqual("unsupportedCoverageCommand", stopped.exception.code)
            self.assertFalse(coverage_config.exists())

            project.module.coverage_command = ("python", "-m", "coverage", "json")
            with (
                patch.object(
                    Path,
                    "write_text",
                    side_effect=OSError("write failed"),
                ),
                patch.object(coverage_backend, "_run_process") as failed_run,
            ):
                with self.assertRaises(coverage_backend.CoverageRunnerError) as stopped:
                    coverage_backend._execute_coverage(
                        project,
                        module_root,
                        report_path,
                        temporary_root,
                    )
            self.assertEqual("coverageConfigWriteFailed", stopped.exception.code)
            failed_run.assert_not_called()

    def test_검사_전용_coverage는_기본_제외_줄도_미실행으로_보고한다(self):
        from sentinel_py.coverage import load_coverage_json
        from sentinel_py.runner.coverage_backend import _execute_coverage

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-defaults-") as directory:
            temporary_root = Path(directory)
            module_root = temporary_root / "project"
            tests = module_root / "tests"
            tests.mkdir(parents=True)
            (module_root / "subject.py").write_text(
                "def typed_stub() -> int:\n"
                "    ...\n\n"
                "def not_called():\n"
                "    hidden = 1  # pragma: no cover\n"
                "    return hidden\n",
                encoding="utf-8",
            )
            (tests / "test_subject.py").write_text(
                "import subject\n\n"
                "def test_imported():\n"
                "    assert subject is not None\n",
                encoding="utf-8",
            )
            report_path = module_root / "coverage.json"
            project = SimpleNamespace(
                module=SimpleNamespace(
                    root=module_root,
                    test_roots=(tests,),
                    coverage_command=("python", "-m", "coverage", "json"),
                )
            )

            _execute_coverage(project, module_root, report_path, temporary_root)

            raw_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual([], raw_report["files"]["subject.py"]["excluded_lines"])
            parsed = load_coverage_json(report_path.read_bytes())
            self.assertEqual((2, 5, 6), parsed.files["subject.py"].missing_lines)
            self.assertEqual((), parsed.files["subject.py"].excluded_lines)

    def test_검사_전용_coverage는_프로젝트_coverage_설정을_상속하지_않는다(self):
        from sentinel_py.coverage import load_coverage_json
        from sentinel_py.runner.coverage_backend import _execute_coverage

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-project-config-") as directory:
            temporary_root = Path(directory)
            module_root = temporary_root / "project"
            tests = module_root / "tests"
            tests.mkdir(parents=True)
            (module_root / "subject.py").write_text(
                "def not_called():\n"
                "    hidden = 1\n"
                "    return hidden\n",
                encoding="utf-8",
            )
            (tests / "test_subject.py").write_text(
                "import subject\n\n"
                "def test_imported():\n"
                "    assert subject is not None\n",
                encoding="utf-8",
            )
            project_config = module_root / ".coveragerc"
            project_config.write_text(
                "[run]\n"
                "omit =\n"
                "    subject.py\n"
                "[report]\n"
                "exclude_lines =\n"
                "    return hidden\n"
                "exclude_also =\n"
                "    hidden = 1\n"
                "fail_under = 100\n",
                encoding="utf-8",
            )
            original_config = project_config.read_bytes()
            report_path = module_root / "coverage.json"
            project = SimpleNamespace(
                module=SimpleNamespace(
                    root=module_root,
                    test_roots=(tests,),
                    coverage_command=("python", "-m", "coverage", "json"),
                )
            )

            _execute_coverage(project, module_root, report_path, temporary_root)

            raw_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIn("subject.py", raw_report["files"])
            self.assertEqual([], raw_report["files"]["subject.py"]["excluded_lines"])
            parsed = load_coverage_json(report_path.read_bytes())
            self.assertEqual((2, 3), parsed.files["subject.py"].missing_lines)
            self.assertEqual(original_config, project_config.read_bytes())

    def test_실제_coverage가_source_src_root_검사_복사본의_실행_줄을_보고한다(self):
        from sentinel_py.runner.coverage_backend import _execute_coverage

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-binding-") as directory:
            root = Path(directory).resolve()
            cases = (
                ("src", ("src",), "src"),
                ("flat", ("flat",), "flat"),
                ("src-flat", ("src", "flat"), "src"),
                ("source-src-flat", ("source", "src", "flat"), "source"),
            )
            for layout, locations, selected in cases:
                with self.subTest(layout=layout):
                    temporary_root = root / layout
                    module_root = temporary_root / "project"
                    tests = module_root / "tests"
                    tests.mkdir(parents=True)
                    (module_root / "flat_helper.py").write_text(
                        "MARKER = 'snapshot-helper'\n",
                        encoding="utf-8",
                    )
                    for location in locations:
                        source_root = (
                            module_root if location == "flat" else module_root / location
                        )
                        package = source_root / "sentinel_py"
                        package.mkdir(parents=True)
                        (package / "__init__.py").write_text("", encoding="utf-8")
                        (package / "mutation.py").write_text(
                            f"MARKER = {location!r}\n"
                            "import flat_helper\n\n"
                            "def observed_marker():\n"
                            "    return MARKER + ':' + flat_helper.MARKER\n",
                            encoding="utf-8",
                        )
                    (tests / "test_subject.py").write_text(
                        "import json\n"
                        "from pathlib import Path\n"
                        "import flat_helper\n"
                        "from sentinel_py import mutation\n\n"
                        "def test_검사_복사본을_실행한다():\n"
                        "    observed = getattr(\n"
                        "        mutation, 'observed_marker', lambda: 'installed-original'\n"
                        "    )()\n"
                        "    Path(__file__).with_name('import.json').write_text(\n"
                        "        json.dumps({\n"
                        "            'path': str(Path(mutation.__file__).resolve()),\n"
                        "            'value': observed,\n"
                        "            'helperPath': str(Path(flat_helper.__file__).resolve()),\n"
                        "        }),\n"
                        "        encoding='utf-8',\n"
                        "    )\n",
                        encoding="utf-8",
                    )
                    report_path = module_root / "coverage.json"
                    project = SimpleNamespace(
                        module=SimpleNamespace(
                            root=module_root,
                            test_roots=(tests,),
                            coverage_command=("python", "-m", "coverage", "json"),
                        )
                    )

                    _execute_coverage(
                        project,
                        module_root,
                        report_path,
                        temporary_root,
                    )

                    selected_root = (
                        module_root if selected == "flat" else module_root / selected
                    )
                    selected_key = (
                        "sentinel_py/mutation.py"
                        if selected == "flat"
                        else f"{selected}/sentinel_py/mutation.py"
                    )
                    observation = json.loads(
                        (tests / "import.json").read_text(encoding="utf-8")
                    )
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        str(selected_root / "sentinel_py/mutation.py"),
                        observation["path"],
                    )
                    self.assertEqual(
                        f"{selected}:snapshot-helper",
                        observation["value"],
                    )
                    self.assertEqual(
                        str(module_root / "flat_helper.py"),
                        observation["helperPath"],
                    )
                    self.assertEqual(
                        [1, 2, 4, 5],
                        report["files"][selected_key]["executed_lines"],
                    )
                    self.assertEqual(
                        [1],
                        report["files"]["flat_helper.py"]["executed_lines"],
                    )

    def test_fresh_coverage_preserves_backend_errors_and_source_inventory(self):
        from sentinel_py.runner import coverage_backend
        from sentinel_py.runner.mutation_backend import MutationBackendError

        project = SimpleNamespace(project_root=Path("/project"))
        execution = object()
        with (
            patch.object(
                coverage_backend,
                "_safe_inventory",
                side_effect=(("before",), ("before",)),
            ) as inventory,
            patch.object(
                coverage_backend,
                "_run_snapshot_coverage",
                return_value=execution,
            ) as run_snapshot,
        ):
            self.assertIs(execution, coverage_backend.run_fresh_coverage(project))
        self.assertEqual(
            [call(project.project_root), call(project.project_root)],
            inventory.call_args_list,
        )
        run_snapshot.assert_called_once_with(project)

        backend_error = MutationBackendError("snapshotCopyFailed")
        with (
            patch.object(
                coverage_backend,
                "_safe_inventory",
                side_effect=(("before",), ("before",)),
            ),
            patch.object(
                coverage_backend,
                "_run_snapshot_coverage",
                side_effect=backend_error,
            ),
            self.assertRaises(coverage_backend.CoverageRunnerError) as stopped,
        ):
            coverage_backend.run_fresh_coverage(project)
        self.assertEqual("snapshotCopyFailed", stopped.exception.code)
        self.assertIs(backend_error, stopped.exception.__cause__)

        with (
            patch.object(
                coverage_backend,
                "_safe_inventory",
                side_effect=(("before",), ("after",)),
            ),
            patch.object(
                coverage_backend,
                "_run_snapshot_coverage",
                return_value=execution,
            ),
            self.assertRaises(coverage_backend.CoverageRunnerError) as stopped,
        ):
            coverage_backend.run_fresh_coverage(project)
        self.assertEqual("protectedSourceChanged", stopped.exception.code)

    def test_coverage_safe_inventory_preserves_backend_error_code(self):
        from sentinel_py.runner import coverage_backend
        from sentinel_py.runner.mutation_backend import MutationBackendError

        backend_error = MutationBackendError("protectedInventoryUnavailable")
        with (
            patch.object(
                coverage_backend,
                "_protected_inventory",
                side_effect=backend_error,
            ),
            self.assertRaises(coverage_backend.CoverageRunnerError) as stopped,
        ):
            coverage_backend._safe_inventory(Path("/project"))

        self.assertEqual("protectedInventoryUnavailable", stopped.exception.code)
        self.assertIs(backend_error, stopped.exception.__cause__)

    def test_snapshot_coverage_uses_named_private_project_and_exact_pipeline(self):
        from sentinel_py.runner import coverage_backend

        project = SimpleNamespace(project_root=Path("/source"))
        temporary = Path("/temporary")
        snapshot = temporary / "project"
        module_root = snapshot / "module"
        report_path = snapshot / "coverage.json"
        directory = MagicMock()
        directory.__enter__.return_value = str(temporary)
        directory.__exit__.return_value = False

        with (
            patch.object(
                coverage_backend.tempfile,
                "TemporaryDirectory",
                return_value=directory,
            ) as temporary_directory,
            patch.object(coverage_backend, "_copy_project") as copy_project,
            patch.object(coverage_backend, "_prepare_observer") as prepare_observer,
            patch.object(coverage_backend, "_require_two_baselines") as baselines,
            patch.object(
                coverage_backend,
                "_snapshot_module_root",
                return_value=module_root,
            ) as snapshot_module_root,
            patch.object(
                coverage_backend,
                "_snapshot_report_path",
                return_value=report_path,
            ) as snapshot_report_path,
            patch.object(coverage_backend, "_remove_old_report") as remove_report,
            patch.object(coverage_backend, "_execute_coverage") as execute_coverage,
            patch.object(
                coverage_backend,
                "_snapshot_sources",
                return_value={"app.py": b"source"},
            ) as snapshot_sources,
            patch.object(
                coverage_backend,
                "_read_report",
                return_value=b"report",
            ) as read_report,
        ):
            execution = coverage_backend._run_snapshot_coverage(project)

        temporary_directory.assert_called_once_with(prefix="sentinel-py-coverage-")
        copy_project.assert_called_once_with(project.project_root, snapshot)
        prepare_observer.assert_called_once_with(temporary)
        baselines.assert_called_once_with(project, snapshot, temporary)
        snapshot_module_root.assert_called_once_with(project, snapshot)
        snapshot_report_path.assert_called_once_with(project, snapshot)
        remove_report.assert_called_once_with(report_path)
        execute_coverage.assert_called_once_with(
            project,
            module_root,
            report_path,
            temporary,
        )
        snapshot_sources.assert_called_once_with(project, snapshot)
        read_report.assert_called_once_with(report_path)
        self.assertEqual({"app.py": b"source"}, execution.sources)
        self.assertEqual(b"report", execution.report)

    def test_old_coverage_report_removal_is_fail_closed(self):
        from sentinel_py.runner import coverage_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-old-coverage-") as directory:
            root = Path(directory)
            missing = root / "missing.json"
            self.assertIsNone(coverage_backend._remove_old_report(missing))

            regular = root / "coverage.json"
            regular.write_bytes(b"old")
            self.assertIsNone(coverage_backend._remove_old_report(regular))
            self.assertFalse(regular.exists())

            invalid = root / "report-directory"
            invalid.mkdir()
            with self.assertRaises(coverage_backend.CoverageRunnerError) as stopped:
                coverage_backend._remove_old_report(invalid)

        self.assertEqual("coverageReportPathInvalid", stopped.exception.code)

    def test_snapshot_source_read_failure_has_the_exact_error(self):
        from sentinel_py.runner import coverage_backend

        project = SimpleNamespace(
            project_root=Path("/project"),
            production_sources=(
                SimpleNamespace(
                    path=Path("/project/app.py"),
                    module_relative_path="app.py",
                ),
            ),
        )
        failure = OSError("unavailable")
        with (
            patch.object(Path, "read_bytes", side_effect=failure),
            self.assertRaises(coverage_backend.CoverageRunnerError) as stopped,
        ):
            coverage_backend._snapshot_sources(project, Path("/snapshot"))

        self.assertEqual("productionSourceUnavailable", stopped.exception.code)
        self.assertIs(failure, stopped.exception.__cause__)

    def test_coverage_report_reader_accepts_only_one_regular_readable_file(self):
        from sentinel_py.runner import coverage_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-coverage-report-") as directory:
            root = Path(directory)
            report = root / "coverage.json"
            report.write_bytes(b'{"coverage":true}')
            self.assertEqual(b'{"coverage":true}', coverage_backend._read_report(report))

            link = root / "coverage-link.json"
            link.symlink_to(report)
            for invalid in (root / "missing.json", root, link):
                with self.subTest(path=invalid.name):
                    with self.assertRaises(
                        coverage_backend.CoverageRunnerError
                    ) as stopped:
                        coverage_backend._read_report(invalid)
                    self.assertEqual("coverageReportUnavailable", stopped.exception.code)

            failure = OSError("read failed")
            with patch.object(Path, "read_bytes", side_effect=failure):
                with self.assertRaises(
                    coverage_backend.CoverageRunnerError
                ) as stopped:
                    coverage_backend._read_report(report)
            self.assertEqual("coverageReportUnavailable", stopped.exception.code)
            self.assertIs(failure, stopped.exception.__cause__)

    def test_injected_backend_config_loads_the_external_structured_observer(self):
        from sentinel_py.runner.mutation_backend import (
            _OBSERVER_MODULE,
            _backend_config_lines,
        )

        lines = _backend_config_lines(
            ("app/subject.py",),
            ("tests",),
            ("README.md", "fixtures"),
        )

        self.assertEqual(
            [
                "",
                "[mutmut]",
                "source_paths =",
                "    app/subject.py",
                "pytest_add_cli_args =",
                "    -p",
                "    " + _OBSERVER_MODULE,
                "pytest_add_cli_args_test_selection =",
                "    tests",
                "also_copy =",
                "    README.md",
                "    fixtures",
                "mutate_only_covered_lines = false",
                "use_git_change_detection = false",
                "",
            ],
            lines,
        )
        self.assertNotIn("    sentinel_py.runner.pytest_reporter", lines)

    def test_external_observer_copy_is_owner_only_and_byte_exact(self):
        from sentinel_py.runner.mutation_backend import (
            _observer_source,
            _prepare_observer,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-observer-") as directory:
            observer = _prepare_observer(Path(directory))
            expected = _observer_source().read_bytes()

            self.assertEqual(expected, observer.read_bytes())
            self.assertEqual(0o600, observer.stat().st_mode & 0o777)
            self.assertEqual(0o700, observer.parent.stat().st_mode & 0o777)

    def test_observer_copy_requests_exact_owner_only_modes(self):
        from sentinel_py.runner import mutation_backend

        source = MagicMock()
        source.is_symlink.return_value = False
        source.is_file.return_value = True
        source.read_bytes.return_value = b"observer"
        with (
            patch.object(mutation_backend, "_observer_source", return_value=source),
            patch.object(Path, "mkdir") as mkdir,
            patch.object(Path, "write_bytes") as write_bytes,
            patch.object(Path, "chmod") as chmod,
        ):
            destination = mutation_backend._prepare_observer(Path("/temporary"))

        self.assertEqual(
            Path("/temporary/observer/_sentinel_pytest_observer_v1.py"),
            destination,
        )
        mkdir.assert_called_once_with(mode=0o700)
        write_bytes.assert_called_once_with(b"observer")
        chmod.assert_called_once_with(0o600)

    def test_observer_copy_rejects_invalid_sources_and_reports_io_failure(self):
        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-observer-errors-") as directory:
            root = Path(directory)
            source = root / "reporter.py"
            source.write_bytes(b"reporter")
            invalid_sources = (root / "missing.py", root)
            for invalid in invalid_sources:
                with self.subTest(source=invalid.name):
                    with patch.object(
                        mutation_backend,
                        "_observer_source",
                        return_value=invalid,
                    ):
                        with self.assertRaises(
                            mutation_backend.MutationBackendError
                        ) as stopped:
                            mutation_backend._prepare_observer(root / "temporary")
                    self.assertEqual("observerSourceInvalid", stopped.exception.code)

            link = root / "reporter-link.py"
            link.symlink_to(source)
            with patch.object(
                mutation_backend,
                "_observer_source",
                return_value=link,
            ):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._prepare_observer(root / "linked-temporary")
            self.assertEqual("observerSourceInvalid", stopped.exception.code)

            failure = OSError("read failed")
            with (
                patch.object(
                    mutation_backend,
                    "_observer_source",
                    return_value=source,
                ),
                patch.object(Path, "read_bytes", side_effect=failure),
            ):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._prepare_observer(root / "failed-temporary")
            self.assertEqual("observerCopyFailed", stopped.exception.code)
            self.assertIs(failure, stopped.exception.__cause__)

    def test_nested_self_mutation_uses_the_pristine_observer_source(self):
        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-pristine-observer-") as directory:
            pristine = Path(directory).resolve() / "project"
            relative_root = Path("src/sentinel_py/runner")
            pristine_runner = pristine / relative_root
            mutated_runner = pristine / "mutants" / relative_root
            pristine_runner.mkdir(parents=True)
            mutated_runner.mkdir(parents=True)
            pristine_reporter = pristine_runner / "pytest_reporter.py"
            pristine_reporter.write_text("pristine\n", encoding="utf-8")
            mutated_reporter = mutated_runner / "pytest_reporter.py"
            mutated_reporter.write_text("trampoline\n", encoding="utf-8")
            mutated_backend = mutated_runner / "mutation_backend.py"
            mutated_backend.write_text("backend\n", encoding="utf-8")

            with (
                patch.object(mutation_backend, "__file__", str(mutated_backend)),
                patch.dict(
                    os.environ,
                    {"SENTINEL_MUTMUT_PRISTINE_ROOT": str(pristine)},
                    clear=False,
                ),
            ):
                selected = mutation_backend._observer_source()

            self.assertEqual(pristine_reporter, selected)
            self.assertNotEqual(mutated_reporter, selected)

    def test_observer_source_uses_the_exact_local_fallback_and_strict_resolution(self):
        from sentinel_py.runner import mutation_backend

        current = Path(mutation_backend.__file__)
        expected = current.with_name("pytest_reporter.py")
        without_pristine = dict(os.environ)
        without_pristine.pop("SENTINEL_MUTMUT_PRISTINE_ROOT", None)
        with patch.dict(os.environ, without_pristine, clear=True):
            self.assertEqual(expected, mutation_backend._observer_source())

        with (
            patch.dict(
                os.environ,
                {
                    **os.environ,
                    "SENTINEL_MUTMUT_PRISTINE_ROOT": "/missing-pristine",
                },
                clear=True,
            ),
            patch.object(
                mutation_backend,
                "_nested_pristine_source",
                return_value=None,
            ) as nested_source,
        ):
            self.assertEqual(expected, mutation_backend._observer_source())
        nested_source.assert_called_once_with(current, "/missing-pristine")

        resolved_root = Path("/resolved/project")
        resolved_current = resolved_root / "mutants/src/pkg/mutation_backend.py"
        with patch.object(
            Path,
            "resolve",
            autospec=True,
            side_effect=(resolved_root, resolved_current),
        ) as resolve:
            self.assertEqual(
                resolved_root / "src/pkg/pytest_reporter.py",
                mutation_backend._nested_pristine_source(
                    Path("/input/mutation_backend.py"),
                    "/input/project",
                ),
            )
        self.assertEqual(
            [
                call(Path("/input/project"), strict=True),
                call(Path("/input/mutation_backend.py"), strict=True),
            ],
            resolve.call_args_list,
        )

    def test_repository_pyproject_does_not_override_the_locked_backend_config(self):
        from sentinel_py.runner.mutation_backend import _pyproject_has_mutmut

        self.assertFalse(_pyproject_has_mutmut(REPOSITORY_ROOT / "pyproject.toml"))

    def test_pristine_baselines_run_before_backend_config_is_injected(self):
        from sentinel_py.runner.mutation_backend import (
            _BackendReports,
            MutationExecution,
            _run_in_temporary_snapshot,
        )

        events = []
        project = SimpleNamespace(project_root=Path("/approved-project"))
        source_payload = {"src/subject.py": b"value = 1\n"}
        candidates = ("candidate-a", "candidate-b")
        raw_results = {"candidate-a": 1, "candidate-b": 0}
        failure_states = {"candidate-a": "killed"}
        records = (object(), object())

        with tempfile.TemporaryDirectory(prefix="sentinel-orchestration-test-") as directory:
            temporary_root = Path(directory) / "controlled-run"
            snapshot = temporary_root / "project"
            reports = _BackendReports(
                temporary_root / "mutmut-reports",
                "nonce",
                bytes(range(32)),
            )

            def record_copy(project_root, actual_snapshot):
                events.append("copy")
                self.assertEqual(project.project_root, project_root)
                self.assertEqual(snapshot, actual_snapshot)
                (actual_snapshot / "mutants" / "nested").mkdir(parents=True)

            def record_observer(actual_temporary_root):
                events.append("observer")
                self.assertEqual(temporary_root, actual_temporary_root)

            def record_baselines(actual_project, actual_snapshot, actual_temporary_root):
                events.append("baselines")
                self.assertIs(project, actual_project)
                self.assertEqual(snapshot, actual_snapshot)
                self.assertEqual(temporary_root, actual_temporary_root)

            def record_config(actual_project, actual_snapshot):
                events.append("config")
                self.assertIs(project, actual_project)
                self.assertEqual(snapshot, actual_snapshot)

            def record_backend(actual_snapshot, actual_temporary_root):
                events.append("backend")
                self.assertEqual(snapshot, actual_snapshot)
                self.assertEqual(temporary_root, actual_temporary_root)
                (actual_snapshot / "mutants" / "z.py.meta").write_text("z")
                (actual_snapshot / "mutants" / "nested" / "a.py.meta").write_text("a")
                return reports

            expected_meta_paths = (
                snapshot / "mutants" / "nested" / "a.py.meta",
                snapshot / "mutants" / "z.py.meta",
            )
            controlled_directory = contextlib.nullcontext(str(temporary_root))
            with (
                patch(
                    "sentinel_py.runner.mutation_backend.tempfile.TemporaryDirectory",
                    return_value=controlled_directory,
                ) as temporary_directory,
                patch(
                    "sentinel_py.runner.mutation_backend._copy_project",
                    side_effect=record_copy,
                ),
                patch(
                    "sentinel_py.runner.mutation_backend._prepare_observer",
                    side_effect=record_observer,
                ),
                patch(
                    "sentinel_py.runner.mutation_backend._source_bytes",
                    return_value=source_payload,
                ) as source_bytes,
                patch(
                    "sentinel_py.runner.mutation_backend.enumerate_candidates",
                    return_value=candidates,
                ) as enumerate_mutants,
                patch(
                    "sentinel_py.runner.mutation_backend._require_two_baselines",
                    side_effect=record_baselines,
                ),
                patch(
                    "sentinel_py.runner.mutation_backend._install_backend_config",
                    side_effect=record_config,
                ),
                patch(
                    "sentinel_py.runner.mutation_backend._run_backend",
                    side_effect=record_backend,
                ),
                patch(
                    "sentinel_py.runner.mutation_backend.read_raw_exit_codes",
                    return_value=raw_results,
                ) as read_results,
                patch(
                    "sentinel_py.runner.mutation_backend._require_raw_candidate_set",
                ) as require_candidates,
                patch(
                    "sentinel_py.runner.mutation_backend._classify_test_failures",
                    return_value=failure_states,
                ) as classify_failures,
                patch(
                    "sentinel_py.runner.mutation_backend.load_results",
                    return_value=records,
                ) as load_results,
            ):
                execution = _run_in_temporary_snapshot(project)

        self.assertEqual(MutationExecution(candidates, records), execution)
        self.assertEqual(["copy", "observer", "baselines", "config", "backend"], events)
        temporary_directory.assert_called_once_with(prefix="sentinel-py-mutmut-")
        source_bytes.assert_called_once_with(project)
        enumerate_mutants.assert_called_once_with(source_payload)
        read_results.assert_called_once_with(expected_meta_paths)
        require_candidates.assert_called_once_with(candidates, raw_results)
        classify_failures.assert_called_once_with(
            project,
            snapshot,
            temporary_root,
            raw_results,
            reports,
        )
        load_results.assert_called_once_with(
            candidates,
            expected_meta_paths,
            failure_states,
        )

    def test_two_baselines_require_both_passes_and_one_stable_inventory(self):
        from sentinel_py.runner.mutation_backend import (
            BaselineFailure,
            _require_two_baselines,
        )

        project_root = Path("/project")
        snapshot = Path("/snapshot")
        temporary_root = Path("/temporary")
        project = SimpleNamespace(
            project_root=project_root,
            module=SimpleNamespace(
                test_command=("python", "-m", "pytest"),
                test_roots=(project_root / "tests", project_root / "checks"),
            ),
        )
        selection = ("tests", "checks")

        for first_passed, second_passed in (
            (False, False),
            (False, True),
            (True, False),
        ):
            with self.subTest(first=first_passed, second=second_passed):
                observations = (
                    SimpleNamespace(complete_pass=first_passed, collected=7),
                    SimpleNamespace(complete_pass=second_passed, collected=7),
                )
                with patch(
                    "sentinel_py.runner.mutation_backend._run_pytest",
                    side_effect=observations,
                ) as run_pytest:
                    with self.assertRaises(BaselineFailure) as stopped:
                        _require_two_baselines(project, snapshot, temporary_root)
                self.assertEqual("baselineTestsFailed", stopped.exception.code)
                self.assertEqual(
                    [
                        call(snapshot, selection, "", temporary_root),
                        call(snapshot, selection, "", temporary_root),
                    ],
                    run_pytest.call_args_list,
                )

        stable = (
            SimpleNamespace(complete_pass=True, collected=7),
            SimpleNamespace(complete_pass=True, collected=7),
        )
        with patch(
            "sentinel_py.runner.mutation_backend._run_pytest",
            side_effect=stable,
        ) as run_pytest:
            self.assertIsNone(_require_two_baselines(project, snapshot, temporary_root))
        self.assertEqual(2, run_pytest.call_count)

        changed = (
            SimpleNamespace(complete_pass=True, collected=7),
            SimpleNamespace(complete_pass=True, collected=8),
        )
        with patch(
            "sentinel_py.runner.mutation_backend._run_pytest",
            side_effect=changed,
        ):
            with self.assertRaises(BaselineFailure) as stopped:
                _require_two_baselines(project, snapshot, temporary_root)
        self.assertEqual("baselineInventoryChanged", stopped.exception.code)

        project.module.test_command = ("pytest",)
        with self.assertRaises(BaselineFailure) as stopped:
            _require_two_baselines(project, snapshot, temporary_root)
        self.assertEqual("unsupportedTestCommand", stopped.exception.code)

    def test_raw_candidate_results_match_the_exact_planned_identity_set(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _require_raw_candidate_set,
        )

        _require_raw_candidate_set(
            ("candidate-a", "candidate-b"),
            {"candidate-b": 0, "candidate-a": 1},
        )
        for reported in (
            {"candidate-a": 1},
            {"candidate-a": 1, "candidate-b": 0, "candidate-c": 1},
        ):
            with self.subTest(reported=reported):
                with self.assertRaises(MutationBackendError) as stopped:
                    _require_raw_candidate_set(
                        ("candidate-a", "candidate-b"),
                        reported,
                    )
                self.assertEqual(
                    "candidateResultSetMismatch",
                    stopped.exception.code,
                )

    def test_snapshot_omits_only_tool_owned_derived_directories(self):
        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-copy-policy-") as directory:
            root = Path(directory)
            project = root / "project"
            snapshot = root / "snapshot"
            (project / "app").mkdir(parents=True)
            (project / "app" / "subject.py").write_text("value = 1\n")
            derived = (
                ".git/hooks/helper.py",
                ".sentinel/state-v1/private.py",
                ".toolchain/venv/dependency.py",
                ".venv/lib/dependency.py",
                "venv/lib/dependency.py",
                ".tox/environment/dependency.py",
                ".nox/environment/dependency.py",
                "mutants/generated.py",
                "app/__pycache__/generated.py",
                "app/.pytest_cache/generated.py",
            )
            for relative_path in derived:
                path = project / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("secret = 1\n")

            mutation_backend._copy_project(project, snapshot)

            self.assertEqual("value = 1\n", (snapshot / "app" / "subject.py").read_text())
            for relative_path in derived:
                with self.subTest(relative_path=relative_path):
                    self.assertFalse((snapshot / relative_path).exists())

            failure = OSError("copy unavailable")
            with (
                patch.object(mutation_backend.shutil, "copytree", side_effect=failure),
                self.assertRaises(mutation_backend.MutationBackendError) as stopped,
            ):
                mutation_backend._copy_project(project, root / "failed-snapshot")
            self.assertEqual("snapshotCopyFailed", stopped.exception.code)
            self.assertIs(failure, stopped.exception.__cause__)

    def test_existing_pyproject_mutmut_section_is_rejected(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _reject_existing_mutmut_config,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-mutmut-config-") as directory:
            snapshot = Path(directory)
            (snapshot / "pyproject.toml").write_text(
                '[tool.mutmut]\nsource_paths = ["hidden"]\n',
                encoding="utf-8",
            )

            with self.assertRaises(MutationBackendError) as stopped:
                _reject_existing_mutmut_config(snapshot)

            self.assertEqual("unauthorizedBackendConfig", stopped.exception.code)

    def test_existing_setup_cfg_mutmut_section_is_rejected(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _reject_existing_mutmut_config,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-mutmut-config-") as directory:
            snapshot = Path(directory)
            (snapshot / "setup.cfg").write_text(
                "[mutmut]\nsource_paths = hidden\n",
                encoding="utf-8",
            )

            with self.assertRaises(MutationBackendError) as stopped:
                _reject_existing_mutmut_config(snapshot)

            self.assertEqual("unauthorizedBackendConfig", stopped.exception.code)

    def test_absent_backend_config_is_not_opened_or_parsed(self):
        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-no-mutmut-config-") as directory:
            snapshot = Path(directory)
            with (
                patch.object(
                    mutation_backend,
                    "_pyproject_has_mutmut",
                ) as inspect_pyproject,
                patch.object(
                    mutation_backend,
                    "_setup_has_mutmut",
                ) as inspect_setup,
            ):
                mutation_backend._reject_existing_mutmut_config(snapshot)

        inspect_pyproject.assert_not_called()
        inspect_setup.assert_not_called()

    def test_mutation_test_environment_seals_the_pristine_parent(self):
        from sentinel_py.runner.mutation_backend import _mutation_test_environment

        with tempfile.TemporaryDirectory(prefix="sentinel-mutmut-environment-") as directory:
            root = Path(directory)
            pristine = root / "project"
            pristine.mkdir()

            environment = _mutation_test_environment(root, pristine)

            self.assertEqual(
                str(pristine),
                environment["SENTINEL_MUTMUT_PRISTINE_ROOT"],
            )
            self.assertEqual(str(root / "home"), environment["HOME"])
            self.assertEqual(str(root / "observer"), environment["PYTHONPATH"])

    def test_minimal_environment_has_the_exact_sealed_contract(self):
        from sentinel_py.runner.mutation_backend import _minimal_environment

        with tempfile.TemporaryDirectory(prefix="sentinel-minimal-environment-") as directory:
            root = Path(directory)
            with patch.object(Path, "mkdir") as make_directory:
                self.assertEqual(
                    {
                        "HOME": str(root / "home"),
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONHASHSEED": "0",
                    },
                    _minimal_environment(root),
                )

            make_directory.assert_called_once_with(mode=0o700, exist_ok=True)

    def test_run_mutmut_binds_the_backend_and_protected_inventory_exactly(self):
        from sentinel_py.runner import mutation_backend

        project = SimpleNamespace(project_root=Path("/project"))
        execution = object()
        with (
            patch.object(mutation_backend, "verify_backend_lock") as verify_lock,
            patch.object(
                mutation_backend,
                "_protected_inventory",
                side_effect=(("before",), ("before",)),
            ) as protected_inventory,
            patch.object(
                mutation_backend,
                "_run_in_temporary_snapshot",
                return_value=execution,
            ) as run_snapshot,
        ):
            self.assertIs(execution, mutation_backend.run_mutmut(project))

        verify_lock.assert_called_once_with()
        self.assertEqual(
            [call(project.project_root), call(project.project_root)],
            protected_inventory.call_args_list,
        )
        run_snapshot.assert_called_once_with(project)

        with (
            patch.object(mutation_backend, "verify_backend_lock"),
            patch.object(
                mutation_backend,
                "_protected_inventory",
                side_effect=(("before",), ("after",)),
            ),
            patch.object(
                mutation_backend,
                "_run_in_temporary_snapshot",
                return_value=execution,
            ),
            self.assertRaises(mutation_backend.MutationBackendError) as stopped,
        ):
            mutation_backend.run_mutmut(project)
        self.assertEqual("protectedSourceChanged", stopped.exception.code)

        failure = RuntimeError("snapshot failed")
        with (
            patch.object(mutation_backend, "verify_backend_lock"),
            patch.object(
                mutation_backend,
                "_protected_inventory",
                side_effect=(("before",), ("before",)),
            ) as protected_inventory,
            patch.object(
                mutation_backend,
                "_run_in_temporary_snapshot",
                side_effect=failure,
            ),
            self.assertRaises(RuntimeError) as stopped,
        ):
            mutation_backend.run_mutmut(project)
        self.assertIs(failure, stopped.exception)
        self.assertEqual(
            [call(project.project_root), call(project.project_root)],
            protected_inventory.call_args_list,
        )

    def test_invalid_pyproject_is_not_ignored(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _reject_existing_mutmut_config,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-mutmut-toml-") as directory:
            snapshot = Path(directory)
            (snapshot / "pyproject.toml").write_bytes(b"[broken")

            with self.assertRaises(MutationBackendError) as stopped:
                _reject_existing_mutmut_config(snapshot)

            self.assertEqual("projectMetadataInvalid", stopped.exception.code)

    def test_run_process_preserves_the_exact_default_timeout_and_completion(self):
        from sentinel_py.runner import mutation_backend

        command = ("python", "-m", "subject")
        cwd = Path("/project")
        environment = {"SAFE": "value"}
        process = Mock()
        process.communicate.return_value = (b"stdout", b"stderr")
        completed = object()
        with (
            patch.object(
                mutation_backend,
                "_start_process",
                return_value=process,
            ) as start_process,
            patch.object(
                mutation_backend,
                "_completed_process",
                return_value=completed,
            ) as complete_process,
        ):
            self.assertIs(
                completed,
                mutation_backend._run_process(command, cwd, environment),
            )

        start_process.assert_called_once_with(command, cwd, environment)
        process.communicate.assert_called_once_with(timeout=300)
        complete_process.assert_called_once_with(
            command,
            process,
            b"stdout",
            b"stderr",
        )

    def test_progress_runner_preserves_every_process_input_and_output(self):
        from sentinel_py.runner import mutation_backend

        command = ("python", "-m", "backend")
        cwd = Path("/project")
        environment = {"SAFE": "value"}
        reports = Path("/reports")
        process = Mock()
        completed = object()
        with (
            patch.object(
                mutation_backend,
                "_start_process",
                return_value=process,
            ) as start_process,
            patch.object(
                mutation_backend,
                "_wait_for_progress",
                return_value=(b"stdout", b"stderr"),
            ) as wait_for_progress,
            patch.object(
                mutation_backend,
                "_completed_process",
                return_value=completed,
            ) as complete_process,
        ):
            self.assertIs(
                completed,
                mutation_backend._run_process_with_progress(
                    command,
                    cwd,
                    environment,
                    reports,
                    startup_timeout_seconds=11,
                    idle_timeout_seconds=12,
                    absolute_timeout_seconds=13,
                    poll_seconds=0.25,
                ),
            )

        start_process.assert_called_once_with(command, cwd, environment)
        wait_for_progress.assert_called_once_with(
            process,
            reports,
            11,
            12,
            13,
            0.25,
        )
        complete_process.assert_called_once_with(
            command,
            process,
            b"stdout",
            b"stderr",
        )

    def test_process_start_failure_preserves_the_exact_code_and_cause(self):
        from sentinel_py.runner import mutation_backend

        failure = OSError("cannot start")
        with (
            patch.object(
                mutation_backend.subprocess,
                "Popen",
                side_effect=failure,
            ),
            self.assertRaises(mutation_backend.MutationBackendError) as stopped,
        ):
            mutation_backend._start_process(
                ("python", "-m", "subject"),
                Path("/project"),
                {"SAFE": "value"},
            )

        self.assertEqual("childProcessFailed", stopped.exception.code)
        self.assertIs(failure, stopped.exception.__cause__)

    def test_completed_process_and_group_probe_preserve_the_exact_contract(self):
        from sentinel_py.runner import mutation_backend

        command = ("python", "-m", "subject")
        process = Mock(pid=73, returncode=17)
        with patch.object(
            mutation_backend,
            "_process_group_exists",
            return_value=False,
        ) as group_exists:
            completed = mutation_backend._completed_process(
                command,
                process,
                b"stdout",
                b"stderr",
            )

        group_exists.assert_called_once_with(73)
        self.assertEqual(command, completed.args)
        self.assertEqual(17, completed.returncode)
        self.assertEqual(b"stdout", completed.stdout)
        self.assertEqual(b"stderr", completed.stderr)

        with patch.object(mutation_backend.os, "killpg") as kill_group:
            self.assertTrue(mutation_backend._process_group_exists(73))
        kill_group.assert_called_once_with(73, 0)

        for failure, expected in (
            (ProcessLookupError(), False),
            (PermissionError(), True),
        ):
            with self.subTest(failure=type(failure).__name__), patch.object(
                mutation_backend.os,
                "killpg",
                side_effect=failure,
            ):
                self.assertIs(
                    expected,
                    mutation_backend._process_group_exists(73),
                )

    def test_progress_timeout_classifies_the_exact_absolute_boundary(self):
        from sentinel_py.runner import mutation_backend

        for now, absolute_deadline, expected_code in (
            (99.0, 100.0, "backendProcessStalled"),
            (100.0, 100.0, "backendProcessTimedOut"),
            (101.0, 100.0, "backendProcessTimedOut"),
        ):
            with (
                self.subTest(now=now, absolute_deadline=absolute_deadline),
                self.assertRaises(mutation_backend.MutationBackendError) as stopped,
            ):
                mutation_backend._raise_progress_timeout(now, absolute_deadline)
            self.assertEqual(expected_code, stopped.exception.code)

    def test_progress_wait_uses_startup_limit_until_the_fingerprint_changes(self):
        import subprocess

        from sentinel_py.runner import mutation_backend

        fingerprint = (("existing.json", 1, 2, 3),)
        process = Mock()
        process.communicate.side_effect = (
            subprocess.TimeoutExpired(("backend",), 0.1),
            (b"stdout", b"stderr"),
        )
        with (
            patch.object(
                mutation_backend.time,
                "monotonic",
                side_effect=(0.0, 0.0, 0.02, 0.1),
            ),
            patch.object(
                mutation_backend,
                "_progress_fingerprint",
                side_effect=(fingerprint, fingerprint),
            ) as progress_fingerprint,
        ):
            result = mutation_backend._wait_for_progress(
                process,
                Path("/reports"),
                startup_timeout_seconds=5.0,
                idle_timeout_seconds=0.05,
                absolute_timeout_seconds=100.0,
                poll_seconds=0.1,
            )

        self.assertEqual((b"stdout", b"stderr"), result)
        self.assertEqual(2, progress_fingerprint.call_count)
        self.assertEqual(
            [call(timeout=0.1), call(timeout=0.1)],
            process.communicate.call_args_list,
        )

    def test_progress_wait_switches_to_idle_without_recounting_same_fingerprint(self):
        import subprocess

        from sentinel_py.runner import mutation_backend

        initial = (("initial.json", 1, 2, 3),)
        changed = (("changed.json", 1, 2, 4),)
        process = Mock()
        process.communicate.side_effect = (
            subprocess.TimeoutExpired(("backend",), 0.1),
            subprocess.TimeoutExpired(("backend",), 0.1),
        )
        with (
            patch.object(
                mutation_backend.time,
                "monotonic",
                side_effect=(0.0, 0.0, 1.0, 1.2, 1.5, 1.6, 1.7),
            ),
            patch.object(
                mutation_backend,
                "_progress_fingerprint",
                side_effect=(initial, changed, changed, changed),
            ),
            self.assertRaises(mutation_backend.MutationBackendError) as stopped,
        ):
            mutation_backend._wait_for_progress(
                process,
                Path("/reports"),
                startup_timeout_seconds=5.0,
                idle_timeout_seconds=0.5,
                absolute_timeout_seconds=100.0,
                poll_seconds=0.1,
            )

        self.assertEqual("backendProcessStalled", stopped.exception.code)
        self.assertEqual(2, process.communicate.call_count)

    def test_progress_wait_passes_only_the_remaining_deadline_to_the_child(self):
        from sentinel_py.runner import mutation_backend

        process = Mock()
        process.communicate.return_value = (b"stdout", b"stderr")
        with (
            patch.object(
                mutation_backend.time,
                "monotonic",
                side_effect=(10.0, 11.0),
            ),
            patch.object(
                mutation_backend,
                "_progress_fingerprint",
                return_value=(),
            ),
        ):
            result = mutation_backend._wait_for_progress(
                process,
                Path("/reports"),
                startup_timeout_seconds=5.0,
                idle_timeout_seconds=2.0,
                absolute_timeout_seconds=100.0,
                poll_seconds=10.0,
            )

        self.assertEqual((b"stdout", b"stderr"), result)
        process.communicate.assert_called_once_with(timeout=4.0)

    def test_progress_fingerprint_is_exactly_sorted_and_reports_inventory_errors(self):
        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-progress-fingerprint-") as directory:
            root = Path(directory)
            for name, payload in (("é.json", b"longer"), ("z.json", b"z")):
                (root / name).write_bytes(payload)

            expected = tuple(
                (
                    path.name,
                    path.lstat().st_mode,
                    path.lstat().st_size,
                    path.lstat().st_mtime_ns,
                )
                for path in (root / "z.json", root / "é.json")
            )
            self.assertEqual(expected, mutation_backend._progress_fingerprint(root))

        failure = OSError("inventory unavailable")
        with (
            patch.object(Path, "iterdir", side_effect=failure),
            self.assertRaises(mutation_backend.MutationBackendError) as stopped,
        ):
            mutation_backend._progress_fingerprint(Path("/reports"))
        self.assertEqual("backendProgressUnavailable", stopped.exception.code)
        self.assertIs(failure, stopped.exception.__cause__)

    def test_timeout_terminates_the_child_process_group(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _minimal_environment,
            _run_process,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-process-group-") as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            child_code = (
                "import os,signal,sys,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            parent_code = (
                "import os,subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r},sys.argv[1]]);"
                "deadline=time.monotonic()+5;"
                "\nwhile not os.path.exists(sys.argv[1]) and time.monotonic()<deadline:"
                " time.sleep(0.01)"
                "\ntime.sleep(60)"
            )

            with self.assertRaises(MutationBackendError) as stopped:
                _run_process(
                    (sys.executable, "-c", parent_code, str(child_pid_path)),
                    root,
                    _minimal_environment(root),
                    timeout_seconds=3,
                )

            self.assertEqual("childProcessTimedOut", stopped.exception.code)
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while _process_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(_process_alive(child_pid))

    def test_child_processes_cannot_create_core_dumps(self):
        from sentinel_py.runner.mutation_backend import (
            _minimal_environment,
            _run_process,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-core-limit-") as directory:
            root = Path(directory)
            child_code = (
                "import resource;"
                "soft,hard=resource.getrlimit(resource.RLIMIT_CORE);"
                "print(f'{soft}:{hard}')"
            )

            result = _run_process(
                (sys.executable, "-c", child_code),
                root,
                _minimal_environment(root),
            )

            self.assertEqual(0, result.returncode)
            self.assertEqual(b"0:0\n", result.stdout)

    def test_core_dump_preexec_sets_both_limits_to_zero(self):
        from sentinel_py.runner import mutation_backend

        with patch.object(mutation_backend.resource, "setrlimit") as set_limit:
            mutation_backend._disable_core_dumps()

        set_limit.assert_called_once_with(
            mutation_backend.resource.RLIMIT_CORE,
            (0, 0),
        )

    def test_successful_parent_cannot_leave_a_background_descendant(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _minimal_environment,
            _run_process,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-process-drain-") as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            child_code = (
                "import os,signal,sys,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            parent_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r},sys.argv[1]],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "time.sleep(0.2)"
            )
            child_pid = None
            try:
                with self.assertRaises(MutationBackendError) as stopped:
                    _run_process(
                        (sys.executable, "-c", parent_code, str(child_pid_path)),
                        root,
                        _minimal_environment(root),
                        timeout_seconds=5,
                    )
                self.assertEqual("childProcessTreeNotDrained", stopped.exception.code)
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 3
                while _process_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(_process_alive(child_pid))
            finally:
                if child_pid is None and child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                if child_pid is not None and _process_alive(child_pid):
                    os.kill(child_pid, 9)

    def test_backend_progress_extends_only_the_idle_deadline(self):
        from sentinel_py.runner.mutation_backend import (
            _minimal_environment,
            _run_process_with_progress,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-progress-ok-") as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            child_code = (
                "from pathlib import Path;import sys,time;"
                "root=Path(sys.argv[1]);"
                "[((root/(str(index)+'.json')).write_text(str(index)),time.sleep(0.25)) "
                "for index in range(12)]"
            )

            result = _run_process_with_progress(
                (sys.executable, "-c", child_code, str(reports)),
                root,
                _minimal_environment(root),
                reports,
                startup_timeout_seconds=2,
                idle_timeout_seconds=1,
                absolute_timeout_seconds=8,
                poll_seconds=0.05,
            )

            self.assertEqual(0, result.returncode)
            self.assertEqual(12, len(tuple(reports.iterdir())))

    def test_backend_without_new_progress_is_stopped_with_its_process_group(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _minimal_environment,
            _run_process_with_progress,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-progress-stall-") as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            child_code = "import time;time.sleep(60)"

            with self.assertRaises(MutationBackendError) as stopped:
                _run_process_with_progress(
                    (sys.executable, "-c", child_code),
                    root,
                    _minimal_environment(root),
                    reports,
                    startup_timeout_seconds=0.3,
                    idle_timeout_seconds=0.3,
                    absolute_timeout_seconds=2,
                    poll_seconds=0.02,
                )

            self.assertEqual("backendProcessStalled", stopped.exception.code)

    def test_backend_absolute_deadline_stops_continuous_fake_progress(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _minimal_environment,
            _run_process_with_progress,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-progress-limit-") as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            child_code = (
                "from pathlib import Path;import sys,time;"
                "root=Path(sys.argv[1]);index=0;"
                "\nwhile True:"
                " (root/(str(index)+'.json')).write_text(str(index));"
                " index+=1;time.sleep(0.02)"
            )

            with self.assertRaises(MutationBackendError) as stopped:
                _run_process_with_progress(
                    (sys.executable, "-c", child_code, str(reports)),
                    root,
                    _minimal_environment(root),
                    reports,
                    startup_timeout_seconds=2,
                    idle_timeout_seconds=1,
                    absolute_timeout_seconds=0.7,
                    poll_seconds=0.01,
                )

            self.assertEqual("backendProcessTimedOut", stopped.exception.code)

    def test_interrupt_always_terminates_the_backend_process_group(self):
        from sentinel_py.runner.mutation_backend import _run_process_with_progress

        process = Mock()
        with (
            patch(
                "sentinel_py.runner.mutation_backend._start_process",
                return_value=process,
            ),
            patch(
                "sentinel_py.runner.mutation_backend._wait_for_progress",
                side_effect=KeyboardInterrupt,
            ),
            patch(
                "sentinel_py.runner.mutation_backend._terminate_process_group",
            ) as terminate,
        ):
            with self.assertRaises(KeyboardInterrupt):
                _run_process_with_progress(
                    ("backend",),
                    Path("project"),
                    {},
                    Path("reports"),
                    startup_timeout_seconds=1,
                    idle_timeout_seconds=2,
                    absolute_timeout_seconds=3,
                    poll_seconds=0.1,
                )

        terminate.assert_called_once_with(process)

    def test_process_group_termination_uses_one_bounded_grace_period(self):
        import signal
        import subprocess

        from sentinel_py.runner import mutation_backend

        process = Mock(pid=41)
        process.communicate.return_value = (b"stdout", b"stderr")
        with (
            patch.object(
                mutation_backend,
                "_signal_process_group",
            ) as send_signal,
            patch.object(
                mutation_backend,
                "_process_group_exists",
                side_effect=(True, True, True),
            ) as group_exists,
            patch.object(
                mutation_backend.time,
                "monotonic",
                side_effect=(10.0, 10.1, 10.3),
            ) as monotonic,
            patch.object(mutation_backend.time, "sleep") as sleep,
        ):
            mutation_backend._terminate_process_group(process)

        self.assertEqual(
            [call(41, signal.SIGTERM), call(41, signal.SIGKILL)],
            send_signal.call_args_list,
        )
        self.assertEqual([call(41), call(41), call(41)], group_exists.call_args_list)
        self.assertEqual(3, monotonic.call_count)
        sleep.assert_called_once_with(0.01)
        process.communicate.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

        process = Mock(pid=42)
        timeout = subprocess.TimeoutExpired(("backend",), 5)
        process.communicate.side_effect = (timeout, (b"", b""))
        with (
            patch.object(mutation_backend, "_signal_process_group") as send_signal,
            patch.object(
                mutation_backend,
                "_process_group_exists",
                side_effect=(False, False),
            ),
            patch.object(mutation_backend.time, "monotonic", return_value=20.0),
        ):
            mutation_backend._terminate_process_group(process)

        send_signal.assert_called_once_with(42, signal.SIGTERM)
        self.assertEqual([call(timeout=5), call()], process.communicate.call_args_list)
        process.kill.assert_called_once_with()

        process = Mock(pid=43)
        process.communicate.return_value = (b"", b"")
        with (
            patch.object(mutation_backend, "_signal_process_group") as send_signal,
            patch.object(
                mutation_backend,
                "_process_group_exists",
                side_effect=(True, False, False),
            ) as group_exists,
            patch.object(
                mutation_backend.time,
                "monotonic",
                side_effect=(10.0, 10.25),
            ) as monotonic,
            patch.object(mutation_backend.time, "sleep") as sleep,
        ):
            mutation_backend._terminate_process_group(process)

        send_signal.assert_called_once_with(43, signal.SIGTERM)
        self.assertEqual([call(43), call(43)], group_exists.call_args_list)
        self.assertEqual(2, monotonic.call_count)
        sleep.assert_not_called()
        process.communicate.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_process_start_forces_bytecode_protection_outside_mutated_environment(self):
        import subprocess

        from sentinel_py.runner import mutation_backend

        command = ("python", "-m", "subject")
        cwd = Path("/project")
        for supplied in (
            {"CUSTOM": "value"},
            {"CUSTOM": "value", "PYTHONDONTWRITEBYTECODE": ""},
        ):
            with self.subTest(supplied=supplied):
                original = dict(supplied)
                process = object()
                with patch.object(
                    mutation_backend.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen:
                    self.assertIs(
                        process,
                        mutation_backend._start_process(command, cwd, supplied),
                    )

                popen.assert_called_once_with(
                    command,
                    cwd=cwd,
                    env={
                        "CUSTOM": "value",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    preexec_fn=mutation_backend._disable_core_dumps,
                )
                self.assertEqual(original, supplied)

    def test_protected_inventory_binds_every_file_attribute_and_rejects_links(self):
        import hashlib

        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-protected-files-") as directory:
            root = Path(directory)
            first = root / "z.txt"
            second = root / "nested" / "a.txt"
            ignored = root / ".sentinel" / "private.json"
            second.parent.mkdir()
            ignored.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            ignored.write_bytes(b"ignored")

            inventory = mutation_backend._protected_inventory(root)
            self.assertEqual(("nested/a.txt", "z.txt"), tuple(item.relative_path for item in inventory))
            by_path = {item.relative_path: item for item in inventory}
            for path, relative, payload in (
                (first, "z.txt", b"first"),
                (second, "nested/a.txt", b"second"),
            ):
                with self.subTest(relative=relative):
                    metadata = path.stat()
                    self.assertEqual(
                        mutation_backend._ProtectedFile(
                            relative_path=relative,
                            device=metadata.st_dev,
                            inode=metadata.st_ino,
                            mode=metadata.st_mode,
                            size=metadata.st_size,
                            modified_ns=metadata.st_mtime_ns,
                            changed_ns=metadata.st_ctime_ns,
                            digest=hashlib.sha256(payload).hexdigest(),
                        ),
                        by_path[relative],
                    )

            linked_root = root / "linked-project"
            linked_root.mkdir()
            (linked_root / "target.txt").write_text("target", encoding="utf-8")
            (linked_root / "link.txt").symlink_to(linked_root / "target.txt")
            with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                mutation_backend._protected_inventory(linked_root)
            self.assertEqual("protectedPathSymlink", stopped.exception.code)

            failure = OSError("unavailable")
            with patch.object(Path, "stat", side_effect=failure):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._protected_file(first, "z.txt")
            self.assertEqual("protectedSourceUnavailable", stopped.exception.code)
            self.assertIs(failure, stopped.exception.__cause__)


class PytestObservationTests(unittest.TestCase):
    def test_observation_value_helpers_preserve_exact_types_and_ordering(self):
        from sentinel_py.runner import mutation_backend

        self.assertEqual(
            ("z", "a", "é"),
            mutation_backend._text_array(["z", "a", "é"]),
        )
        for invalid in (
            ("a",),
            [1],
            ["a", "a"],
        ):
            with self.subTest(text_array=invalid):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._text_array(invalid)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

        for phase in ("setup", "call", "teardown"):
            with self.subTest(phase=phase):
                self.assertEqual(phase, mutation_backend._failure_phase(phase))
        for invalid in (None, "", "collect", "Setup"):
            with self.subTest(invalid_phase=invalid):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._failure_phase(invalid)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

        for value, minimum in ((-1, -1), (0, -1), (1, 1), (7, 1)):
            with self.subTest(integer=value, minimum=minimum):
                self.assertEqual(
                    value,
                    mutation_backend._location_integer(value, minimum),
                )
        for value, minimum in ((True, -1), (1.0, -1), (-2, -1), (0, 1)):
            with self.subTest(invalid_integer=value, minimum=minimum):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._location_integer(value, minimum)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

        base = {
            "collected": ["tests/test_a.py", "tests/test_b.py"],
            "started": ["tests/test_a.py"],
            "failures": [{"id": "first"}, {"id": "second"}],
            "exitCode": 7,
        }
        key = bytes(range(32))
        failure_values = (
            (
                ("signature-a", True, "tests/test_a.py", "setup"),
                ("signature-b", True, "tests/test_a.py", "call"),
            ),
            (
                ("signature-a", True, "tests/test_a.py", "setup"),
                ("signature-b", False, "tests/test_a.py", "call"),
            ),
        )
        for values in failure_values:
            with self.subTest(values=values):
                with patch.object(
                    mutation_backend,
                    "_failure_value",
                    side_effect=values,
                ) as failure_value:
                    observation = mutation_backend._observation_values(base, key)
                self.assertEqual(
                    mutation_backend._PytestObservation(
                        ("tests/test_a.py", "tests/test_b.py"),
                        ("tests/test_a.py",),
                        7,
                        ("signature-a", "signature-b"),
                        all(item[1] for item in values),
                    ),
                    observation,
                )
                self.assertEqual(
                    [call({"id": "first"}, key), call({"id": "second"}, key)],
                    failure_value.call_args_list,
                )

        empty = mutation_backend._observation_values(
            {**base, "failures": [], "exitCode": 0},
            key,
        )
        self.assertFalse(empty.assertion_only)
        self.assertEqual((), empty.failure_signatures)
        for name, value in (
            ("failures", ()),
            ("exitCode", True),
            ("exitCode", "1"),
        ):
            with self.subTest(field=name, value=value):
                with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
                    mutation_backend._observation_values({**base, name: value}, key)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_observation_document_and_exit_type_preserve_exact_error_contracts(self):
        from sentinel_py.runner import mutation_backend

        required_fields = [
            "collected",
            "exitCode",
            "failures",
            "nonce",
            "reportSignature",
            "schemaVersion",
            "started",
        ]
        with tempfile.TemporaryDirectory(prefix="sentinel-observation-document-") as directory:
            root = Path(directory)
            for name, payload in (
                ("missing.json", None),
                ("malformed.json", b"{"),
                ("invalid-utf8.json", b"\xff"),
            ):
                path = root / name
                if payload is not None:
                    path.write_bytes(payload)
                with self.subTest(name=name):
                    with self.assertRaises(
                        mutation_backend.MutationBackendError
                    ) as stopped:
                        mutation_backend._read_observation_document(path)
                    self.assertEqual(
                        "pytestObservationMissing",
                        stopped.exception.code,
                    )
                    self.assertIsNotNone(stopped.exception.__cause__)

            non_object = root / "non-object.json"
            non_object.write_text(json.dumps(required_fields), encoding="utf-8")
            with self.assertRaises(
                mutation_backend.MutationBackendError
            ) as stopped:
                mutation_backend._read_observation_document(non_object)
            self.assertEqual("pytestObservationInvalid", stopped.exception.code)

        invalid_exit = {
            "collected": [],
            "started": [],
            "failures": [],
            "exitCode": True,
        }
        with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
            mutation_backend._observation_values(invalid_exit, bytes(range(32)))
        self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_failure_field_helpers_reject_every_ambiguous_boundary(self):
        from sentinel_py.runner import mutation_backend

        self.assertEqual("value", mutation_backend._failure_text("value"))
        for invalid in ("", None, 1):
            with self.subTest(invalid_text=invalid):
                with self.assertRaises(
                    mutation_backend.MutationBackendError
                ) as stopped:
                    mutation_backend._failure_text(invalid)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

        prefix = "hmac-sha256:"
        self.assertTrue(mutation_backend._canonical_hmac_signature(prefix + "0" * 64))
        for invalid in (
            prefix + "0" * 63,
            prefix + "g" * 64,
            prefix + "X" * 64,
        ):
            with self.subTest(invalid_signature=invalid[-4:]):
                self.assertFalse(mutation_backend._canonical_hmac_signature(invalid))

        boundary = {"column": -1, "line": 1, "path": "tests/test_subject.py"}
        self.assertEqual(boundary, mutation_backend._failure_location(boundary))
        with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
            mutation_backend._failure_location({**boundary, "column": -2})
        self.assertEqual("pytestObservationInvalid", stopped.exception.code)
        for invalid_record in (
            ["column", "line", "path"],
            {"column": 0, "line": 1},
        ):
            with self.subTest(invalid_record=invalid_record):
                with self.assertRaises(
                    mutation_backend.MutationBackendError
                ) as stopped:
                    mutation_backend._location_record(invalid_record)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

        with self.assertRaises(mutation_backend.MutationBackendError) as stopped:
            mutation_backend._location_path(1)
        self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_failure_location_path_accepts_only_raw_canonical_relative_parts(self):
        from sentinel_py.runner import mutation_backend

        for valid in ("a.py", "tests/test_subject.py", "XXXX/file.py"):
            with self.subTest(valid=valid):
                self.assertTrue(mutation_backend._canonical_relative_path(valid))
                self.assertEqual(valid, mutation_backend._location_path(valid))

        for invalid in (
            "",
            ".",
            "..",
            "/absolute.py",
            "./relative.py",
            "tests/./test.py",
            "tests/../test.py",
            "tests//test.py",
            "tests/test.py/",
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(mutation_backend._canonical_relative_path(invalid))
                with self.assertRaises(
                    mutation_backend.MutationBackendError
                ) as stopped:
                    mutation_backend._location_path(invalid)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_node_path_parts_accept_only_safe_relative_test_paths(self):
        from sentinel_py.runner import pytest_reporter

        self.assertEqual(
            ("tests", "nested", "test_subject.py"),
            pytest_reporter._node_path_parts(
                "tests/nested/test_subject.py::SubjectTests::test_answer"
            ),
        )
        for node_id in (
            "",
            "/tests/test_subject.py::test_answer",
            "../test.py",
            "tests/../test.py",
            "./tests/test_subject.py",
            "tests//test_subject.py",
        ):
            with self.subTest(node_id=node_id):
                with self.assertRaises(RuntimeError) as stopped:
                    pytest_reporter._node_path_parts(node_id)
                self.assertEqual(
                    ("sentinelPytestFailureLocationInvalid",),
                    stopped.exception.args,
                )

    def test_reporter_item_path_requires_existing_file_and_exact_suffix(self):
        from sentinel_py.runner import pytest_reporter

        missing = SimpleNamespace(
            path=Path("/sentinel-missing-root/tests/test_subject.py")
        )
        with self.assertRaises(RuntimeError) as stopped:
            pytest_reporter._resolved_item_path(missing)
        self.assertEqual(
            ("sentinelPytestFailureLocationInvalid",),
            stopped.exception.args,
        )
        self.assertIsInstance(stopped.exception.__cause__, OSError)

        pytest_reporter._require_item_suffix(
            Path("tests/test_subject.py"),
            ("tests", "test_subject.py"),
        )
        invalid = (
            (Path("test_subject.py"), ("tests", "test_subject.py")),
            (Path("project/tests/other.py"), ("tests", "test_subject.py")),
        )
        for item_path, parts in invalid:
            with self.subTest(item_path=item_path, parts=parts):
                with self.assertRaises(RuntimeError) as stopped:
                    pytest_reporter._require_item_suffix(item_path, parts)

                self.assertEqual(
                    ("sentinelPytestFailureLocationInvalid",),
                    stopped.exception.args,
                )

    def test_hmac_key_requires_canonical_lowercase_256_bit_hex(self):
        from sentinel_py.runner import pytest_reporter

        key = bytes(range(32))
        self.assertEqual(
            key,
            pytest_reporter._hmac_key(
                {pytest_reporter._HMAC_KEY_ENVIRONMENT: key.hex()}
            ),
        )
        for value in (None, "", "0" * 62, "AA" * 32, "not-hex"):
            environment = {}
            if value is not None:
                environment[pytest_reporter._HMAC_KEY_ENVIRONMENT] = value
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError) as stopped:
                    pytest_reporter._hmac_key(environment)
                self.assertEqual(
                    ("sentinelPytestHmacKeyInvalid",),
                    stopped.exception.args,
                )

    def test_hmac_signatures_are_exact_and_reject_wrong_key_types(self):
        import hashlib
        import hmac

        from sentinel_py.runner import pytest_reporter

        key = bytes(range(32))
        location = {"path": "tests/test_한글.py", "line": 9, "column": 2}
        failure_payload = json.dumps(
            {
                "assertion": True,
                "exceptionType": "builtins.AssertionError",
                "location": location,
                "nodeId": "tests/test_한글.py::test_값",
                "phase": "call",
                "signatureVersion": "sentinel-pytest-failure-v1",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.assertEqual(
            "hmac-sha256:"
            + hmac.new(key, failure_payload, hashlib.sha256).hexdigest(),
            pytest_reporter._failure_signature(
                key,
                True,
                "builtins.AssertionError",
                "tests/test_한글.py::test_값",
                "call",
                location,
            ),
        )

        document = {"z": "한글", "a": [2, 1]}
        report_payload = json.dumps(
            {
                "observation": document,
                "signatureVersion": "sentinel-pytest-report-v1",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.assertEqual(
            "hmac-sha256:"
            + hmac.new(key, report_payload, hashlib.sha256).hexdigest(),
            pytest_reporter._report_signature(key, document),
        )

        for invalid_key in (b"short", bytearray(32)):
            with self.subTest(key_type=type(invalid_key).__name__):
                with self.assertRaises(ValueError) as failure_error:
                    pytest_reporter._failure_signature(
                        invalid_key,
                        True,
                        "builtins.AssertionError",
                        "tests/test_subject.py::test_answer",
                        "call",
                        {"path": "tests/test_subject.py", "line": 1, "column": 0},
                    )
                self.assertEqual(
                    "assertion HMAC key must contain exactly 256 bits",
                    str(failure_error.exception),
                )
                with self.assertRaises(ValueError) as report_error:
                    pytest_reporter._report_signature(invalid_key, {})
                self.assertEqual(
                    "report HMAC key must contain exactly 256 bits",
                    str(report_error.exception),
                )

    def test_session_finish_writes_one_exact_canonical_signed_document(self):
        from sentinel_py.runner import pytest_reporter

        key = bytes(range(32))
        output = "/private/report.json"
        contract = (output, "run-nonce", key)
        first = {
            "signature": "hmac-sha256:" + "1" * 64,
            "phase": "call",
            "nodeId": "tests/test_z.py::test_z",
            "location": {"path": "tests/test_z.py", "line": 7, "column": 3},
            "exceptionType": "builtins.AssertionError",
            "assertion": True,
        }
        second = {
            "signature": "hmac-sha256:" + "2" * 64,
            "phase": "call",
            "nodeId": "tests/test_a.py::test_a",
            "location": {"path": "tests/test_a.py", "line": 5, "column": 1},
            "exceptionType": "builtins.AssertionError",
            "assertion": True,
        }
        state = pytest_reporter._ObservationState(
            collected=[first["nodeId"], second["nodeId"]],
            started=[first["nodeId"], second["nodeId"]],
            failures=[first, second],
        )
        signed_documents = []

        def sign_document(actual_key, document):
            self.assertEqual(key, actual_key)
            signed_documents.append(json.loads(json.dumps(document)))
            return "hmac-sha256:" + "f" * 64

        pytest_reporter._states[contract] = state
        try:
            with (
                patch.object(
                    pytest_reporter,
                    "_report_contract",
                    return_value=contract,
                ),
                patch.object(
                    pytest_reporter,
                    "_report_signature",
                    side_effect=sign_document,
                ) as sign,
                patch.object(Path, "write_text", autospec=True) as write_text,
            ):
                pytest_reporter.pytest_sessionfinish(object(), 7)
        finally:
            pytest_reporter._states.pop(contract, None)

        unsigned = {
            "collected": [first["nodeId"], second["nodeId"]],
            "exitCode": 7,
            "failures": [second, first],
            "nonce": "run-nonce",
            "schemaVersion": "sentinel-pytest-observation-v3",
            "started": [first["nodeId"], second["nodeId"]],
        }
        self.assertEqual(1, sign.call_count)
        self.assertEqual([unsigned], signed_documents)
        signed = {**unsigned, "reportSignature": "hmac-sha256:" + "f" * 64}
        write_text.assert_called_once_with(
            Path(output),
            json.dumps(signed, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertNotIn(contract, pytest_reporter._states)

        with (
            patch.object(pytest_reporter, "_report_contract", return_value=contract),
            patch.object(Path, "write_text", autospec=True) as missing_write,
        ):
            self.assertIsNone(pytest_reporter.pytest_sessionfinish(None, 0))
        missing_write.assert_not_called()

    def test_mutmut_contract_requires_paired_environment_and_exact_mutant_identity(self):
        from sentinel_py.runner import pytest_reporter

        key = bytes(range(32))
        root = Path("/private/reports")
        nonce = "run-nonce"
        mutant = "pkg.subject.x_answer__mutmut_1"
        complete = {
            "MUTANT_UNDER_TEST": mutant,
            "SENTINEL_MUTMUT_REPORT_ROOT": str(root),
            "SENTINEL_MUTMUT_RUN_NONCE": nonce,
            "SENTINEL_PYTEST_HMAC_KEY": key.hex(),
        }
        self.assertEqual(
            (
                str(pytest_reporter.mutmut_report_path(root, mutant)),
                pytest_reporter.mutmut_report_nonce(nonce, mutant),
                key,
            ),
            pytest_reporter._mutmut_report_contract(complete),
        )

        for missing_or_empty in (
            {"SENTINEL_MUTMUT_REPORT_ROOT": str(root)},
            {"SENTINEL_MUTMUT_RUN_NONCE": nonce},
            {
                "SENTINEL_MUTMUT_REPORT_ROOT": "",
                "SENTINEL_MUTMUT_RUN_NONCE": nonce,
            },
            {
                "SENTINEL_MUTMUT_REPORT_ROOT": str(root),
                "SENTINEL_MUTMUT_RUN_NONCE": "",
            },
        ):
            with self.subTest(environment=missing_or_empty):
                with self.assertRaises(RuntimeError) as stopped:
                    pytest_reporter._mutmut_report_contract(missing_or_empty)
                self.assertEqual(
                    ("sentinelPytestContractInvalid",),
                    stopped.exception.args,
                )

        for control in pytest_reporter._MUTMUT_CONTROL_IDS:
            environment = {**complete, "MUTANT_UNDER_TEST": control}
            with self.subTest(control=control):
                self.assertIsNone(
                    pytest_reporter._mutmut_report_contract(environment)
                )

        without_mutant = dict(complete)
        del without_mutant["MUTANT_UNDER_TEST"]
        self.assertIsNone(pytest_reporter._mutmut_report_contract(without_mutant))

    def test_failure_location_rejects_traceback_reader_errors_and_missing_frames(self):
        from sentinel_py.runner import pytest_reporter

        item = SimpleNamespace(
            nodeid="tests/test_mutation_runner.py::test_location",
            path=Path(__file__),
        )
        for failure in (
            AttributeError("traceback"),
            OSError("traceback"),
            RuntimeError("traceback"),
            TypeError("traceback"),
            ValueError("traceback"),
        ):
            with self.subTest(failure=type(failure).__name__), patch.object(
                pytest_reporter.traceback,
                "extract_tb",
                side_effect=failure,
            ):
                with self.assertRaises(RuntimeError) as stopped:
                    pytest_reporter._failure_location(item, RuntimeError("failure"))
                self.assertEqual(
                    ("sentinelPytestFailureLocationInvalid",),
                    stopped.exception.args,
                )
                self.assertIs(failure, stopped.exception.__cause__)

        missing = Path(__file__).with_name("sentinel-frame-does-not-exist.py")
        self.assertFalse(missing.exists())
        frame = SimpleNamespace(filename=str(missing), lineno=17, colno=3)
        with patch.object(pytest_reporter.traceback, "extract_tb", return_value=[frame]):
            with self.assertRaises(RuntimeError) as stopped:
                pytest_reporter._failure_location(item, RuntimeError("failure"))
        self.assertEqual(
            ("sentinelPytestFailureLocationInvalid",),
            stopped.exception.args,
        )

        valid_frame = SimpleNamespace(
            filename=str(Path(__file__).resolve()),
            lineno=23,
            colno=7,
        )
        with patch.object(
            pytest_reporter.traceback,
            "extract_tb",
            return_value=[valid_frame, frame],
        ):
            self.assertEqual(
                {
                    "column": 7,
                    "line": 23,
                    "path": "tests/test_mutation_runner.py",
                },
                pytest_reporter._failure_location(item, RuntimeError("failure")),
            )

        unknown_column = SimpleNamespace(
            filename=str(Path(__file__).resolve()),
            lineno=29,
            colno=None,
        )
        with patch.object(
            pytest_reporter.traceback,
            "extract_tb",
            return_value=[unknown_column],
        ):
            self.assertEqual(
                {
                    "column": -1,
                    "line": 29,
                    "path": "tests/test_mutation_runner.py",
                },
                pytest_reporter._failure_location(item, RuntimeError("failure")),
            )

    def test_reporter_hook_binds_exact_failure_fields_to_the_signature(self):
        from sentinel_py.runner import pytest_reporter

        key = bytes(range(32))
        contract = ("/private/report.json", "nonce", key)
        state = pytest_reporter._ObservationState()
        item = SimpleNamespace(nodeid="tests/test_subject.py::test_answer")
        failure = AssertionError("private")
        call_info = SimpleNamespace(
            excinfo=SimpleNamespace(type=AssertionError, value=failure),
            when="call",
        )
        location = {"column": 4, "line": 12, "path": "tests/test_subject.py"}
        pytest_reporter._states[contract] = state
        try:
            with (
                patch.object(
                    pytest_reporter,
                    "_report_contract",
                    return_value=contract,
                ),
                patch.object(
                    pytest_reporter,
                    "_failure_location",
                    return_value=location,
                ) as locate,
                patch.object(
                    pytest_reporter,
                    "_failure_signature",
                    return_value="hmac-sha256:" + "1" * 64,
                ) as sign,
            ):
                pytest_reporter.pytest_runtest_makereport(item, call_info)
        finally:
            pytest_reporter._states.pop(contract, None)

        locate.assert_called_once_with(item, failure)
        sign.assert_called_once_with(
            key,
            True,
            "builtins.AssertionError",
            item.nodeid,
            "call",
            location,
        )
        self.assertEqual(
            [
                {
                    "assertion": True,
                    "exceptionType": "builtins.AssertionError",
                    "location": location,
                    "nodeId": item.nodeid,
                    "phase": "call",
                    "signature": "hmac-sha256:" + "1" * 64,
                }
            ],
            state.failures,
        )

    def test_reporter_hmac_distinguishes_assertion_locations_without_raw_values(self):
        from sentinel_py.runner import pytest_reporter

        with tempfile.TemporaryDirectory(prefix="sentinel-pytest-hmac-") as directory:
            output = Path(directory) / "report.json"
            key = bytes(range(32))
            item = SimpleNamespace(
                nodeid=(
                    "tests/test_mutation_runner.py::PytestObservationTests::"
                    "test_reporter_hmac_distinguishes_assertion_locations_without_raw_values"
                ),
                path=Path(__file__),
            )
            try:
                assert "first-expected-private" == "first-actual-private"
            except AssertionError as error:
                first_call = SimpleNamespace(
                    excinfo=SimpleNamespace(type=AssertionError, value=error),
                    when="call",
                )
            try:
                assert "second-expected-private" == "second-actual-private"
            except AssertionError as error:
                second_call = SimpleNamespace(
                    excinfo=SimpleNamespace(type=AssertionError, value=error),
                    when="call",
                )

            with patch.dict(
                os.environ,
                {
                    "SENTINEL_PYTEST_HMAC_KEY": key.hex(),
                    "SENTINEL_PYTEST_NONCE": "report-nonce",
                    "SENTINEL_PYTEST_REPORT": str(output),
                },
                clear=True,
            ):
                pytest_reporter.pytest_configure(None)
                pytest_reporter.pytest_runtest_makereport(item, first_call)
                pytest_reporter.pytest_runtest_makereport(item, second_call)
                pytest_reporter.pytest_sessionfinish(None, 1)

            payload = output.read_text(encoding="utf-8")
            document = json.loads(payload)
            failures = document["failures"]
            self.assertEqual(2, len(failures))
            self.assertEqual(2, len({failure["signature"] for failure in failures}))
            self.assertTrue(
                all(
                    failure["signature"].startswith("hmac-sha256:")
                    for failure in failures
                )
            )
            self.assertNotEqual(failures[0]["location"], failures[1]["location"])
            for private_value in (
                "first-expected-private",
                "first-actual-private",
                "second-expected-private",
                "second-actual-private",
                key.hex(),
            ):
                self.assertNotIn(private_value, payload)

    def test_failure_record_rejects_malformed_wrong_or_missing_hmac_key(self):
        from sentinel_py.runner import pytest_reporter
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _failure_value,
        )

        key = bytes(range(32))
        wrong_key = bytes(reversed(range(32)))
        location = {
            "column": 8,
            "line": 42,
            "path": "tests/test_subject.py",
        }
        signature = pytest_reporter._failure_signature(
            key,
            True,
            "builtins.AssertionError",
            "tests/test_subject.py::test_answer",
            "call",
            location,
        )
        valid = {
            "assertion": True,
            "exceptionType": "builtins.AssertionError",
            "location": location,
            "nodeId": "tests/test_subject.py::test_answer",
            "phase": "call",
            "signature": signature,
        }
        self.assertEqual(
            (signature, True, "tests/test_subject.py::test_answer", "call"),
            _failure_value(valid, key),
        )

        invalid_cases = (
            ({**valid, "signature": "not-a-signature"}, key),
            ({**valid, "signature": "hmac-sha256:" + "0" * 64}, key),
            (valid, wrong_key),
            (valid, b""),
        )
        for record, actual_key in invalid_cases:
            with self.subTest(record=record, key_length=len(actual_key)):
                with self.assertRaises(MutationBackendError) as stopped:
                    _failure_value(record, actual_key)
                self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_empty_observation_is_bound_to_the_ephemeral_hmac_key(self):
        from sentinel_py.runner import pytest_reporter
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _load_observation,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-empty-hmac-") as directory:
            path = Path(directory) / "report.json"
            key = bytes(range(32))
            with patch.dict(
                os.environ,
                {
                    "SENTINEL_PYTEST_HMAC_KEY": key.hex(),
                    "SENTINEL_PYTEST_NONCE": "empty-nonce",
                    "SENTINEL_PYTEST_REPORT": str(path),
                },
                clear=True,
            ):
                pytest_reporter.pytest_configure(None)
                pytest_reporter.pytest_sessionfinish(None, 0)

            observation = _load_observation(path, "empty-nonce", 0, key)
            self.assertEqual((), observation.failure_signatures)
            with self.assertRaises(MutationBackendError) as stopped:
                _load_observation(path, "empty-nonce", 0, bytes(reversed(range(32))))
            self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_pytest_runner_binds_exact_command_environment_and_report(self):
        from sentinel_py.runner import mutation_backend

        with tempfile.TemporaryDirectory(prefix="sentinel-pytest-command-") as directory:
            temporary_root = Path(directory)
            project = temporary_root / "project"
            mutant_root = project / "mutants"
            key = bytes(range(32))
            selection = ("tests/test_a.py::test_a", "tests/test_b.py")
            observation = object()

            cases = (
                (project, "", {"BASE": "observer"}, False),
                (mutant_root, "candidate-a", {"BASE": "mutation"}, True),
            )
            for index, (cwd, mutant, base_environment, mutation_mode) in enumerate(cases):
                with self.subTest(mutation_mode=mutation_mode):
                    nonce = f"nonce-{index}"
                    result = SimpleNamespace(returncode=7 + index)
                    with (
                        patch.object(
                            mutation_backend.uuid,
                            "uuid4",
                            return_value=nonce,
                        ),
                        patch.object(
                            mutation_backend,
                            "_observer_test_environment",
                            return_value={"BASE": "observer"},
                        ) as observer_environment,
                        patch.object(
                            mutation_backend,
                            "_mutation_test_environment",
                            return_value={"BASE": "mutation"},
                        ) as mutation_environment,
                        patch.object(
                            mutation_backend,
                            "_run_process",
                            return_value=result,
                        ) as run_process,
                        patch.object(
                            mutation_backend,
                            "_load_observation",
                            return_value=observation,
                        ) as load_observation,
                        patch.object(Path, "mkdir") as make_report_directory,
                    ):
                        self.assertIs(
                            observation,
                            mutation_backend._run_pytest(
                                cwd,
                                selection,
                                mutant,
                                temporary_root,
                                key,
                            ),
                        )

                    make_report_directory.assert_called_once_with(
                        mode=0o700,
                        exist_ok=True,
                    )
                    report_path = temporary_root / "reports" / f"{nonce}.json"
                    expected_environment = {
                        **base_environment,
                        "MUTANT_UNDER_TEST": mutant,
                        "SENTINEL_PYTEST_HMAC_KEY": key.hex(),
                        "SENTINEL_PYTEST_NONCE": nonce,
                        "SENTINEL_PYTEST_REPORT": str(report_path),
                    }
                    run_process.assert_called_once_with(
                        (
                            sys.executable,
                            "-m",
                            "pytest",
                            "--rootdir=.",
                            "-q",
                            "-x",
                            "-p",
                            "no:randomly",
                            "-p",
                            "no:random-order",
                            "-p",
                            mutation_backend._OBSERVER_MODULE,
                            *selection,
                        ),
                        cwd,
                        expected_environment,
                    )
                    load_observation.assert_called_once_with(
                        report_path,
                        nonce,
                        result.returncode,
                        key,
                    )
                    if mutation_mode:
                        mutation_environment.assert_called_once_with(
                            temporary_root,
                            project,
                            mutant_root,
                        )
                        observer_environment.assert_not_called()
                    else:
                        observer_environment.assert_called_once_with(
                            temporary_root,
                            project,
                        )
                        mutation_environment.assert_not_called()

            generated_key = bytes(reversed(range(32)))
            with (
                patch.object(
                    mutation_backend.secrets,
                    "token_bytes",
                    return_value=generated_key,
                ) as token_bytes,
                patch.object(mutation_backend.uuid, "uuid4", return_value="generated"),
                patch.object(
                    mutation_backend,
                    "_observer_test_environment",
                    return_value={},
                ),
                patch.object(
                    mutation_backend,
                    "_run_process",
                    return_value=SimpleNamespace(returncode=0),
                ),
                patch.object(
                    mutation_backend,
                    "_load_observation",
                    return_value=observation,
                ) as load_observation,
            ):
                mutation_backend._run_pytest(
                    project,
                    (),
                    "",
                    temporary_root,
                )
            token_bytes.assert_called_once_with(32)
            load_observation.assert_called_once_with(
                temporary_root / "reports" / "generated.json",
                "generated",
                0,
                generated_key,
            )

    def test_real_pytest_replay_preserves_reverse_selection_and_first_failure(self):
        from sentinel_py.runner.mutation_backend import (
            _prepare_observer,
            _replay_failure,
            _run_pytest,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-real-pytest-") as directory:
            temporary_root = Path(directory)
            project = temporary_root / "project"
            tests = project / "tests"
            tests.mkdir(parents=True)
            (tests / "test_subject.py").write_text(
                "import os\n\n"
                "def test_a_failure():\n"
                "    if os.environ.get('MUTANT_UNDER_TEST'):\n"
                "        assert 'a-expected-private' == 'a-actual-private'\n\n"
                "def test_b_failure():\n"
                "    if os.environ.get('MUTANT_UNDER_TEST'):\n"
                "        assert 'b-expected-private' == 'b-actual-private'\n",
                encoding="utf-8",
            )
            _prepare_observer(temporary_root)
            key = bytes(range(32))
            test_a = "tests/test_subject.py::test_a_failure"
            test_b = "tests/test_subject.py::test_b_failure"
            reverse_selection = (test_b, test_a)
            candidate = "pkg.subject.x_answer__mutmut_1"

            control = _run_pytest(project, reverse_selection, "", temporary_root, key)
            initial = _run_pytest(
                project, reverse_selection, candidate, temporary_root, key
            )
            replays = []

            def run_replay(*args):
                replay = _run_pytest(*args)
                replays.append(replay)
                return replay

            with patch(
                "sentinel_py.runner.mutation_backend._run_pytest",
                side_effect=run_replay,
            ):
                state = _replay_failure(
                    candidate,
                    project,
                    initial,
                    temporary_root,
                    key,
                )
            opposite = _run_pytest(
                project, (test_a, test_b), candidate, temporary_root, key
            )
            self.assertEqual(1, len(replays))
            replay = replays[0]

            self.assertTrue(control.complete_pass)
            self.assertEqual(reverse_selection, control.collected)
            self.assertEqual(reverse_selection, control.started)
            self.assertEqual(reverse_selection, initial.collected)
            self.assertEqual((test_b,), initial.started)
            self.assertEqual(1, initial.exit_code)
            self.assertTrue(initial.assertion_only)
            self.assertEqual(reverse_selection, replay.collected)
            self.assertEqual((test_b,), replay.started)
            self.assertEqual(initial.failure_signatures, replay.failure_signatures)
            self.assertEqual("killed", state)
            self.assertEqual((test_a, test_b), opposite.collected)
            self.assertEqual((test_a,), opposite.started)
            self.assertNotEqual(initial.failure_signatures, opposite.failure_signatures)
            self.assertTrue(initial.failure_signatures[0].startswith("hmac-sha256:"))
            reports = tuple(sorted((temporary_root / "reports").glob("*.json")))
            self.assertEqual(4, len(reports))
            payloads = tuple(path.read_text(encoding="utf-8") for path in reports)
            nonces = {json.loads(payload)["nonce"] for payload in payloads}
            self.assertEqual(4, len(nonces))
            for payload in payloads:
                self.assertNotIn("expected-private", payload)
                self.assertNotIn("actual-private", payload)
                self.assertNotIn(key.hex(), payload)

    def test_real_pytest_classifies_setup_and_call_type_errors_as_runtime_errors(self):
        from sentinel_py.runner.mutation_backend import (
            _prepare_observer,
            _replay_failure,
            _run_pytest,
        )

        with tempfile.TemporaryDirectory(prefix="sentinel-real-type-error-") as directory:
            temporary_root = Path(directory)
            project = temporary_root / "project"
            tests = project / "tests"
            tests.mkdir(parents=True)
            (tests / "test_subject.py").write_text(
                "import pytest\n\n"
                "@pytest.fixture\n"
                "def broken_fixture():\n"
                "    raise TypeError('setup failed')\n\n"
                "def test_setup_error(broken_fixture):\n"
                "    pass\n\n"
                "def test_call_error():\n"
                "    raise TypeError('call failed')\n",
                encoding="utf-8",
            )
            _prepare_observer(temporary_root)
            key = bytes(range(32))
            candidate = "pkg.subject.x_answer__mutmut_1"
            for node_id, phase in (
                ("tests/test_subject.py::test_setup_error", "setup"),
                ("tests/test_subject.py::test_call_error", "call"),
            ):
                with self.subTest(phase=phase):
                    existing_reports = set((temporary_root / "reports").glob("*.json"))
                    initial = _run_pytest(
                        project,
                        (node_id,),
                        candidate,
                        temporary_root,
                        key,
                    )
                    initial_report = (
                        set((temporary_root / "reports").glob("*.json"))
                        - existing_reports
                    ).pop()
                    initial_document = json.loads(
                        initial_report.read_text(encoding="utf-8")
                    )
                    replays = []

                    def run_replay(*args):
                        replay = _run_pytest(*args)
                        replays.append(replay)
                        return replay

                    with patch(
                        "sentinel_py.runner.mutation_backend._run_pytest",
                        side_effect=run_replay,
                    ):
                        state = _replay_failure(
                            candidate,
                            project,
                            initial,
                            temporary_root,
                            key,
                        )

                    self.assertEqual("runtimeError", state)
                    self.assertEqual(1, initial.exit_code)
                    self.assertFalse(initial.assertion_only)
                    self.assertEqual((node_id,), initial.collected)
                    self.assertEqual((node_id,), initial.started)
                    self.assertTrue(initial.failure_signatures)
                    self.assertEqual(
                        "builtins.TypeError",
                        initial_document["failures"][0]["exceptionType"],
                    )
                    self.assertEqual(phase, initial_document["failures"][0]["phase"])
                    self.assertEqual(initial, replays[0])

    def test_실제_pytest가_src와_평평한_검사_복사본만_import한다(self):
        from sentinel_py.runner.mutation_backend import (
            _OBSERVER_MODULE,
            MutationBackendError,
            _prepare_observer,
            _run_pytest,
        )

        candidate = "sentinel_py.mutation.x_answer__mutmut_1"
        key = bytes(range(32))
        test_source = (
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import flat_helper\n"
            "from sentinel_py import mutation\n\n"
            "def test_검사_복사본을_읽는다():\n"
            "    project_root = Path(__file__).resolve().parents[1]\n"
            "    source_root = project_root / 'source'\n"
            "    if not source_root.is_dir():\n"
            "        source_root = project_root / 'src'\n"
            "    if not source_root.is_dir():\n"
            "        source_root = project_root\n"
            "    expected_path = source_root / 'sentinel_py' / 'mutation.py'\n"
            "    location = 'flat' if source_root == project_root else source_root.name\n"
            "    phase = 'mutant' if project_root.name == 'mutants' else 'snapshot'\n"
            "    expected_marker = f'{location}-{phase}'\n"
            "    value = getattr(mutation, 'answer', lambda: 'installed-original')()\n"
            "    run = 'replay' if os.environ.get('MUTANT_UNDER_TEST') else 'control'\n"
            "    observation = {\n"
            "        'path': str(Path(mutation.__file__).resolve()),\n"
            "        'marker': list(mutation.MUTATION_STATES),\n"
            "        'value': value,\n"
            "        'helperPath': str(Path(flat_helper.__file__).resolve()),\n"
            "        'helperMarker': flat_helper.MARKER,\n"
            "    }\n"
            "    Path(__file__).with_name(f'import-{run}.json').write_text(\n"
            "        json.dumps(observation), encoding='utf-8'\n"
            "    )\n"
            "    assert Path(mutation.__file__).resolve() == expected_path\n"
            "    assert mutation.MUTATION_STATES == (expected_marker,)\n"
            "    assert value == 'control'\n"
            "    assert Path(flat_helper.__file__).resolve() == project_root / 'flat_helper.py'\n"
            "    assert flat_helper.MARKER == f'{phase}-helper'\n"
        )

        def subject_source(location, *, mutated):
            marker = location + ("-mutant" if mutated else "-snapshot")
            lines = [f"MUTATION_STATES = ({marker!r},)", "", "def answer():"]
            if mutated:
                lines[0:0] = ["import os", ""]
                lines.extend(
                    (
                        f"    if os.environ.get('MUTANT_UNDER_TEST') == {candidate!r}:",
                        "        return 'mutant'",
                    )
                )
            lines.append("    return 'control'")
            return "\n".join(lines) + "\n"

        with tempfile.TemporaryDirectory(prefix="sentinel-source-binding-") as directory:
            root = Path(directory).resolve()
            cases = (
                ("src", ("src",), "src"),
                ("flat", ("flat",), "flat"),
                ("src-flat", ("src", "flat"), "src"),
                ("source-src-flat", ("source", "src", "flat"), "source"),
            )
            for layout, locations, selected in cases:
                with self.subTest(layout=layout):
                    temporary_root = root / layout
                    project = temporary_root / "project"
                    mutant_root = project / "mutants"
                    source_root = project if selected == "flat" else project / selected
                    mutant_source_root = (
                        mutant_root
                        if selected == "flat"
                        else mutant_root / selected
                    )
                    for helper_root, marker in (
                        (project, "snapshot-helper"),
                        (mutant_root, "mutant-helper"),
                    ):
                        helper_root.mkdir(parents=True, exist_ok=True)
                        (helper_root / "flat_helper.py").write_text(
                            f"MARKER = {marker!r}\n",
                            encoding="utf-8",
                        )
                    for location in locations:
                        snapshot_location = (
                            project if location == "flat" else project / location
                        )
                        mutant_location = (
                            mutant_root
                            if location == "flat"
                            else mutant_root / location
                        )
                        for shadow_root in (snapshot_location, mutant_location):
                            shadow_root.mkdir(parents=True, exist_ok=True)
                            (shadow_root / (_OBSERVER_MODULE + ".py")).write_text(
                                "raise RuntimeError('project observer must not load')\n",
                                encoding="utf-8",
                            )
                        for package_root, source in (
                            (
                                snapshot_location / "sentinel_py",
                                subject_source(location, mutated=False),
                            ),
                            (
                                mutant_location / "sentinel_py",
                                subject_source(location, mutated=True),
                            ),
                        ):
                            package_root.mkdir(parents=True)
                            (package_root / "__init__.py").write_text(
                                "", encoding="utf-8"
                            )
                            (package_root / "mutation.py").write_text(
                                source, encoding="utf-8"
                            )
                    for tests in (project / "tests", mutant_root / "tests"):
                        tests.mkdir()
                        (tests / "test_subject.py").write_text(
                            test_source,
                            encoding="utf-8",
                        )
                    _prepare_observer(temporary_root)
                    selection = ("tests/test_subject.py",)

                    try:
                        baseline = _run_pytest(
                            project, selection, "", temporary_root, key
                        )
                        control = _run_pytest(
                            mutant_root, selection, "", temporary_root, key
                        )
                        replay = _run_pytest(
                            mutant_root,
                            selection,
                            candidate,
                            temporary_root,
                            key,
                        )
                    except MutationBackendError as error:
                        self.fail(f"신뢰된 observer가 가려짐: {error.code}")

                    self.assertTrue(baseline.complete_pass)
                    self.assertTrue(control.complete_pass)
                    self.assertEqual(1, replay.exit_code)
                    self.assertTrue(replay.assertion_only)
                    baseline_import = json.loads(
                        (project / "tests" / "import-control.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    control_import = json.loads(
                        (mutant_root / "tests" / "import-control.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    replay_import = json.loads(
                        (mutant_root / "tests" / "import-replay.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(
                        str(source_root / "sentinel_py/mutation.py"),
                        baseline_import["path"],
                    )
                    self.assertEqual("control", baseline_import["value"])
                    self.assertEqual(
                        str(project / "flat_helper.py"),
                        baseline_import["helperPath"],
                    )
                    self.assertEqual("snapshot-helper", baseline_import["helperMarker"])
                    self.assertEqual(
                        str(mutant_source_root / "sentinel_py/mutation.py"),
                        control_import["path"],
                    )
                    self.assertEqual("control", control_import["value"])
                    self.assertEqual(
                        str(mutant_root / "flat_helper.py"),
                        control_import["helperPath"],
                    )
                    self.assertEqual("mutant-helper", control_import["helperMarker"])
                    self.assertEqual(control_import["path"], replay_import["path"])
                    self.assertEqual("mutant", replay_import["value"])
                    self.assertEqual(
                        control_import["helperPath"], replay_import["helperPath"]
                    )
                    self.assertEqual("mutant-helper", replay_import["helperMarker"])

    def test_classify_test_failures_replays_only_raw_test_failures(self):
        from sentinel_py.runner.mutation_backend import (
            _BackendReports,
            _PytestObservation,
            _classify_test_failures,
        )

        control = _PytestObservation(
            collected=("tests/test_subject.py::test_answer",),
            started=("tests/test_subject.py::test_answer",),
            exit_code=0,
            failure_signatures=(),
            assertion_only=False,
        )
        initial_a = _PytestObservation(
            collected=control.collected,
            started=control.started,
            exit_code=1,
            failure_signatures=("sha256:" + "1" * 64,),
            assertion_only=True,
        )
        initial_b = _PytestObservation(
            collected=control.collected,
            started=control.started,
            exit_code=1,
            failure_signatures=("sha256:" + "2" * 64,),
            assertion_only=True,
        )
        state_a = "killed"
        state_b = "runtimeError"
        project = SimpleNamespace(
            project_root=Path("/project"),
            module=SimpleNamespace(
                test_command=("python", "-m", "pytest"),
                test_roots=(Path("/project/tests"),),
            ),
        )
        snapshot = Path("/snapshot")
        temporary_root = Path("/temporary")
        assertion_key = bytes(range(32))
        reports = _BackendReports(Path("/reports"), "run-nonce", assertion_key)
        selection = ("tests",)

        def initial_for(candidate, actual_reports):
            self.assertIs(reports, actual_reports)
            return {"candidate-a": initial_a, "candidate-b": initial_b}[candidate]

        def replay_for(
            candidate,
            mutant_root,
            initial,
            actual_temporary_root,
            actual_assertion_key,
        ):
            self.assertEqual(snapshot / "mutants", mutant_root)
            self.assertEqual(temporary_root, actual_temporary_root)
            self.assertEqual(assertion_key, actual_assertion_key)
            self.assertIs(
                {"candidate-a": initial_a, "candidate-b": initial_b}[candidate],
                initial,
            )
            return {"candidate-a": state_a, "candidate-b": state_b}[candidate]

        with (
            patch(
                "sentinel_py.runner.mutation_backend._pytest_selection",
                return_value=selection,
            ) as select_tests,
            patch(
                "sentinel_py.runner.mutation_backend._run_pytest",
                return_value=control,
            ) as run_pytest,
            patch(
                "sentinel_py.runner.mutation_backend._initial_mutmut_observation",
                side_effect=initial_for,
            ) as initial_observation,
            patch(
                "sentinel_py.runner.mutation_backend._replay_failure",
                side_effect=replay_for,
            ) as replay,
        ):
            states = _classify_test_failures(
                project,
                snapshot,
                temporary_root,
                {"candidate-b": 1, "candidate-c": 0, "candidate-a": 1},
                reports,
            )

        self.assertEqual(["candidate-a", "candidate-b"], list(states))
        self.assertEqual({"candidate-a": state_a, "candidate-b": state_b}, states)
        select_tests.assert_called_once_with(project)
        run_pytest.assert_called_once_with(
            snapshot / "mutants",
            selection,
            "",
            temporary_root,
            assertion_key,
        )
        self.assertEqual(
            [call("candidate-a", reports), call("candidate-b", reports)],
            initial_observation.call_args_list,
        )
        self.assertEqual(2, replay.call_count)

        self.assertEqual(
            {},
            _classify_test_failures(
                SimpleNamespace(),
                Path("/snapshot"),
                Path("/temporary"),
                {"candidate": 0},
                _BackendReports(Path("/reports"), "run-nonce", assertion_key),
            ),
        )

    def test_classify_test_failures_stops_before_replay_when_control_fails(self):
        from sentinel_py.runner.mutation_backend import (
            BaselineFailure,
            _BackendReports,
            _PytestObservation,
            _classify_test_failures,
        )

        control = _PytestObservation(
            collected=("tests/test_subject.py::test_answer",),
            started=("tests/test_subject.py::test_answer",),
            exit_code=1,
            failure_signatures=("hmac-sha256:" + "1" * 64,),
            assertion_only=True,
        )
        project = SimpleNamespace()
        reports = _BackendReports(Path("/reports"), "run-nonce", bytes(range(32)))
        with (
            patch(
                "sentinel_py.runner.mutation_backend._pytest_selection",
                return_value=("tests",),
            ),
            patch(
                "sentinel_py.runner.mutation_backend._run_pytest",
                return_value=control,
            ),
            patch(
                "sentinel_py.runner.mutation_backend._initial_mutmut_observation",
            ) as initial_observation,
            patch(
                "sentinel_py.runner.mutation_backend._replay_failure",
            ) as replay,
        ):
            with self.assertRaises(BaselineFailure) as stopped:
                _classify_test_failures(
                    project,
                    Path("/snapshot"),
                    Path("/temporary"),
                    {"candidate": 1},
                    reports,
                )

        self.assertEqual("baselineTestsFailed", stopped.exception.code)
        initial_observation.assert_not_called()
        replay.assert_not_called()

    def test_initial_mutmut_observation_uses_runtime_candidate_and_bound_nonce(self):
        from sentinel_py.runner import pytest_reporter
        from sentinel_py.runner.mutation_backend import (
            _BackendReports,
            _initial_mutmut_observation,
        )

        expected = object()
        assertion_key = bytes(range(32))
        reports = _BackendReports(Path("/reports"), "run-nonce", assertion_key)
        candidate = "pkg.subject.x__init__.answer__mutmut_1"
        runtime_candidate = candidate.replace("__init__.", "")
        with patch(
            "sentinel_py.runner.mutation_backend._load_observation",
            return_value=expected,
        ) as load:
            actual = _initial_mutmut_observation(candidate, reports)

        self.assertIs(expected, actual)
        load.assert_called_once_with(
            pytest_reporter.mutmut_report_path(reports.root, runtime_candidate),
            pytest_reporter.mutmut_report_nonce(reports.run_nonce, runtime_candidate),
            1,
            assertion_key,
        )

    def test_failure_record_requires_exact_shape_boolean_and_signature(self):
        from sentinel_py.runner.pytest_reporter import _failure_signature
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _failure_value,
        )

        assertion_key = bytes(range(32))
        location = {
            "column": 8,
            "line": 42,
            "path": "tests/test_subject.py",
        }
        valid = {
            "assertion": True,
            "exceptionType": "builtins.AssertionError",
            "location": location,
            "nodeId": "tests/test_subject.py::test_answer",
            "phase": "call",
        }
        valid["signature"] = _failure_signature(
            assertion_key,
            valid["assertion"],
            valid["exceptionType"],
            valid["nodeId"],
            valid["phase"],
            location,
        )
        self.assertEqual(
            (
                valid["signature"],
                True,
                "tests/test_subject.py::test_answer",
                "call",
            ),
            _failure_value(valid, assertion_key),
        )
        for invalid in (
            None,
            {**valid, "extra": True},
            {**valid, "assertion": 1},
            {**valid, "signature": None},
            {**valid, "location": {**location, "path": "../outside.py"}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(MutationBackendError) as stopped:
                _failure_value(invalid, assertion_key)
            self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_mutmut_report_is_bound_to_the_runtime_mutant_and_run_nonce(self):
        from sentinel_py.runner import pytest_reporter

        with tempfile.TemporaryDirectory(prefix="sentinel-mutmut-report-") as directory:
            root = Path(directory)
            mutant = "pkg.subject.x_answer__mutmut_1"
            nonce = "run-nonce"
            assertion_key = bytes(range(32))
            item = SimpleNamespace(nodeid="tests/test_subject.py::test_answer")
            with patch.dict(
                os.environ,
                {
                    "MUTANT_UNDER_TEST": mutant,
                    "SENTINEL_MUTMUT_REPORT_ROOT": str(root),
                    "SENTINEL_MUTMUT_RUN_NONCE": nonce,
                    "SENTINEL_PYTEST_HMAC_KEY": assertion_key.hex(),
                },
                clear=True,
            ):
                pytest_reporter.pytest_configure(None)
                pytest_reporter.pytest_collection_finish(SimpleNamespace(items=(item,)))
                pytest_reporter.pytest_runtest_logstart(item.nodeid, None)
                pytest_reporter.pytest_sessionfinish(None, 0)

            output = pytest_reporter.mutmut_report_path(root, mutant)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                pytest_reporter.mutmut_report_nonce(nonce, mutant),
                document["nonce"],
            )
            self.assertEqual([item.nodeid], document["collected"])
            self.assertEqual(document["collected"], document["started"])

    def test_nested_reporter_contract_does_not_replace_outer_session_state(self):
        from sentinel_py.runner import pytest_reporter

        with tempfile.TemporaryDirectory(prefix="sentinel-nested-report-") as directory:
            root = Path(directory)
            outer_output = root / "outer.json"
            inner_output = root / "inner.json"
            assertion_key = bytes(range(32))
            outer_items = (
                SimpleNamespace(nodeid="tests/test_outer.py::test_first"),
                SimpleNamespace(nodeid="tests/test_outer.py::test_second"),
            )
            inner_item = SimpleNamespace(nodeid="tests/test_inner.py::test_inner")
            with patch.dict(
                os.environ,
                {
                    "SENTINEL_PYTEST_REPORT": str(outer_output),
                    "SENTINEL_PYTEST_HMAC_KEY": assertion_key.hex(),
                    "SENTINEL_PYTEST_NONCE": "outer-nonce",
                },
                clear=False,
            ):
                pytest_reporter.pytest_configure(None)
                pytest_reporter.pytest_collection_finish(
                    SimpleNamespace(items=outer_items)
                )
                pytest_reporter.pytest_runtest_logstart(outer_items[0].nodeid, None)
                with patch.dict(
                    os.environ,
                    {
                        "SENTINEL_PYTEST_REPORT": str(inner_output),
                        "SENTINEL_PYTEST_NONCE": "inner-nonce",
                    },
                    clear=False,
                ):
                    pytest_reporter.pytest_configure(None)
                    pytest_reporter.pytest_collection_finish(
                        SimpleNamespace(items=(inner_item,))
                    )
                    pytest_reporter.pytest_runtest_logstart(inner_item.nodeid, None)
                    pytest_reporter.pytest_sessionfinish(None, 0)
                pytest_reporter.pytest_runtest_logstart(outer_items[1].nodeid, None)
                pytest_reporter.pytest_sessionfinish(None, 0)

            outer = json.loads(outer_output.read_text(encoding="utf-8"))
            inner = json.loads(inner_output.read_text(encoding="utf-8"))
            self.assertEqual(
                [item.nodeid for item in outer_items],
                outer["collected"],
            )
            self.assertEqual(outer["collected"], outer["started"])
            self.assertEqual([inner_item.nodeid], inner["collected"])
            self.assertEqual(inner["collected"], inner["started"])

    def test_reporter_records_typed_assertion_and_complete_inventory(self):
        from sentinel_py.runner import pytest_reporter

        with tempfile.TemporaryDirectory(prefix="sentinel-pytest-report-") as directory:
            output = Path(directory) / "report.json"
            nonce = "00000000-0000-4000-8000-000000000000"
            assertion_key = bytes(range(32))
            node_id = (
                "tests/test_mutation_runner.py::PytestObservationTests::"
                "test_reporter_records_typed_assertion_and_complete_inventory"
            )
            item = SimpleNamespace(nodeid=node_id, path=Path(__file__))
            session = SimpleNamespace(items=[item])
            passing_call = SimpleNamespace(excinfo=None, when="setup")
            try:
                assert False
            except AssertionError as error:
                failed_call = SimpleNamespace(
                    excinfo=SimpleNamespace(type=AssertionError, value=error),
                    when="call",
                )

            with patch.dict(
                os.environ,
                {
                    "SENTINEL_PYTEST_REPORT": str(output),
                    "SENTINEL_PYTEST_HMAC_KEY": assertion_key.hex(),
                    "SENTINEL_PYTEST_NONCE": nonce,
                },
                clear=False,
            ):
                pytest_reporter.pytest_configure(None)
                pytest_reporter.pytest_collection_finish(session)
                pytest_reporter.pytest_runtest_logstart(item.nodeid, ("test_a.py", 1, "test_a"))
                pytest_reporter.pytest_runtest_makereport(item, passing_call)
                pytest_reporter.pytest_runtest_makereport(item, failed_call)
                pytest_reporter.pytest_sessionfinish(None, 1)

            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(nonce, document["nonce"])
            self.assertEqual([item.nodeid], document["collected"])
            self.assertEqual([item.nodeid], document["started"])
            self.assertTrue(document["failures"][0]["assertion"])
            self.assertEqual("builtins.AssertionError", document["failures"][0]["exceptionType"])

    def test_reporter_without_output_contract_does_not_write(self):
        from sentinel_py.runner import pytest_reporter

        with patch.dict(
            os.environ,
            {"SENTINEL_PYTEST_REPORT": "", "SENTINEL_PYTEST_NONCE": ""},
            clear=False,
        ):
            del os.environ["SENTINEL_PYTEST_REPORT"]
            del os.environ["SENTINEL_PYTEST_NONCE"]
            pytest_reporter.pytest_configure(None)
            self.assertIsNone(pytest_reporter.pytest_sessionfinish(None, 0))

    def test_reporter_rejects_incomplete_contract_or_invalid_hmac_key(self):
        from sentinel_py.runner import pytest_reporter

        cases = (
            (
                {"SENTINEL_PYTEST_REPORT": "/tmp/report.json"},
                "sentinelPytestContractInvalid",
            ),
            (
                {"SENTINEL_PYTEST_NONCE": "nonce"},
                "sentinelPytestContractInvalid",
            ),
            (
                {
                    "SENTINEL_PYTEST_REPORT": "/tmp/report.json",
                    "SENTINEL_PYTEST_NONCE": "nonce",
                },
                "sentinelPytestHmacKeyInvalid",
            ),
            (
                {
                    "SENTINEL_PYTEST_HMAC_KEY": "0" * 63,
                    "SENTINEL_PYTEST_REPORT": "/tmp/report.json",
                    "SENTINEL_PYTEST_NONCE": "nonce",
                },
                "sentinelPytestHmacKeyInvalid",
            ),
        )
        active_mutant = os.environ.get("MUTANT_UNDER_TEST")
        mutant_environment = (
            {} if active_mutant is None else {"MUTANT_UNDER_TEST": active_mutant}
        )
        for environment, expected_code in cases:
            with self.subTest(environment=environment), patch.dict(
                os.environ,
                {**mutant_environment, **environment},
                clear=True,
            ):
                with self.assertRaises(RuntimeError) as stopped:
                    pytest_reporter._report_contract()
                self.assertEqual(
                    (expected_code,),
                    stopped.exception.args,
                )

    def test_observation_loader_rejects_shape_nonce_schema_and_exit_mismatch(self):
        from sentinel_py.runner.pytest_reporter import (
            _failure_signature,
            _report_signature,
        )
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _load_observation,
        )

        assertion_key = bytes(range(32))
        unsigned = {
            "collected": [
                "tests/test_b.py::test_b",
                "tests/test_a.py::test_a",
            ],
            "exitCode": 0,
            "failures": [],
            "nonce": "nonce",
            "schemaVersion": "sentinel-pytest-observation-v3",
            "started": [
                "tests/test_b.py::test_b",
                "tests/test_a.py::test_a",
            ],
        }

        def signed(**changes):
            document = {**unsigned, **changes}
            return {
                **document,
                "reportSignature": _report_signature(assertion_key, document),
            }

        def failure(node_id, phase):
            location = {"column": 0, "line": 1, "path": "tests/test_a.py"}
            return {
                "assertion": False,
                "exceptionType": "builtins.TypeError",
                "location": location,
                "nodeId": node_id,
                "phase": phase,
                "signature": _failure_signature(
                    assertion_key,
                    False,
                    "builtins.TypeError",
                    node_id,
                    phase,
                    location,
                ),
            }

        signed_valid = signed()
        cases = (
            {**signed(), "unexpected": True},
            signed(nonce="wrong"),
            signed(schemaVersion="wrong"),
            signed(schemaVersion="sentinel-pytest-observation-v2"),
            signed(exitCode=1),
            {**signed(), "reportSignature": "not-a-signature"},
            {
                **signed_valid,
                "collected": list(reversed(signed_valid["collected"])),
            },
            signed(collected=[unsigned["collected"][0]] * 2),
            signed(started=[unsigned["started"][0]] * 2),
            signed(started=[unsigned["started"][1]]),
            signed(
                started=[unsigned["started"][0]],
                failures=[failure(unsigned["started"][1], "call")],
            ),
            signed(
                started=[unsigned["started"][0]],
                failures=[
                    failure(unsigned["started"][0], "setup"),
                    failure(unsigned["started"][0], "setup"),
                ],
            ),
        )
        with tempfile.TemporaryDirectory(prefix="sentinel-observation-load-") as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(signed_valid), encoding="utf-8")
            observation = _load_observation(path, "nonce", 0, assertion_key)
            self.assertEqual(tuple(unsigned["collected"]), observation.collected)
            self.assertEqual(tuple(unsigned["started"]), observation.started)
            for index, document in enumerate(cases):
                with self.subTest(index=index):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(MutationBackendError) as stopped:
                        _load_observation(path, "nonce", 0, assertion_key)
                    self.assertEqual("pytestObservationInvalid", stopped.exception.code)

    def test_failure_replay_uses_initial_mutmut_tests_and_returns_killed(self):
        from sentinel_py.runner.mutation_backend import (
            _PytestObservation,
            _replay_failure,
        )

        initial = _PytestObservation(
            collected=(
                "tests/test_subject.py::test_b",
                "tests/test_subject.py::test_a",
            ),
            started=("tests/test_subject.py::test_b",),
            exit_code=1,
            failure_signatures=("sha256:" + "1" * 64,),
            assertion_only=True,
        )
        assertion_key = bytes(range(32))
        with patch(
            "sentinel_py.runner.mutation_backend._run_pytest",
            return_value=initial,
        ) as run_pytest:
            state = _replay_failure(
                "pkg.subject.x_answer__mutmut_1",
                Path("mutants"),
                initial,
                Path("temporary"),
                assertion_key,
            )

        self.assertEqual("killed", state)
        run_pytest.assert_called_once_with(
            Path("mutants"),
            initial.collected,
            "pkg.subject.x_answer__mutmut_1",
            Path("temporary"),
            assertion_key,
        )

    def test_failure_replay_requires_matching_exit_inventory_order_type_and_signature(self):
        from sentinel_py.runner.mutation_backend import (
            MutationBackendError,
            _PytestObservation,
            _replay_failure,
        )

        def observation(
            exit_code=1,
            assertion_only=True,
            signature="hmac-sha256:" + "1" * 64,
            collected=("tests/test_subject.py::test_answer",),
            started=("tests/test_subject.py::test_answer",),
        ):
            return _PytestObservation(
                collected=collected,
                started=started,
                exit_code=exit_code,
                failure_signatures=() if signature is None else (signature,),
                assertion_only=assertion_only,
            )

        matching = "hmac-sha256:" + "1" * 64
        different = "hmac-sha256:" + "2" * 64
        other = ("tests/test_subject.py::test_other",)
        cases = (
            (observation(exit_code=2), observation()),
            (observation(), observation(exit_code=0)),
            (observation(assertion_only=False), observation()),
            (observation(), observation(signature=different)),
            (observation(collected=other, started=other), observation()),
            (observation(), observation(started=())),
            (observation(signature=None), observation(signature=None)),
        )
        assertion_key = bytes(range(32))
        for initial, replay in cases:
            with self.subTest(initial=initial, replay=replay), patch(
                "sentinel_py.runner.mutation_backend._run_pytest",
                return_value=replay,
            ):
                with self.assertRaises(MutationBackendError) as stopped:
                    _replay_failure(
                        "pkg.subject.x_answer__mutmut_1",
                        Path("mutants"),
                        initial,
                        Path("temporary"),
                        assertion_key,
                    )
            self.assertEqual("killProofInvalid", stopped.exception.code)

        for assertion_only, expected in ((True, "killed"), (False, "runtimeError")):
            initial = observation(assertion_only=assertion_only, signature=matching)
            with self.subTest(expected=expected), patch(
                "sentinel_py.runner.mutation_backend._run_pytest",
                return_value=initial,
            ):
                self.assertEqual(
                    expected,
                    _replay_failure(
                        "pkg.subject.x_answer__mutmut_1",
                        Path("mutants"),
                        initial,
                        Path("temporary"),
                        assertion_key,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
