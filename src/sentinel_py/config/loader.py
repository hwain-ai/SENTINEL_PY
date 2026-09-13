"""Strict project-config loading and production source discovery."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable

from ..project_files import is_tool_owned_path
from .models import ModuleConfig, ResolvedModule, ScopeEvidence, UsageConfigError
from .resolver import resolve_python_module, resolve_python_modules
from .scope import classify_python_scope


_ROOT_FIELDS = frozenset(("specVersion", "modules"))
_MODULE_FIELDS = frozenset(
    (
        "id",
        "language",
        "root",
        "production",
        "testCommand",
        "coverage",
        "testRoots",
        "testPatterns",
        "excluded",
    )
)
_COVERAGE_FIELDS = frozenset(("command", "format", "report"))


@dataclass(frozen=True)
class ProductionSource:
    """One validated production file and its module-relative identity."""

    path: Path
    module_relative_path: str


@dataclass(frozen=True)
class LoadedProject:
    """Validated inputs required before a quality run may create state."""

    project_root: Path
    config_path: Path
    module: ResolvedModule
    production_sources: tuple[ProductionSource, ...]


def load_project(
    project: str | None,
    config: str | None,
    requested_module: str | None,
) -> LoadedProject:
    """Resolve one Python module and its nonempty production inventory."""

    project_root = _project_root(project)
    config_path = _config_path(project_root, config)
    document = _load_document(config_path)
    modules = _project_modules(document)
    module = resolve_python_module(project_root, modules, requested_module)
    sources = _production_sources(module)
    resolved_modules = resolve_python_modules(project_root, modules)
    _verify_project_scope(project_root, resolved_modules, module, sources)
    return LoadedProject(project_root, config_path, module, sources)


def restrict_production(project: LoadedProject, changed: Iterable[str]) -> LoadedProject | None:
    """Keep only production sources named in ``changed`` (project-relative POSIX paths).

    Returns None when no changed path is a production source: in changed mode there is
    then no changed production code to judge.
    """

    wanted = frozenset(_changed_path(value) for value in changed)
    kept = tuple(
        source
        for source in project.production_sources
        if source.path.relative_to(project.project_root).as_posix() in wanted
    )
    if not kept:
        return None
    return dataclasses.replace(project, production_sources=kept)


def _changed_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value or value.startswith("/"):
        raise UsageConfigError("changedPathInvalid")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UsageConfigError("changedPathInvalid")
    return value


def _project_root(value: str | None) -> Path:
    raw = Path.cwd() if value is None else Path(value)
    try:
        project_root = raw.resolve()
    except (OSError, RuntimeError) as error:
        raise UsageConfigError("projectRootInvalid") from error
    if not project_root.is_dir():
        raise UsageConfigError("projectRootInvalid")
    return project_root


def _config_path(project_root: Path, value: str | None) -> Path:
    raw = Path("sentinel.config.json") if value is None else Path(value)
    candidate = raw if raw.is_absolute() else project_root / raw
    if candidate.is_symlink():
        raise UsageConfigError("projectConfigPathInvalid")
    try:
        path = candidate.resolve()
    except (OSError, RuntimeError) as error:
        raise UsageConfigError("projectConfigPathInvalid") from error
    if not _is_within(path, project_root):
        raise UsageConfigError("projectConfigPathInvalid")
    if not path.is_file():
        raise UsageConfigError("projectConfigNotFound")
    return path


def _load_document(path: Path) -> dict:
    try:
        text = path.read_bytes().decode()
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UsageConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UsageConfigError("projectConfigInvalid") from error
    if not isinstance(document, dict) or set(document) != _ROOT_FIELDS:
        raise UsageConfigError("projectConfigShapeInvalid")
    if document["specVersion"] != "1.0.0":
        raise UsageConfigError("specVersionUnsupported")
    return document


def _unique_object(pairs: Iterable[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsageConfigError("projectConfigDuplicateKey")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise UsageConfigError("projectConfigInvalid")


def _project_modules(document: dict) -> tuple[ModuleConfig, ...]:
    values = document["modules"]
    if not isinstance(values, list) or not values:
        raise UsageConfigError("projectConfigShapeInvalid")
    return tuple(_module_config(value) for value in values)


def _module_config(value: object) -> ModuleConfig:
    required = frozenset(("id", "language", "root", "production"))
    if not isinstance(value, dict):
        raise UsageConfigError("projectConfigShapeInvalid")
    fields = set(value)
    if not required.issubset(fields) or not fields.issubset(_MODULE_FIELDS):
        raise UsageConfigError("projectConfigShapeInvalid")
    coverage = _coverage_config(value.get("coverage"))
    return ModuleConfig(
        module_id=value["id"],
        language=value["language"],
        root=value["root"],
        production=value["production"],
        test_command=value.get("testCommand"),
        coverage_command=coverage.get("command"),
        coverage_format=coverage.get("format"),
        coverage_report=coverage.get("report"),
        test_roots=value.get("testRoots"),
        test_patterns=value.get("testPatterns"),
        excluded=value.get("excluded"),
    )


def _coverage_config(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != _COVERAGE_FIELDS:
        raise UsageConfigError("projectConfigShapeInvalid")
    return value


def _production_sources(module: ResolvedModule) -> tuple[ProductionSource, ...]:
    files = {}
    for pattern in module.production:
        for candidate in module.root.glob(pattern.as_posix()):
            if _is_tool_owned_candidate(module, candidate):
                continue
            source = _production_source(module, candidate)
            files[source.module_relative_path] = source
    if not files:
        raise UsageConfigError("emptyProductionInventory")
    return tuple(files[path] for path in sorted(files))


def _is_tool_owned_candidate(module: ResolvedModule, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(module.project_root)
    except ValueError:
        return False
    return is_tool_owned_path(relative)


def _production_source(
    module: ResolvedModule,
    candidate: Path,
) -> ProductionSource:
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(module.root).as_posix()
    except (OSError, RuntimeError, ValueError) as error:
        raise UsageConfigError("productionPathInvalid") from error
    if candidate.is_symlink() or not resolved.is_file():
        raise UsageConfigError("productionPathInvalid")
    if not _is_within(resolved, module.project_root):
        raise UsageConfigError("productionPathInvalid")
    try:
        relative.encode()
    except UnicodeEncodeError as error:
        raise UsageConfigError("productionPathInvalid") from error
    return ProductionSource(resolved, relative)


def _verify_project_scope(
    project_root: Path,
    modules: tuple[ResolvedModule, ...],
    selected: ResolvedModule,
    sources: tuple[ProductionSource, ...],
) -> None:
    evidence = ScopeEvidence(
        verified_tests=_verified_test_sources(project_root, modules),
        excluded=_excluded_sources(project_root, modules),
    )
    scope = classify_python_scope(
        project_root,
        modules,
        selected.module_id,
        evidence,
    )
    direct = frozenset(source.path.relative_to(project_root) for source in sources)
    if direct != frozenset(scope.production):
        raise UsageConfigError("invalidProductionScope")


def _excluded_sources(
    project_root: Path,
    modules: tuple[ResolvedModule, ...],
) -> tuple[Path, ...]:
    """Project Python files a module declares as neither production nor tests (docs, tooling)."""

    paths = set()
    for module in modules:
        for pattern in module.excluded:
            for candidate in module.root.glob(pattern.as_posix()):
                if candidate.is_symlink() or not candidate.is_file() or candidate.suffix != ".py":
                    continue
                if _is_tool_owned_candidate(module, candidate):
                    continue
                paths.add(candidate.resolve().relative_to(project_root))
    return tuple(sorted(paths, key=_path_sort_key))


def _verified_test_sources(
    project_root: Path,
    modules: tuple[ResolvedModule, ...],
) -> tuple[Path, ...]:
    paths = set()
    for module in modules:
        for root in module.test_roots:
            paths.update(_matching_test_sources(project_root, root, module.test_patterns))
    return tuple(sorted(paths, key=_path_sort_key))


def _matching_test_sources(
    project_root: Path,
    root: Path,
    patterns: tuple[str, ...],
) -> set[Path]:
    if not root.is_dir():
        return set()
    matches = set()
    for path in root.rglob("*.py"):
        if path.is_symlink():
            raise UsageConfigError("invalidTestScope")
        if path.is_file() and any(fnmatchcase(path.name, pattern) for pattern in patterns):
            matches.add(path.resolve().relative_to(project_root))
    return matches


def _path_sort_key(path: Path) -> bytes:
    return path.as_posix().encode()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
