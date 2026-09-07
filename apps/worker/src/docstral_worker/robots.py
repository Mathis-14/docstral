from crawlee.http_clients import HttpClient
from protego import Protego

from docstral_worker.fetch import FetchError, FetchHttpStatusError, get
from docstral_worker.urls import DOCS_HOST

ROBOTS_AGENT = "Docstral"
ROBOTS_URL = f"https://{DOCS_HOST}/robots.txt"


class RobotsDeniedError(FetchError):
    pass


async def load_robots(client: HttpClient) -> Protego:
    response = await get(client, ROBOTS_URL, follow_redirects=True)
    status = response.status_code
    if 400 <= status < 500 and status != 429:
        content = "User-agent: *\nAllow: /"
    elif status != 200:
        raise FetchHttpStatusError(ROBOTS_URL, status)
    else:
        try:
            content = (await response.read()).decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise FetchError(ROBOTS_URL, "robots.txt is not UTF-8") from error
    # Use Crawlee's parser directly: its RobotsTxtFile wrapper truncates delays.
    return Protego.parse(content)


def check_robots(policy: Protego, url: str) -> None:
    if not policy.can_fetch(url, ROBOTS_AGENT):
        raise RobotsDeniedError(url, "robots_disallowed")


def request_delay(policy: Protego, configured: float) -> float:
    return max(configured, policy.crawl_delay(ROBOTS_AGENT) or 0)
