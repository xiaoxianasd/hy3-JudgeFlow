from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from hy3_tracejudge.api.app import create_app
from hy3_tracejudge.api.config import WebConfig
from hy3_tracejudge.api.jobs import EvaluationJobManager, JobNotFound, JobQueueFull
from hy3_tracejudge.catalog import ADAPTER_HEADING, get_problem
from hy3_tracejudge.hy3_client import Hy3APIError


API_KEY = "test-web-api-key-with-32-characters"
JOB_ID = "a" * 32


class FakeManager:
    def __init__(self, *, queue_full: bool = False) -> None:
        self.accepting_jobs = True
        self.queue_full = queue_full
        self.submissions: list[tuple[str, int, str]] = []
        self.code_submissions: list[dict] = []
        self.model_runtimes: list[dict | None] = []

    def submit(self, problem_id: str, examples: int, mode: str, *, submission=None, runtime=None):
        if self.queue_full:
            raise JobQueueFull()
        self.submissions.append((problem_id, examples, mode))
        self.model_runtimes.append(runtime)
        if submission is not None:
            self.code_submissions.append(submission)
        return {
            "id": JOB_ID,
            "status": "queued",
            "phase": "queued",
            "problem_id": problem_id,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "result": None,
            "error": None,
        }

    def snapshot(self, job_id: str):
        if job_id != JOB_ID:
            raise JobNotFound(job_id)
        return {
            "id": JOB_ID,
            "status": "succeeded",
            "phase": "completed",
            "problem_id": "two_sum_exists",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "result": {"answer": {}, "evaluation": {}},
            "error": None,
        }

    def stats(self):
        return {
            "backend": "test",
            "capacity": 10,
            "counts": {"queued": 1},
            "active": 1,
            "oldest_queued_age_seconds": 0.1,
        }


def test_config(**overrides) -> WebConfig:
    values = {
        "app_env": "test",
        "api_key": API_KEY,
        "expose_docs": False,
        "max_request_bytes": 1_024,
        "rate_limit_per_minute": 20,
    }
    values.update(overrides)
    return WebConfig(**values)


class WebAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = FakeManager()
        self.client = TestClient(create_app(test_config(), self.manager))  # type: ignore[arg-type]
        self.auth = {"X-API-Key": API_KEY}

    def test_public_health_and_security_headers(self) -> None:
        response = self.client.get("/api/v1/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])
        self.assertTrue(response.headers["x-request-id"])

    def test_job_response_preserves_unknown_verdict_and_coverage(self) -> None:
        job = self.manager.snapshot(JOB_ID)
        job["result"]["evaluation"] = {
            "final_correct": True, "process_correct": None, "process_status": "uncertain",
            "unsupported_correct": None, "first_error_step": None,
            "review_coverage": {"expected": 5, "completed": 0, "conclusive": 0},
            "hypothesis": {"enabled": False, "status": "unsupported", "examples_checked": 0},
        }
        with patch.object(self.manager, "snapshot", return_value=job):
            response = self.client.get(f"/api/v1/evaluations/{JOB_ID}", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        result = response.json()["job"]["result"]["evaluation"]
        self.assertIsNone(result["process_correct"])
        self.assertIsNone(result["unsupported_correct"])
        self.assertEqual(result["review_coverage"]["completed"], 0)
        self.assertEqual(result["hypothesis"]["status"], "unsupported")

    def test_submission_requires_api_key(self) -> None:
        response = self.client.post(
            "/api/v1/evaluations",
            json={"problem_id": "two_sum_exists"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_valid_submission_returns_async_job(self) -> None:
        response = self.client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={
                "problem_id": "two_sum_exists",
                "hypothesis_examples": 25,
                "review_mode": "supervisor",
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job"]["id"], JOB_ID)
        self.assertEqual(
            response.json()["status_url"],
            f"/api/v1/evaluations/{JOB_ID}",
        )
        self.assertEqual(self.manager.submissions, [("two_sum_exists", 25, "supervisor")])

    def test_model_and_provider_key_are_forwarded_without_leaking(self) -> None:
        secret = "provider-secret-for-test"
        response = self.client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "two_sum_exists", "model": "hy4-preview", "provider_api_key": secret},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.manager.model_runtimes[-1], {"model": "hy4-preview", "api_key": secret})
        self.assertNotIn(secret, response.text)

    def test_model_configuration_is_public_but_credentials_are_not(self) -> None:
        response = self.client.get("/api/v1/config")
        self.assertEqual(response.status_code, 200)
        runtime = response.json()["model_runtime"]
        self.assertEqual(runtime["models"], ["hy3", "hy4-preview"])
        self.assertTrue(runtime["per_job_credentials"])
        self.assertNotIn("api_key", response.text.lower())

    def test_model_fields_are_strictly_validated(self) -> None:
        for payload in (
            {"model": "hy5"},
            {"provider_api_key": "bad\nkey"},
            {"provider_api_key": False},
        ):
            response = self.client.post(
                "/api/v1/evaluations", headers=self.auth,
                json={"problem_id": "two_sum_exists", **payload},
            )
            self.assertEqual(response.status_code, 422)

    @patch("hy3_tracejudge.api.app.Hy3Client")
    def test_selected_model_health_uses_transient_provider_key(self, client_class) -> None:
        secret = "health-provider-secret"
        client_class.return_value.probe.return_value = {
            "ok": True, "requested_model": "hy4-preview", "latency_ms": 12.5,
            "probe": "inference",
        }
        response = self.client.post(
            "/api/v1/health/model", headers=self.auth,
            json={"model": "hy4-preview", "provider_api_key": secret},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["probe"], "inference")
        configured = client_class.call_args.args[0]
        self.assertEqual(configured.model, "hy4-preview")
        self.assertEqual(configured.api_key, secret)
        self.assertLessEqual(configured.timeout_seconds, 30)
        self.assertNotIn(secret, response.text)

    @patch("hy3_tracejudge.api.app.Hy3Client")
    def test_model_probe_returns_actionable_safe_upstream_error(self, client_class) -> None:
        client_class.return_value.probe.side_effect = Hy3APIError(
            "Hy3 HTTP 429: private provider detail",
            status_code=429,
        )
        response = self.client.post(
            "/api/v1/health/model",
            headers=self.auth,
            json={"model": "hy4-preview", "provider_api_key": "safe-test-key"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "upstream_rate_limited")
        self.assertIn("HTTP 429", response.json()["error"]["message"])
        self.assertNotIn("private provider detail", response.text)

    def test_external_problem_can_be_selected_and_submitted(self) -> None:
        response = self.client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "mbpp_Mbpp/8", "review_mode": "single"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.manager.submissions, [("mbpp_Mbpp/8", 60, "single")])

    def test_problem_catalog_contains_selector_metadata(self) -> None:
        response = self.client.get("/api/v1/problems")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        external = next(item for item in catalog if item["id"] == "mbpp_Mbpp/8")
        self.assertEqual(external["tier"], "external")
        self.assertNotEqual(external["title"], '"""')
        self.assertGreater(external["public_test_count"], 0)
        self.assertGreater(external["hidden_test_count"], 0)

    def test_validation_rejects_unknown_fields(self) -> None:
        response = self.client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "two_sum_exists", "unexpected": "value"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")

    @patch.dict("os.environ", {"SANDBOX_BACKEND": "docker"})
    @patch("hy3_tracejudge.api.app.DockerReadinessProbe.ready", return_value=True)
    def test_code_submission_queues_original_code_and_optional_steps(self, _ready) -> None:
        code = "\n\ndef solve_case(case):\n    return True\n"
        response = self.client.post("/api/v1/code-submissions", headers=self.auth, json={
            "problem_id": "two_sum_exists", "code": code, "reasoning_steps": ["  用户步骤  "],
        })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.manager.submissions, [("two_sum_exists", 60, "submission")])
        self.assertEqual(self.manager.code_submissions, [{"code": code, "reasoning_steps": ["用户步骤"]}])
        self.assertNotIn(code, response.text)
        self.assertEqual(response.json()["status_url"], f"/api/v1/evaluations/{JOB_ID}")

    def test_code_submission_requires_auth_and_valid_payload(self) -> None:
        payload = {"problem_id": "two_sum_exists", "code": "def solve_case(case): return True"}
        self.assertEqual(self.client.post("/api/v1/code-submissions", json=payload).status_code, 401)
        for invalid in ({"code": "  "}, {"code": "x\x00"}, {"code": 12},
                        {"reasoning_steps": [""]}, {"reasoning_steps": ["x"] * 21},
                        {"reasoning_steps": [False]}, {"review_mode": "supervisor"},
                        {"reference_solution": "must not be accepted"}):
            with self.subTest(invalid=invalid):
                response = self.client.post("/api/v1/code-submissions", headers=self.auth, json={**payload, **invalid})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.manager.submissions, [])

    @patch.dict("os.environ", {"SANDBOX_BACKEND": "local", "ALLOW_UNSAFE_LOCAL_EXECUTION": "true"})
    def test_code_submission_can_use_opted_in_local_sandbox_on_loopback(self) -> None:
        response = self.client.post("/api/v1/code-submissions", headers=self.auth, json={
            "problem_id": "two_sum_exists", "code": "def solve_case(case): return True",
        })
        self.assertEqual(response.status_code, 202)
        config = self.client.get("/api/v1/config").json()["code_submission"]
        self.assertTrue(config["enabled"])
        self.assertEqual(config["sandbox_mode"], "local")
        self.assertTrue(self.manager.code_submissions[0]["_allow_local_sandbox"])

    @patch.dict("os.environ", {"SANDBOX_BACKEND": "local", "ALLOW_UNSAFE_LOCAL_EXECUTION": "true"})
    def test_code_submission_rejects_local_sandbox_on_non_loopback(self) -> None:
        client = TestClient(create_app(test_config(host="0.0.0.0"), self.manager))
        response = client.post("/api/v1/code-submissions", headers=self.auth, json={
            "problem_id": "two_sum_exists", "code": "def solve_case(case): return True",
        })
        self.assertEqual(response.status_code, 503)
        self.assertFalse(client.get("/api/v1/config").json()["code_submission"]["enabled"])

    @patch.dict("os.environ", {"SANDBOX_BACKEND": "docker"})
    @patch("hy3_tracejudge.api.app.DockerReadinessProbe.ready", return_value=False)
    def test_code_submission_fails_closed_when_docker_is_unavailable(self, _ready) -> None:
        response = self.client.post("/api/v1/code-submissions", headers=self.auth, json={
            "problem_id": "two_sum_exists", "code": "def solve_case(case): return True",
        })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "submission_sandbox_unavailable")
        self.assertEqual(self.manager.submissions, [])

    def test_code_submission_rejects_oversize_and_unknown_problem(self) -> None:
        client = TestClient(create_app(test_config(max_request_bytes=100_000), self.manager))
        for payload in ({"code": "x" * 20001}, {"reasoning_steps": ["x" * 2001]}):
            response = client.post("/api/v1/code-submissions", headers=self.auth, json={
                "problem_id": "two_sum_exists", "code": "pass", **payload,
            })
            self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/v1/code-submissions", headers=self.auth, json={"problem_id": "unknown", "code": "pass"})
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/v1/code-submissions", headers=self.auth, json={"problem_id": "two_sum_exists", "code": "x" * 2000})
        self.assertEqual(response.status_code, 413)

    def test_code_submissions_share_generation_rate_limit(self) -> None:
        client = TestClient(create_app(test_config(rate_limit_per_minute=1), self.manager))
        self.assertEqual(client.post("/api/v1/evaluations", headers=self.auth, json={"problem_id": "two_sum_exists"}).status_code, 202)
        response = client.post("/api/v1/code-submissions", headers=self.auth, json={"problem_id": "two_sum_exists", "code": "pass"})
        self.assertEqual(response.status_code, 429)

    def test_problem_details_include_full_public_content(self) -> None:
        catalog = self.client.get("/api/v1/problems").json()
        for problem_id in ("two_sum_exists", "mbpp_Mbpp/8"):
            with self.subTest(problem_id=problem_id):
                original = get_problem(problem_id)
                detail = next(item for item in catalog if item["id"] == problem_id)
                self.assertEqual(detail["input_schema"], original["input_schema"])
                self.assertEqual(detail["constraints"], original["constraints"])
                self.assertEqual(detail["public_tests"], original["public_tests"])
                self.assertEqual(
                    detail["source_statement"],
                    original.get("source_statement", original["statement"]),
                )
        external = next(item for item in catalog if item["id"] == "mbpp_Mbpp/8")
        self.assertNotIn(ADAPTER_HEADING, external["source_statement"])
        self.assertIn(ADAPTER_HEADING, external["statement"])

    def test_problem_details_never_expose_private_evaluation_material(self) -> None:
        allowed = {
            "id", "title", "difficulty", "statement", "source_statement", "source",
            "tier", "function_name", "input_schema", "constraints", "public_tests",
            "public_test_count", "hidden_test_count",
        }
        original = get_problem("two_sum_exists")
        sample = {
            **original,
            "private_annotation": "must-not-leak",
            "public_tests": [{**original["public_tests"][0], "internal_note": "must-not-leak"}],
        }
        with patch("hy3_tracejudge.api.app.load_problems", return_value=[sample]):
            response = self.client.get("/api/v1/problems")
        detail = response.json()[0]
        self.assertEqual(set(detail), allowed)
        self.assertEqual(set(detail["public_tests"][0]), {"name", "input", "expected"})
        self.assertNotIn("must-not-leak", response.text)
        for item in self.client.get("/api/v1/problems").json():
            self.assertEqual(set(item), allowed)

    def test_homepage_has_professional_heading_and_persistent_details(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Hy3 JudgeFlow", response.text)
        self.assertIn("Hy3 推理评估", response.text)
        self.assertIn('id="modelName"', response.text)
        self.assertIn('value="hy4-preview"', response.text)
        self.assertIn('id="modelApiKey"', response.text)
        submission = self.client.get("/submit")
        self.assertEqual(submission.status_code, 200)
        self.assertIn("提交我的代码", submission.text)
        self.assertIn('id="modelName"', submission.text)
        self.assertNotIn("过程真的成立吗", response.text)
        self.assertIn('id="problemDetails"', response.text)
        self.assertIn('id="publicExamples"', response.text)
        self.assertLess(response.text.index('id="result"'), response.text.index('id="problemDetails"'))

    def test_unknown_problem_is_not_accepted(self) -> None:
        response = self.client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "does_not_exist"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "problem_not_found")

    def test_non_json_and_oversized_requests_are_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/evaluations",
            headers={**self.auth, "Content-Type": "text/plain"},
            content="not json",
        )
        self.assertEqual(response.status_code, 415)
        oversized = "x" * 2_000
        response = self.client.post(
            "/api/v1/evaluations",
            headers={**self.auth, "Content-Type": "application/json"},
            content=oversized,
        )
        self.assertEqual(response.status_code, 413)

    def test_job_lookup_is_authenticated(self) -> None:
        self.assertEqual(self.client.get(f"/api/v1/evaluations/{JOB_ID}").status_code, 401)
        response = self.client.get(f"/api/v1/evaluations/{JOB_ID}", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["status"], "succeeded")

    def test_queue_stats_are_authenticated(self) -> None:
        self.assertEqual(self.client.get("/api/v1/queue/stats").status_code, 401)
        response = self.client.get("/api/v1/queue/stats", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["backend"], "test")
        self.assertEqual(response.json()["active"], 1)

    def test_full_queue_returns_retryable_error(self) -> None:
        client = TestClient(create_app(test_config(), FakeManager(queue_full=True)))  # type: ignore[arg-type]
        response = client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "two_sum_exists"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "queue_full")
        self.assertEqual(response.headers["retry-after"], "10")

    @patch.dict("os.environ", {"SANDBOX_BACKEND": "docker"})
    @patch("hy3_tracejudge.api.app.shutil.which", return_value=None)
    def test_production_readiness_requires_docker_image(self, _which) -> None:
        config = test_config(
            app_env="production",
            allowed_origins=("https://example.com",),
            queue_backend="mysql",
            database_url="mysql+pymysql://user:password@127.0.0.1/tracejudge",
        )
        client = TestClient(create_app(config, FakeManager()))  # type: ignore[arg-type]
        response = client.get("/api/v1/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["sandbox_image"])

    def test_submission_rate_limit_is_enforced(self) -> None:
        manager = FakeManager()
        client = TestClient(create_app(test_config(rate_limit_per_minute=1), manager))  # type: ignore[arg-type]
        first = client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "two_sum_exists"},
        )
        second = client.post(
            "/api/v1/evaluations",
            headers=self.auth,
            json={"problem_id": "two_sum_exists"},
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "rate_limited")


