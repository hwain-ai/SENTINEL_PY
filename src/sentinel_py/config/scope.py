"""Project-wide Python source discovery and exact scope reconciliation."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable

from ..project_files import is_tool_owned_path
from .models import (
    ClassifiedScope,
    ClassifiedSource,
    ModuleProduction,
    ResolvedModule,
    ScopeError,
    ScopeEvidence,
    SourceCategory,
)


_PARTIAL_TEST_FLAGS = frozenset(
    (
        "-k",
        "-m",
        "--deselect",
        "--failed-first",
        "--ff",
        "--ignore",
        "--ignore-glob",
        "--last-failed",
        "--lf",
        "--new-first",
        "--nf",
        "--pyargs",
        "--stepwise",
        "--stepwise-skip",
        "--sw",
    )
)


def classify_python_scope(
    project_root: Path,
    modules: Iterable[ResolvedModule],
    selected_module_id: str,
    evidence: ScopeEvidence,
) -> ClassifiedScope:
    """Assign every discovered .py file to exactly one approved category."""

    project = project_root.resolve()
    resolved_modules = tuple(modules)
    selected = _selected_module(project, resolved_modules, selected_module_id)
    _reject_partial_test_argv(selected.test_command)
    native_sources = _discover_native_sources(project)
    production = _production_inventories(project, resolved_modules, native_sources)
    evidence_sets = _evidence_sets(project, native_sources, evidence)
    test_candidates = _test_candidates(project, resolved_modules, native_sources)
    _verify_full_test_selection(test_candidates, evidence_sets["verified-test"])
    classifications = _classifications(native_sources, production, evidence_sets)
    return _scope_result(selected_module_id, native_sources, production, evidence_sets, classifications)


def _selected_module(
    project: Path,
    modules: tuple[ResolvedModule, ...],
    selected_module_id: str,
) -> ResolvedModule:
    if any(module.project_root != project for module in modules):
        raise ScopeError("invalidModuleRoot")
    matches = tuple(module for module in modules if module.module_id == selected_module_id)
    if len(matches) != 1:
        raise ScopeError("ambiguousModule")
    return matches[0]


def _discover_native_sources(project: Path) -> tuple[Path, ...]:
    if not project.is_dir():
        raise ScopeError("invalidProjectRoot")
    sources = tuple(
        relative
        for path in project.rglob("*.py")
        if path.is_file()
        for relative in (_relative_source(project, path),)
        if not is_tool_owned_path(relative)
    )
    return _stable_paths(sources)


def _relative_source(project: Path, path: Path) -> Path:
    try:
        relative = path.resolve().relative_to(project)
    except ValueError as error:
        raise ScopeError("invalidNativeSource") from error
    return relative


def _production_inventories(
    project: Path,
    modules: tuple[ResolvedModule, ...],
    native_sources: tuple[Path, ...],
) -> dict[str, frozenset[Path]]:
    inventories = {
        module.module_id: _module_production(project, module, native_sources)
        for module in modules
    }
    if any(not paths for paths in inventories.values()):
        raise ScopeError("emptyProductionScope")
    return inventories


def _module_production(
    project: Path,
    module: ResolvedModule,
    native_sources: tuple[Path, ...],
) -> frozenset[Path]:
    discovered = set(native_sources)
    matches: set[Path] = set()
    for pattern in module.production:
        matches.update(_glob_python_sources(project, module.root, pattern))
    if not matches.issubset(discovered):
        raise ScopeError("invalidProductionScope")
    return frozenset(matches)


def _glob_python_sources(project: Path, root: Path, pattern: Path) -> set[Path]:
    matches: set[Path] = set()
    for path in root.glob(pattern.as_posix()):
        if path.is_file() and path.suffix == ".py":
            relative = _relative_source(project, path)
            if not is_tool_owned_path(relative):
                matches.add(relative)
    return matches


def _evidence_sets(
    project: Path,
    native_sources: tuple[Path, ...],
    evidence: ScopeEvidence,
) -> dict[SourceCategory, frozenset[Path]]:
    return {
        "verified-test": _normalize_evidence(project, native_sources, evidence.verified_tests),
        "generated": _normalize_evidence(project, native_sources, evidence.generated),
        "vendor": _normalize_evidence(project, native_sources, evidence.vendor),
        "build-output": _normalize_evidence(project, native_sources, evidence.build_output),
    }


def _normalize_evidence(
    project: Path,
    native_sources: tuple[Path, ...],
    paths: object,
) -> frozenset[Path]:
    if not isinstance(paths, (list, tuple)):
        raise ScopeError("invalidScopeEvidence")
    normalized = tuple(_normalize_evidence_path(project, path) for path in paths)
    if len(normalized) != len(set(normalized)):
        raise ScopeError("duplicateScopeEvidence")
    if not set(normalized).issubset(native_sources):
        raise ScopeError("invalidScopeEvidence")
    return frozenset(normalized)


def _normalize_evidence_path(project: Path, value: object) -> Path:
    if not isinstance(value, Path):
        raise ScopeError("invalidScopeEvidence")
    path = value.resolve() if value.is_absolute() else (project / value).resolve()
    try:
        relative = path.relative_to(project)
    except ValueError as error:
        raise ScopeError("invalidScopeEvidence") from error
    if relative.suffix != ".py":
        raise ScopeError("invalidScopeEvidence")
    return relative


def _test_candidates(
    project: Path,
    modules: tuple[ResolvedModule, ...],
    native_sources: tuple[Path, ...],
) -> frozenset[Path]:
    candidates: set[Path] = set()
    for module in modules:
        candidates.update(_module_test_candidates(project, module))
    if not candidates.issubset(native_sources):
        raise ScopeError("invalidTestScope")
    return frozenset(candidates)


def _module_test_candidates(project: Path, module: ResolvedModule) -> set[Path]:
    candidates: set[Path] = set()
    for root in module.test_roots:
        for path in root.rglob("*.py") if root.is_dir() else ():
            if path.is_file() and _matches_test_pattern(path.name, module.test_patterns):
                candidates.add(_relative_source(project, path))
    return candidates


def _matches_test_pattern(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _verify_full_test_selection(
    candidates: frozenset[Path],
    verified: frozenset[Path],
) -> None:
    if verified != candidates:
        raise ScopeError("partialTestSelection")


def _reject_partial_test_argv(argv: tuple[str, ...]) -> None:
    pytest_args = _pytest_arguments(argv)
    if any(_is_partial_test_argument(argument) for argument in pytest_args):
        raise ScopeError("partialTestSelection")


def _pytest_arguments(argv: tuple[str, ...]) -> tuple[str, ...]:
    try:
        pytest_index = argv.index("pytest")
    except ValueError:
        return ()
    return argv[pytest_index + 1 :]


def _is_partial_test_argument(argument: str) -> bool:
    option = argument.partition("=")[0]
    return option in _PARTIAL_TEST_FLAGS or "::" in argument or argument.endswith(".py")


def _classifications(
    native_sources: tuple[Path, ...],
    production: dict[str, frozenset[Path]],
    evidence: dict[SourceCategory, frozenset[Path]],
) -> tuple[ClassifiedSource, ...]:
    rows = tuple(_classify_source(path, production, evidence) for path in native_sources)
    return rows


def _classify_source(
    path: Path,
    production: dict[str, frozenset[Path]],
    evidence: dict[SourceCategory, frozenset[Path]],
) -> ClassifiedSource:
    owners = tuple(module_id for module_id, files in production.items() if path in files)
    categories = tuple(category for category, files in evidence.items() if path in files)
    label_count = len(owners) + len(categories)
    if label_count == 0:
        raise ScopeError("unclassifiedSource")
    if label_count != 1:
        raise ScopeError("scopeOverlap")
    if owners:
        return ClassifiedSource(path=path, category="production", module_id=owners[0])
    return ClassifiedSource(path=path, category=categories[0], module_id=None)


def _scope_result(
    selected_module_id: str,
    native_sources: tuple[Path, ...],
    production: dict[str, frozenset[Path]],
    evidence: dict[SourceCategory, frozenset[Path]],
    classifications: tuple[ClassifiedSource, ...],
) -> ClassifiedScope:
    inventories = tuple(
        ModuleProduction(module_id=module_id, files=_stable_paths(files))
        for module_id, files in sorted(production.items())
    )
    return ClassifiedScope(
        module_id=selected_module_id,
        production=_stable_paths(production[selected_module_id]),
        production_by_module=inventories,
        verified_tests=_stable_paths(evidence["verified-test"]),
        generated=_stable_paths(evidence["generated"]),
        vendor=_stable_paths(evidence["vendor"]),
        build_output=_stable_paths(evidence["build-output"]),
        native_sources=native_sources,
        classifications=classifications,
    )


def _stable_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(sorted(paths, key=_path_sort_key))

def _path_sort_key(path: Path) -> bytes:
    return path.as_posix().encode()
