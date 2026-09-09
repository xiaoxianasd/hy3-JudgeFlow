from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


def _load_env_file() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class WebConfig:
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8765
    api_key: str = ""
    allowed_origins: tuple[str, ...] = ()
    trust_proxy_headers: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    rate_limit_per_minute: int = 20
    max_request_bytes: int = 65_536
    job_workers: int = 1
    job_queue_size: int = 8
    job_ttl_seconds: int = 3_600
    max_stored_jobs: int = 500
    http_concurrency: int = 100
    expose_docs: bool = True
    log_level: str = "info"
    queue_backend: str = "memory"
    database_url: str = ""
    database_pool_size: int = 5
    database_max_overflow: int = 5
    database_pool_recycle_seconds: int = 1_800
    job_poll_interval_seconds: float = 0.5
    job_lease_seconds: int = 900
    job_heartbeat_seconds: int = 15
    job_max_attempts: int = 3
    job_retention_seconds: int = 604_800

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def authentication_required(self) -> bool:
        return bool(self.api_key)

    def validate(self) -> "WebConfig":
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV must be development, test, or production")
        if self.production and len(self.api_key) < 24:
            raise ValueError("WEB_API_KEY must contain at least 24 characters in production")
        if self.production and self.api_key.upper().startswith("REPLACE_"):
            raise ValueError("WEB_API_KEY production placeholder must be replaced")
        if self.queue_backend not in {"memory", "mysql"}:
            raise ValueError("QUEUE_BACKEND must be memory or mysql")
        if self.production and self.queue_backend != "mysql":
            raise ValueError("QUEUE_BACKEND must be mysql in production")
        if self.queue_backend == "mysql" and not self.database_url:
            raise ValueError("DATABASE_URL is required when QUEUE_BACKEND=mysql")
        if self.database_url and not self.database_url.startswith("mysql+pymysql://"):
            raise ValueError("DATABASE_URL must use the mysql+pymysql driver")
        if self.production and "*" in self.allowed_origins:
            raise ValueError("WEB_ALLOWED_ORIGINS cannot contain * in production")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
                raise ValueError(f"WEB_ALLOWED_ORIGINS contains an invalid origin: {origin}")
        if self.production and self.trust_proxy_headers and self.forwarded_allow_ips == "*":
            raise ValueError("WEB_FORWARDED_ALLOW_IPS cannot be * in production")
        if not 1 <= self.port <= 65_535:
            raise ValueError("WEB_PORT must be between 1 and 65535")
        numeric_ranges = {
            "WEB_RATE_LIMIT_PER_MINUTE": (self.rate_limit_per_minute, 1, 10_000),
            "WEB_MAX_REQUEST_BYTES": (self.max_request_bytes, 1_024, 2_097_152),
            "WEB_JOB_WORKERS": (self.job_workers, 1, 16),
            "WEB_JOB_QUEUE_SIZE": (self.job_queue_size, 0, 1_000),
            "WEB_JOB_TTL_SECONDS": (self.job_ttl_seconds, 60, 86_400),
            "WEB_MAX_STORED_JOBS": (self.max_stored_jobs, 10, 100_000),
            "WEB_HTTP_CONCURRENCY": (self.http_concurrency, 10, 10_000),
            "DATABASE_POOL_SIZE": (self.database_pool_size, 1, 100),
            "DATABASE_MAX_OVERFLOW": (self.database_max_overflow, 0, 100),
            "DATABASE_POOL_RECYCLE_SECONDS": (
                self.database_pool_recycle_seconds,
                60,
                86_400,
            ),
            "JOB_LEASE_SECONDS": (self.job_lease_seconds, 30, 7_200),
            "JOB_HEARTBEAT_SECONDS": (self.job_heartbeat_seconds, 5, 600),
            "JOB_MAX_ATTEMPTS": (self.job_max_attempts, 1, 10),
            "JOB_RETENTION_SECONDS": (self.job_retention_seconds, 3_600, 31_536_000),
        }
        for name, (value, minimum, maximum) in numeric_ranges.items():
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.log_level not in {"critical", "error", "warning", "info", "debug", "trace"}:
            raise ValueError("WEB_LOG_LEVEL is invalid")
        if not 0.1 <= self.job_poll_interval_seconds <= 30:
            raise ValueError("JOB_POLL_INTERVAL_SECONDS must be between 0.1 and 30")
        if self.job_heartbeat_seconds * 2 >= self.job_lease_seconds:
            raise ValueError("JOB_HEARTBEAT_SECONDS must be less than half JOB_LEASE_SECONDS")
        return self

    @classmethod
    def from_env(cls) -> "WebConfig":
        _load_env_file()
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        origins = tuple(
            item.strip()
            for item in os.getenv("WEB_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        database_url = os.getenv("DATABASE_URL", "").strip()
        config = cls(
            app_env=app_env,
            host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
            port=_int("WEB_PORT", 8765, 1, 65_535),
            api_key=os.getenv("WEB_API_KEY", "").strip(),
            allowed_origins=origins,
            trust_proxy_headers=_bool("WEB_TRUST_PROXY_HEADERS", False),
            forwarded_allow_ips=os.getenv("WEB_FORWARDED_ALLOW_IPS", "127.0.0.1").strip(),
            rate_limit_per_minute=_int("WEB_RATE_LIMIT_PER_MINUTE", 20, 1, 10_000),
            max_request_bytes=_int("WEB_MAX_REQUEST_BYTES", 65_536, 1_024, 2_097_152),
            job_workers=_int("WEB_JOB_WORKERS", 1, 1, 16),
            job_queue_size=_int("WEB_JOB_QUEUE_SIZE", 8, 0, 1_000),
            job_ttl_seconds=_int("WEB_JOB_TTL_SECONDS", 3_600, 60, 86_400),
            max_stored_jobs=_int("WEB_MAX_STORED_JOBS", 500, 10, 100_000),
            http_concurrency=_int("WEB_HTTP_CONCURRENCY", 100, 10, 10_000),
            expose_docs=_bool("WEB_EXPOSE_DOCS", app_env != "production"),
            log_level=os.getenv("WEB_LOG_LEVEL", "info").strip().lower(),
            queue_backend=os.getenv(
                "QUEUE_BACKEND",
                "mysql" if database_url else "memory",
            ).strip().lower(),
            database_url=database_url,
            database_pool_size=_int("DATABASE_POOL_SIZE", 5, 1, 100),
            database_max_overflow=_int("DATABASE_MAX_OVERFLOW", 5, 0, 100),
            database_pool_recycle_seconds=_int(
                "DATABASE_POOL_RECYCLE_SECONDS", 1_800, 60, 86_400
            ),
            job_poll_interval_seconds=_float("JOB_POLL_INTERVAL_SECONDS", 0.5, 0.1, 30),
            job_lease_seconds=_int("JOB_LEASE_SECONDS", 900, 30, 7_200),
            job_heartbeat_seconds=_int("JOB_HEARTBEAT_SECONDS", 15, 5, 600),
            job_max_attempts=_int("JOB_MAX_ATTEMPTS", 3, 1, 10),
            job_retention_seconds=_int(
                "JOB_RETENTION_SECONDS", 604_800, 3_600, 31_536_000
            ),
        )
        return config.validate()
