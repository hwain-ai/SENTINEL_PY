"""Public pure config and source-scope contract for SENTINEL_PY."""

from .models import (
    ClassifiedScope,
    ClassifiedSource,
    ModuleConfig,
    ModuleOverrides,
    ModuleProduction,
    PythonDefaults,
    ResolvedModule,
    ScopeError,
    ScopeEvidence,
    SourceCategory,
    UsageConfigError,
)
from .loader import LoadedProject, ProductionSource, load_project
from .resolver import (
    APPROVED_PYTHON_DEFAULTS,
    resolve_python_module,
    resolve_python_modules,
)
from .scope import classify_python_scope

__all__ = [
    "APPROVED_PYTHON_DEFAULTS",
    "ClassifiedScope",
    "ClassifiedSource",
    "ModuleConfig",
    "ModuleOverrides",
    "ModuleProduction",
    "PythonDefaults",
    "LoadedProject",
    "ProductionSource",
    "ResolvedModule",
    "ScopeError",
    "ScopeEvidence",
    "SourceCategory",
    "UsageConfigError",
    "classify_python_scope",
    "load_project",
    "resolve_python_module",
    "resolve_python_modules",
]
