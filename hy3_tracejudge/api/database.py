from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    create_engine,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .jobs import JobNotFound, JobQueueFull


TERMINAL_STATUSES = {"succeeded", "failed"}
ACTIVE_STATUSES = {"queued", "running"}
DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
PRECISE_DATETIME = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


class DatabaseUnavailable(RuntimeError):
    pass


class Base(DeclarativeBase):
    pass


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    problem_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    hypothesis_examples: Mapped[int] = mapped_column(Integer, nullable=False)
    review_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(32), nullable=False, default="hy3")
    requires_transient_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submission_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    available_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME, index=True)

    __table_args__ = (
        Index("ix_evaluation_jobs_claim", "status", "available_at", "priority", "created_at"),
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def public_job(job: EvaluationJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "problem_id": job.problem_id,
        "hypothesis_examples": job.hypothesis_examples,
        "review_mode": job.review_mode,
        "requested_model": job.requested_model,
        "kind": "code_submission" if job.submission_json is not None else "hy3_generation",
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "result": job.result_json,
        "error": job.error_json,
    }


def create_database_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_recycle_seconds: int = 1_800,
) -> Engine:
    url = make_url(database_url)
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": pool_recycle_seconds,
        "future": True,
    }
    if url.get_backend_name() != "sqlite":
        options.update(pool_size=pool_size, max_overflow=max_overflow)
    return create_engine(url, **options)


