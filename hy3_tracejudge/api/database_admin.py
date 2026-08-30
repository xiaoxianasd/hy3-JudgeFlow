from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.util.exc import CommandError
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .config import ROOT, WebConfig
from .database import DatabaseUnavailable, create_database_engine, create_database_if_missing


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(config: WebConfig | None = None) -> dict[str, Any]:
    config = config or WebConfig.from_env()
    if not config.database_url:
        raise ValueError("DATABASE_URL is not configured")
    url = make_url(config.database_url)
    try:
        if url.get_backend_name() == "mysql":
            create_database_if_missing(config.database_url)
        command.upgrade(_alembic_config(config.database_url), "head")
        return database_status(config)
    except (SQLAlchemyError, CommandError, DatabaseUnavailable) as exc:
        raise RuntimeError("Database creation or migration failed") from exc


def database_status(config: WebConfig | None = None) -> dict[str, Any]:
    config = config or WebConfig.from_env()
    if not config.database_url:
        raise ValueError("DATABASE_URL is not configured")
    url = make_url(config.database_url)
    engine = create_database_engine(
        config.database_url,
        pool_size=config.database_pool_size,
        max_overflow=config.database_max_overflow,
        pool_recycle_seconds=config.database_pool_recycle_seconds,
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            tables = inspect(connection).get_table_names()
            revision = None
            if "alembic_version" in tables:
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return {
            "ok": True,
            "backend": url.get_backend_name(),
            "database": url.database,
            "schema_revision": revision,
            "queue_table": "evaluation_jobs" in tables,
        }
    except SQLAlchemyError as exc:
        raise RuntimeError("Database connection or schema check failed") from exc
    finally:
        engine.dispose()
