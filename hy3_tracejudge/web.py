from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .catalog import get_problem, load_problems
from .evaluator import evaluate_answer
from .hy3_client import Hy3APIError, Hy3Client


ASSET = Path(__file__).resolve().parent / "web_assets" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "Hy3TraceJudge/0.1"

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            data = ASSET.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/problems":
            self._json(
                [
                    {key: item[key] for key in ("id", "title", "difficulty", "statement", "source")}
                    for item in load_problems()
                ]
            )
            return
        if self.path == "/api/health":
            try:
                self._json(Hy3Client().health())
            except Hy3APIError as exc:
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/run":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            problem = get_problem(payload["problem_id"])
            examples = max(1, min(int(payload.get("hypothesis_examples", 60)), 500))
            review_mode = str(payload.get("review_mode", "supervisor"))
            if review_mode not in {"single", "supervisor", "swarm"}:
                raise ValueError(f"Unsupported review mode: {review_mode}")
            client = Hy3Client()
            answer, generation = client.solve(problem)
            evaluation = evaluate_answer(
                problem,
                answer,
                hypothesis_examples=examples,
                hy3_client=client,
                review_mode=review_mode,
                reveal_hidden=False,
            )
            self._json({"answer": answer, "generation": generation, "evaluation": evaluation})
        except (Hy3APIError, KeyError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        print("[web] " + (format % args))


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Hy3 TraceJudge: http://{host}:{port}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    serve()
