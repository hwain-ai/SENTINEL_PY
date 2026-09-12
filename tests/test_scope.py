from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import sentinel_py.config.scope as scope_module

from sentinel_py.config import (
    ModuleConfig,
    ScopeError,
    ScopeEvidence,
    classify_python_scope,
    resolve_python_module,
    resolve_python_modules,
)


def _write(root: Path, relative_path: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("value = 1\n", encoding="utf-8")
    return path


def _config(**changes: object) -> ModuleConfig:
    values: dict[str, object] = {
        "module_id": "api",
        "language": "python",
        "root": ".",
        "production": ("api/**/*.py",),
        "test_command": ("python", "-m", "pytest"),
        "coverage_command": ("python", "-m", "coverage", "json"),
        "coverage_format": "coverage-py-json",
        "coverage_report": "coverage.json",
        "test_roots": ("tests",),
        "test_patterns": ("test_*.py",),
    }
    values.update(changes)
    return ModuleConfig(**values)  # type: ignore[arg-type]


def _resolved(project: Path, **changes: object):
    return resolve_python_module(project, (_config(**changes),))


def test_every_native_python_source_has_exactly_one_classification(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_service.py")
    _write(tmp_path, "generated/schema.py")
    _write(tmp_path, "vendor/library.py")
    _write(tmp_path, "build/generated_client.py")
    _write(tmp_path, "web/not_python.ts")
    module = _resolved(tmp_path)
    evidence = ScopeEvidence(
        verified_tests=(Path("tests/test_service.py"),),
        generated=(Path("generated/schema.py"),),
        vendor=(Path("vendor/library.py"),),
        build_output=(Path("build/generated_client.py"),),
    )

    scope = classify_python_scope(tmp_path, (module,), "api", evidence)

    assert scope.production == (Path("api/service.py"),)
    assert scope.verified_tests == (Path("tests/test_service.py"),)
    assert scope.generated == (Path("generated/schema.py"),)
    assert scope.vendor == (Path("vendor/library.py"),)
    assert scope.build_output == (Path("build/generated_client.py"),)
    assert scope.native_sources == tuple(row.path for row in scope.classifications)
    assert scope.module_id == "api"
    assert {row.category for row in scope.classifications} == {
        "production",
        "verified-test",
        "generated",
        "vendor",
        "build-output",
    }
    rows = {row.path: row for row in scope.classifications}
    assert rows[Path("api/service.py")].module_id == "api"
    assert rows[Path("tests/test_service.py")].module_id is None
    assert Path("web/not_python.ts") not in scope.native_sources


def test_polyglot_project_assigns_only_python_module_production(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "web/view.ts")
    modules = (
        _config(module_id="web", language="typescript", production=("web/**/*.ts",)),
        _config(module_id="api"),
    )
    python_modules = resolve_python_modules(tmp_path, modules)

    scope = classify_python_scope(
        tmp_path,
        python_modules,
        "api",
        ScopeEvidence(),
    )

    assert scope.production == (Path("api/service.py"),)
    assert scope.native_sources == (Path("api/service.py"),)


def test_all_python_modules_are_reconciled_before_selecting_one(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "worker/job.py")
    modules = resolve_python_modules(
        tmp_path,
        (
            _config(module_id="worker", production=("worker/**/*.py",)),
            _config(module_id="api", production=("api/**/*.py",)),
        ),
    )

    scope = classify_python_scope(tmp_path, modules, "api", ScopeEvidence())

    assert scope.production == (Path("api/service.py"),)
    assert tuple(row.module_id for row in scope.production_by_module) == (
        "api",
        "worker",
    )
    assert scope.production_by_module[1].files == (Path("worker/job.py"),)


def test_source_inventory_uses_posix_utf8_path_order(tmp_path: Path) -> None:
    _write(tmp_path, "a-/service.py")
    _write(tmp_path, "a/worker.py")
    module = _resolved(tmp_path, production=("a-/**/*.py", "a/**/*.py"))

    scope = classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert scope.production == (Path("a-/service.py"), Path("a/worker.py"))
    assert scope.native_sources == (Path("a-/service.py"), Path("a/worker.py"))


def test_tool_owned_directories_are_not_treated_as_project_source(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    for relative_path in (
        ".git/hooks/helper.py",
        ".sentinel/state-v1/private.py",
        ".toolchain/venv/dependency.py",
        ".venv/lib/dependency.py",
        "venv/lib/dependency.py",
        ".tox/environment/dependency.py",
        ".nox/environment/dependency.py",
        "mutants/generated.py",
        "api/__pycache__/generated.py",
        "api/.pytest_cache/generated.py",
    ):
        _write(tmp_path, relative_path)
    module = _resolved(tmp_path)

    scope = classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert scope.production == (Path("api/service.py"),)
    assert scope.native_sources == (Path("api/service.py"),)


def test_empty_selected_production_scope_fails_closed(tmp_path: Path) -> None:
    module = _resolved(tmp_path)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "emptyProductionScope"


def test_unclassified_python_source_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "scripts/forgotten.py")
    module = _resolved(tmp_path)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "unclassifiedSource"


def test_source_matching_production_and_test_fails_as_overlap(tmp_path: Path) -> None:
    _write(tmp_path, "api/test_service.py")
    module = _resolved(
        tmp_path,
        test_roots=("api",),
        test_patterns=("test_*.py",),
    )
    evidence = ScopeEvidence(verified_tests=(Path("api/test_service.py"),))

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "scopeOverlap"


def test_two_python_modules_cannot_own_the_same_source(tmp_path: Path) -> None:
    _write(tmp_path, "shared/service.py")
    modules = resolve_python_modules(
        tmp_path,
        (
            _config(module_id="api", production=("shared/**/*.py",)),
            _config(module_id="worker", production=("shared/**/*.py",)),
        ),
    )

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, modules, "api", ScopeEvidence())

    assert stopped.value.code == "scopeOverlap"


def test_partial_structured_test_selection_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_first.py")
    _write(tmp_path, "tests/test_second.py")
    module = _resolved(tmp_path)
    partial = ScopeEvidence(verified_tests=(Path("tests/test_first.py"),))

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", partial)

    assert stopped.value.code == "partialTestSelection"


def test_test_selector_in_configured_argv_fails_before_classification(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_first.py")
    module = _resolved(
        tmp_path,
        test_command=("python", "-m", "pytest", "-k", "first"),
    )
    evidence = ScopeEvidence(verified_tests=(Path("tests/test_first.py"),))

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "partialTestSelection"


@pytest.mark.parametrize(
    "argument",
    ("--ignore=cache=copy", "tests/test_first.py::test_one"),
)
def test_embedded_pytest_selectors_fail_closed(
    tmp_path: Path,
    argument: str,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_first.py")
    module = _resolved(
        tmp_path,
        test_command=("python", "-m", "pytest", argument),
    )
    evidence = ScopeEvidence(verified_tests=(Path("tests/test_first.py"),))

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "partialTestSelection"


def test_full_pytest_output_option_is_not_mistaken_for_partial_selection(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_first.py")
    module = _resolved(
        tmp_path,
        test_command=("python", "-m", "pytest", "-q"),
    )
    evidence = ScopeEvidence(verified_tests=(Path("tests/test_first.py"),))

    scope = classify_python_scope(tmp_path, (module,), "api", evidence)

    assert scope.verified_tests == (Path("tests/test_first.py"),)


def test_explicit_test_file_in_configured_argv_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_first.py")
    module = _resolved(
        tmp_path,
        test_command=("python", "-m", "pytest", "tests/test_first.py"),
    )
    evidence = ScopeEvidence(verified_tests=(Path("tests/test_first.py"),))

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "partialTestSelection"


@pytest.mark.parametrize(
    "evidence",
    (
        ScopeEvidence(generated=(Path("missing.py"),)),
        ScopeEvidence(vendor=(Path("not-python.ts"),)),
        ScopeEvidence(build_output=(Path("../outside.py"),)),
    ),
)
def test_scope_evidence_must_name_discovered_project_python_files(
    tmp_path: Path,
    evidence: ScopeEvidence,
) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "invalidScopeEvidence"


def test_duplicate_scope_evidence_is_not_silently_deduplicated(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "generated/model.py")
    module = _resolved(tmp_path)
    evidence = ScopeEvidence(
        generated=(Path("generated/model.py"), Path("generated/model.py")),
    )

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "duplicateScopeEvidence"


def test_absolute_and_relative_evidence_normalize_to_project_relative_paths(
    tmp_path: Path,
) -> None:
    production = _write(tmp_path, "api/service.py")
    test_file = _write(tmp_path, "tests/test_service.py")
    module = _resolved(tmp_path)
    evidence = ScopeEvidence(verified_tests=(test_file,))

    scope = classify_python_scope(tmp_path, (module,), "api", evidence)

    assert production.relative_to(tmp_path) in scope.production
    assert scope.verified_tests == (Path("tests/test_service.py"),)


def test_selected_module_must_exist_exactly_once_in_resolved_modules(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "worker", ScopeEvidence())

    assert stopped.value.code == "ambiguousModule"


def test_resolved_module_project_root_must_match_classification_root(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)
    mismatched = replace(module, project_root=tmp_path / "different")

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (mismatched,), "api", ScopeEvidence())

    assert stopped.value.code == "invalidModuleRoot"


def test_project_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    module = _resolved(missing)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(missing, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "invalidProjectRoot"


def test_python_symlink_escaping_project_is_not_a_native_source(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "escaped.py").symlink_to(outside)
    module = _resolved(tmp_path, production=("*.py",))

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "invalidNativeSource"


def test_directory_named_like_python_source_is_not_discovered(tmp_path: Path) -> None:
    (tmp_path / "api" / "directory.py").mkdir(parents=True)
    module = _resolved(tmp_path)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "emptyProductionScope"


def test_scope_evidence_container_must_be_an_array(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)
    evidence = ScopeEvidence(generated="api/service.py")  # type: ignore[arg-type]

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "invalidScopeEvidence"


def test_scope_evidence_entry_must_be_a_path(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)
    evidence = ScopeEvidence(generated=("api/service.py",))  # type: ignore[arg-type]

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "invalidScopeEvidence"


def test_non_pytest_command_relies_on_exact_test_inventory_join(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/test_service.py")
    module = _resolved(tmp_path, test_command=("custom-runner", "--all"))
    evidence = ScopeEvidence(verified_tests=(Path("tests/test_service.py"),))

    scope = classify_python_scope(tmp_path, (module,), "api", evidence)

    assert scope.verified_tests == (Path("tests/test_service.py"),)


def test_python_under_test_root_without_approved_naming_is_unclassified(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "tests/helper.py")
    module = _resolved(tmp_path)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "unclassifiedSource"


def test_two_nonproduction_evidence_categories_overlap(tmp_path: Path) -> None:
    _write(tmp_path, "api/service.py")
    _write(tmp_path, "derived/model.py")
    module = _resolved(tmp_path)
    evidence = ScopeEvidence(
        generated=(Path("derived/model.py"),),
        vendor=(Path("derived/model.py"),),
    )

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", evidence)

    assert stopped.value.code == "scopeOverlap"


def test_production_discovered_after_native_inventory_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)

    def changed_production(*_arguments: object) -> set[Path]:
        return {Path("api/service.py"), Path("api/late.py")}

    monkeypatch.setattr(scope_module, "_glob_python_sources", changed_production)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "invalidProductionScope"


def test_test_source_discovered_after_native_inventory_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "api/service.py")
    module = _resolved(tmp_path)

    def changed_tests(*_arguments: object) -> set[Path]:
        return {Path("tests/test_late.py")}

    monkeypatch.setattr(scope_module, "_module_test_candidates", changed_tests)

    with pytest.raises(ScopeError) as stopped:
        classify_python_scope(tmp_path, (module,), "api", ScopeEvidence())

    assert stopped.value.code == "invalidTestScope"
