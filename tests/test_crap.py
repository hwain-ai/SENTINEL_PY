import ast
import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.crap import (  # noqa: E402
    AmbiguousCallableError,
    AnalysisError,
    CrapResult,
    _build_parent_edges,
    _byte_offset,
    _callable_body_nodes,
    _require_integer,
    _require_unique_identity_descriptors,
    _required_position,
    _semantic_declaration_hash,
    _source_range,
    analyze_source,
    calculate_crap,
)


FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "callables.py"
MODULE_PATH = "tests/fixtures/callables.py"


class CallableInventoryTests(unittest.TestCase):
    def setUp(self):
        self.source = FIXTURE_PATH.read_bytes()
        self.callables = analyze_source(self.source, MODULE_PATH)

    def test_finds_each_supported_callable_kind(self):
        actual = {
            item.qualified_name: item.kind
            for item in self.callables
        }

        self.assertEqual(
            {
                "plain": "function",
                "async_work": "async-function",
                "Worker.run": "method",
                "outer": "function",
                "outer.<locals>.inner": "nested-function",
            },
            actual,
        )

    def test_builds_unique_structural_identity_descriptors(self):
        descriptors = [item.identity_descriptor for item in self.callables]

        self.assertEqual(len(descriptors), len(set(descriptors)))
        plain = next(item for item in self.callables if item.qualified_name == "plain")
        self.assertEqual(MODULE_PATH, plain.identity_descriptor.module_relative_path)
        self.assertEqual(
            "sha256:" + hashlib.sha256(self.source).hexdigest(),
            plain.source_digest,
        )
        self.assertEqual("function", plain.identity_descriptor.kind)
        self.assertEqual("plain", plain.identity_descriptor.qualified_name)
        self.assertEqual("(flag,items)", plain.normalized_signature)

    def test_fixed_fixture_has_exact_stable_callable_ids_and_metadata(self):
        actual = {
            item.qualified_name: (
                item.callable_id,
                item.kind,
                item.source_range.start_byte,
                item.source_range.end_byte,
                item.declaration_line,
                item.body_start_line,
                item.body_end_line,
                item.excluded_line_ranges,
                item.complexity,
            )
            for item in self.callables
        }

        self.assertEqual(
            {
                "plain": (
                    "python:v1:3464a33efbe3fee59eeaf18ef2c0a643b65794f60474abd62e467c3287add1f3",
                    "function", 0, 85, 1, 2, 4, (), 3,
                ),
                "async_work": (
                    "python:v1:4c2dbd51d3fecf2b3636d1b02f0e6fa8e6b0cd3485fddff4cb52a450956fbee4",
                    "async-function", 88, 169, 7, 8, 10, (), 2,
                ),
                "Worker.run": (
                    "python:v1:4009f27d9452fabd34eda69d757f474beef95bf92bca355cdb13bbe0fde9869b",
                    "method", 190, 313, 14, 15, 18, (), 3,
                ),
                "outer": (
                    "python:v1:ff8557f6e85f21012315cf9be6f6cd8b7cf628f145e96198a6f218b10e91e890",
                    "function", 316, 469, 21, 22, 27, ((23, 25),), 3,
                ),
                "outer.<locals>.inner": (
                    "python:v1:df7d5c40ec9cbd56d618ed3805f8244cb16ac58579869deb0d4549865c79a376",
                    "nested-function", 339, 416, 22, 23, 25, (), 2,
                ),
            },
            actual,
        )

    def test_uses_zero_based_utf8_byte_half_open_source_ranges(self):
        prefix = "이름 = 1\n\n"
        function = "def sample():\n    return 이름"
        source = (prefix + function).encode("utf-8")

        result = analyze_source(source, "sample.py")[0]

        self.assertEqual(len(prefix.encode("utf-8")), result.source_range.start_byte)
        self.assertEqual(len(source), result.source_range.end_byte)

    def test_accepts_utf8_bom_and_keeps_its_three_raw_bytes_in_the_range(self):
        source = b"\xef\xbb\xbfdef sample():\n    return 1\n"

        result = analyze_source(source, "sample.py")[0]

        self.assertEqual(3, result.source_range.start_byte)
        self.assertEqual(len(source) - 1, result.source_range.end_byte)

    def test_accepts_utf8_cookie_with_and_without_utf8_bom(self):
        cookie = b"# coding: utf-8\n"
        function = b"def sample():\n    return 1\n"

        for prefix in (b"", b"\xef\xbb\xbf"):
            with self.subTest(prefix=prefix):
                source = prefix + cookie + function

                result = analyze_source(source, "sample.py")[0]

                self.assertEqual(len(prefix) + len(cookie), result.source_range.start_byte)
                self.assertEqual(len(source) - 1, result.source_range.end_byte)

    def test_rejects_unknown_pep_263_encoding_cookie(self):
        source = b"# coding: sentinel-unknown\ndef sample():\n    return 1\n"

        with self.assertRaises(AnalysisError):
            analyze_source(source, "sample.py")

    def test_rejects_utf8_bom_with_latin_1_cookie(self):
        source = b"\xef\xbb\xbf# coding: latin-1\ndef sample():\n    return 1\n"

        with self.assertRaises(AnalysisError):
            analyze_source(source, "sample.py")

    def test_rejects_effective_non_utf8_encoding_cookie(self):
        sources = (
            b"# coding: latin-1\ndef sample():\n    return 1\n",
            "# coding: latin-1\ndef sample():\n    return \"é\"  # padding\n".encode(
                "utf-8"
            ),
        )

        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(AnalysisError):
                    analyze_source(source, "sample.py")

    def test_accepts_utf8_alias_and_preserves_multibyte_raw_range(self):
        function = 'def sample():\n    return "é"'
        for cookie in ("utf-8", "UTF-8", "utf_8", "utf8"):
            with self.subTest(cookie=cookie):
                prefix = f"# coding: {cookie}\n"
                source = (prefix + function + "  \n").encode("utf-8")

                result = analyze_source(source, "sample.py")[0]

                self.assertEqual(len(prefix.encode("utf-8")), result.source_range.start_byte)
                self.assertEqual(
                    len((prefix + function).encode("utf-8")),
                    result.source_range.end_byte,
                )

    def test_ignores_third_logical_line_cookie_with_lone_cr_newlines(self):
        prefix = b"# first\r# second\r# coding: latin-1\r"
        function = "def sample():\r    return 'é'".encode("utf-8")
        source = prefix + function

        result = analyze_source(source, "sample.py")[0]

        self.assertEqual(len(prefix), result.source_range.start_byte)
        self.assertEqual(len(source), result.source_range.end_byte)

    def test_maps_lf_crlf_and_lone_cr_to_exact_raw_byte_ranges(self):
        prefix = "이름 = 1".encode("utf-8")
        function = b"def sample():\n    return 1"

        for newline in (b"\n", b"\r\n", b"\r"):
            with self.subTest(newline=newline):
                source = prefix + newline + function.replace(b"\n", newline) + newline

                result = analyze_source(source, "sample.py")[0]

                self.assertEqual(len(prefix) + len(newline), result.source_range.start_byte)
                self.assertEqual(len(source) - len(newline), result.source_range.end_byte)

    def test_distinguishes_same_name_redefinitions_by_semantic_hash(self):
        source = b"def duplicate(value):\n    return value\n\ndef duplicate(value):\n    return value + 1\n"

        definitions = analyze_source(source, "duplicate.py")

        self.assertEqual(2, len(definitions))
        self.assertEqual(
            (
                (
                    "a50370559563ea3ed9e01a99de87985e13bfe3647bf2b9e6a3bf07f0ecc7f97c",
                    "python:v1:2d8b785e257a412f5d376fffb441e49a284b6fce5d723dfcf6c909d696fbc5ea",
                ),
                (
                    "b34de773310079eb337b7cf07125b9d669c6239d103abb8a0b573301d1ffa45f",
                    "python:v1:7ee86cefaee781e64ad31d481beb0e7596e364efbbfdaf912eb3f59b4d90897c",
                ),
            ),
            tuple(
                (item.semantic_discriminator, item.callable_id)
                for item in definitions
            ),
        )

    def test_rejects_semantically_identical_same_name_redefinitions(self):
        source = b"def duplicate(value):\n    return value\n\ndef duplicate(value):\n    return value\n"

        with self.assertRaisesRegex(
            AmbiguousCallableError,
            "^duplicate callable declaration is ambiguous$",
        ):
            analyze_source(source, "duplicate.py")

    def test_rejects_duplicate_group_after_an_unrelated_unique_callable(self):
        source = (
            b"def unique():\n"
            b"    return 1\n\n"
            b"def duplicate(value):\n"
            b"    return value\n\n"
            b"def duplicate(value):\n"
            b"    return value\n"
        )

        with self.assertRaisesRegex(
            AmbiguousCallableError,
            "^duplicate callable declaration is ambiguous$",
        ):
            analyze_source(source, "duplicate_after_unique.py")

    def test_inventories_bound_call_argument_and_nested_lambdas(self):
        source = (
            b"bound = lambda value: value + 1\n"
            b"registered = register(lambda item: item if item else 0)\n"
            b"def outer():\n"
            b"    return lambda flag: (lambda value: value)(1) if flag else 0\n"
        )

        definitions = analyze_source(source, "lambda_case.py")
        lambdas = [item for item in definitions if item.kind == "lambda"]

        self.assertEqual(4, len(lambdas))
        self.assertEqual(4, len({item.callable_id for item in lambdas}))
        self.assertEqual((1, 2, 2, 1), tuple(item.complexity for item in lambdas))
        outer_lambda = next(item for item in lambdas if item.semantic_anchor == "return")
        inner_lambda = next(
            item for item in lambdas if item.semantic_anchor.startswith("lambda-result")
        )
        self.assertTrue(
            inner_lambda.qualified_name.startswith(
                outer_lambda.qualified_name + ".<locals>."
            )
        )

    def test_inventories_semantic_lambda_roles_without_source_positions(self):
        source = (
            b"def configured(callback=lambda: 1, *, keyword=lambda: 2) -> (lambda: 3):\n"
            b"    return callback\n\n"
            b"def annotated(value: (lambda: int)):\n"
            b"    return value\n\n"
            b"@(lambda target: target)\n"
            b"def decorated():\n"
            b"    return 1\n\n"
            b"@(lambda target: target)\n"
            b"class Decorated:\n"
            b"    pass\n\n"
            b"mapping = {'handler': lambda: 4}\n"
            b"spread = {**(lambda: {})}\n"
            b"registered = register(handler=lambda: 5)\n"
            b"expanded = register(**(lambda: {}))\n"
            b"keyed = {(lambda: 'key'): 'value'}\n"
            b"first = second = lambda: 6\n"
        )

        lambdas = [
            item
            for item in analyze_source(source, "lambda_roles.py")
            if item.kind == "lambda"
        ]

        self.assertEqual(
            {
                "function-declaration:configured/parameter-default:callback",
                "function-declaration:configured/parameter-default:keyword",
                "function-declaration:configured",
                "function-decorator:decorated",
                "class-decorator:Decorated",
                "function-declaration:annotated/parameter-annotation:value",
                "binding:[Name(id='mapping', ctx=Store())]/property:Constant(value='handler')",
                "binding:[Name(id='spread', ctx=Store())]/dictionary-unpack",
                "binding:[Name(id='registered', ctx=Store())]/call:Name(id='register', ctx=Load())/keyword:handler",
                "binding:[Name(id='expanded', ctx=Store())]/call:Name(id='register', ctx=Load())/keyword:unpack",
                "binding:[Name(id='keyed', ctx=Store())]",
                "binding:[Name(id='first', ctx=Store()),Name(id='second', ctx=Store())]",
            },
            {item.semantic_anchor for item in lambdas},
        )

    def test_distinguishes_each_positional_lambda_default(self):
        source = (
            b"def configured(required, first=lambda: 1, second=lambda: 2):\n"
            b"    return first, second\n"
        )

        anchors = tuple(
            item.semantic_anchor
            for item in analyze_source(source, "lambda_defaults.py")
            if item.kind == "lambda"
        )

        self.assertEqual(
            (
                "function-declaration:configured/parameter-default:first",
                "function-declaration:configured/parameter-default:second",
            ),
            anchors,
        )

    def test_rejects_unanchored_lambda_in_variable_annotation(self):
        with self.assertRaisesRegex(
            AmbiguousCallableError,
            "^lambda has no position-independent semantic anchor$",
        ):
            analyze_source(
                b"annotated: (lambda: object) = 1\n",
                "lambda_annotation.py",
            )

    def test_inventories_every_supported_lambda_container_role(self):
        source = (
            b"annotated: object = lambda: 1\n"
            b"walrus = (callback := lambda: 2)\n"
            b"positional = register(lambda: 3)\n"
            b"immediate = (lambda: (lambda: 4))()\n\n"
            b"def generated():\n"
            b"    yield lambda: 5\n"
            b"    yield from (lambda: ())\n\n"
            b"def returned():\n"
            b"    return lambda: 6\n\n"
            b"class Based((lambda: object), metaclass=(lambda: type)):\n"
            b"    pass\n\n"
            b"def generic[T: (lambda: object)]():\n"
            b"    return 1\n\n"
            b"class Generic[T: (lambda: object)]:\n"
            b"    pass\n"
        )

        anchors = {
            item.semantic_anchor
            for item in analyze_source(source, "lambda_containers.py")
            if item.kind == "lambda"
        }

        self.assertEqual(
            {
                "binding:Name(id='annotated', ctx=Store())",
                "binding:[Name(id='walrus', ctx=Store())]/binding:Name(id='callback', ctx=Store())",
                "binding:[Name(id='positional', ctx=Store())]/call-positional:Name(id='register', ctx=Load())",
                "binding:[Name(id='immediate', ctx=Store())]/immediate-callee",
                "lambda-result",
                "return",
                "yield",
                "yield-from",
                "class-declaration:Based",
                "class-declaration:Based/keyword:metaclass",
                "function-declaration:generic",
                "class-declaration:Generic",
            },
            anchors,
        )

    def test_classifies_async_methods_and_nested_async_functions(self):
        source = (
            b"class Worker:\n"
            b"    async def run(self):\n"
            b"        return 1\n\n"
            b"def outer():\n"
            b"    async def inner():\n"
            b"        return 1\n"
            b"    return inner\n"
        )

        actual = {
            item.qualified_name: item.kind
            for item in analyze_source(source, "async_kinds.py")
        }

        self.assertEqual(
            {
                "Worker.run": "async-method",
                "outer": "function",
                "outer.<locals>.inner": "nested-async-function",
            },
            actual,
        )

    def test_normalizes_every_python_parameter_kind_and_annotation(self):
        source = (
            b"def signature(a: int, /, b=1, *args: str, "
            b"c: bool=True, **kwargs: bytes) -> str:\n"
            b"    return str(a)\n"
        )

        definition = analyze_source(source, "signature.py")[0]

        self.assertEqual(
            "(a:Name(id='int', ctx=Load()),/,b=Constant(value=1),"
            "*args:Name(id='str', ctx=Load()),"
            "c:Name(id='bool', ctx=Load())=Constant(value=True),"
            "**kwargs:Name(id='bytes', ctx=Load()))"
            "->Name(id='str', ctx=Load())",
            definition.normalized_signature,
        )

    def test_normalizes_keyword_only_marker_without_varargs(self):
        definition = analyze_source(
            b"def keyword_only(*, value: int = 1):\n    return value\n",
            "keyword_only.py",
        )[0]

        self.assertEqual(
            "(*,value:Name(id='int', ctx=Load())=Constant(value=1))",
            definition.normalized_signature,
        )

    def test_lambda_identity_survives_line_insertion_and_binding_reorder(self):
        first = (
            b"alpha = lambda value: value + 1\n"
            b"beta = lambda value, flag=False: value if flag else 0\n"
        )
        moved = (
            b"# inserted line\n\n"
            b"beta = lambda value, flag=False: value if flag else 0\n"
            b"alpha = lambda value: value + 1\n"
        )

        first_ids = {
            item.semantic_anchor: item.callable_id
            for item in analyze_source(first, "lambda_case.py")
        }
        moved_ids = {
            item.semantic_anchor: item.callable_id
            for item in analyze_source(moved, "lambda_case.py")
        }

        self.assertEqual(first_ids, moved_ids)

    def test_rejects_lambdas_without_a_position_independent_anchor(self):
        source = b"(lambda value: value)\n"

        with self.assertRaisesRegex(
            AmbiguousCallableError,
            "^lambda has no position-independent semantic anchor$",
        ):
            analyze_source(source, "lambda_case.py")

    def test_rejects_duplicate_anonymous_descriptor_without_numbering_it(self):
        source = (
            b"handlers = [\n"
            b"    lambda value: value + 1,\n"
            b"    lambda value: value + 2,\n"
            b"]\n"
        )

        with self.assertRaisesRegex(
            AmbiguousCallableError,
            "^anonymous callable identity is ambiguous$",
        ):
            analyze_source(source, "lambda_case.py")

    def test_uses_parent_discriminator_for_lambdas_in_named_redefinitions(self):
        source = (
            b"def duplicate():\n"
            b"    return lambda value: value\n\n"
            b"def duplicate():\n"
            b"    return lambda value: value + 1\n"
        )

        definitions = analyze_source(source, "lambda_case.py")
        lambdas = [item for item in definitions if item.kind == "lambda"]

        self.assertEqual(
            (
                (
                    "return",
                    "0a21b194e8660857a000e13c1783d18c89a73df017c572c6c8020f9fa4672177",
                    "python:v1:39f0d4ee2eafa533b2963ce654bc63b0ba7ff60cd53c4f3094d690638d122d45",
                ),
                (
                    "return",
                    "bb9f2103c4d08ec62ee5a072148154986a5d1d7bc18323e5d923e7a9fd260f85",
                    "python:v1:5ab834f18d79fe7cf5490b48e111a6f2940664d8d7c1b05e5c2ae3f0f6f32781",
                ),
            ),
            tuple(
                (item.semantic_anchor, item.semantic_discriminator, item.callable_id)
                for item in lambdas
            ),
        )

    def test_nested_lambda_discriminator_uses_nul_between_context_hashes(self):
        source = (
            b"def outer():\n"
            b"    def repeated():\n"
            b"        return lambda: 1\n"
            b"    def repeated():\n"
            b"        return lambda: 2\n"
            b"    return repeated\n"
        )
        tree = ast.parse(source)
        outer = tree.body[0]
        nested = (outer.body[0], outer.body[1])
        prefix = b"sentinel-python-context-v1\x00"
        outer_hash = _semantic_declaration_hash(outer).encode()
        expected = {
            hashlib.sha256(
                prefix + outer_hash + b"\x00" + _semantic_declaration_hash(node).encode()
            ).hexdigest()
            for node in nested
        }

        lambdas = tuple(
            item
            for item in analyze_source(source, "nested_context.py")
            if item.kind == "lambda"
        )

        self.assertEqual(expected, {item.semantic_discriminator for item in lambdas})

    def test_duplicate_descriptor_guard_has_an_exact_failure(self):
        definition = self.callables[0]

        with self.assertRaisesRegex(
            AmbiguousCallableError,
            "^duplicate callable identity descriptor$",
        ):
            _require_unique_identity_descriptors((definition, definition))

    def test_rejects_invalid_utf8_source(self):
        with self.assertRaises(AnalysisError):
            analyze_source(b"\xff", "invalid.py")

    def test_rejects_syntax_error(self):
        with self.assertRaisesRegex(
            AnalysisError,
            "^source has invalid Python syntax: invalid syntax$",
        ):
            analyze_source(b"def broken(:\n    pass\n", "broken.py")

    def test_rejects_missing_ast_end_position(self):
        tree = ast.Module(
            body=[
                ast.FunctionDef(
                    name="missing_end",
                    args=ast.arguments(
                        posonlyargs=[],
                        args=[],
                        vararg=None,
                        kwonlyargs=[],
                        kw_defaults=[],
                        kwarg=None,
                        defaults=[],
                    ),
                    body=[ast.Pass(lineno=2, col_offset=4)],
                    decorator_list=[],
                    lineno=1,
                    col_offset=0,
                )
            ],
            type_ignores=[],
        )

        with patch("sentinel_py.crap.ast.parse", return_value=tree), patch(
            "builtins.compile"
        ):
            with self.assertRaisesRegex(
                AnalysisError,
                "^AST node is missing integer end_lineno$",
            ):
                analyze_source(b"def missing_end():\n    pass\n", "missing_end.py")

    def test_rejects_ast_only_source_that_the_python_compiler_rejects(self):
        invalid_sources = (
            b"return 1\ndef valid():\n    pass\n",
            b"break\ndef valid():\n    pass\n",
            b"continue\ndef valid():\n    pass\n",
            b"nonlocal missing\ndef valid():\n    pass\n",
            b"def duplicate(value, value):\n    pass\n",
        )

        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(AnalysisError):
                    analyze_source(source, "invalid_context.py")

    def test_rejects_non_normalized_module_path(self):
        for path in ("/absolute.py", "./relative.py", "a//b.py", "a\\b.py", "a/../b.py"):
            with self.subTest(path=path):
                with self.assertRaises(AnalysisError):
                    analyze_source(b"def valid():\n    pass\n", path)

    def test_rejects_dot_as_a_module_path(self):
        with self.assertRaisesRegex(
            AnalysisError,
            "^module path must not contain empty, dot, or parent segments$",
        ):
            analyze_source(b"def valid():\n    pass\n", ".")

    def test_public_validation_errors_have_exact_stable_messages(self):
        cases = (
            (b"def valid():\n    pass\n", "", "module path must be a non-empty string"),
            (b"def valid():\n    pass\n", "a\\b.py", "module path must use normalized POSIX separators"),
            (b"def valid():\n    pass\n", "bad\x00.py", "module path must use normalized POSIX separators"),
            (b"def valid():\n    pass\n", "bad\ud800.py", "module path must be valid UTF-8"),
            (b"def valid():\n    pass\n", "/absolute.py", "module path must be normalized and project-relative"),
            (b"def valid():\n    pass\n", "..", "module path must not contain empty, dot, or parent segments"),
            ("not-bytes", "valid.py", "source must be UTF-8 bytes"),
            (b"\xff", "valid.py", "source is not valid UTF-8"),
            (b"# coding: sentinel-unknown\ndef valid():\n    pass\n", "valid.py", "source has invalid encoding declaration: unknown encoding: sentinel-unknown"),
            (b"# coding: latin-1\ndef valid():\n    pass\n", "valid.py", "source encoding declaration must resolve to UTF-8"),
        )

        for source, path, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(AnalysisError) as raised:
                    analyze_source(source, path)
                self.assertEqual(expected, str(raised.exception))

    def test_low_level_numeric_boundaries_are_exact(self):
        maximum = 9_007_199_254_740_991

        self.assertIsNone(_require_integer("value", maximum, 0))
        with self.assertRaisesRegex(
            ValueError,
            "^value must be an integer from 0 through 9007199254740991$",
        ):
            _require_integer("value", maximum + 1, 0)
        with self.assertRaisesRegex(
            AnalysisError,
            "^AST source position is outside the source$",
        ):
            _byte_offset(0, 0, b"x", (0,))
        with self.assertRaisesRegex(
            AnalysisError,
            "^AST byte column is outside its source line$",
        ):
            _byte_offset(1, 2, b"x", (0,))
        with self.assertRaisesRegex(
            AnalysisError,
            "^AST source position is outside the source$",
        ):
            _byte_offset(2, 0, b"x", (0,))

    def test_low_level_ast_invariants_fail_closed_with_exact_messages(self):
        shared = ast.Constant(value=1)
        tree = ast.Module(
            body=[
                ast.Expr(ast.Tuple(elts=[shared, shared], ctx=ast.Load())),
            ],
            type_ignores=[],
        )

        with self.assertRaisesRegex(
            AnalysisError,
            "^AST node has more than one parent$",
        ):
            _build_parent_edges(tree)
        bodyless = ast.Pass()
        bodyless.body = None
        with self.assertRaisesRegex(
            AnalysisError,
            "^callable body is missing$",
        ):
            _callable_body_nodes(bodyless)
        with self.assertRaisesRegex(
            AnalysisError,
            "^AST node is missing integer lineno$",
        ):
            _required_position(ast.Pass(), "lineno")

        empty = ast.Pass(
            lineno=1,
            col_offset=0,
            end_lineno=1,
            end_col_offset=0,
        )
        with self.assertRaisesRegex(
            AnalysisError,
            "^callable source range must be non-empty$",
        ):
            _source_range(empty, b"x", (0,))


