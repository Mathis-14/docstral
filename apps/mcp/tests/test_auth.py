import base64
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fastmcp
import httpx2
import pytest
from docstral_backend import AnswerResponse
from docstral_mcp.auth import GoogleAuthConfig
from docstral_mcp.serve import main
from docstral_mcp.server import create_server
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ORIGIN = "http://localhost:8000"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
SCOPES = f"openid {EMAIL_SCOPE}"


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> GoogleAuthConfig:
    for key, value in {
        "DOCSTRAL_GOOGLE_CLIENT_ID": "google-client",
        "DOCSTRAL_GOOGLE_CLIENT_SECRET": "google-secret",
        "DOCSTRAL_OAUTH_BASE_URL": ORIGIN,
        "DOCSTRAL_ALLOWED_EMAILS": " Invite@Example.com ",
        "DOCSTRAL_OAUTH_SIGNING_KEY": "test-signing-material-not-a-real-key",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
    return GoogleAuthConfig()


class _Answerer:
    def __init__(self) -> None:
        self.questions: list[str] = []

    async def answer(self, question: str) -> AnswerResponse:
        self.questions.append(question)
        return AnswerResponse(
            answer=(
                "I couldn't find enough information in the Mistral documentation to "
                "answer this question."
            ),
            abstained=True,
            citations=(),
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCSTRAL_GOOGLE_CLIENT_ID", None),
        ("DOCSTRAL_GOOGLE_CLIENT_SECRET", " "),
        ("DOCSTRAL_OAUTH_SIGNING_KEY", "short-secret"),
        ("DOCSTRAL_ALLOWED_EMAILS", ""),
        ("DOCSTRAL_ALLOWED_EMAILS", "*@example.com"),
        ("DOCSTRAL_ALLOWED_EMAILS", "invite@example.com,"),
        ("DOCSTRAL_OAUTH_BASE_URL", "http://public.example.com"),
        ("DOCSTRAL_OAUTH_BASE_URL", "https://user:secret@example.com"),
        ("DOCSTRAL_OAUTH_BASE_URL", "https://example.com/mcp"),
        ("DOCSTRAL_OAUTH_BASE_URL", "https://example.com?secret=value"),
    ],
)
def test_invalid_oauth_stops_before_backend_without_leaking_input(
    oauth: GoogleAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str | None,
) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as caught:
        main(["--auth", "google"])

    assert caught.value.code == 2
    stderr = capsys.readouterr().err
    assert "input_value" not in stderr
    assert "google-secret" not in stderr
    assert oauth.oauth_signing_key.get_secret_value() not in stderr
    if value is not None and value.strip():
        assert value not in stderr


def test_command_wires_google_and_disables_access_logs(
    oauth: GoogleAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    options: dict[str, object] = {}
    http_logger = logging.getLogger("httpx2")
    monkeypatch.setattr(http_logger, "level", logging.INFO)

    def run(server: fastmcp.FastMCP, **kwargs: object) -> None:
        assert server.auth is not None
        assert server.auth.required_scopes == SCOPES.split()
        http_logger.info("tokeninfo?access_token=must-not-be-logged")
        options.update(kwargs)

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(fastmcp.FastMCP, "run", run)
    assert main(["--auth", "google"]) == 0
    assert options["uvicorn_config"] == {"access_log": False}
    assert options["path"] == "/mcp"
    assert options["stateless_http"] is True
    assert options["json_response"] is True
    assert "must-not-be-logged" not in caplog.text


async def _login(client: httpx2.AsyncClient) -> OAuthToken:
    """Exercise DCR, PKCE and consent over HTTP; only Google is simulated."""
    origin = str(client.base_url).rstrip("/")
    redirect = "http://localhost:49152/callback"
    response = await client.post(
        "/register",
        json={
            "client_name": "Docstral test client",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPES,
        },
    )
    assert response.status_code == 201, response.text
    registration = OAuthClientInformationFull.model_validate(response.json())
    assert registration.client_id is not None
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    response = await client.get(
        "/authorize",
        params={
            "client_id": registration.client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "scope": SCOPES,
            "state": "client-state",
            "resource": f"{origin}/mcp",
        },
    )
    assert response.status_code == 302, response.text
    consent_url = response.headers["location"]
    transaction = parse_qs(urlparse(consent_url).query)["txn_id"][0]
    response = await client.get(consent_url)
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert cookies
    if origin.startswith("https://"):
        assert all("Secure" in cookie and "HttpOnly" in cookie for cookie in cookies)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert csrf is not None
    response = await client.post(
        "/consent",
        data={"txn_id": transaction, "csrf_token": csrf[1], "action": "approve"},
    )
    assert response.status_code == 302, response.text
    upstream = urlparse(response.headers["location"])
    assert upstream.hostname == "accounts.google.com"
    assert parse_qs(upstream.query)["redirect_uri"] == [f"{origin}/auth/callback"]
    response = await client.get(
        "/auth/callback", params={"state": transaction, "code": "google-code"}
    )
    assert response.status_code == 302, response.text
    callback = parse_qs(urlparse(response.headers["location"]).query)
    assert callback["state"] == ["client-state"]
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration.client_id,
            "code": callback["code"][0],
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "resource": f"{origin}/mcp",
        },
    )
    assert response.status_code == 200, response.text
    return OAuthToken.model_validate(response.json())