class WebConfigTests(unittest.TestCase):
    def test_production_requires_real_api_key(self) -> None:
        with self.assertRaises(ValueError):
            WebConfig(app_env="production", api_key="").validate()
        with self.assertRaises(ValueError):
            WebConfig(
                app_env="production",
                api_key="REPLACE_WITH_A_RANDOM_VALUE_OF_AT_LEAST_32_CHARACTERS",
            ).validate()

    def test_production_rejects_wildcard_cors_and_proxy_trust(self) -> None:
        database = {
            "queue_backend": "mysql",
            "database_url": "mysql+pymysql://user:password@127.0.0.1/tracejudge",
        }
        with self.assertRaises(ValueError):
            WebConfig(
                app_env="production",
                api_key=API_KEY,
                allowed_origins=("*",),
                **database,
            ).validate()
        with self.assertRaises(ValueError):
            WebConfig(
                app_env="production",
                api_key=API_KEY,
                trust_proxy_headers=True,
                forwarded_allow_ips="*",
                **database,
            ).validate()

    def test_production_requires_mysql_queue(self) -> None:
        with self.assertRaisesRegex(ValueError, "QUEUE_BACKEND must be mysql"):
            WebConfig(app_env="production", api_key=API_KEY).validate()


class JobManagerTests(unittest.TestCase):
    def test_job_lifecycle_and_bounded_capacity(self) -> None:
        release = threading.Event()

        def runner(problem_id, examples, mode, update_phase):
            update_phase("hy3_generation")
            release.wait(timeout=2)
            return {"problem_id": problem_id, "examples": examples, "mode": mode}

        manager = EvaluationJobManager(
            workers=1,
            queue_size=0,
            ttl_seconds=60,
            max_stored_jobs=10,
            runner=runner,
        )
        try:
            first = manager.submit("two_sum_exists", 12, "single")
            with self.assertRaises(JobQueueFull):
                manager.submit("two_sum_exists", 12, "single")
            release.set()
            deadline = time.monotonic() + 3
            snapshot = manager.snapshot(first["id"])
            while snapshot["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = manager.snapshot(first["id"])
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["result"]["examples"], 12)
        finally:
            release.set()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
