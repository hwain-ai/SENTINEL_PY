import os
import tempfile
import time
import unittest
from pathlib import Path

from sentinel_py.runner.parallel import run_pair


def await_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("other worker did not start")
        time.sleep(0.01)


class ParallelTests(unittest.TestCase):
    def test_default_starts_both_jobs_before_either_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def job(name, other):
                (root / name).touch()
                await_file(root / other)
                return name, tempfile.gettempdir()

            left, right = run_pair(lambda: job("crap", "mutation"), lambda: job("mutation", "crap"))
            self.assertEqual((left[0], right[0]), ("crap", "mutation"))
            self.assertNotEqual(left[1], right[1])
            self.assertFalse(Path(left[1]).exists())
            self.assertFalse(Path(right[1]).exists())

    def test_sequential_does_not_start_second_job_early(self):
        order = []
        def first():
            order.append("first")
            return 1
        def second():
            self.assertEqual(order, ["first"])
            order.append("second")
            return 2
        self.assertEqual(run_pair(first, second, "sequential"), (1, 2))
        self.assertEqual(order, ["first", "second"])

    def test_failed_job_interrupts_sibling_and_runs_its_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def first():
                await_file(root / "started")
                raise ValueError("measurement failed")
            def second():
                try:
                    (root / "started").touch()
                    while True:
                        time.sleep(0.01)
                finally:
                    (root / "cleaned").touch()
            with self.assertRaisesRegex(ValueError, "measurement failed"):
                run_pair(first, second)
            self.assertTrue((root / "cleaned").is_file())

    def test_dead_worker_is_an_error_and_not_a_partial_result(self):
        with self.assertRaisesRegex(RuntimeError, "parallelWorkerFailed"):
            run_pair(lambda: os._exit(9), lambda: 42)

    def test_invalid_mode_does_not_run_any_job(self):
        with self.assertRaisesRegex(ValueError, "invalidExecutionMode"):
            run_pair(lambda: self.fail("started"), lambda: self.fail("started"), "automatic")
