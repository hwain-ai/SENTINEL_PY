from __future__ import annotations

from pathlib import Path

import pytest

from sentinel_py.crap import analyze_source
from sentinel_py.config import (
    APPROVED_PYTHON_DEFAULTS,
    ModuleConfig,
    ModuleOverrides,
    PythonDefaults,
    UsageConfigError,
    resolve_python_module,
    resolve_python_modules,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_SOURCE_ROOT = REPOSITORY_ROOT / "src" / "sentinel_py" / "config"


def _module(**changes: object) -> ModuleConfig:
    values: dict[str, object] = {
        "module_id": "api",
        "language": "python",
        "root": ".",
        "production": ("src/**/*.py",),
        "test_command": ("python", "-m", "pytest"),
        "coverage_command": ("python", "-m", "coverage", "json"),
        "coverage_format": "coverage-py-json",
        "coverage_report": "coverage.json",
        "test_roots": ("tests",),
        "test_patterns": ("test_*.py", "*_test.py"),
    }
    values.update(changes)
    return ModuleConfig(**values)  # type: ignore[arg-type]


def _defaults() -> PythonDefaults:
    return PythonDefaults(
        test_command=("default-test",),
        coverage_command=("default-coverage",),
        coverage_format="default-format",
        coverage_report=Path("default-report.json"),
        test_roots=(Path("default-tests"),),
        test_patterns=("default_test_*.py",),
    )


def test_cli_override_wins_over_module_config_and_default(tmp_path: Path) -> None:
    module = _module()
    overrides = ModuleOverrides(
        production=("application/**/*.py",),
        test_command=("cli-test", "--all"),
        coverage_command=("cli-coverage", "--fresh"),
        coverage_format="cli-format",
        coverage_report="generated/cli.json",
        test_roots=("checks",),
        test_patterns=("check_*.py",),
    )

    resolved = resolve_python_module(
        tmp_path,
        (module,),
        cli_overrides=overrides,
        defaults=_defaults(),
    )

    assert resolved.production == (Path("application/**/*.py"),)
    assert resolved.test_command == ("cli-test", "--all")
    assert resolved.coverage_command == ("cli-coverage", "--fresh")
    assert resolved.coverage_format == "cli-format"
    assert resolved.coverage_report == tmp_path / "generated" / "cli.json"
    assert resolved.test_roots == (tmp_path / "checks",)
    assert resolved.test_patterns == ("check_*.py",)


def test_selected_module_config_wins_over_approved_default(tmp_path: Path) -> None:
    resolved = resolve_python_module(tmp_path, (_module(),), defaults=_defaults())

    assert resolved.test_command == ("python", "-m", "pytest")
    assert resolved.coverage_command == ("python", "-m", "coverage", "json")
    assert resolved.coverage_format == "coverage-py-json"
    assert resolved.coverage_report == tmp_path / "coverage.json"
    assert resolved.test_roots == (tmp_path / "tests",)
    assert resolved.test_patterns == ("test_*.py", "*_test.py")


def test_approved_defaults_fill_only_omitted_module_values(tmp_path: Path) -> None:
    module = _module(
        test_command=None,
        coverage_command=None,
        coverage_format=None,
        coverage_report=None,
        test_roots=None,
        test_patterns=None,
    )

    resolved = resolve_python_module(tmp_path, (module,))

    assert resolved.test_command == APPROVED_PYTHON_DEFAULTS.test_command
    assert resolved.coverage_command == APPROVED_PYTHON_DEFAULTS.coverage_command
    assert resolved.coverage_format == APPROVED_PYTHON_DEFAULTS.coverage_format
    assert resolved.coverage_report == tmp_path / APPROVED_PYTHON_DEFAULTS.coverage_report
    assert resolved.test_roots == (tmp_path / "tests",)
    assert resolved.test_patterns == APPROVED_PYTHON_DEFAULTS.test_patterns


@pytest.mark.parametrize("field", ("test_command", "coverage_command"))
def test_shell_command_string_is_rejected_in_module_config(
    tmp_path: Path,
    field: str,
) -> None:
    module = _module(**{field: "python -m pytest"})

    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (module,))

    assert stopped.value.code == f"invalid{field.title().replace('_', '')}"


