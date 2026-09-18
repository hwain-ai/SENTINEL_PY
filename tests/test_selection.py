import tempfile
import unittest
from pathlib import Path

from test_changed_scope import _write_project
from sentinel_py.config import load_project, UsageConfigError
from sentinel_py.selection import select_project, scope_result
from sentinel_py.mutmut_adapter import candidate_details
from sentinel_py.runner.mutation_backend import _candidate_selection


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        _write_project(self.root)
        self.project = load_project(str(self.root), None, None)

    def test_explicit_source_and_tests_work_without_a_git_change(self):
        selected = select_project(self.project, ["app/first.py"], ["first"], ["tests/test_all.py"])
        self.assertEqual(["app/first.py"], scope_result(selected)["files"])
        self.assertEqual(("tests/test_all.py",), selected.selected_tests)
        self.assertEqual(1, len(selected.selected_functions))
        self.assertEqual(2, len(self.project.production_sources))

    def test_unknown_function_or_a_test_as_production_is_an_error(self):
        for files, functions in [(["tests/test_all.py"], []), (["app/first.py"], ["absent"]), ([], ["first"]), (["app/first.py"], ["first()"] )]:
            with self.subTest(files=files, functions=functions), self.assertRaises(UsageConfigError):
                select_project(self.project, files, functions)

    def test_function_selection_excludes_other_function_mutants(self):
        source = b"def first(x):\n    return x + 1\n\ndef second(x):\n    return x - 1\n"
        (self.root / "app/first.py").write_bytes(source)
        project = load_project(str(self.root), None, None)
        selected = select_project(project, ["app/first.py"], ["first"])
        sources = {"app/first.py": source}
        plan = candidate_details(sources)
        identifiers, details = _candidate_selection(selected, sources, tuple(plan))
        self.assertTrue(identifiers)
        self.assertLess(len(identifiers), len(plan))
        self.assertEqual({"first"}, {details[key]["function"] for key in identifiers})
        self.assertTrue(all(details[key]["line"] == 2 for key in identifiers))

    def test_test_selection_rejects_a_production_file(self):
        with self.assertRaises(UsageConfigError):
            select_project(self.project, ["app/first.py"], [], ["app/second.py"])


if __name__ == "__main__":
    unittest.main()
