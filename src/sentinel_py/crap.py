"""Python callable discovery, cyclomatic complexity, and exact CRAP math."""

from __future__ import annotations

import ast
import codecs
import hashlib
import io
import math
import re
import tokenize
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import List, Optional, Sequence, Tuple

from .rendering import render_canonical_decimal


MAX_SAFE_INTEGER = 9_007_199_254_740_991
_UTF8_BOM = bytes((0xEF, 0xBB, 0xBF))
_LF = bytes((10,))
_NEWLINE_PATTERN = re.compile(bytes((13, 10)) + b"|" + bytes((13,)) + b"|" + _LF)
_UTF8_ENCODINGS = frozenset(("utf-8", "utf-8-sig"))


class AnalysisError(ValueError):
    """Raised when Python source cannot be analyzed without guessing."""


class UnsupportedCallableError(AnalysisError):
    """Raised when source contains a callable kind outside this implementation slice."""


class AmbiguousCallableError(AnalysisError):
    """Raised when two callables produce the same structural descriptor."""


@dataclass(frozen=True)
class SourceRange:
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class IdentityDescriptor:
    module_relative_path: str
    kind: str
    qualified_name: str
    normalized_signature: str
    semantic_discriminator: Optional[str]


@dataclass(frozen=True)
class CallableDefinition:
    module_relative_path: str
    source_digest: str
    kind: str
    qualified_name: str
    normalized_signature: str
    source_range: SourceRange
    declaration_line: int
    body_start_line: int
    body_end_line: int
    excluded_line_ranges: Tuple[Tuple[int, int], ...]
    complexity: int
    semantic_anchor: Optional[str]
    semantic_discriminator: Optional[str]

    @property
    def identity_descriptor(self) -> IdentityDescriptor:
        return IdentityDescriptor(
            module_relative_path=self.module_relative_path,
            kind=self.kind,
            qualified_name=self.qualified_name,
            normalized_signature=self.normalized_signature,
            semantic_discriminator=self.semantic_discriminator,
        )

    @property
    def callable_id(self) -> str:
        payload = _identity_payload(self.identity_descriptor)
        return "python:v1:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CrapResult:
    complexity: int
    covered: int
    total: int
    numerator: Optional[int]
    denominator: Optional[int]
    decimal: Optional[str]
    passed: Optional[bool]
    unknown_reason: Optional[str]


@dataclass(frozen=True)
class _Owner:
    category: str
    name: str
    node: Optional[ast.AST] = None


@dataclass(frozen=True)
class _CollectedCallable:
    node: ast.AST
    kind: str
    qualified_name: str
    callable_ancestors: Tuple[ast.AST, ...]
    semantic_anchor: Optional[str]
    semantic_context: Tuple[ast.AST, ...] = ()


@dataclass(frozen=True)
class _ParentEdge:
    parent: ast.AST
    field_name: str
    index: Optional[int]


def normalize_module_path(module_relative_path: str) -> str:
    _require_non_empty_module_path(module_relative_path)
    _require_utf8_posix_module_path(module_relative_path)
    _require_normalized_relative_module_path(module_relative_path)
    return module_relative_path


def _require_non_empty_module_path(module_relative_path: str) -> None:
    if not isinstance(module_relative_path, str) or not module_relative_path:
        raise AnalysisError("module path must be a non-empty string")


def _require_utf8_posix_module_path(module_relative_path: str) -> None:
    if "\\" in module_relative_path or "\x00" in module_relative_path:
        raise AnalysisError("module path must use normalized POSIX separators")
    try:
        module_relative_path.encode()
    except UnicodeEncodeError as error:
        raise AnalysisError("module path must be valid UTF-8") from error


def _require_normalized_relative_module_path(module_relative_path: str) -> None:
    path = PurePosixPath(module_relative_path)
    if path.is_absolute() or str(path) != module_relative_path:
        raise AnalysisError("module path must be normalized and project-relative")
    if not path.parts or ".." in path.parts:
        raise AnalysisError("module path must not contain empty, dot, or parent segments")


def _build_parent_edges(tree: ast.AST) -> dict:
    edges = {}
    for parent in ast.walk(tree):
        for field_name, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                _add_parent_edge(edges, value, parent, field_name, None)
                continue
            if isinstance(value, list):
                _add_parent_list_edges(edges, value, parent, field_name)
    return edges


def _add_parent_list_edges(edges: dict, values: list, parent: ast.AST, field_name: str) -> None:
    for index, value in enumerate(values):
        if isinstance(value, ast.AST):
            _add_parent_edge(edges, value, parent, field_name, index)


def _add_parent_edge(
    edges: dict,
    child: ast.AST,
    parent: ast.AST,
    field_name: str,
    index: Optional[int],
) -> None:
    if child in edges:
        if isinstance(
            child,
            (ast.boolop, ast.cmpop, ast.expr_context, ast.operator, ast.unaryop),
        ):
            return
        raise AnalysisError("AST node has more than one parent")
    edges[child] = _ParentEdge(parent, field_name, index)


def _lambda_semantic_anchor(
    node: ast.Lambda,
    edges: dict,
    owner_nodes: Tuple[ast.AST, ...],
) -> str:
    components = []
    current: ast.AST = node
    while current in edges:
        edge = edges[current]
        if edge.parent in owner_nodes:
            if isinstance(edge.parent, ast.Lambda):
                components.append("lambda-result")
            break
        component = _lambda_role_component(edge)
        if component is not None:
            components.append(component)
        current = edge.parent
    if not components:
        raise AmbiguousCallableError("lambda has no position-independent semantic anchor")
    return "/".join(reversed(components))


def _lambda_semantic_context(node: ast.Lambda, edges: dict) -> Tuple[ast.AST, ...]:
    context = []
    current: ast.AST = node
    declaration_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    while current in edges:
        current = edges[current].parent
        if isinstance(current, declaration_types):
            context.append(current)
    return tuple(reversed(context))


def _lambda_role_component(edge: _ParentEdge) -> Optional[str]:
    parent = edge.parent
    binding_role = _binding_lambda_role(parent, edge.field_name)
    if binding_role is not None:
        return binding_role
    return _container_lambda_role(parent, edge)


def _binding_lambda_role(parent: ast.AST, field_name: str) -> Optional[str]:
    if isinstance(parent, ast.Assign) and field_name == "value":
        return "binding:" + _canonical_ast_sequence(parent.targets)
    if isinstance(parent, (ast.AnnAssign, ast.NamedExpr)) and field_name == "value":
        return "binding:" + _canonical_ast(parent.target)
    return None


def _container_lambda_role(parent: ast.AST, edge: _ParentEdge) -> Optional[str]:
    if isinstance(parent, ast.arguments):
        return _argument_default_role(parent, edge)
    if isinstance(parent, ast.arg):
        return "parameter-annotation:" + parent.arg
    if isinstance(parent, ast.keyword) and edge.field_name == "value":
        return _keyword_lambda_role(parent)
    if isinstance(parent, ast.Call):
        return _call_role(parent, edge)
    if isinstance(parent, ast.Dict):
        return _dict_role(parent, edge)
    return _simple_lambda_role(parent, edge.field_name)


def _keyword_lambda_role(keyword: ast.keyword) -> str:
    name = keyword.arg if keyword.arg is not None else "unpack"
    return "keyword:" + name


def _argument_default_role(arguments: ast.arguments, edge: _ParentEdge) -> Optional[str]:
    if edge.index is None:
        return None
    if edge.field_name == "defaults":
        positional = list(arguments.posonlyargs) + list(arguments.args)
        first_default = len(positional) - len(arguments.defaults)
        return "parameter-default:" + positional[first_default + edge.index].arg
    if edge.field_name == "kw_defaults":
        return "parameter-default:" + arguments.kwonlyargs[edge.index].arg
    return None


def _call_role(call: ast.Call, edge: _ParentEdge) -> Optional[str]:
    callee = _canonical_ast(call.func)
    if edge.field_name == "args":
        return "call-positional:" + callee
    if edge.field_name == "keywords":
        return "call:" + callee
    if edge.field_name == "func":
        return "immediate-callee"
    return None


def _dict_role(dictionary: ast.Dict, edge: _ParentEdge) -> Optional[str]:
    if edge.field_name != "values" or edge.index is None:
        return None
    key = dictionary.keys[edge.index]
    if key is None:
        return "dictionary-unpack"
    return "property:" + _canonical_ast(key)


def _simple_lambda_role(parent: ast.AST, field_name: str) -> Optional[str]:
    expression_roles = {
        (ast.Return, "value"): "return",
        (ast.Yield, "value"): "yield",
        (ast.YieldFrom, "value"): "yield-from",
    }
    expression_role = expression_roles.get((type(parent), field_name))
    if expression_role is not None:
        return expression_role
    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _function_declaration_role(parent, field_name)
    if isinstance(parent, ast.ClassDef):
        return _class_declaration_role(parent, field_name)
    return None


def _function_declaration_role(node: ast.AST, field_name: str) -> Optional[str]:
    if field_name == "decorator_list":
        return "function-decorator:" + getattr(node, "name")
    if field_name in ("args", "returns", "type_params"):
        return "function-declaration:" + getattr(node, "name")
    return None


def _class_declaration_role(node: ast.ClassDef, field_name: str) -> Optional[str]:
    if field_name == "decorator_list":
        return "class-decorator:" + node.name
    if field_name in ("bases", "keywords", "type_params"):
        return "class-declaration:" + node.name
    return None


def _canonical_ast(node: ast.AST) -> str:
    return ast.dump(node)


def _canonical_ast_sequence(nodes: Sequence[ast.AST]) -> str:
    return "[" + ",".join(_canonical_ast(node) for node in nodes) + "]"


class _CallableCollector(ast.NodeVisitor):
    def __init__(self, tree: ast.AST) -> None:
        self.owners: List[_Owner] = []
        self.items: List[_CollectedCallable] = []
        self.parent_edges = _build_parent_edges(tree)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_definition_expressions(node)
        self.owners.append(_Owner("class", node.name))
        for statement in node.body:
            self.visit(statement)
        self.owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_named_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_named_callable(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        anchor = _lambda_semantic_anchor(
            node,
            self.parent_edges,
            tuple(owner.node for owner in self.owners if owner.node is not None),
        )
        name = "<lambda:" + hashlib.sha256(anchor.encode()).hexdigest() + ">"
        self.items.append(
            _CollectedCallable(
                node=node,
                kind="lambda",
                qualified_name=self._qualified_name(name),
                callable_ancestors=self._callable_ancestors(),
                semantic_anchor=anchor,
                semantic_context=_lambda_semantic_context(node, self.parent_edges),
            )
        )
        self.owners.append(_Owner("callable", name, node))
        self.visit(node.body)
        self.owners.pop()

    def _visit_named_callable(self, node: ast.AST) -> None:
        self._visit_definition_expressions(node)
        name = getattr(node, "name")
        self.items.append(
            _CollectedCallable(
                node=node,
                kind=self._callable_kind(isinstance(node, ast.AsyncFunctionDef)),
                qualified_name=self._qualified_name(name),
                callable_ancestors=self._callable_ancestors(),
                semantic_anchor=None,
            )
        )
        self.owners.append(_Owner("callable", name, node))
        for statement in getattr(node, "body"):
            self.visit(statement)
        self.owners.pop()

    def _visit_definition_expressions(self, node: ast.AST) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword)

    def _callable_kind(self, asynchronous: bool) -> str:
        nearest_owner = self.owners[-1].category if self.owners else None
        if nearest_owner == "class":
            return "async-method" if asynchronous else "method"
        if any(owner.category == "callable" for owner in self.owners):
            return "nested-async-function" if asynchronous else "nested-function"
        return "async-function" if asynchronous else "function"

    def _qualified_name(self, name: str) -> str:
        segments: List[str] = []
        for owner in self.owners:
            segments.append(owner.name)
            if owner.category == "callable":
                segments.append("<locals>")
        segments.append(name)
        return ".".join(segments)

    def _callable_ancestors(self) -> Tuple[ast.AST, ...]:
        return tuple(
            owner.node for owner in self.owners if owner.category == "callable"
        )


