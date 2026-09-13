"""Pure precedence and validation rules for Python module configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .models import (
    ModuleConfig,
    ModuleOverrides,
    PythonDefaults,
    ResolvedModule,
    UsageConfigError,
)


APPROVED_PYTHON_DEFAULTS = PythonDefaults(
    test_command=("python", "-m", "pytest"),
    coverage_command=("python", "-m", "coverage", "json"),
    coverage_format="coverage-py-json",
    coverage_report=Path("coverage.json"),
    test_roots=(Path("tests"),),
    test_patterns=("test_*.py", "*_test.py"),
)


def resolve_python_module(
    project_root: Path,
    modules: Iterable[ModuleConfig],
    requested_module: str | None = None,
    *,
    cli_overrides: ModuleOverrides | None = None,
    defaults: PythonDefaults = APPROVED_PYTHON_DEFAULTS,
) -> ResolvedModule:
    """Select exactly one Python module and apply the approved precedence."""

    candidates = _python_candidates(modules)
    selected = _select_module(candidates, requested_module)
    overrides = cli_overrides or ModuleOverrides()
    return _resolve_module(project_root, selected, overrides, defaults)


def resolve_python_modules(
    project_root: Path,
    modules: Iterable[ModuleConfig],
    *,
    defaults: PythonDefaults = APPROVED_PYTHON_DEFAULTS,
) -> tuple[ResolvedModule, ...]:
    """Resolve every Python module for project-wide ownership reconciliation."""

    candidates = _python_candidates(modules)
    resolved = (
        _resolve_module(project_root, module, ModuleOverrides(), defaults)
        for module in candidates
    )
    return tuple(sorted(resolved, key=_module_sort_key))


def _module_sort_key(module: ResolvedModule) -> bytes:
    return module.module_id.encode()


def _python_candidates(modules: Iterable[ModuleConfig]) -> tuple[ModuleConfig, ...]:
    candidates = tuple(module for module in modules if module.language == "python")
    if not candidates:
        raise UsageConfigError("pythonModuleNotFound")
    _reject_duplicate_ids(candidates)
    return candidates


def _reject_duplicate_ids(modules: tuple[ModuleConfig, ...]) -> None:
    module_ids = tuple(module.module_id for module in modules)
    if len(module_ids) != len(set(module_ids)):
        raise UsageConfigError("ambiguousModule")


def _select_module(
    modules: tuple[ModuleConfig, ...],
    requested_module: str | None,
) -> ModuleConfig:
    if requested_module is None:
        if len(modules) != 1:
            raise UsageConfigError("moduleSelectionRequired")
        return modules[0]
    matches = tuple(module for module in modules if module.module_id == requested_module)
    if len(matches) != 1:
        raise UsageConfigError("pythonModuleNotFound")
    return matches[0]


def _resolve_module(
    project_root: Path,
    module: ModuleConfig,
    overrides: ModuleOverrides,
    defaults: PythonDefaults,
) -> ResolvedModule:
    project = project_root.resolve()
    root = _resolve_module_root(project, module.root)
    production = _production_patterns(_pick(overrides.production, module.production, None))
    return ResolvedModule(
        project_root=project,
        module_id=_required_text(module.module_id, "invalidModuleId"),
        root=root,
        language="python",
        production=production,
        test_command=_argv_value(overrides.test_command, module.test_command, defaults.test_command, "TestCommand"),
        coverage_command=_argv_value(
            overrides.coverage_command,
            module.coverage_command,
            defaults.coverage_command,
            "CoverageCommand",
        ),
        coverage_format=_text_value(
            overrides.coverage_format,
            module.coverage_format,
            defaults.coverage_format,
            "missingCoverageFormat",
        ),
        coverage_report=_report_path(
            root,
            _pick(overrides.coverage_report, module.coverage_report, defaults.coverage_report),
        ),
        test_roots=_root_paths(
            root,
            _pick(overrides.test_roots, module.test_roots, defaults.test_roots),
        ),
        test_patterns=_test_patterns(
            _pick(overrides.test_patterns, module.test_patterns, defaults.test_patterns),
        ),
        excluded=() if module.excluded is None else _production_patterns(module.excluded),
    )


def _pick(override: object | None, configured: object | None, default: object) -> object:
    if override is not None:
        return override
    if configured is not None:
        return configured
    return default


def _argv_value(
    override: object | None,
    configured: object | None,
    default: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    value = _pick(override, configured, default)
    code = f"invalid{field_name}"
    if not isinstance(value, (list, tuple)) or not value:
        raise UsageConfigError(code)
    if not all(_valid_argv_item(item) for item in value):
        raise UsageConfigError(code)
    return tuple(value)


def _valid_argv_item(item: object) -> bool:
    return isinstance(item, str) and bool(item) and "\x00" not in item


def _text_value(
    override: object | None,
    configured: object | None,
    default: str,
    code: str,
) -> str:
    return _required_text(_pick(override, configured, default), code)


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UsageConfigError(code)
    return value


def _resolve_module_root(project_root: Path, value: object) -> Path:
    raw = _path_value(value, "invalidModuleRoot")
    candidate = raw.resolve() if raw.is_absolute() else (project_root / raw).resolve()
    if not _is_within(candidate, project_root):
        raise UsageConfigError("invalidModuleRoot")
    return candidate


def _production_patterns(value: object) -> tuple[Path, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise UsageConfigError("missingProductionScope")
    patterns = tuple(_production_pattern(item) for item in value)
    if len(patterns) != len(set(patterns)):
        raise UsageConfigError("invalidProductionPattern")
    return patterns


def _production_pattern(value: object) -> Path:
    path = _path_value(value, "invalidProductionPattern")
    if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
        raise UsageConfigError("invalidProductionPattern")
    return path


def _report_path(module_root: Path, value: object) -> Path:
    raw = _path_value(value, "missingCoverageReport")
    if raw.is_absolute() or ".." in raw.parts:
        raise UsageConfigError("invalidCoverageReport")
    return (module_root / raw).resolve()


def _root_paths(module_root: Path, value: object) -> tuple[Path, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise UsageConfigError("invalidTestRoots")
    paths = tuple(_relative_root(module_root, item) for item in value)
    if len(paths) != len(set(paths)):
        raise UsageConfigError("invalidTestRoots")
    return paths


def _relative_root(module_root: Path, value: object) -> Path:
    path = _path_value(value, "invalidTestRoots")
    if path.is_absolute() or ".." in path.parts:
        raise UsageConfigError("invalidTestRoots")
    return (module_root / path).resolve()


def _test_patterns(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise UsageConfigError("invalidTestPatterns")
    patterns = tuple(_test_pattern(item) for item in value)
    if len(patterns) != len(set(patterns)):
        raise UsageConfigError("invalidTestPatterns")
    return patterns


def _test_pattern(value: object) -> str:
    pattern = _required_text(value, "invalidTestPatterns")
    if "/" in pattern or "\\" in pattern or not pattern.endswith(".py"):
        raise UsageConfigError("invalidTestPatterns")
    return pattern


def _path_value(value: object, code: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise UsageConfigError(code)
    if not str(value) or "\x00" in str(value):
        raise UsageConfigError(code)
    return Path(value)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
