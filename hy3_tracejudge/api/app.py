from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from ..catalog import get_problem, load_problems
from ..hy3_client import Hy3APIError, Hy3Client
from .config import WebConfig
from .database import DatabaseUnavailable, MySQLJobStore, create_database_engine
from .db_jobs import DatabaseEvaluationJobManager
from .jobs import EvaluationJobManager, JobNotFound, JobQueueFull


ASSETS = Path(__file__).resolve().parent.parent / "web_assets"
REVIEW_MODES = {"single", "supervisor", "swarm"}
LOGGER = logging.getLogger("hy3_tracejudge.api")


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    problem_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
    hypothesis_examples: int = Field(default=60, ge=1, le=500)
    review_mode: Literal["single", "supervisor", "swarm"] = "supervisor"


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class CodeSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
    hypothesis_examples: int = Field(default=60, ge=1, le=500)
    code: str = Field(min_length=1, max_length=20_000)
    reasoning_steps: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("code")
    @classmethod
    def nonempty_code(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("代码不能为空或包含空字符")
        return value  # Preserve indentation and exact line numbers.

    @field_validator("reasoning_steps")
    @classmethod
    def bounded_steps(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("每个步骤应包含 1–2000 个字符")
        return [value.strip() for value in values]


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def consume(self, key: str) -> int | None:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self._limit:
                return max(1, int(self._window - (now - events[0])) + 1)
            events.append(now)
            if not events:
                self._events.pop(key, None)
        return None


class DockerReadinessProbe:
    def __init__(self, cache_seconds: float = 15.0) -> None:
        self._cache_seconds = cache_seconds
        self._checked_at = 0.0
        self._ready = False
        self._lock = threading.Lock()

    def ready(self, image: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._checked_at < self._cache_seconds:
                return self._ready
            docker = shutil.which("docker")
            if docker is None:
                self._ready = False
            else:
                try:
                    result = subprocess.run(
                        [docker, "image", "inspect", image],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                        check=False,
                    )
                    self._ready = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    self._ready = False
            self._checked_at = now
            return self._ready


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message},
        "request_id": _request_id(request),
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body, headers=headers)


def _client_address(request: Request, config: WebConfig) -> str:
    direct = request.client.host if request.client else "unknown"
    if not config.trust_proxy_headers:
        return direct
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if not forwarded:
        return direct
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return direct


async def _bounded_payload(request: Request, config: WebConfig, request_model: type[BaseModel] = EvaluationRequest) -> Any:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise APIError(415, "unsupported_media_type", "Content-Type 必须是 application/json")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > config.max_request_bytes:
            raise APIError(413, "payload_too_large", "请求体超过允许大小")
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise APIError(400, "invalid_json", "请求体必须是合法 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise APIError(422, "validation_error", "请求体必须是 JSON 对象")
    try:
        return request_model.model_validate(raw)
    except ValidationError as exc:
        details = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors(include_input=False)
        ]
        error = APIError(422, "validation_error", "请求参数不符合要求")
        setattr(error, "details", details)
        raise error from exc


def create_app(
    config: WebConfig | None = None,
    manager: Any | None = None,
) -> FastAPI:
    config = (config or WebConfig.from_env()).validate()
    owns_manager = manager is None
    if manager is None and config.queue_backend == "mysql":
        engine = create_database_engine(
            config.database_url,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            pool_recycle_seconds=config.database_pool_recycle_seconds,
        )
        store = MySQLJobStore(
            engine,
            capacity=config.job_workers + config.job_queue_size,
            max_attempts=config.job_max_attempts,
        )
        manager = DatabaseEvaluationJobManager(
            store,
            workers=config.job_workers,
            poll_interval_seconds=config.job_poll_interval_seconds,
            lease_seconds=config.job_lease_seconds,
            heartbeat_seconds=config.job_heartbeat_seconds,
            retention_seconds=config.job_retention_seconds,
        )
    elif manager is None:
        manager = EvaluationJobManager(
            workers=config.job_workers,
            queue_size=config.job_queue_size,
            ttl_seconds=config.job_ttl_seconds,
            max_stored_jobs=config.max_stored_jobs,
        )
    limiter = SlidingWindowLimiter(config.rate_limit_per_minute)
    docker_probe = DockerReadinessProbe()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if owns_manager:
            manager.start()
        try:
            yield
        finally:
            if owns_manager:
                manager.shutdown()

    docs_url = "/api/docs" if config.expose_docs else None
    app = FastAPI(
        title="TraceJudge API",
        version="1.0.0",
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/api/openapi.json" if config.expose_docs else None,
        lifespan=lifespan,
    )
    app.state.web_config = config
    app.state.job_manager = manager

    if config.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Accept", "Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
            max_age=600,
        )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        supplied_id = request.headers.get("x-request-id", "")
        request.state.request_id = (
            supplied_id if 1 <= len(supplied_id) <= 64 and supplied_id.replace("-", "").isalnum()
            else uuid.uuid4().hex
        )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > config.max_request_bytes:
                    return _error_response(
                        request,
                        status=413,
                        code="payload_too_large",
                        message="请求体超过允许大小",
                    )
            except ValueError:
                return _error_response(
                    request,
                    status=400,
                    code="invalid_content_length",
                    message="Content-Length 无效",
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; form-action 'none'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return _error_response(
            request,
            status=exc.status,
            code=exc.code,
            message=exc.message,
            headers=exc.headers,
            details=getattr(exc, "details", None),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        details = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return _error_response(
            request,
            status=422,
            code="validation_error",
            message="请求参数不符合要求",
            details=details,
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        message = str(exc.detail) if isinstance(exc.detail, str) else "请求失败"
        return _error_response(
            request,
            status=exc.status_code,
            code="http_error",
            message=message,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        LOGGER.exception("Unhandled API error; request_id=%s", _request_id(request), exc_info=exc)
        return _error_response(
            request,
            status=500,
            code="internal_error",
            message="服务内部错误，请向管理员提供 request_id",
        )

    async def require_api_key(request: Request) -> str:
        if not config.authentication_required:
            return "development"
        token = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        if not token and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token or not hmac.compare_digest(token, config.api_key):
            raise APIError(
                401,
                "unauthorized",
                "缺少或无效的 API 访问密钥",
                {"WWW-Authenticate": "Bearer"},
            )
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    @app.get("/api/v1/config")
    async def public_config() -> dict[str, Any]:
        return {
            "api_version": "v1",
            "authentication_required": config.authentication_required,
            "hypothesis_examples": {"default": 60, "minimum": 1, "maximum": 500},
            "review_modes": sorted(REVIEW_MODES),
            "code_submission": {
                "enabled": os.getenv("SANDBOX_BACKEND", "local").strip().lower() == "docker",
                "language": "python", "max_code_chars": 20_000, "max_steps": 20,
                "max_step_chars": 2000, "max_request_bytes": config.max_request_bytes,
            },
        }

    @app.get("/api/v1/health/live")
    async def liveness() -> dict[str, Any]:
        return {"ok": True, "service": "tracejudge-api", "version": "1.0.0"}

    @app.get("/api/v1/health/ready")
    async def readiness(request: Request):
        sandbox_backend = os.getenv("SANDBOX_BACKEND", "local").strip().lower()
        checks = {
            "job_manager": manager.accepting_jobs,
            "queue_backend": config.queue_backend,
            "sandbox_backend": sandbox_backend,
        }
        if config.production:
            checks["production_sandbox"] = sandbox_backend == "docker"
            checks["sandbox_image"] = docker_probe.ready(
                os.getenv("SANDBOX_DOCKER_IMAGE", "hy3-process-sandbox:py3.12")
            )
        ready = all(value is True or isinstance(value, str) for value in checks.values())
        if config.production:
            ready = bool(
                checks["job_manager"]
                and checks.get("production_sandbox")
                and checks.get("sandbox_image")
            )
        content = {"ok": ready, "checks": checks}
        return JSONResponse(status_code=200 if ready else 503, content=content)

    @app.get("/api/v1/health/hy3")
    async def upstream_health(_: str = Depends(require_api_key)):
        try:
            health = await run_in_threadpool(Hy3Client().health)
        except Hy3APIError:
            raise APIError(503, "upstream_unavailable", "Hy3 服务当前不可用")
        return {
            "ok": bool(health.get("ok")),
            "requested_model": health.get("requested_model"),
            "latency_ms": health.get("latency_ms"),
        }

    @app.get("/api/v1/problems")
    async def problems() -> list[dict[str, Any]]:
        result = []
        for item in load_problems():
            title = str(item.get("title", "")).strip().strip('"').strip()
            if not title:
                title = next(
                    (
                        line.strip().strip('"').strip()
                        for line in str(item.get("statement", "")).splitlines()
                        if line.strip().strip('"').strip()
                        and not line.lstrip().startswith("assert ")
                    ),
                    item["id"],
                )
            result.append(
                {
                    "id": item["id"],
                    "title": title,
                    "difficulty": item["difficulty"],
                    "statement": item["statement"],
                    "source_statement": item.get("source_statement") or item["statement"],
                    "input_schema": item.get("input_schema", {}),
                    "constraints": item.get("constraints", []),
                    "public_tests": [
                        {
                            "name": test.get("name", ""),
                            "input": test["input"],
                            "expected": test["expected"],
                        }
                        for test in item.get("public_tests", [])
                    ],
                    "source": item.get("source", "未标注"),
                    "tier": item.get("tier", "seed"),
                    "function_name": item.get("function_name", "solve_case"),
                    "public_test_count": len(item.get("public_tests", [])),
                    "hidden_test_count": len(item.get("hidden_tests", [])),
                }
            )
        return result

    @app.post(
        "/api/v1/evaluations",
        status_code=202,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": EvaluationRequest.model_json_schema()}},
            }
        },
    )
    async def create_evaluation(request: Request, principal: str = Depends(require_api_key)):
        payload = await _bounded_payload(request, config)
        return await enqueue_evaluation(request, principal, payload)

    @app.post(
        "/api/v1/code-submissions", status_code=202,
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": CodeSubmissionRequest.model_json_schema()},
        }}},
    )
    async def create_code_submission(request: Request, principal: str = Depends(require_api_key)):
        payload = await _bounded_payload(request, config, CodeSubmissionRequest)
        return await enqueue_evaluation(request, principal, payload, submission={
            "code": payload.code, "reasoning_steps": payload.reasoning_steps,
        })

    async def enqueue_evaluation(request: Request, principal: str, payload: Any, submission: dict[str, Any] | None = None):
        retry_after = limiter.consume(f"{principal}:{_client_address(request, config)}")
        if retry_after is not None:
            raise APIError(
                429,
                "rate_limited",
                "提交过于频繁，请稍后重试",
                {"Retry-After": str(retry_after)},
            )
        try:
            get_problem(payload.problem_id)
        except KeyError:
            raise APIError(404, "problem_not_found", "指定题目不存在")
        if submission is not None:
            if os.getenv("SANDBOX_BACKEND", "local").strip().lower() != "docker":
                raise APIError(503, "submission_sandbox_required", "提交用户代码必须启用 Docker 安全沙盒，请配置 SANDBOX_BACKEND=docker 后重启服务")
            if not await run_in_threadpool(docker_probe.ready, os.getenv("SANDBOX_DOCKER_IMAGE", "hy3-process-sandbox:py3.12")):
                raise APIError(503, "submission_sandbox_unavailable", "Docker 沙盒未就绪，请启动 Docker 并构建沙盒镜像后重试")
        try:
            options = {"submission": submission} if submission is not None else {}
            job = await run_in_threadpool(
                manager.submit,
                payload.problem_id,
                payload.hypothesis_examples,
                "submission" if submission is not None else payload.review_mode,
                **options,
            )
        except JobQueueFull:
            raise APIError(
                503,
                "queue_full",
                "评估队列已满，请稍后重试",
                {"Retry-After": "10"},
            )
        except DatabaseUnavailable:
            raise APIError(
                503,
                "database_unavailable",
                "任务数据库当前不可用，请稍后重试",
                {"Retry-After": "5"},
            )
        return {"job": job, "status_url": f"/api/v1/evaluations/{job['id']}"}

    @app.get("/api/v1/evaluations/{job_id}")
    async def get_evaluation(job_id: str, _: str = Depends(require_api_key)):
        if len(job_id) != 32 or not job_id.isalnum():
            raise APIError(404, "job_not_found", "评估任务不存在或已过期")
        try:
            job = manager.snapshot(job_id)
        except JobNotFound:
            raise APIError(404, "job_not_found", "评估任务不存在或已过期")
        except DatabaseUnavailable:
            raise APIError(
                503,
                "database_unavailable",
                "任务数据库当前不可用，请稍后重试",
                {"Retry-After": "5"},
            )
        return {"job": job}

    @app.get("/api/v1/queue/stats")
    async def queue_stats(_: str = Depends(require_api_key)):
        try:
            return await run_in_threadpool(manager.stats)
        except DatabaseUnavailable:
            raise APIError(
                503,
                "database_unavailable",
                "任务数据库当前不可用，请稍后重试",
                {"Retry-After": "5"},
            )

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(ASSETS / "index.html", media_type="text/html")

    return app
