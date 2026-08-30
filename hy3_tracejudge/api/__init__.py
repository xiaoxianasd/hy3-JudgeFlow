"""Production HTTP API for TraceJudge."""

from .app import create_app
from .config import WebConfig

__all__ = ["WebConfig", "create_app"]