@pytest.mark.parametrize("origin", [ORIGIN, "https://mcp.example.com"])
@pytest.mark.parametrize(
    ("email", "verified", "audience", "google_status", "permitted"),
    [
        ("INVITE@example.com", True, "google-client", 200, True),
        ("invite@example.com", "true", "google-client", 200, True),
        ("outsider@example.com", True, "google-client", 200, False),
        ("invite@example.com", "false", "google-client", 200, False),
        ("invite@example.com", False, "google-client", 200, False),
        ("invite@example.com", None, "google-client", 200, False),
        ("invite@example.com", True, "another-google-client", 200, False),
        ("invite@example.com", True, "google-client", 503, False),
    ],
)
async def test_google_http_access_and_restart(
    oauth: GoogleAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email: str,
    verified: bool | str | None,
    audience: str,
    google_status: int,
    permitted: bool,
    origin: str,
) -> None:
    monkeypatch.setenv("DOCSTRAL_OAUTH_BASE_URL", origin)
    oauth = GoogleAuthConfig()

    async def google(
        _transport: httpx2.AsyncHTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        if request.url == "https://oauth2.googleapis.com/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "google-access-token",
                    "refresh_token": "google-refresh-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": SCOPES,
                },
            )
        if (
            request.url.host == "oauth2.googleapis.com"
            and request.url.path == "/tokeninfo"
        ):
            return httpx2.Response(
                google_status,
                json={
                    "aud": audience,
                    "sub": "google-subject",
                    "scope": SCOPES,
                    "expires_in": 3600,
                    "email": email,
                    "email_verified": verified,
                },
            )
        if request.url == "https://www.googleapis.com/oauth2/v2/userinfo":
            return httpx2.Response(200, json={})
        raise AssertionError(f"Unexpected external request: {request.url.host}")

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", google)
    answerer = _Answerer()
    app = create_server(answerer, oauth=oauth).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=origin
        ) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.text == "ok"
            discovery = await client.get("/.well-known/oauth-authorization-server")
            assert discovery.status_code == 200
            assert discovery.json()["authorization_endpoint"] == f"{origin}/authorize"
            for headers in ({}, {"Authorization": "Bearer invalid-token"}):
                denied = await client.post("/mcp", json={}, headers=headers)
                assert denied.status_code == 401
                assert "resource_metadata=" in denied.headers["www-authenticate"]
            assert answerer.questions == []
            token = await _login(client)

    # Reconstruct the server: registrations, keys and tokens must survive.
    app = create_server(answerer, oauth=oauth).http_app(
        path="/mcp", stateless_http=True, json_response=True
    )
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=origin,
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "Accept": "application/json, text/event-stream",
            },
        ) as client:
            tools = await client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            result = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "ask_docs", "arguments": {"question": "Test?"}},
                },
            )
    if permitted:
        assert result.status_code == 200, result.text
        assert tools.json()["result"]["tools"][0]["name"] == "ask_docs"
        assert result.json()["result"]["structuredContent"]["abstained"] is True
        assert answerer.questions == ["Test?"]
    else:
        assert answerer.questions == []
        if audience != "google-client" or google_status != 200:
            assert result.status_code == 401
        else:
            assert tools.json()["result"]["tools"] == []
            assert result.json()["result"]["isError"] is True
    persisted = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert persisted
    assert all(b"google-access-token" not in path.read_bytes() for path in persisted)
    assert all(b"google-refresh-token" not in path.read_bytes() for path in persisted)
