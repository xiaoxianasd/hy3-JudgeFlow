ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 65532 sandbox \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin sandbox

WORKDIR /app
COPY hy3_tracejudge/sandbox/runner.py /app/runner.py
RUN chown root:root /app/runner.py && chmod 0555 /app/runner.py

USER 65532:65532
ENTRYPOINT ["python", "-I", "-S", "/app/runner.py"]
