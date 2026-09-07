from collections.abc import Callable

from protego import Protego
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError, _safe_url
from docstral_worker.urls import DOCS_HOST

ROBOTS_AGENT = "Docstral"
ROBOTS_URL = f"https://{DOCS_HOST}/robots.txt"


class RobotsError(IngestionError):
    def __init__(self, url: str, detail: str) -> None:
        self.url = _safe_url(url)
        super().__init__(f"Robots {self.url!r}: {detail}")


class RobotsUnavailableError(RobotsError):
    def __init__(
        self, url: str, detail: str, *, status_code: int | None = None
    ) -> None:
        self.status_code = status_code
        super().__init__(url, detail)


class RobotsDeniedError(RobotsError):
    pass


class RobotsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    status_code: int = Field(ge=100, le=599)
    content_type: str | None
    body: bytes


class RobotsPolicy:
    def __init__(self, parser: Protego | None, request_interval: float) -> None:
        self._parser = parser
        self.request_interval = request_interval

    def check(self, url: str) -> None:
        if self._parser is not None and not self._parser.can_fetch(url, ROBOTS_AGENT):
            raise RobotsDeniedError(url, "disallowed by robots.txt")


def load_robots(
    loader: Callable[[], RobotsResponse], configured_delay: float
) -> RobotsPolicy:
    response = loader()
    if 400 <= response.status_code < 500 and response.status_code != 429:
        return RobotsPolicy(None, configured_delay)
    if response.status_code != 200:
        raise RobotsUnavailableError(
            response.url,
            f"unexpected HTTP {response.status_code}",
            status_code=response.status_code,
        )
    if response.content_type != "text/plain":
        raise RobotsUnavailableError(
            response.url, f"expected text/plain, got {response.content_type!r}"
        )
    try:
        text = response.body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RobotsUnavailableError(
            response.url, "response is not valid UTF-8"
        ) from exc

    parser = Protego.parse(text)
    return RobotsPolicy(parser, _request_interval(parser, configured_delay))


def _request_interval(parser: Protego, configured_delay: float) -> float:
    intervals = [configured_delay]
    crawl_delay = parser.crawl_delay(ROBOTS_AGENT)
    if crawl_delay is not None:
        intervals.append(float(crawl_delay))
    request_rate = parser.request_rate(ROBOTS_AGENT)
    if request_rate is not None and request_rate.requests > 0:
        intervals.append(request_rate.seconds / request_rate.requests)
    return max(intervals)
