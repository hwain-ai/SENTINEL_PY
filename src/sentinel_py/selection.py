"""Explicit source/function/test selection; never infer test relevance."""

from dataclasses import replace
from pathlib import Path

from .config import UsageConfigError, restrict_production
from .crap import analyze_source


def select_project(project, files=(), functions=(), tests=()):
    if not (files or functions or tests):
        return project
    if functions and len(files) != 1:
        raise UsageConfigError("functionRequiresOneFile")
    project = _select_files(project, files)
    selected = _select_functions(project, functions)
    selected_tests = tuple(dict.fromkeys(_select_test(project, value) for value in tests))
    return replace(project, selected_functions=selected, selected_tests=selected_tests)


def _select_files(project, files):
    if files:
        requested = set(files)
        known = {source.path.relative_to(project.project_root).as_posix() for source in project.production_sources}
        if not requested <= known:
            raise UsageConfigError("sourceSelectionInvalid")
        project = restrict_production(project, files)
    return project


def _select_functions(project, functions):
    selected = []
    if functions:
        source = project.production_sources[0]
        callables = analyze_source(source.path.read_bytes(), source.module_relative_path)
        for name in functions:
            selected.append(_one_function(callables, name).callable_id)
    return tuple(selected)


def _one_function(callables, name):
    matches = [item for item in callables if name in (item.qualified_name, item.callable_id)]
    if not matches:
        matches = [item for item in callables if item.qualified_name.rsplit(".", 1)[-1] == name]
    if len(matches) != 1:
        raise UsageConfigError("functionSelectionAmbiguous" if matches else "functionSelectionMissing")
    return matches[0]


def _select_test(project, value):
    path = project.project_root / value
    try:
        relative = path.resolve(strict=True).relative_to(project.project_root.resolve()).as_posix()
    except (OSError, ValueError):
        raise UsageConfigError("testSelectionInvalid")
    if not path.is_file() or path.is_symlink() or path.suffix != ".py":
        raise UsageConfigError("testSelectionInvalid")
    if not any(path.resolve().is_relative_to(root.resolve()) for root in project.module.test_roots):
        raise UsageConfigError("testSelectionOutsideTestRoots")
    return relative


def selected_metrics(project, metrics):
    wanted = getattr(project, "selected_functions", ())
    return tuple(item for item in metrics if not wanted or item.callable.callable_id in wanted)


def scope_result(project):
    return {
        "files": [source.path.relative_to(project.project_root).as_posix() for source in project.production_sources],
        "functions": list(project.selected_functions),
        "tests": list(project.selected_tests) or [path.relative_to(project.project_root).as_posix() for path in project.module.test_roots],
        "testSelection": "explicit" if project.selected_tests else "configured",
    }
