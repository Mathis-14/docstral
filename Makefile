.DEFAULT_GOAL := local
.PHONY: local ingestion check

VESPA_CONTAINER ?= docstral-vespa
VESPA_QUERY_PORT ?= 8080
VESPA_CONFIG_PORT ?= 19071

local ingestion:
	uv sync --locked --all-packages
	VESPA_CONTAINER=$(VESPA_CONTAINER) VESPA_QUERY_PORT=$(VESPA_QUERY_PORT) VESPA_CONFIG_PORT=$(VESPA_CONFIG_PORT) uv run --locked --all-packages --env-file .env python task.py $(if $(filter ingestion,$@),--refresh,)

check:
	uv run --locked --all-packages ruff check .
	uv run --locked --all-packages ruff format --check .
	uv run --locked --all-packages mypy
	uv run --locked --all-packages pytest
