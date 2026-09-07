import os
from logging import LogRecord

from mistralai import workflows

from docstral_worker import IngestionError
from docstral_worker.refresh.config import refresh_config
from docstral_worker.refresh.workflow import RefreshDocumentation


def _redact_vespa_exceptions(record: LogRecord) -> bool:
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    # structlog's payload and the LogRecord feed separate OTel body/attribute paths.
    if isinstance(record.msg, dict):
        for field in ("exc_info", "exception", "stack_info", "stack"):
            record.msg.pop(field, None)
    return True


async def run_worker() -> None:
    import logging

    from mistralai.workflows.core.logging import setup_logging

    config = workflows.config.common
    setup_logging(
        log_level=config.log_level,
        log_format=config.log_format,
        app_version=config.app_version,
        inject_otel_trace=config.otel_enabled and config.otel_inject_logs,
    )
    # Filter at the emitting SDK logger so every handler receives the safe record.
    logging.getLogger(
        "mistralai.search.toolkit.plugins.vespa.search.document_per_chunk_index"
    ).addFilter(_redact_vespa_exceptions)
    refresh_config()
    if not os.environ.get("DEPLOYMENT_NAME", "").strip():
        raise IngestionError("DEPLOYMENT_NAME is required for the Workflows worker")
    await workflows.run_worker([RefreshDocumentation])