class ComplexityTests(unittest.TestCase):
    def test_counts_decisions_without_nested_double_counting(self):
        actual = {
            item.qualified_name: item.complexity
            for item in analyze_source(FIXTURE_PATH.read_bytes(), MODULE_PATH)
        }

        self.assertEqual(
            {
                "plain": 3,
                "async_work": 2,
                "Worker.run": 3,
                "outer": 3,
                "outer.<locals>.inner": 2,
            },
            actual,
        )

    def test_counts_each_supported_decision_syntax(self):
        source = (
            b"def branch(flag):\n"
            b"    if flag:\n"
            b"        return 1\n"
            b"    return 0\n\n"
            b"def loop(items):\n"
            b"    for item in items:\n"
            b"        pass\n"
            b"    while items:\n"
            b"        break\n\n"
            b"async def async_loop(items):\n"
            b"    async for item in items:\n"
            b"        pass\n\n"
            b"def handler(operation):\n"
            b"    try:\n"
            b"        return operation()\n"
            b"    except ValueError:\n"
            b"        return 0\n\n"
            b"def conditional(flag):\n"
            b"    return 1 if flag else 0\n\n"
            b"def boolean(first, second, third):\n"
            b"    return first and second or third\n\n"
            b"def comprehension(rows):\n"
            b"    return [value for row in rows if row for value in row if value]\n"
        )

        actual = {
            item.qualified_name: item.complexity
            for item in analyze_source(source, "decision_matrix.py")
        }

        self.assertEqual(
            {
                "branch": 2,
                "loop": 3,
                "async_loop": 2,
                "handler": 2,
                "conditional": 2,
                "boolean": 3,
                "comprehension": 5,
            },
            actual,
        )

    def test_counts_repeated_decisions_of_the_same_syntax(self):
        source = (
            b"def repeated(flags, rows):\n"
            b"    if flags:\n"
            b"        pass\n"
            b"    if rows:\n"
            b"        pass\n"
            b"    for flag in flags:\n"
            b"        pass\n"
            b"    for row in rows:\n"
            b"        pass\n"
            b"    while flags:\n"
            b"        break\n"
            b"    while rows:\n"
            b"        break\n"
            b"    return (1 if flags else 0) + (1 if rows else 0)\n\n"
            b"async def repeated_async(first, second):\n"
            b"    async for item in first:\n"
            b"        pass\n"
            b"    async for item in second:\n"
            b"        pass\n"
        )

        actual = {
            item.qualified_name: item.complexity
            for item in analyze_source(source, "repeated_decisions.py")
        }

        self.assertEqual({"repeated": 9, "repeated_async": 3}, actual)

    @unittest.skipUnless(hasattr(ast, "Match"), "match syntax requires Python 3.10+")
    def test_counts_multiple_non_wildcard_match_cases(self):
        source = (
            b"def classify(value):\n"
            b"    match value:\n"
            b"        case 1:\n"
            b"            return 'one'\n"
            b"        case 2:\n"
            b"            return 'two'\n"
            b"        case _:\n"
            b"            return 'other'\n"
        )

        result = analyze_source(source, "match_cases.py")[0]

        self.assertEqual(3, result.complexity)

    @unittest.skipUnless(hasattr(ast, "Match"), "match syntax requires Python 3.10+")
    def test_counts_only_non_wildcard_match_cases(self):
        source = (
            "def classify(value):\n"
            "    match value:\n"
            "        case 1:\n"
            "            return 'one'\n"
            "        case _:\n"
            "            return 'other'\n"
        ).encode("utf-8")

        result = analyze_source(source, "match_case.py")[0]

        self.assertEqual(2, result.complexity)

    @unittest.skipUnless(hasattr(ast, "Match"), "match syntax requires Python 3.10+")
    def test_counts_each_match_subject_decision_exactly_once(self):
        cases = (
            ("(value if flag else 0)", "1", 3),
            ("(left and right)", "True", 3),
            ("[item for item in items if item]", "[]", 4),
        )

        for subject, pattern, expected in cases:
            with self.subTest(subject=subject):
                source = (
                    "def classify(value=None, flag=None, left=None, right=None, items=()):\n"
                    f"    match {subject}:\n"
                    f"        case {pattern}:\n"
                    "            return True\n"
                    "        case _:\n"
                    "            return False\n"
                ).encode("utf-8")

                result = analyze_source(source, "match_subject.py")[0]

                self.assertEqual(expected, result.complexity)

    @unittest.skipUnless(hasattr(ast, "Match"), "match syntax requires Python 3.10+")
    def test_counts_match_guard_as_a_branch(self):
        source = (
            "def classify(value, ready, enabled):\n"
            "    match value:\n"
            "        case _ if ready and enabled:\n"
            "            return True\n"
            "        case _:\n"
            "            return False\n"
        ).encode("utf-8")

        result = analyze_source(source, "match_guard.py")[0]

        self.assertEqual(3, result.complexity)

    @unittest.skipUnless(hasattr(ast, "Match"), "match syntax requires Python 3.10+")
    def test_match_guard_accumulates_after_multiple_case_decisions(self):
        source = (
            b"def classify(value, ready):\n"
            b"    match value:\n"
            b"        case 1:\n"
            b"            return 'one'\n"
            b"        case 2:\n"
            b"            return 'two'\n"
            b"        case _ if ready:\n"
            b"            return 'ready'\n"
            b"        case _:\n"
            b"            return 'other'\n"
        )

        result = analyze_source(source, "match_guard_accumulation.py")[0]

        self.assertEqual(4, result.complexity)


