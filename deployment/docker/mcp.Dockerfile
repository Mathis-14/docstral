FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.18@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv needs every workspace manifest, even when installing one application.
COPY apps/backend/pyproject.toml apps/backend/pyproject.toml
COPY apps/mcp/pyproject.toml apps/mcp/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/vespa/pyproject.toml packages/vespa/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package docstral-mcp

COPY apps/backend/src apps/backend/src
COPY apps/mcp/src apps/mcp/src
COPY packages/vespa/src packages/vespa/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package docstral-mcp

FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
RUN groupadd --gid 1000 docstral \
    && useradd --uid 1000 --gid docstral --create-home docstral
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["docstral-mcp", "--host", "0.0.0.0"]
