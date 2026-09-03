from __future__ import annotations

import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import text, update

from hy3_tracejudge.api.config import WebConfig
from hy3_tracejudge.api.database import (
    Base,
    EvaluationJob,
    MySQLJobStore,
    create_database_engine,
    utcnow,
)
from hy3_tracejudge.api.database_admin import _alembic_config, upgrade_database
from hy3_tracejudge.api.db_jobs import DatabaseEvaluationJobManager
from hy3_tracejudge.api.jobs import JobQueueFull


class DatabaseQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        path = Path(self.tempdir.name) / "queue.sqlite3"
        self.url = f"sqlite:///{path.as_posix()}"
        self.engine = create_database_engine(self.url)
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def store(self, capacity: int = 4, max_attempts: int = 2) -> MySQLJobStore:
        return MySQLJobStore(self.engine, capacity=capacity, max_attempts=max_attempts)

    def test_job_is_persisted_claimed_and_completed(self) -> None:
        store = self.store()
        queued = store.enqueue("two_sum_exists", 20, "supervisor")
        self.assertEqual(queued["status"], "queued")
        claimed = store.claim("worker-a", 60)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(claimed["attempts"], 1)
        self.assertTrue(store.set_phase(queued["id"], "worker-a", "process_evaluation"))
        self.assertTrue(store.complete(queued["id"], "worker-a", {"evaluation": {"ok": True}}))
        snapshot = self.store().get(queued["id"])
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertTrue(snapshot["result"]["evaluation"]["ok"])
        self.assertIsNotNone(snapshot["finished_at"])

    def test_global_capacity_is_bounded(self) -> None:
        store = self.store(capacity=1)
        store.enqueue("two_sum_exists", 10, "single")
        stats = store.stats()
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["counts"]["queued"], 1)
        with self.assertRaises(JobQueueFull):
            store.enqueue("bracket_balance", 10, "single")

    def test_submission_survives_store_restart_without_leaking_in_status(self) -> None:
        submission = {"code": "def solve_case(case): return True", "reasoning_steps": ["原始步骤"]}
        store = self.store()
        queued = store.enqueue("two_sum_exists", 2, "submission", submission=submission)
        self.assertEqual(queued["kind"], "code_submission")
        self.assertNotIn("submission", queued)
        self.assertNotIn("submission_json", queued)
        claimed = self.store().claim("restarted-worker", 60)
        self.assertEqual(claimed["submission"], submission)
        self.assertNotIn("submission", store.get(queued["id"]))

    def test_database_manager_delivers_submission_to_runner(self) -> None:
        received = []
        def runner(problem_id, examples, mode, update_phase, *, submission):
            received.append(submission)
            return {"source": "user_submission"}
        manager = DatabaseEvaluationJobManager(self.store(), workers=1, poll_interval_seconds=0.01,
            lease_seconds=60, heartbeat_seconds=5, retention_seconds=3600, runner=runner)
        payload = {"code": "pass", "reasoning_steps": []}
        job = manager.submit("two_sum_exists", 1, "submission", submission=payload)
        claimed = self.store().claim("test-worker", 60)
        try:
            manager._execute("test-worker", claimed)
            self.assertEqual(received, [payload])
            self.assertEqual(manager.snapshot(job["id"])["result"], {"source": "user_submission"})
        finally:
            manager.shutdown()

    def test_retryable_upstream_error_is_requeued(self) -> None:
        store = self.store(max_attempts=2)
        queued = store.enqueue("mbpp_Mbpp/123", 1, "supervisor")
        first = store.claim("worker-a", 60)
        self.assertEqual(first["attempts"], 1)
        outcome = store.fail_or_retry(
            queued["id"],
            "worker-a",
            {"code": "upstream_error", "message": "temporary structured output error"},
            retryable=True,
        )
        self.assertEqual(outcome, "retrying")
        snapshot = store.get(queued["id"])
        self.assertEqual(snapshot["status"], "queued")
        self.assertEqual(snapshot["phase"], "retry_queued")
        self.assertIsNone(snapshot["error"])
        with self.engine.begin() as connection:
            connection.execute(
                update(EvaluationJob)
                .where(EvaluationJob.id == queued["id"])
                .values(available_at=utcnow() - timedelta(seconds=1))
            )
        second = store.claim("worker-b", 60)
        self.assertEqual(second["attempts"], 2)

    def test_expired_lease_is_recovered_then_failed_at_attempt_limit(self) -> None:
        store = self.store(max_attempts=2)
        queued = store.enqueue("two_sum_exists", 10, "single")
        store.claim("worker-a", 60)
        with self.engine.begin() as connection:
            connection.execute(
                update(EvaluationJob)
                .where(EvaluationJob.id == queued["id"])
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
        self.assertEqual(store.recover_expired_leases(), (1, 0))
        second = store.claim("worker-b", 60)
        self.assertEqual(second["attempts"], 2)
        with self.engine.begin() as connection:
            connection.execute(
                update(EvaluationJob)
                .where(EvaluationJob.id == queued["id"])
                .values(lease_expires_at=utcnow() - timedelta(seconds=1))
            )
        self.assertEqual(store.recover_expired_leases(), (0, 1))
        failed = store.get(queued["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "worker_lost")

    def test_database_manager_executes_and_persists_result(self) -> None:
        def runner(problem_id, examples, mode, update_phase):
            update_phase("process_evaluation")
            return {"problem_id": problem_id, "examples": examples, "mode": mode}

        manager = DatabaseEvaluationJobManager(
            self.store(),
            workers=1,
            poll_interval_seconds=0.05,
            lease_seconds=60,
            heartbeat_seconds=5,
            retention_seconds=3_600,
            runner=runner,
        )
        manager.start()
        try:
            submitted = manager.submit("two_sum_exists", 17, "single")
            deadline = time.monotonic() + 3
            snapshot = manager.snapshot(submitted["id"])
            while snapshot["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.02)
                snapshot = manager.snapshot(submitted["id"])
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["result"]["examples"], 17)
        finally:
            manager.shutdown()


class MigrationTests(unittest.TestCase):
    def test_submission_migration_preserves_existing_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory).as_posix()}/old-queue.sqlite3"
            command.upgrade(_alembic_config(url), "0002")
            engine = create_database_engine(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO evaluation_jobs (id, status, phase, problem_id, hypothesis_examples, "
                        "review_mode, priority, attempts, max_attempts, available_at, created_at, updated_at) "
                        "VALUES (:id, 'queued', 'queued', 'two_sum_exists', 20, 'single', 0, 0, 2, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ), {"id": "a" * 32})
                upgrade_database(WebConfig(database_url=url))
                store = MySQLJobStore(engine, capacity=4, max_attempts=2)
                preserved = store.get("a" * 32)
                self.assertEqual(preserved["kind"], "hy3_generation")
                self.assertEqual(preserved["hypothesis_examples"], 20)
                self.assertIsNone(store.claim("worker", 60)["submission"])
            finally:
                engine.dispose()

    def test_initial_migration_builds_queue_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.sqlite3"
            config = WebConfig(database_url=f"sqlite:///{path.as_posix()}")
            status = upgrade_database(config)
            self.assertEqual(status["schema_revision"], "0003")
            self.assertTrue(status["queue_table"])


if __name__ == "__main__":
    unittest.main()