def test_shell_command_string_is_rejected_in_cli_override(tmp_path: Path) -> None:
    overrides = ModuleOverrides(test_command="python -m pytest")  # type: ignore[arg-type]

    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (_module(),), cli_overrides=overrides)

    assert stopped.value.code == "invalidTestCommand"


def test_argv_is_copied_to_an_immutable_tuple(tmp_path: Path) -> None:
    test_command = ["python", "-m", "pytest"]
    coverage_command = ["python", "-m", "coverage", "json"]
    module = _module(
        test_command=test_command,
        coverage_command=coverage_command,
    )

    resolved = resolve_python_module(tmp_path, (module,))
    test_command.append("tests/test_only_one.py")
    coverage_command.clear()

    assert resolved.test_command == ("python", "-m", "pytest")
    assert resolved.coverage_command == ("python", "-m", "coverage", "json")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("test_command", (), "invalidTestCommand"),
        ("test_command", ("python", ""), "invalidTestCommand"),
        ("coverage_command", ("python", 1), "invalidCoverageCommand"),
        ("coverage_format", "", "missingCoverageFormat"),
        ("coverage_report", "", "missingCoverageReport"),
        ("test_roots", (), "invalidTestRoots"),
        ("test_patterns", (), "invalidTestPatterns"),
    ),
)
def test_invalid_selected_values_do_not_fall_back_to_defaults(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (_module(**{field: value}),))

    assert stopped.value.code == code
    assert str(stopped.value) == code


def test_one_python_module_is_selected_without_claiming_typescript(
    tmp_path: Path,
) -> None:
    modules = (
        _module(),
        _module(
            module_id="web",
            language="typescript",
            root="web",
            production=("src/**/*.ts",),
        ),
    )

    resolved = resolve_python_module(tmp_path, modules)

    assert resolved.module_id == "api"
    assert resolved.language == "python"


def test_multiple_python_modules_require_an_explicit_selection(tmp_path: Path) -> None:
    modules = (_module(module_id="api"), _module(module_id="worker"))

    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, modules)

    assert stopped.value.code == "moduleSelectionRequired"


def test_requested_module_must_be_an_exact_python_module(tmp_path: Path) -> None:
    modules = (
        _module(module_id="api"),
        _module(module_id="web", language="typescript"),
    )

    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, modules, requested_module="web")

    assert stopped.value.code == "pythonModuleNotFound"


def test_requested_python_module_is_selected_exactly(tmp_path: Path) -> None:
    modules = (_module(module_id="worker"), _module(module_id="api"))

    resolved = resolve_python_module(tmp_path, modules, requested_module="api")

    assert resolved.module_id == "api"


def test_project_without_a_python_module_fails_closed(tmp_path: Path) -> None:
    modules = (_module(module_id="web", language="typescript"),)

    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, modules)

    assert stopped.value.code == "pythonModuleNotFound"


def test_duplicate_python_module_id_is_ambiguous(tmp_path: Path) -> None:
    modules = (_module(), _module(root="worker"))

    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, modules, requested_module="api")

    assert stopped.value.code == "ambiguousModule"


def test_all_python_modules_are_resolved_in_stable_id_order(tmp_path: Path) -> None:
    modules = (
        _module(module_id="worker", root="worker"),
        _module(module_id="web", language="typescript", root="web"),
        _module(module_id="api", root="api"),
    )

    resolved = resolve_python_modules(tmp_path, modules)

    assert tuple(module.module_id for module in resolved) == ("api", "worker")


