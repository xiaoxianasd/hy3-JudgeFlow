from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from hy3_tracejudge.catalog import get_problem
from hy3_tracejudge.executor import run_candidate
from hy3_tracejudge.sandbox.backends import (
    DockerSandboxBackend,
    SandboxLimits,
    execute_payload,
)


class LocalSandboxPolicyTests(unittest.TestCase):
    def test_import_is_rejected_before_execution(self) -> None:
        problem = get_problem("two_sum_exists")
        code = "import os\n\ndef solve_case(case):\n    return bool(os.environ)"
        result = run_candidate(problem, code)
        self.assertFalse(result.all_passed)
        self.assertIn("ImportNotAllowed", result.harness_error or "")

    def test_dunder_introspection_is_rejected(self) -> None:
        problem = get_problem("two_sum_exists")
        code = "def solve_case(case):\n    return (1).__class__"
        result = run_candidate(problem, code)
        self.assertFalse(result.all_passed)
        self.assertIn("PrivateAttributeNotAllowed", result.harness_error or "")

    def test_infinite_loop_is_terminated(self) -> None:
        problem = get_problem("two_sum_exists")
        code = "def solve_case(case):\n    while True:\n        pass"
        result = run_candidate(problem, code, timeout_seconds=0.2)
        self.assertFalse(result.all_passed)
        self.assertEqual(result.harness_error, "TimeLimitExceeded")

    def test_large_return_value_is_rejected(self) -> None:
        problem = get_problem("two_sum_exists")
        code = "def solve_case(case):\n    return 'x' * 70000"
        result = run_candidate(problem, code)
        self.assertFalse(result.all_passed)
        self.assertTrue(result.tests)
        self.assertIn("ReturnValueLimitExceeded", result.tests[0].error or "")

    def test_production_cannot_fall_back_to_local_backend(self) -> None:
        payload = {"code": "def solve_case(case): return True", "function_name": "solve_case", "tests": []}
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "SANDBOX_BACKEND": "local",
                "ALLOW_UNSAFE_LOCAL_EXECUTION": "true",
            },
        ):
            result = execute_payload(payload, timeout_seconds=1.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "UnsafeLocalSandboxDisabled")


class DockerSandboxCommandTests(unittest.TestCase):
    def test_docker_backend_applies_required_isolation_flags(self) -> None:
        payload = {"code": "def solve_case(case): return True", "function_name": "solve_case", "tests": []}
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "results": []}).encode(),
            stderr=b"",
        )
        with patch("hy3_tracejudge.sandbox.backends.subprocess.run", return_value=completed) as run:
            result = DockerSandboxBackend("hy3-process-sandbox:test").run(
                payload,
                SandboxLimits(timeout_seconds=1.0),
            )
        self.assertTrue(result.ok)
        command = run.call_args.args[0]
        for required in (
            "--network",
            "none",
            "--read-only",
            "--memory",
            "--memory-swap",
            "--cpus",
            "--pids-limit",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65532:65532",
        ):
            self.assertIn(required, command)
        self.assertNotIn("-v", command)
        self.assertNotIn("--volume", command)

    def test_invalid_image_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DockerSandboxBackend("--privileged")

    def test_timed_out_container_is_force_removed_by_exact_name(self) -> None:
        payload = {"code": "def solve_case(case): return True", "function_name": "solve_case", "tests": []}
        cleanup = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with patch(
            "hy3_tracejudge.sandbox.backends.subprocess.run",
            side_effect=[subprocess.TimeoutExpired(["docker", "run"], 1), cleanup],
        ) as run:
            result = DockerSandboxBackend("hy3-process-sandbox:test").run(
                payload,
                SandboxLimits(timeout_seconds=0.1),
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "TimeLimitExceeded")
        cleanup_command = run.call_args_list[1].args[0]
        self.assertEqual(cleanup_command[:3], ["docker", "rm", "-f"])
        self.assertRegex(cleanup_command[3], r"^hy3-sandbox-[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