class ExactCrapTests(unittest.TestCase):
    def test_calculates_reduced_fraction_without_float(self):
        result = calculate_crap(complexity=4, covered=3, total=4)

        self.assertEqual(17, result.numerator)
        self.assertEqual(4, result.denominator)
        self.assertEqual("4.25", result.decimal)
        self.assertTrue(result.passed)
        self.assertIsNone(result.unknown_reason)
        self.assertEqual((4, 3, 4), (result.complexity, result.covered, result.total))

    def test_calculates_full_half_and_zero_coverage(self):
        full = calculate_crap(complexity=4, covered=2, total=2)
        half = calculate_crap(complexity=4, covered=1, total=2)
        zero = calculate_crap(complexity=4, covered=0, total=2)

        self.assertEqual((4, 1, "4"), (full.numerator, full.denominator, full.decimal))
        self.assertEqual((6, 1, "6"), (half.numerator, half.denominator, half.decimal))
        self.assertEqual((20, 1, "20"), (zero.numerator, zero.denominator, zero.decimal))

    def test_uses_exact_eight_boundary(self):
        exact = calculate_crap(complexity=8, covered=1, total=1)
        above = calculate_crap(complexity=9, covered=1, total=1)

        self.assertEqual("8", exact.decimal)
        self.assertTrue(exact.passed)
        self.assertEqual("9", above.decimal)
        self.assertFalse(above.passed)

    def test_renders_repeating_fraction_canonically(self):
        result = calculate_crap(complexity=1, covered=2, total=3)

        self.assertEqual((28, 27), (result.numerator, result.denominator))
        self.assertEqual("1.037037037037", result.decimal)

    def test_rejects_invalid_metric_counts(self):
        unsafe = 9_007_199_254_740_992
        invalid = (
            {"complexity": 0, "covered": 0, "total": 1},
            {"complexity": True, "covered": 0, "total": 1},
            {"complexity": 1, "covered": -1, "total": 1},
            {"complexity": 1, "covered": 2, "total": 1},
            {"complexity": 1, "covered": 0, "total": 0},
            {"complexity": unsafe, "covered": 0, "total": 1},
            {"complexity": 1, "covered": unsafe, "total": unsafe},
            {"complexity": 1, "covered": 0, "total": unsafe},
        )

        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    calculate_crap(**arguments)

    def test_unknown_result_preserves_inputs_and_has_no_numeric_claim(self):
        result = calculate_crap(3, 0, 0, "coverage-file-missing")

        self.assertEqual(
            CrapResult(
                complexity=3,
                covered=0,
                total=0,
                numerator=None,
                denominator=None,
                decimal=None,
                passed=None,
                unknown_reason="coverage-file-missing",
            ),
            result,
        )

    def test_invalid_crap_inputs_have_exact_stable_messages(self):
        cases = (
            ({"complexity": 0, "covered": 0, "total": 1}, "complexity must be an integer from 1 through 9007199254740991"),
            ({"complexity": 1, "covered": -1, "total": 1}, "covered must be an integer from 0 through 9007199254740991"),
            ({"complexity": 1, "covered": 0, "total": -1}, "total must be an integer from 0 through 9007199254740991"),
            ({"complexity": 1, "covered": 2, "total": 1}, "covered must not exceed total"),
            ({"complexity": 1, "covered": 0, "total": 0}, "total must be positive when coverage is known"),
            ({"complexity": 1, "covered": 0, "total": 0, "unknown_reason": ""}, "unknown_reason must be a non-empty string"),
        )

        for arguments, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError) as raised:
                    calculate_crap(**arguments)
                self.assertEqual(expected, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
