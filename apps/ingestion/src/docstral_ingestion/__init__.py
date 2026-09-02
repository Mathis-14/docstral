from urllib.parse import urlsplit, urlunsplit


class IngestionError(Exception):
    """Base error for documentation ingestion."""


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "<invalid URL>"

    if host is None:
        netloc = ""
    else:
        normalized_host = f"[{host}]" if ":" in host else host
        netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
