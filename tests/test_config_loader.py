from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def _document() -> dict:
    return {
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
            }
        ],
    }


class ConfigLoaderTests(unittest.TestCase):
    def test_load_project_preserves_requested_module_and_resolved_config(self):
        from sentinel_py.config import loader

        project_root = Path("/project")
        config_path = project_root / "chosen.json"
        document = {"document": True}
        modules = (object(), object())
        selected = SimpleNamespace(module_id="chosen")
        sources = (object(),)
        resolved_modules = (selected,)

        with (
            patch.object(loader, "_project_root", return_value=project_root),
            patch.object(loader, "_config_path", return_value=config_path),
            patch.object(loader, "_load_document", return_value=document),
            patch.object(loader, "_project_modules", return_value=modules),
            patch.object(
                loader,
                "resolve_python_module",
                return_value=selected,
            ) as resolve_selected,
            patch.object(
                loader,
                "resolve_python_modules",
                return_value=resolved_modules,
            ),
            patch.object(loader, "_production_sources", return_value=sources),
            patch.object(loader, "_verify_project_scope") as verify_scope,
        ):
            loaded = loader.load_project("project", "chosen.json", "chosen")

        self.assertEqual(
            loader.LoadedProject(project_root, config_path, selected, sources),
            loaded,
        )
        resolve_selected.assert_called_once_with(project_root, modules, "chosen")
        verify_scope.assert_called_once_with(
            project_root,
            resolved_modules,
            selected,
            sources,
        )

    def test_project_root_maps_resolution_and_non_directory_failures_exactly(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _project_root

        resolution_error = OSError("denied")
        with (
            patch.object(Path, "resolve", side_effect=resolution_error),
            self.assertRaises(UsageConfigError) as stopped,
        ):
            _project_root("project")

        self.assertEqual("projectRootInvalid", stopped.exception.code)
        self.assertIs(resolution_error, stopped.exception.__cause__)

        with tempfile.NamedTemporaryFile() as regular_file:
            with self.assertRaises(UsageConfigError) as stopped:
                _project_root(regular_file.name)

        self.assertEqual("projectRootInvalid", stopped.exception.code)

    def test_config_path_maps_resolution_and_missing_file_failures_exactly(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _config_path

        with tempfile.TemporaryDirectory(prefix="sentinel-config-path-") as directory:
            project = Path(directory)
            resolution_error = OSError("denied")
            with (
                patch.object(Path, "resolve", side_effect=resolution_error),
                self.assertRaises(UsageConfigError) as stopped,
            ):
                _config_path(project, "chosen.json")

            self.assertEqual("projectConfigPathInvalid", stopped.exception.code)
            self.assertIs(resolution_error, stopped.exception.__cause__)

            with self.assertRaises(UsageConfigError) as stopped:
                _config_path(project, "missing.json")

        self.assertEqual("projectConfigNotFound", stopped.exception.code)

    def test_load_document_rejects_non_finite_json_and_unsupported_spec_exactly(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _load_document

        with tempfile.TemporaryDirectory(prefix="sentinel-load-document-") as directory:
            root = Path(directory)
            non_finite = root / "non-finite.json"
            non_finite.write_bytes(
                b'{"specVersion":"1.0.0","modules":[NaN]}'
            )
            with self.assertRaises(UsageConfigError) as stopped:
                _load_document(non_finite)
            self.assertEqual("projectConfigInvalid", stopped.exception.code)

            malformed = root / "malformed.json"
            malformed.write_bytes(b"\xff")
            with self.assertRaises(UsageConfigError) as stopped:
                _load_document(malformed)
            self.assertEqual("projectConfigInvalid", stopped.exception.code)
            self.assertIsInstance(stopped.exception.__cause__, UnicodeError)

            unsupported = root / "unsupported.json"
            self._write_json(
                unsupported,
                {"specVersion": "2.0.0", "modules": []},
            )
            with self.assertRaises(UsageConfigError) as stopped:
                _load_document(unsupported)
            self.assertEqual("specVersionUnsupported", stopped.exception.code)

            valid = root / "valid.json"
            self._write_json(valid, _document())
            self.assertEqual(_document(), _load_document(valid))

    def test_project_modules_requires_a_nonempty_list_with_exact_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _project_modules

        for modules in ([], ("module",)):
            with self.subTest(modules=modules):
                with self.assertRaises(UsageConfigError) as stopped:
                    _project_modules({"modules": modules})

                self.assertEqual("projectConfigShapeInvalid", stopped.exception.code)

    def test_coverage_config_requires_the_exact_object_shape(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _coverage_config

        valid = {
            "command": ["python", "-m", "coverage", "json"],
            "format": "coverage-py-json",
            "report": "coverage.json",
        }
        self.assertEqual({}, _coverage_config(None))
        self.assertIs(valid, _coverage_config(valid))

        invalid_values = (
            tuple(valid),
            {"command": valid["command"], "format": valid["format"]},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(UsageConfigError) as stopped:
                    _coverage_config(value)

                self.assertEqual("projectConfigShapeInvalid", stopped.exception.code)

    def test_production_inventory_skips_one_tool_file_without_losing_later_files(self):
        from sentinel_py.config.loader import ProductionSource, _production_sources

        root = MagicMock()
        owned = Path("/project/.sentinel/generated.py")
        kept = Path("/project/app/subject.py")
        root.glob.return_value = (owned, kept)
        module = SimpleNamespace(root=root, production=(Path("**/*.py"),))
        expected = ProductionSource(kept, "app/subject.py")

        with (
            patch(
                "sentinel_py.config.loader._is_tool_owned_candidate",
                side_effect=(True, False),
            ),
            patch(
                "sentinel_py.config.loader._production_source",
                return_value=expected,
            ) as production_source,
        ):
            actual = _production_sources(module)

        self.assertEqual((expected,), actual)
        production_source.assert_called_once_with(module, kept)

    def test_empty_production_inventory_has_the_exact_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _production_sources

        root = MagicMock()
        root.glob.return_value = ()
        module = SimpleNamespace(root=root, production=(Path("**/*.py"),))

        with self.assertRaises(UsageConfigError) as stopped:
            _production_sources(module)

        self.assertEqual("emptyProductionInventory", stopped.exception.code)

    def test_candidate_outside_project_is_not_mistaken_for_tool_output(self):
        from sentinel_py.config.loader import _is_tool_owned_candidate

        module = SimpleNamespace(project_root=Path("/project"))

        self.assertFalse(
            _is_tool_owned_candidate(module, Path("/outside/subject.py"))
        )

    def test_project_scope_mismatch_has_the_exact_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import ProductionSource, _verify_project_scope

        project = Path("/project")
        selected = SimpleNamespace(module_id="api", excluded=())
        source = ProductionSource(project / "app" / "subject.py", "app/subject.py")
        scope = SimpleNamespace(production=(Path("app/other.py"),))

        with (
            patch(
                "sentinel_py.config.loader._verified_test_sources",
                return_value=(),
            ),
            patch(
                "sentinel_py.config.loader.classify_python_scope",
                return_value=scope,
            ),
            self.assertRaises(UsageConfigError) as stopped,
        ):
            _verify_project_scope(project, (selected,), selected, (source,))

        self.assertEqual("invalidProductionScope", stopped.exception.code)

    def test_verified_test_sources_use_utf8_path_order_not_path_part_order(self):
        from sentinel_py.config.loader import _verified_test_sources

        module = SimpleNamespace(
            test_roots=(Path("/project/tests"),),
            test_patterns=("test_*.py",),
        )
        discovered = {Path("a/x.py"), Path("a.b.py")}

        with patch(
            "sentinel_py.config.loader._matching_test_sources",
            return_value=discovered,
        ):
            actual = _verified_test_sources(Path("/project"), (module,))

        self.assertEqual((Path("a.b.py"), Path("a/x.py")), actual)

    def test_matching_test_sources_excludes_matching_directories(self):
        from sentinel_py.config.loader import _matching_test_sources

        with tempfile.TemporaryDirectory(prefix="sentinel-matching-tests-") as directory:
            project = Path(directory)
            root = project / "tests"
            root.mkdir()
            expected = root / "test_subject.py"
            expected.write_bytes(b"def test_subject():\n    assert True\n")
            (root / "helper.py").write_bytes(b"HELPER = True\n")
            (root / "test_directory.py").mkdir()

            actual = _matching_test_sources(
                project,
                root,
                ("test_*.py",),
            )

        self.assertEqual({Path("tests/test_subject.py")}, actual)

    def test_matching_test_sources_rejects_symlink_with_exact_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _matching_test_sources

        with tempfile.TemporaryDirectory(prefix="sentinel-test-link-") as directory:
            project = Path(directory)
            root = project / "tests"
            root.mkdir()
            target = project / "target.py"
            target.write_bytes(b"def test_target():\n    assert True\n")
            (root / "test_link.py").symlink_to(target)

            with self.assertRaises(UsageConfigError) as stopped:
                _matching_test_sources(project, root, ("test_*.py",))

        self.assertEqual("invalidTestScope", stopped.exception.code)

    def test_module_config_preserves_every_optional_field_exactly(self):
        from sentinel_py.config.loader import _module_config
        from sentinel_py.config.models import ModuleConfig

        value = {
            "id": "api",
            "language": "python",
            "root": "services/api",
            "production": ["src/**/*.py"],
            "testCommand": ["python", "-m", "pytest"],
            "coverage": {
                "command": ["python", "-m", "coverage", "json"],
                "format": "coverage-py-json",
                "report": "build/coverage.json",
            },
            "testRoots": ["tests", "checks"],
            "testPatterns": ["test_*.py", "*_spec.py"],
        }

        self.assertEqual(
            ModuleConfig(
                module_id="api",
                language="python",
                root="services/api",
                production=["src/**/*.py"],
                test_command=["python", "-m", "pytest"],
                coverage_command=["python", "-m", "coverage", "json"],
                coverage_format="coverage-py-json",
                coverage_report="build/coverage.json",
                test_roots=["tests", "checks"],
                test_patterns=["test_*.py", "*_spec.py"],
            ),
            _module_config(value),
        )

    def test_module_config_non_object_has_the_exact_shape_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _module_config

        with self.assertRaises(UsageConfigError) as stopped:
            _module_config(("not", "an", "object"))

        self.assertEqual("projectConfigShapeInvalid", stopped.exception.code)
        self.assertEqual(("projectConfigShapeInvalid",), stopped.exception.args)

    def test_module_config_rejects_missing_or_extra_field_with_exact_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _module_config

        valid = {
            "id": "api",
            "language": "python",
            "root": ".",
            "production": ["app/**/*.py"],
        }
        invalid_values = (
            {key: value for key, value in valid.items() if key != "id"},
            {**valid, "unknown": True},
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(UsageConfigError) as stopped:
                    _module_config(value)

                self.assertEqual("projectConfigShapeInvalid", stopped.exception.code)
                self.assertEqual(("projectConfigShapeInvalid",), stopped.exception.args)

    def test_non_standard_json_number_has_the_exact_config_error(self):
        from sentinel_py.config.loader import _reject_constant
        from sentinel_py.config import UsageConfigError

        with self.assertRaises(UsageConfigError) as stopped:
            _reject_constant("Infinity")

        self.assertEqual("projectConfigInvalid", stopped.exception.code)
        self.assertEqual(("projectConfigInvalid",), stopped.exception.args)

    def test_repository_self_config_classifies_every_first_party_python_file(self):
        from sentinel_py.config import load_project

        loaded = load_project(str(REPOSITORY_ROOT), None, "sentinel-py")
        actual = {
            source.path.relative_to(REPOSITORY_ROOT).as_posix()
            for source in loaded.production_sources
        }
        expected = {
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for root in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT / "scripts")
            for path in root.rglob("*.py")
        }

        self.assertEqual(expected, actual)

    def test_config_symlink_is_rejected_before_loading_its_target(self):
        from sentinel_py.config import UsageConfigError, load_project

        with tempfile.TemporaryDirectory(prefix="sentinel-config-link-") as directory:
            project = Path(directory)
            self._write_source(project)
            target = project / "real-config.json"
            self._write_json(target, _document())
            (project / "sentinel.config.json").symlink_to(target.name)

            with self.assertRaises(UsageConfigError) as stopped:
                load_project(str(project), None, None)

            self.assertEqual("projectConfigPathInvalid", stopped.exception.code)

    def test_duplicate_json_key_is_rejected_without_last_value_wins(self):
        from sentinel_py.config import UsageConfigError, load_project

        with tempfile.TemporaryDirectory(prefix="sentinel-config-duplicate-") as directory:
            project = Path(directory)
            self._write_source(project)
            (project / "sentinel.config.json").write_bytes(
                b'{"specVersion":"1.0.0","specVersion":"2.0.0","modules":[]}'
            )

            with self.assertRaises(UsageConfigError) as stopped:
                load_project(str(project), None, None)

            self.assertEqual("projectConfigDuplicateKey", stopped.exception.code)

    def test_explicit_config_cannot_escape_the_project_root(self):
        from sentinel_py.config import UsageConfigError, load_project

        with tempfile.TemporaryDirectory(prefix="sentinel-config-escape-") as directory:
            parent = Path(directory)
            project = parent / "project"
            project.mkdir()
            self._write_source(project)
            outside = parent / "outside.json"
            self._write_json(outside, _document())

            with self.assertRaises(UsageConfigError) as stopped:
                load_project(str(project), str(outside), None)

            self.assertEqual("projectConfigPathInvalid", stopped.exception.code)

    def test_production_file_symlink_is_not_accepted_as_source(self):
        from sentinel_py.config import UsageConfigError, load_project

        with tempfile.TemporaryDirectory(prefix="sentinel-source-link-") as directory:
            project = Path(directory)
            (project / "app").mkdir()
            target = project / "subject-target.py"
            target.write_bytes(b"def answer():\n    return 42\n")
            (project / "app" / "subject.py").symlink_to(target)
            self._write_json(project / "sentinel.config.json", _document())

            with self.assertRaises(UsageConfigError) as stopped:
                load_project(str(project), None, None)

            self.assertEqual("productionPathInvalid", stopped.exception.code)

    def test_production_source_maps_each_path_failure_to_the_exact_error(self):
        from sentinel_py.config import UsageConfigError
        from sentinel_py.config.loader import _production_source

        with tempfile.TemporaryDirectory(prefix="sentinel-source-errors-") as directory:
            root = Path(directory)
            project = root / "project"
            module_root = project / "module"
            outside_root = root / "outside"
            module_root.mkdir(parents=True)
            outside_root.mkdir()
            outside = outside_root / "subject.py"
            outside.write_bytes(b"def answer():\n    return 42\n")

            relative_failure_module = SimpleNamespace(
                project_root=project,
                root=module_root,
            )
            with self.assertRaises(UsageConfigError) as stopped:
                _production_source(relative_failure_module, outside)
            self.assertEqual("productionPathInvalid", stopped.exception.code)
            self.assertIsInstance(stopped.exception.__cause__, ValueError)

            containment_failure_module = SimpleNamespace(
                project_root=project,
                root=outside_root,
            )
            with self.assertRaises(UsageConfigError) as stopped:
                _production_source(containment_failure_module, outside)
            self.assertEqual("productionPathInvalid", stopped.exception.code)

        candidate = MagicMock()
        resolved = MagicMock()
        relative = MagicMock()
        relative.as_posix.return_value = "\udcff.py"
        resolved.relative_to.return_value = relative
        resolved.is_file.return_value = True
        candidate.resolve.return_value = resolved
        candidate.is_symlink.return_value = False
        module = SimpleNamespace(project_root=Path("/project"), root=Path("/project"))
        with (
            patch("sentinel_py.config.loader._is_within", return_value=True),
            self.assertRaises(UsageConfigError) as stopped,
        ):
            _production_source(module, candidate)
        self.assertEqual("productionPathInvalid", stopped.exception.code)
        self.assertIsInstance(stopped.exception.__cause__, UnicodeEncodeError)

    def test_overlapping_patterns_are_deduplicated_and_utf8_byte_sorted(self):
        from sentinel_py.config import load_project

        with tempfile.TemporaryDirectory(prefix="sentinel-source-order-") as directory:
            project = Path(directory)
            document = _document()
            document["modules"][0]["production"] = ["app/**/*.py", "app/a*.py"]
            (project / "app").mkdir()
            (project / "app" / "한.py").write_bytes(b"def hangul():\n    return 1\n")
            (project / "app" / "alpha.py").write_bytes(b"def alpha():\n    return 1\n")
            self._write_json(project / "sentinel.config.json", document)

            loaded = load_project(str(project), None, None)

            self.assertEqual(
                ("app/alpha.py", "app/한.py"),
                tuple(item.module_relative_path for item in loaded.production_sources),
            )

    @staticmethod
    def _write_source(project: Path) -> None:
        (project / "app").mkdir()
        (project / "app" / "subject.py").write_bytes(
            b"def answer():\n    return 42\n"
        )

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