class _DecisionCounter(ast.NodeVisitor):
    def __init__(self) -> None:
        self.decisions = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def visit_If(self, node: ast.If) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.decisions += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.decisions += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.decisions += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.AST) -> None:
        self.visit(node.subject)
        for case in node.cases:
            pattern = case.pattern
            is_wildcard = (
                type(pattern).__name__ == "MatchAs"
                and pattern.pattern is None
                and pattern.name is None
            )
            if not is_wildcard:
                self.decisions += 1
            self.visit(pattern)
            guard = case.guard
            if guard is not None:
                self.decisions += 1
                self.visit(guard)
            for statement in case.body:
                self.visit(statement)


def analyze_source(source: bytes, module_relative_path: str) -> Tuple[CallableDefinition, ...]:
    module_relative_path = normalize_module_path(module_relative_path)
    if not isinstance(source, bytes):
        raise AnalysisError("source must be UTF-8 bytes")
    _validate_source_encoding(source)
    has_utf8_bom = source.startswith(_UTF8_BOM)
    tree = _parse_source_tree(source, module_relative_path)

    collector = _CallableCollector(tree)
    collector.visit(tree)
    line_starts = _line_start_offsets(source, 3 if has_utf8_bom else 0)
    source_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    definitions: List[CallableDefinition] = []

    for item in collector.items:
        definitions.append(
            _build_callable_definition(
                item,
                collector.items,
                source,
                line_starts,
                module_relative_path,
                source_digest,
            )
        )

    resolved = _resolve_identity_collisions(definitions, collector.items)
    _require_unique_identity_descriptors(resolved)
    return tuple(sorted(resolved, key=_source_start_byte))


def _parse_source_tree(source: bytes, module_relative_path: str) -> ast.AST:
    try:
        tree = ast.parse(source)
        compile(tree, module_relative_path, "exec")
    except SyntaxError as error:
        raise AnalysisError(f"source has invalid Python syntax: {error.msg}") from error
    return tree


def _resolve_identity_collisions(
    definitions: Sequence[CallableDefinition],
    items: Sequence[_CollectedCallable],
) -> List[CallableDefinition]:
    groups = {}
    for index, definition in enumerate(definitions):
        key = _base_identity_key(definition)
        groups.setdefault(key, []).append(index)

    resolved = list(definitions)
    for indices in groups.values():
        if len(indices) == 1:
            continue
        _resolve_identity_group(resolved, items, indices)
    return resolved


def _resolve_identity_group(
    definitions: List[CallableDefinition],
    items: Sequence[_CollectedCallable],
    indices: Sequence[int],
) -> None:
    if any(items[index].kind == "lambda" for index in indices):
        _resolve_anonymous_identity_group(definitions, items, indices)
        return
    hashes = [_semantic_declaration_hash(items[index].node) for index in indices]
    if len(hashes) != len(set(hashes)):
        raise AmbiguousCallableError("duplicate callable declaration is ambiguous")
    for index, semantic_hash in zip(indices, hashes):
        definitions[index] = replace(
            definitions[index],
            semantic_discriminator=semantic_hash,
        )


def _resolve_anonymous_identity_group(
    definitions: List[CallableDefinition],
    items: Sequence[_CollectedCallable],
    indices: Sequence[int],
) -> None:
    context_hashes = [
        _semantic_context_hash(items[index].semantic_context) for index in indices
    ]
    if len(context_hashes) != len(set(context_hashes)):
        raise AmbiguousCallableError("anonymous callable identity is ambiguous")
    for index, context_hash in zip(indices, context_hashes):
        definitions[index] = replace(
            definitions[index],
            semantic_discriminator=context_hash,
        )


def _semantic_context_hash(context: Sequence[ast.AST]) -> str:
    payload = b"\x00".join(
        _semantic_declaration_hash(node).encode() for node in context
    )
    return hashlib.sha256(b"sentinel-python-context-v1\x00" + payload).hexdigest()


def _base_identity_key(definition: CallableDefinition) -> tuple:
    return (
        definition.module_relative_path,
        definition.kind,
        definition.qualified_name,
        definition.normalized_signature,
    )


