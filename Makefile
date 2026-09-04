.PHONY: vespa-up vespa-reset migrate crawl extract ingest mcp check

VESPA_CONTAINER ?= docstral-vespa
VESPA_QUERY_PORT ?= 8080
VESPA_CONFIG_PORT ?= 19071
VESPA_ENDPOINT ?= http://localhost:$(VESPA_QUERY_PORT)
VESPA_CONFIG_URL ?= http://localhost:$(VESPA_CONFIG_PORT)
VESPA_APP_DIR := packages/vespa/src/docstral_vespa

vespa-up:
	uv run mistral-vespa local up --query-port $(VESPA_QUERY_PORT) --config-port $(VESPA_CONFIG_PORT) --name $(VESPA_CONTAINER)

vespa-reset:
	uv run mistral-vespa local down --name $(VESPA_CONTAINER)

migrate:
	uv run mistral-vespa migrate --app-dir $(VESPA_APP_DIR) --config-server $(VESPA_CONFIG_URL) --query-port $(VESPA_QUERY_PORT)

crawl:
	uv run docstral-worker crawl

extract:
	uv run docstral-worker extract

ingest:
	@echo "Rebuilding local Vespa; the existing local index will be removed."
	$(MAKE) vespa-reset
	$(MAKE) vespa-up
	$(MAKE) migrate
	uv run --env-file .env docstral-worker ingest --vespa-endpoint $(VESPA_ENDPOINT)

mcp:
	uv run --env-file .env docstral-mcp --vespa-endpoint $(VESPA_ENDPOINT)

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest
