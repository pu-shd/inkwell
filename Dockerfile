# syntax=docker/dockerfile:1.7
FROM python:3.12.11-slim-bookworm AS base
LABEL org.opencontainers.image.source="https://github.com/pu-shd/inkwell" \
      org.opencontainers.image.description="Presidio-based scrubber for sensitive records"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Unprivileged runtime user; input is mounted read-only.
RUN useradd --create-home --uid 10001 scrub
USER scrub
ENTRYPOINT ["inkwell"]

# ---------------------------------------------------------------------------
FROM base AS test
USER root
RUN uv sync --frozen
COPY tests ./tests
COPY CHANGELOG.md ./
COPY scripts/check_dist.py ./scripts/
USER scrub
ENV RUFF_CACHE_DIR=/tmp/ruff-cache MYPY_CACHE_DIR=/tmp/mypy-cache
ENTRYPOINT []
CMD ["sh", "-c", "ruff check src tests scripts && ruff format --check src tests scripts && mypy src scripts/check_dist.py && pytest -q -p no:cacheprovider"]
