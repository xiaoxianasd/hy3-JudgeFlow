from __future__ import annotations

import os
import threading
import unittest

from sqlalchemy import delete, update

from hy3_tracejudge.api.config import WebConfig
from hy3_tracejudge.api.database import EvaluationJob, MySQLJobStore, create_database_engine


@unittest.skipUnless(
    os.getenv("TRACEJUDGE_RUN_MYSQL_TESTS") == "1",
    "set TRACEJUDGE_RUN_MYSQL_TESTS=1 to use the configured MySQL database",
)
class MySQLIntegrationTests(unittest.TestCase):
    def test_real_mysql_queue_round_trip_and_persistence(self) -> None:
        config = WebConfig.from_env()
        self.assertEqual(config.queue_backend, "mysql")
        engine = create_database_engine(
            config.database_url,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            pool_recycle_seconds=config.database_pool_recycle_seconds,
        )
        store = MySQLJobStore(engine, capacity=1_000, max_attempts=2)
        job_id = None
        try:
            queued = store.enqueue("mysql_integration_probe", 1, "single")
            job_id = queued["id"]
            with engine.begin() as connection:
                connection.execute(
                    update(EvaluationJob)
                    .where(EvaluationJob.id == job_id)
                    .values(priority=32_767)
                )
            claimed = store.claim("integration-worker", 60)
            self.assertEqual(claimed["id"], job_id)
            self.assertTrue(
                store.complete(
                    job_id,
                    "integration-worker",
                    {"probe": "mysql-persistence-ok"},
                )
            )
            engine.dispose()
            second_engine = create_database_engine(config.database_url)
            try:
                persisted = MySQLJobStore(
                    second_engine,
                    capacity=1_000,
                    max_attempts=2,
                ).get(job_id)
                self.assertEqual(persisted["status"], "succeeded")
                self.assertEqual(persisted["result"]["probe"], "mysql-persistence-ok")
            finally:
                second_engine.dispose()
        finally:
            if job_id is not None:
                cleanup_engine = create_database_engine(config.database_url)
                try:
                    with cleanup_engine.begin() as connection:
                        connection.execute(delete(EvaluationJob).where(EvaluationJob.id == job_id))
                finally:
                    cleanup_engine.dispose()
            engine.dispose()

    def test_two_workers_claim_distinct_jobs_with_skip_locked(self) -> None:
        config = WebConfig.from_env()
        engine = create_database_engine(config.database_url)
        store = MySQLJobStore(engine, capacity=1_000, max_attempts=2)
        job_ids: list[str] = []
        barrier = threading.Barrier(3)
        claimed: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        try:
            for index in range(2):
                job = store.enqueue(f"mysql_concurrency_probe_{index}", 1, "single")
                job_ids.append(job["id"])
            with engine.begin() as connection:
                for index, job_id in enumerate(job_ids):
                    connection.execute(
                        update(EvaluationJob)
                        .where(EvaluationJob.id == job_id)
                        .values(priority=32_767 - index)
                    )

            def claim(worker: str) -> None:
                try:
                    barrier.wait(timeout=3)
                    job = store.claim(worker, 60)
                    with lock:
                        if job is not None:
                            claimed.append(job["id"])
                except BaseException as exc:
                    with lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=claim, args=(f"integration-worker-{index}",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=3)
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertCountEqual(claimed, job_ids)
        finally:
            with engine.begin() as connection:
                if job_ids:
                    connection.execute(delete(EvaluationJob).where(EvaluationJob.id.in_(job_ids)))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
