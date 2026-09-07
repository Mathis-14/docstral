import asyncio

import httpx
import pytest
from docstral_worker.models import PageResult
from docstral_worker.workflows.activities import activity_scope, sync_page
from mistralai.workflows.exceptions import WorkflowError
from temporalio.testing import ActivityEnvironment
from worker_fixtures import Services, html
from worker_fixtures import services as services


async def sync(path: str = "/a") -> PageResult:
    return await ActivityEnvironment().run(sync_page, f"https://docs.mistral.ai{path}")


async def test_unchanged_page_skips_embedding_and_indexing(services: Services) -> None:
    assert (await sync()).status == "indexed"
    services.requests.clear()
    assert (await sync()).status == "unchanged"
    assert services.calls()["mistral.test"] == 0
    assert all(request.method == "GET" for request in services.requests)


@pytest.mark.parametrize("status_code", [429, 503])
async def test_temporary_robots_failure_allows_native_page_retry(
    services: Services, status_code: int
) -> None:
    services.robots_status = status_code
    with pytest.raises(WorkflowError) as error:
        await sync()
    assert not error.value.non_retryable
    assert [request.url.path for request in services.requests] == ["/robots.txt"]


async def test_changed_page_replaces_chunks_and_confirms_hash(
    services: Services,
) -> None:
    await sync()
    services.pages["/a"] = html("A updated", "New evidence")
    assert (await sync()).status == "indexed"
    chunks = [
        fields["content"]
        for path, fields in services.documents.items()
        if "/docs/docs/" in path
    ]
    assert chunks == ["# A updated\n\nNew evidence"]
    assert (await sync()).status == "unchanged"


async def test_interrupted_indexing_does_not_confirm_page(services: Services) -> None:
    await sync()
    services.pages["/a"] = html("Changed", "Changed evidence")
    services.fail = "index"
    with pytest.raises(WorkflowError) as error:
        await sync()
    assert "private-api-credential" not in str(error.value)
    assert not error.value.non_retryable
    assert all(
        fields["index_hash"] == ""
        for path, fields in services.documents.items()
        if "/pages/pages/" in path
    )
    services.fail = None
    services.pages["/a"] = html("A", "Evidence A")
    assert (await sync()).status == "indexed"


async def test_extraction_failure_preserves_indexed_page(services: Services) -> None:
    await sync()
    previous = services.documents.copy()
    services.pages["/a"] = b"<html>Missing article</html>"
    assert (await sync()).status == "extraction_failed"
    assert services.documents == previous


async def test_redirect_returns_target_without_fetching_it(services: Services) -> None:
    services.redirects["/a"] = "/b"
    result = await sync()
    assert result.redirect_url == "https://docs.mistral.ai/b"
    assert all(request.url.path != "/b" for request in services.requests)
    assert not services.documents


async def test_syntax_redirect_is_fetched_under_same_identity(
    services: Services,
) -> None:
    services.redirects["/a"] = "/en/a/"
    services.pages["/en/a/"] = services.pages["/a"]
    assert (await sync()).status == "indexed"
    assert {fields["source_id"] for fields in services.documents.values()} == {
        "https://docs.mistral.ai/a"
    }


async def test_failed_vespa_cleanup_does_not_leak_secrets_in_logs(
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from docstral_worker.worker import run_worker
    from mistralai import workflows
    from temporalio.api.failure.v1 import Failure
    from temporalio.converter import DataConverter

    async def execute(definitions: list[type[object]]) -> None:
        services.fail = "cleanup"
        with pytest.raises(WorkflowError) as error:
            await sync()
        failure = Failure()
        await DataConverter.default.encode_failure(error.value, failure)
        assert "private-api-credential" not in str(failure)

    monkeypatch.setenv("DEPLOYMENT_NAME", "test")
    monkeypatch.setattr(workflows, "run_worker", execute)
    await run_worker()
    logs = capsys.readouterr()
    assert "private-api-credential" not in logs.out + logs.err
    assert "refresh_activity_failed" in logs.out + logs.err


async def test_lost_heartbeat_stops_page_before_index_writes(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mistralai import workflows

    def disconnected() -> None:
        raise httpx.ConnectError("Control plane unavailable")

    monkeypatch.setattr(workflows, "activity_heartbeat", disconnected)
    with pytest.raises(WorkflowError) as error:
        await sync()
    assert not error.value.non_retryable
    assert not services.documents


async def test_robots_denial_is_returned_as_unreliable_exploration(
    services: Services,
) -> None:
    services.responses["/robots.txt"] = httpx.Response(
        200, text="User-agent: *\nDisallow: /a\n"
    )
    result = await sync()
    assert result.reason == "robots_disallowed"
    assert not services.documents
    assert all(request.url.path == "/robots.txt" for request in services.requests)


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_activity_stops_heartbeats_on_exit(outcome: str) -> None:
    received = asyncio.Event()
    heartbeat_tasks: list[asyncio.Task[object]] = []
    environment = ActivityEnvironment()

    def on_heartbeat(*details: object) -> None:
        task = asyncio.current_task()
        assert task is not None
        heartbeat_tasks.append(task)
        received.set()

    environment.on_heartbeat = on_heartbeat

    async def execute() -> None:
        async with activity_scope("sync_page", "https://docs.mistral.ai/a"):
            await received.wait()
            if outcome == "failure":
                raise RuntimeError("private-api-credential")
            if outcome == "cancel":
                await asyncio.Future[None]()

    async with asyncio.timeout(5):
        activity = asyncio.create_task(environment.run(execute))
        try:
            await received.wait()
            if outcome == "cancel":
                activity.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await activity
            elif outcome == "failure":
                with pytest.raises(
                    WorkflowError, match=r"sync_page.*RuntimeError"
                ) as error:
                    await activity
                assert not error.value.non_retryable
                assert "private-api-credential" not in str(error.value)
            else:
                await activity
        finally:
            if not activity.done():
                activity.cancel()
                await asyncio.gather(activity, return_exceptions=True)
    assert heartbeat_tasks
    assert all(task.cancelled() for task in heartbeat_tasks)
