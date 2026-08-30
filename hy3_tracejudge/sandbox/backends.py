from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = Path(__file__).resolve().with_name("runner.py")
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")


def _load_env_file() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().startswith(("SANDBOX_", "APP_ENV")):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 3.0
    memory_mb: int = 256
    cpus: float = 1.0
    pids: int = 32
    max_input_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_code_bytes: int = 64 * 1024
    max_tests: int = 512
    max_value_bytes: int = 64 * 1024

    @classmethod
    def from_env(cls, *, timeout_seconds: float) -> "SandboxLimits":
        _load_env_file()
        return cls(
            timeout_seconds=max(0.1, min(float(timeout_seconds), 30.0)),
            memory_mb=_bounded_int("SANDBOX_MEMORY_MB", 256, 64, 2048),
            cpus=_bounded_float("SANDBOX_CPUS", 1.0, 0.1, 4.0),
            pids=_bounded_int("SANDBOX_PIDS_LIMIT", 32, 8, 128),
            max_input_bytes=_bounded_int(
                "SANDBOX_MAX_INPUT_BYTES", 2 * 1024 * 1024, 64 * 1024, 8 * 1024 * 1024
            ),
            max_output_bytes=_bounded_int(
                "SANDBOX_MAX_OUTPUT_BYTES", 1024 * 1024, 64 * 1024, 8 * 1024 * 1024
            ),
            max_code_bytes=_bounded_int(
                "SANDBOX_MAX_CODE_BYTES", 64 * 1024, 1024, 256 * 1024
            ),
            max_tests=_bounded_int("SANDBOX_MAX_TESTS", 512, 1, 2000),
            max_value_bytes=_bounded_int(
                "SANDBOX_MAX_VALUE_BYTES", 64 * 1024, 1024, 1024 * 1024
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_mb": self.memory_mb,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_code_bytes": self.max_code_bytes,
            "max_tests": self.max_tests,
            "max_value_bytes": self.max_value_bytes,
        }


@dataclass(frozen=True)
class SandboxExecution:
    ok: bool
    results: list[dict[str, Any]]
    error: str | None
    backend: str


class LocalSandboxBackend:
    """Development backend. It applies the runner policy but is not a host boundary."""

    name = "local"

    def run(self, payload: dict[str, Any], limits: SandboxLimits) -> SandboxExecution:
        command = [sys.executable, "-I", "-S", str(RUNNER_PATH)]
        return _run_command(command, payload, limits, backend=self.name)


class DockerSandboxBackend:
    """Production backend: one locked-down, disposable container per execution."""

    name = "docker"

    def __init__(self, image: str | None = None):
        _load_env_file()
        self.image = image or os.getenv("SANDBOX_DOCKER_IMAGE", "hy3-process-sandbox:py3.12")
        if not _IMAGE_PATTERN.fullmatch(self.image):
            raise ValueError("Invalid SANDBOX_DOCKER_IMAGE")

    def run(self, payload: dict[str, Any], limits: SandboxLimits) -> SandboxExecution:
        container_name = "hy3-sandbox-" + uuid.uuid4().hex
        memory = f"{limits.memory_mb}m"
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--ipc",
            "none",
            "--read-only",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65532:65532",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--stop-timeout",
            "1",
            "--init",
            "-i",
            self.image,
        ]
        try:
            return _run_command(
                command,
                payload,
                limits,
                backend=self.name,
                startup_grace_seconds=5.0,
            )
        except subprocess.TimeoutExpired:
            _remove_timed_out_container(container_name)
            return SandboxExecution(False, [], "TimeLimitExceeded", self.name)


def _encoded_payload(payload: dict[str, Any], limits: SandboxLimits) -> bytes:
    wrapped = {**payload, "limits": limits.to_payload()}
    encoded = json.dumps(wrapped, ensure_ascii=False).encode("utf-8")
    if len(encoded) > limits.max_input_bytes:
        raise ValueError("SandboxInputLimitExceeded")
    return encoded


def _run_command(
    command: list[str],
    payload: dict[str, Any],
    limits: SandboxLimits,
    *,
    backend: str,
    startup_grace_seconds: float = 0.5,
) -> SandboxExecution:
    try:
        encoded = _encoded_payload(payload, limits)
    except (TypeError, ValueError) as exc:
        return SandboxExecution(False, [], str(exc), backend)
    try:
        completed = subprocess.run(
            command,
            input=encoded,
            capture_output=True,
            timeout=limits.timeout_seconds + startup_grace_seconds,
            check=False,
        )
    except FileNotFoundError:
        return SandboxExecution(False, [], f"SandboxBackendUnavailable:{command[0]}", backend)
    except subprocess.TimeoutExpired:
        if backend == "docker":
            raise
        return SandboxExecution(False, [], "TimeLimitExceeded", backend)
    if len(completed.stdout) > limits.max_output_bytes:
        return SandboxExecution(False, [], "SandboxOutputLimitExceeded", backend)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[:500].strip()
        suffix = f":{stderr}" if stderr else ""
        return SandboxExecution(
            False,
            [],
            f"SandboxExit{completed.returncode}{suffix}",
            backend,
        )
    try:
        output = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SandboxExecution(False, [], "InvalidSandboxOutput", backend)
    if not isinstance(output, dict) or not isinstance(output.get("ok"), bool):
        return SandboxExecution(False, [], "InvalidSandboxProtocol", backend)
    results = output.get("results", [])
    if not isinstance(results, list):
        return SandboxExecution(False, [], "InvalidSandboxResults", backend)
    return SandboxExecution(
        bool(output["ok"]),
        results,
        None if output["ok"] else str(output.get("error", "sandbox execution failed"))[:500],
        backend,
    )


def _remove_timed_out_container(container_name: str) -> None:
    if not re.fullmatch(r"hy3-sandbox-[0-9a-f]{32}", container_name):
        return
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def execute_payload(
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> SandboxExecution:
    _load_env_file()
    limits = SandboxLimits.from_env(timeout_seconds=timeout_seconds)
    backend_name = os.getenv("SANDBOX_BACKEND", "local").strip().lower()
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    allow_local = _env_bool("ALLOW_UNSAFE_LOCAL_EXECUTION", True)
    if backend_name == "local":
        if app_env == "production" or not allow_local:
            return SandboxExecution(
                False,
                [],
                "UnsafeLocalSandboxDisabled",
                "local",
            )
        backend = LocalSandboxBackend()
    elif backend_name == "docker":
        try:
            backend = DockerSandboxBackend()
        except ValueError as exc:
            return SandboxExecution(False, [], str(exc), "docker")
    else:
        return SandboxExecution(False, [], f"UnknownSandboxBackend:{backend_name}", backend_name)
    return backend.run(payload, limits)
