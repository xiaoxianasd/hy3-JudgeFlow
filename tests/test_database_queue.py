from __future__ import annotations

import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import update

from hy3_tracejudge.api.config import WebConfig
from hy3_tracejudge.api.database import (
    Base,
    EvaluationJob,
    MySQLJobStore,
    create_database_engine,
    utcnow,
)
from hy3_tracejudge.api.database_admin import upgrade_database
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
    def test_initial_migration_builds_queue_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.sqlite3"
            config = WebConfig(database_url=f"sqlite:///{path.as_posix()}")
            status = upgrade_database(config)
            self.assertEqual(status["schema_revision"], "0002")
            self.assertTrue(status["queue_table"])


if __name__ == "__main__":
    unittest.main()
