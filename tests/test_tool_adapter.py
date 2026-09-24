import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


LANGUAGE = "python"
ADAPTER_DIRECTORY = Path(__file__).resolve().parents[1] / "sentinel-tool"


class AdapterChangedScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.bundle = self.base / "bundle"
        self.bundle.mkdir()
        self.home = self.base / "checker"
        (self.home / "scripts").mkdir(parents=True)
        self.project = self.base / "project"
        self.project.mkdir()
        shutil.copyfile(ADAPTER_DIRECTORY / "sentinel-tool", self.bundle / "sentinel-tool")
        self.version = (ADAPTER_DIRECTORY / "version").read_text().strip()
        (self.bundle / "home").write_text(str(self.home) + "\n")
        (self.bundle / "sentinel-tool.json").write_text(json.dumps({"language": LANGUAGE, "version": self.version}))
        (self.home / "scripts" / "toolchain.py").write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "(Path(__file__).parents[1] / 'arguments.json').write_text(json.dumps(sys.argv))\n"
            "reply = json.loads((Path(__file__).parents[1] / 'reply.json').read_text())\n"
            "if '--output' in sys.argv:\n"
            "    Path(sys.argv[sys.argv.index('--output') + 1]).write_text(reply['payload'])\n"
            "else:\n"
            "    sys.stdout.write(reply['payload'])\n"
            "sys.stderr.write('checker diagnostic\\n')\n"
            "raise SystemExit(reply['exit'])\n"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def check(self, payload, exit_code=0, *, changed=True, execution_mode=None):
        (self.home / "reply.json").write_text(json.dumps({"payload": payload, "exit": exit_code}))
        request = {
            "protocolVersion": "sentinel-tool-protocol-v1", "requestId": "adapter-test", "command": "check",
            "moduleId": "module", "language": LANGUAGE, "projectRoot": str(self.project), "config": None,
        }
        if changed:
            request["changedFiles"] = ["README.md"]
        if execution_mode is not None:
            request["executionMode"] = execution_mode
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(self.bundle / "sentinel-tool")],
            input=json.dumps(request), text=True, capture_output=True, check=False,
        )
        response = json.loads(completed.stdout)
        self.assertEqual(response["exitCode"], completed.returncode)
        self.assertEqual(response["toolVersion"], self.version)
        return completed, response

    def test_default_and_explicit_modes_reach_the_checker(self):
        for requested, expected in ((None, "parallel"), ("parallel", "parallel"), ("sequential", "sequential")):
            with self.subTest(requested=requested):
                completed, response = self.check('{"run":{"terminalStatus":"passed"}}', execution_mode=requested)
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(response["executionMode"], expected)
                arguments = json.loads((self.home / "arguments.json").read_text())
                self.assertEqual(arguments[arguments.index("--execution-mode") + 1], expected)

    def test_invalid_mode_does_not_start_the_checker(self):
        completed, response = self.check('{"run":{"terminalStatus":"passed"}}', execution_mode="automatic")
        self.assertEqual((completed.returncode, response["status"]), (3, "usageConfigError"))
        self.assertFalse((self.home / "arguments.json").exists())

    def test_cancelled_checker_is_not_reported_as_a_backend_failure(self):
        completed, response = self.check("{}", exit_code=8)
        self.assertEqual((completed.returncode, response["status"]), (8, "cancelled"))

    def test_empty_changed_scope_is_not_a_quality_pass(self):
        completed, response = self.check('{"changedScope":"empty","run":{"terminalStatus":"passed"}}')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(response["status"], "noChanges")
        self.assertFalse(response["passed"])

    def test_actual_success_remains_a_quality_pass(self):
        completed, response = self.check('{"run":{"terminalStatus":"passed"}}')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(response["status"], "passed")
        self.assertTrue(response["passed"])

    def test_success_without_readable_structured_output_fails_closed(self):
        for payload in ("", "not JSON", "[]"):
            with self.subTest(payload=payload):
                completed, response = self.check(payload)
                self.assertEqual(completed.returncode, 6, completed.stderr)
                self.assertEqual(response["status"], "backendError")

    def test_empty_scope_without_changed_request_is_rejected(self):
        completed, response = self.check('{"changedScope":"empty","run":{"terminalStatus":"passed"}}', changed=False)
        self.assertEqual(completed.returncode, 6, completed.stderr)
        self.assertFalse(response["passed"])

    def test_quality_failure_exit_is_preserved(self):
        completed, response = self.check('{"run":{"terminalStatus":"qualityFailed"}}', exit_code=2)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(response["status"], "qualityFailed")

    def test_launcher_failure_is_not_reported_as_a_quality_failure(self):
        for payload in ("", "not JSON", "{}", '{"run":{"terminalStatus":"passed"}}'):
            with self.subTest(payload=payload):
                completed, response = self.check(payload, exit_code=2)
                self.assertEqual(completed.returncode, 6, completed.stderr)
                self.assertEqual(response["status"], "backendError")


if __name__ == "__main__":
    unittest.main()