def create_database_if_missing(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "mysql":
        raise ValueError("Database bootstrap only supports MySQL URLs")
    database = url.database or ""
    if not DATABASE_NAME_PATTERN.fullmatch(database):
        raise ValueError("MySQL database name must contain only letters, digits, or underscores")
    # SQLAlchemy URL.set(database=None) preserves the previous database; an
    # empty database component connects to the MySQL server itself.
    server_url = url.set(database="")
    engine = create_engine(server_url, pool_pre_ping=True, future=True)
    try:
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not create MySQL database") from exc
    finally:
        engine.dispose()
    return database


class MySQLJobStore:
    def __init__(
        self,
        engine: Engine,
        *,
        capacity: int,
        max_attempts: int,
    ) -> None:
        self.engine = engine
        self.capacity = capacity
        self.max_attempts = max_attempts
        self._sessions = sessionmaker(engine, expire_on_commit=False, future=True)
        self._mysql = engine.dialect.name == "mysql"
        self._capacity_lock_name = "tracejudge_evaluation_queue_capacity"

    def _now(self, session: Session) -> datetime:
        if self._mysql:
            return session.execute(text("SELECT UTC_TIMESTAMP(6)")).scalar_one()
        return utcnow()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def stats(self) -> dict[str, Any]:
        try:
            with self._sessions() as session:
                now = self._now(session)
                counts = {
                    status: int(count)
                    for status, count in session.execute(
                        select(EvaluationJob.status, func.count(EvaluationJob.id)).group_by(
                            EvaluationJob.status
                        )
                    )
                }
                oldest = session.scalar(
                    select(func.min(EvaluationJob.created_at)).where(
                        EvaluationJob.status == "queued"
                    )
                )
                return {
                    "backend": "mysql",
                    "capacity": self.capacity,
                    "counts": counts,
                    "active": sum(counts.get(status, 0) for status in ACTIVE_STATUSES),
                    "oldest_queued_age_seconds": (
                        max(0.0, round((now - oldest).total_seconds(), 3))
                        if oldest is not None
                        else None
                    ),
                }
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not read queue statistics") from exc

    def enqueue(
        self, problem_id: str, hypothesis_examples: int, review_mode: str, *,
        submission: dict[str, Any] | None = None,
        requested_model: str = "hy3",
        requires_transient_credentials: bool = False,
    ) -> dict[str, Any]:
        try:
            with self._sessions() as session, session.begin():
                lock_acquired = False
                try:
                    if self._mysql:
                        lock_acquired = session.execute(
                            text("SELECT GET_LOCK(:name, 2)"),
                            {"name": self._capacity_lock_name},
                        ).scalar_one() == 1
                        if not lock_acquired:
                            raise DatabaseUnavailable("could not acquire queue capacity lock")
                    active = session.scalar(
                        select(func.count(EvaluationJob.id)).where(
                            EvaluationJob.status.in_(ACTIVE_STATUSES)
                        )
                    ) or 0
                    if active >= self.capacity:
                        raise JobQueueFull("evaluation queue is full")
                    now = self._now(session)
                    job = EvaluationJob(
                        id=uuid.uuid4().hex,
                        status="queued",
                        phase="queued",
                        problem_id=problem_id,
                        hypothesis_examples=hypothesis_examples,
                        review_mode=review_mode,
                        requested_model=requested_model,
                        requires_transient_credentials=requires_transient_credentials,
                        submission_json=submission,
                        priority=0,
                        attempts=0,
                        max_attempts=self.max_attempts,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(job)
                    session.flush()
                finally:
                    if self._mysql and lock_acquired:
                        session.execute(
                            text("SELECT RELEASE_LOCK(:name)"),
                            {"name": self._capacity_lock_name},
                        )
            return public_job(job)
        except (JobQueueFull, DatabaseUnavailable):
            raise
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not enqueue evaluation job") from exc

    def get(self, job_id: str) -> dict[str, Any]:
        try:
            with self._sessions() as session:
                job = session.get(EvaluationJob, job_id)
                if job is None:
                    raise JobNotFound(job_id)
                return public_job(job)
        except JobNotFound:
            raise
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not read evaluation job") from exc

    def claim(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                statement = (
                    select(EvaluationJob)
                    .where(
                        EvaluationJob.status == "queued",
                        EvaluationJob.available_at <= now,
                    )
                    .order_by(EvaluationJob.priority.desc(), EvaluationJob.created_at.asc())
                    .limit(1)
                )
                if self._mysql:
                    statement = statement.with_for_update(skip_locked=True)
                else:
                    statement = statement.with_for_update()
                job = session.execute(statement).scalar_one_or_none()
                if job is None:
                    return None
                job.status = "running"
                job.phase = "starting"
                job.attempts += 1
                job.lease_owner = worker_id
                job.lease_expires_at = now + timedelta(seconds=lease_seconds)
                job.heartbeat_at = now
                job.started_at = job.started_at or now
                job.updated_at = now
                session.flush()
                return {
                    **public_job(job),
                    "submission": job.submission_json,
                    "requires_transient_credentials": job.requires_transient_credentials,
                }
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not claim evaluation job") from exc

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                result = session.execute(
                    update(EvaluationJob)
                    .where(
                        EvaluationJob.id == job_id,
                        EvaluationJob.status == "running",
                        EvaluationJob.lease_owner == worker_id,
                    )
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                )
                return bool(result.rowcount)
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not heartbeat evaluation job") from exc

    def set_phase(self, job_id: str, worker_id: str, phase: str) -> bool:
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                result = session.execute(
                    update(EvaluationJob)
                    .where(
                        EvaluationJob.id == job_id,
                        EvaluationJob.status == "running",
                        EvaluationJob.lease_owner == worker_id,
                    )
                    .values(phase=phase, updated_at=now)
                )
                return bool(result.rowcount)
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not update evaluation phase") from exc

    def complete(self, job_id: str, worker_id: str, result_json: dict[str, Any]) -> bool:
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                result = session.execute(
                    update(EvaluationJob)
                    .where(
                        EvaluationJob.id == job_id,
                        EvaluationJob.status == "running",
                        EvaluationJob.lease_owner == worker_id,
                    )
                    .values(
                        status="succeeded",
                        phase="completed",
                        result_json=result_json,
                        error_json=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                return bool(result.rowcount)
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not complete evaluation job") from exc

    def fail_or_retry(
        self,
        job_id: str,
        worker_id: str,
        error_json: dict[str, Any],
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> str:
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                statement = select(EvaluationJob).where(
                    EvaluationJob.id == job_id,
                    EvaluationJob.status == "running",
                    EvaluationJob.lease_owner == worker_id,
                )
                if self._mysql:
                    statement = statement.with_for_update()
                job = session.execute(statement).scalar_one_or_none()
                if job is None:
                    return "lost"
                if retryable and job.attempts < job.max_attempts:
                    backoff = 5 * (2 ** max(0, job.attempts - 1))
                    delay = min(300, max(backoff, retry_after_seconds or 0))
                    job.status = "queued"
                    job.phase = "retry_queued"
                    job.available_at = now + timedelta(seconds=delay)
                    job.error_json = None
                    outcome = "retrying"
                else:
                    job.status = "failed"
                    job.phase = "failed"
                    job.error_json = error_json
                    job.finished_at = now
                    outcome = "failed"
                job.lease_owner = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.updated_at = now
                return outcome
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not fail or retry evaluation job") from exc

    def recover_expired_leases(self) -> tuple[int, int]:
        recovered = 0
        failed = 0
        try:
            with self._sessions() as session, session.begin():
                now = self._now(session)
                statement = select(EvaluationJob).where(
                    EvaluationJob.status == "running",
                    EvaluationJob.lease_expires_at.is_not(None),
                    EvaluationJob.lease_expires_at < now,
                )
                if self._mysql:
                    statement = statement.with_for_update(skip_locked=True)
                jobs = list(session.execute(statement).scalars())
                for job in jobs:
                    if job.attempts < job.max_attempts:
                        job.status = "queued"
                        job.phase = "lease_recovered"
                        job.available_at = now
                        job.error_json = None
                        recovered += 1
                    else:
                        job.status = "failed"
                        job.phase = "failed"
                        job.finished_at = now
                        job.error_json = {
                            "code": "worker_lost",
                            "message": "任务执行节点失联且已达到最大重试次数",
                        }
                        failed += 1
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                    job.updated_at = now
            return recovered, failed
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not recover expired leases") from exc

    def purge_finished(self, retention_seconds: int, batch_size: int = 1_000) -> int:
        try:
            with self._sessions() as session, session.begin():
                cutoff = self._now(session) - timedelta(seconds=retention_seconds)
                ids = list(
                    session.scalars(
                        select(EvaluationJob.id)
                        .where(
                            EvaluationJob.status.in_(TERMINAL_STATUSES),
                            EvaluationJob.finished_at < cutoff,
                        )
                        .order_by(EvaluationJob.finished_at.asc())
                        .limit(batch_size)
                    )
                )
                if ids:
                    session.execute(delete(EvaluationJob).where(EvaluationJob.id.in_(ids)))
                return len(ids)
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("could not purge finished evaluation jobs") from exc