def _semantic_declaration_hash(node: ast.AST) -> str:
    return hashlib.sha256(_canonical_ast(node).encode()).hexdigest()


def _require_unique_identity_descriptors(
    definitions: Sequence[CallableDefinition],
) -> None:
    seen = set()
    for definition in definitions:
        descriptor = definition.identity_descriptor
        if descriptor in seen:
            raise AmbiguousCallableError("duplicate callable identity descriptor")
        seen.add(descriptor)


def _build_callable_definition(
    item: _CollectedCallable,
    all_items: Sequence[_CollectedCallable],
    source: bytes,
    line_starts: Sequence[int],
    module_relative_path: str,
    source_digest: str,
) -> CallableDefinition:
    node = item.node
    body = _callable_body_nodes(node)
    return CallableDefinition(
        module_relative_path=module_relative_path,
        source_digest=source_digest,
        kind=item.kind,
        qualified_name=item.qualified_name,
        normalized_signature=_normalized_signature(node),
        source_range=_source_range(node, source, line_starts),
        declaration_line=_required_position(node, "lineno"),
        body_start_line=_required_position(body[0], "lineno"),
        body_end_line=_required_position(node, "end_lineno"),
        excluded_line_ranges=_excluded_line_ranges(node, all_items),
        complexity=_callable_complexity(body),
        semantic_anchor=item.semantic_anchor,
        semantic_discriminator=None,
    )


def _excluded_line_ranges(
    node: ast.AST,
    all_items: Sequence[_CollectedCallable],
) -> Tuple[Tuple[int, int], ...]:
    ranges = set()
    for other in all_items:
        if node not in other.callable_ancestors:
            continue
        declaration_line = _required_position(other.node, "lineno")
        body = _callable_body_nodes(other.node)
        body_start_line = _required_position(body[0], "lineno")
        body_end_line = _required_position(other.node, "end_lineno")
        exclusion_start = max(body_start_line, declaration_line + 1)
        if exclusion_start <= body_end_line:
            ranges.add((exclusion_start, body_end_line))
    return tuple(sorted(ranges))


def _callable_body_nodes(node: ast.AST) -> Tuple[ast.AST, ...]:
    body = getattr(node, "body")
    if isinstance(body, list):
        return tuple(body)
    if isinstance(body, ast.AST):
        return (body,)
    raise AnalysisError("callable body is missing")


def _callable_complexity(body: Sequence[ast.AST]) -> int:
    counter = _DecisionCounter()
    for statement in body:
        counter.visit(statement)
    return 1 + counter.decisions


def _source_start_byte(item: CallableDefinition) -> int:
    return item.source_range.start_byte


def _identity_payload(descriptor: IdentityDescriptor) -> bytes:
    fields = (
        descriptor.module_relative_path,
        descriptor.kind,
        descriptor.qualified_name,
        descriptor.normalized_signature,
        descriptor.semantic_discriminator or "",
    )
    encoded = [b"sentinel-python-callable-v1"]
    for field in fields:
        value = field.encode()
        encoded.extend((str(len(value)).encode(), b":", value))
    return b"\x00".join(encoded)


def _validate_source_encoding(source: bytes) -> None:
    try:
        source.decode()
    except UnicodeDecodeError as error:
        raise AnalysisError("source is not valid UTF-8") from error
    detection_source = _NEWLINE_PATTERN.sub(_LF, source)
    try:
        detected_encoding, _ = tokenize.detect_encoding(
            io.BytesIO(detection_source).readline
        )
    except SyntaxError as error:
        raise AnalysisError(f"source has invalid encoding declaration: {error.msg}") from error
    canonical_encoding = codecs.lookup(detected_encoding).name
    if canonical_encoding not in _UTF8_ENCODINGS:
        raise AnalysisError("source encoding declaration must resolve to UTF-8")


