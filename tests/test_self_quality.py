import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.config import load_project  # noqa: E402
from sentinel_py.crap import analyze_source  # noqa: E402


class SelfQualityTests(unittest.TestCase):
    def test_every_production_callable_has_complexity_at_most_eight(self):
        callables = []
        project = load_project(str(REPOSITORY_ROOT), None, "sentinel-py")
        for source in project.production_sources:
            callables.extend(
                analyze_source(source.path.read_bytes(), source.module_relative_path)
            )

        self.assertTrue(callables, "production callable inventory must not be empty")
        self.assertLessEqual(
            max(item.complexity for item in callables),
            8,
        )


if __name__ == "__main__":
    unittest.main()
