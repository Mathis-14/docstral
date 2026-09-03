import pytest
from docstral_ingestion.urls import (
    RejectionReason,
    UrlCanonicalizationError,
    admit,
    canonicalize,
)

BASE = "https://docs.mistral.ai/getting-started/overview"


@pytest.mark.parametrize(
    ("raw", "expected_url", "expected_anchor"),
    [
        ("/studio/", "https://docs.mistral.ai/studio", None),
        ("/Studio", "https://docs.mistral.ai/studio", None),
        ("/en/studio", "https://docs.mistral.ai/studio", None),
        ("/en", "https://docs.mistral.ai/", None),
        ("/enterprise", "https://docs.mistral.ai/enterprise", None),
        ("mailto:team@example.com", "mailto:team@example.com", None),
        ("//docs.mistral.ai/studio", "https://docs.mistral.ai/studio", None),
        (
            "https://DOCS.MISTRAL.AI/studio",
            "https://docs.mistral.ai/studio",
            None,
        ),
        (
            "/studio/?source=nav#chat",
            "https://docs.mistral.ai/studio",
            "chat",
        ),
        ("http://docs.mistral.ai/models/", "https://docs.mistral.ai/models", None),
        (
            "http://docs.mistral.ai:80/models/",
            "https://docs.mistral.ai/models",
            None,
        ),
    ],
)
def test_canonicalize(raw: str, expected_url: str, expected_anchor: str | None) -> None:
    result = canonicalize(raw, BASE)

    assert result.url == expected_url
    assert result.anchor == expected_anchor


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://docs.mistral.ai/studio", None),
        (
            "https://docs.mistral.ai/api",
            RejectionReason.EXCLUDED_ROUTE,
        ),
        ("https://example.com/studio", RejectionReason.OUTSIDE_HOST),
        ("https://docs.mistral.ai/fr/guide", RejectionReason.FRENCH),
        ("https://docs.mistral.ai/logo.PNG", RejectionReason.ASSET),
        (
            "https://docs.mistral.ai/api/endpoint/chat",
            RejectionReason.EXCLUDED_ROUTE,
        ),
        (
            "https://docs.mistral.ai/resources/cookbooks/rag",
            RejectionReason.EXCLUDED_ROUTE,
        ),
    ],
)
def test_admit(url: str, reason: RejectionReason | None) -> None:
    decision = admit(canonicalize(url, BASE))

    assert decision.admitted is (reason is None)
    assert decision.reason is reason


def test_canonicalize_names_invalid_url() -> None:
    with pytest.raises(UrlCanonicalizationError, match="Cannot canonicalize"):
        canonicalize("https://docs.mistral.ai:invalid/studio", BASE)