def calculate_crap(
    complexity: int,
    covered: int,
    total: int,
    unknown_reason: Optional[str] = None,
) -> CrapResult:
    _require_integer("complexity", complexity, minimum=1)
    _require_integer("covered", covered, minimum=0)
    _require_integer("total", total, minimum=0)
    if covered > total:
        raise ValueError("covered must not exceed total")

    if unknown_reason is not None:
        if not isinstance(unknown_reason, str) or not unknown_reason:
            raise ValueError("unknown_reason must be a non-empty string")
        return CrapResult(
            complexity=complexity,
            covered=covered,
            total=total,
            numerator=None,
            denominator=None,
            decimal=None,
            passed=None,
            unknown_reason=unknown_reason,
        )
    if total == 0:
        raise ValueError("total must be positive when coverage is known")

    denominator = total**3
    numerator = complexity**2 * (total - covered) ** 3 + complexity * denominator
    divisor = math.gcd(numerator, denominator)
    reduced_numerator = numerator // divisor
    reduced_denominator = denominator // divisor
    return CrapResult(
        complexity=complexity,
        covered=covered,
        total=total,
        numerator=reduced_numerator,
        denominator=reduced_denominator,
        decimal=render_canonical_decimal(reduced_numerator, reduced_denominator),
        passed=numerator <= 8 * denominator,
        unknown_reason=None,
    )


def _line_start_offsets(source: bytes, content_start: int) -> Tuple[int, ...]:
    newline_ends = (
        match.end() for match in _NEWLINE_PATTERN.finditer(source)
    )
    return (content_start, *newline_ends)


def _source_range(
    node: ast.AST,
    source: bytes,
    line_starts: Sequence[int],
) -> SourceRange:
    start = _byte_offset(
        _required_position(node, "lineno"),
        _required_position(node, "col_offset"),
        source,
        line_starts,
    )
    end = _byte_offset(
        _required_position(node, "end_lineno"),
        _required_position(node, "end_col_offset"),
        source,
        line_starts,
    )
    if start >= end:
        raise AnalysisError("callable source range must be non-empty")
    return SourceRange(start, end)


def _required_position(node: ast.AST, name: str) -> int:
    value = getattr(node, name, None)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AnalysisError(f"AST node is missing integer {name}")
    return value


def _byte_offset(
    line: int,
    column: int,
    source: bytes,
    line_starts: Sequence[int],
) -> int:
    if line < 1 or line > len(line_starts) or column < 0:
        raise AnalysisError("AST source position is outside the source")
    start = line_starts[line - 1]
    limit = line_starts[line] if line < len(line_starts) else len(source)
    offset = start + column
    if offset > limit:
        raise AnalysisError("AST byte column is outside its source line")
    return offset


def _normalized_signature(node: ast.AST) -> str:
    arguments = getattr(node, "args")
    positional = list(arguments.posonlyargs) + list(arguments.args)
    padded_defaults = [None] * (len(positional) - len(arguments.defaults)) + list(
        arguments.defaults
    )
    pieces = [
        _format_argument(argument, default)
        for argument, default in zip(positional, padded_defaults)
    ]
    if arguments.posonlyargs:
        pieces.insert(len(arguments.posonlyargs), "/")
    if arguments.vararg is not None:
        pieces.append("*" + _format_argument(arguments.vararg, None))
    elif arguments.kwonlyargs:
        pieces.append("*")
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        pieces.append(_format_argument(argument, default))
    if arguments.kwarg is not None:
        pieces.append("**" + _format_argument(arguments.kwarg, None))

    result = "(" + ",".join(pieces) + ")"
    returns = getattr(node, "returns", None)
    if returns is not None:
        result += "->" + ast.dump(returns)
    return result


def _format_argument(argument: ast.arg, default: Optional[ast.AST]) -> str:
    result = argument.arg
    if argument.annotation is not None:
        result += ":" + ast.dump(argument.annotation)
    if default is not None:
        result += "=" + ast.dump(default)
    return result


def _require_integer(name: str, value: int, minimum: int) -> None:
    invalid_type = not isinstance(value, int) or isinstance(value, bool)
    if invalid_type or value < minimum or value > MAX_SAFE_INTEGER:
        raise ValueError(
            f"{name} must be an integer from {minimum} through {MAX_SAFE_INTEGER}"
        )