def test_production_scope_cannot_be_omitted_or_empty(tmp_path: Path) -> None:
    for production in (None, (), []):
        with pytest.raises(UsageConfigError) as stopped:
            resolve_python_module(tmp_path, (_module(production=production),))

        assert stopped.value.code == "missingProductionScope"


def test_duplicate_production_pattern_is_not_silently_deduplicated(
    tmp_path: Path,
) -> None:
    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(
            tmp_path,
            (_module(production=("src/**/*.py", "src/**/*.py")),),
        )

    assert stopped.value.code == "invalidProductionPattern"


@pytest.mark.parametrize("root", ("../outside", "/tmp/outside"))
def test_module_root_cannot_escape_the_project(tmp_path: Path, root: str) -> None:
    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (_module(root=root),))

    assert stopped.value.code == "invalidModuleRoot"


def test_absolute_module_root_inside_project_is_canonicalized(tmp_path: Path) -> None:
    module_root = tmp_path / "api"

    resolved = resolve_python_module(tmp_path, (_module(root=module_root),))

    assert resolved.root == module_root


@pytest.mark.parametrize("pattern", ("../hidden.py", "/absolute/*.py", "src/*.ts"))
def test_production_pattern_must_be_relative_python_source(
    tmp_path: Path,
    pattern: str,
) -> None:
    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (_module(production=(pattern,)),))

    assert stopped.value.code == "invalidProductionPattern"


@pytest.mark.parametrize("report", ("../coverage.json", "/tmp/coverage.json"))
def test_coverage_report_must_be_module_relative(
    tmp_path: Path,
    report: str,
) -> None:
    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (_module(coverage_report=report),))

    assert stopped.value.code == "invalidCoverageReport"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("production", (7,), "invalidProductionPattern"),
        ("test_roots", ("tests", "tests"), "invalidTestRoots"),
        ("test_roots", ("../tests",), "invalidTestRoots"),
        ("test_roots", ("/tmp/tests",), "invalidTestRoots"),
        ("test_roots", (7,), "invalidTestRoots"),
        ("test_patterns", ("test_*.py", "test_*.py"), "invalidTestPatterns"),
        ("test_patterns", ("tests/test_*.py",), "invalidTestPatterns"),
        ("test_patterns", ("tests\\test_*.py",), "invalidTestPatterns"),
        ("test_patterns", ("test_*",), "invalidTestPatterns"),
        ("test_patterns", (7,), "invalidTestPatterns"),
        ("test_patterns", ("",), "invalidTestPatterns"),
        ("root", 7, "invalidModuleRoot"),
        ("module_id", "", "invalidModuleId"),
    ),
)
def test_invalid_path_and_pattern_boundaries_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    with pytest.raises(UsageConfigError) as stopped:
        resolve_python_module(tmp_path, (_module(**{field: value}),))

    assert stopped.value.code == code


def test_nul_byte_is_rejected_from_argv_and_paths(tmp_path: Path) -> None:
    cases = (
        (_module(test_command=("python", "bad\x00argument")), "invalidTestCommand"),
        (_module(root="bad\x00root"), "invalidModuleRoot"),
        (_module(module_id="bad\x00module"), "invalidModuleId"),
        (_module(test_patterns=("bad\x00.py",)), "invalidTestPatterns"),
    )
    for module, code in cases:
        with pytest.raises(UsageConfigError) as stopped:
            resolve_python_module(tmp_path, (module,))

        assert stopped.value.code == code


def test_config_core_has_no_anonymous_callable_with_ambiguous_line_coverage() -> None:
    callables = tuple(
        callable_
        for source_path in sorted(CONFIG_SOURCE_ROOT.glob("*.py"))
        for callable_ in analyze_source(
            source_path.read_bytes(),
            source_path.relative_to(REPOSITORY_ROOT).as_posix(),
        )
    )

    assert not tuple(callable_ for callable_ in callables if callable_.kind == "lambda")
