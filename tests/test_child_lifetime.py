import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path


class ChildLifetimeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux parent-death signal")
    def test_an_unlimited_test_stops_when_its_checker_is_killed(self):
        code = """
import os, sys, time
from pathlib import Path
from sentinel_py.runner.mutation_backend import _start_process
child = _start_process([sys.executable, '-c', 'import time; time.sleep(600)'], Path.cwd(), os.environ)
print(child.pid, flush=True)
time.sleep(600)
"""
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        parent = subprocess.Popen([sys.executable, "-B", "-c", code], env=environment, stdout=subprocess.PIPE, text=True)
        child_pid = None
        try:
            child_pid = int(parent.stdout.readline())
            parent.kill()
            parent.wait(timeout=5)
            for _ in range(100):
                if not self.running(child_pid):
                    break
                time.sleep(0.02)
            self.assertFalse(self.running(child_pid), "cancelled checker left an unlimited test running")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            parent.stdout.close()
            if child_pid is not None and self.running(child_pid):
                os.kill(child_pid, signal.SIGKILL)

    @staticmethod
    def running(pid):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except FileNotFoundError:
            return False
