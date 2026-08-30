from __future__ import annotations

from dataclasses import replace

import uvicorn

from .api import WebConfig, create_app


def serve(host: str | None = None, port: int | None = None) -> None:
    config = WebConfig.from_env()
    if host is not None:
        config = replace(config, host=host)
    if port is not None:
        config = replace(config, port=port)
    config.validate()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        access_log=True,
        server_header=False,
        proxy_headers=config.trust_proxy_headers,
        forwarded_allow_ips=config.forwarded_allow_ips if config.trust_proxy_headers else "",
        limit_concurrency=config.http_concurrency,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=30,
    )


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
