from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from hy3_tracejudge.api.app import create_app
from hy3_tracejudge.api.config import WebConfig
from hy3_tracejudge.api.jobs import EvaluationJobManager, JobNotFound, JobQueueFull


API_KEY = "test-web-api-key-with-32-characters"
JOB_ID = "a" * 32


class FakeManager:
    def __init__(self, *, queue_full: bool = False) -> None:
        self.accepting_jobs = True
        self.queue_full = queue_full
        self.submissions: list[tuple[str, int, str]] = []

    def submit(self, problem_id: str, examples: int, mode: str):
        if self.queue_full:
            raise JobQueueFull()
        self.submissions.append((problem_id, examples, mode))
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
