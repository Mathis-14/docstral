import asyncio
from collections import deque
from datetime import timedelta

from mistralai import workflows
from mistralai.workflows.exceptions import ActivityError, WorkflowError

with workflows.workflow.unsafe.imports_passed_through():
    from docstral_worker.refresh import WORKFLOW_NAME
    from docstral_worker.refresh.activities import (
        delete_page,
        discover_urls,
        plan_deletions,
        sync_page,
    )
    from docstral_worker.refresh.models import PageResult, RefreshResult
    from docstral_worker.urls import admit, canonicalize


@workflows.workflow.define(
    name=WORKFLOW_NAME,
    enforce_determinism=True,
    execution_timeout=timedelta(minutes=50),
)
class RefreshDocumentation:
    @workflows.workflow.entrypoint
    async def run(self) -> RefreshResult:
        started = workflows.workflow.now()
        discovery = await discover_urls()
        queue: deque[str] = deque()
        seen: set[str] = set()
        present: set[str] = set()
        failed: set[str] = set()
        redirects: dict[str, str] = {}
        indexed = unchanged = deleted = 0
        reliable = True

        def enqueue(url: str) -> None:
            target = canonicalize(url, url)
            if not admit(target).admitted or target.url in seen:
                return
            if len(seen) >= discovery.max_pages:
                raise WorkflowError(
                    "URL limit reached; exploration is incomplete, deletions refused",
                    non_retryable=True,
                )
            seen.add(target.url)
            queue.append(target.url)

        for url in discovery.urls:
            enqueue(url)
        while queue:
            batch: list[tuple[str, asyncio.Task[PageResult]]] = []
            for _ in range(min(discovery.concurrency, len(queue))):
                url = queue.popleft()
                batch.append((url, asyncio.create_task(sync_page(url))))
                await asyncio.sleep(discovery.request_delay)
            for url, task in batch:
                try:
                    result = await task
                except (ActivityError, WorkflowError):
                    failed.add(url)
                    reliable = False
                    continue
                if result.status == "redirected":
                    destination = result.redirect_url
                    if destination is None:
                        raise WorkflowError(
                            "Redirect has no destination", non_retryable=True
                        )
                    redirects[url] = destination
                    path = {url}
                    while destination in redirects:
                        if destination in path:
                            raise WorkflowError(
                                "Redirect cycle detected", non_retryable=True
                            )
                        path.add(destination)
                        destination = redirects[destination]
                    enqueue(redirects[url])
                elif result.status in ("indexed", "unchanged", "extraction_failed"):
                    present.add(result.url)
                    indexed += result.status == "indexed"
                    unchanged += result.status == "unchanged"
                    if result.status == "extraction_failed":
                        failed.add(result.url)
                elif result.reason == "robots_disallowed":
                    failed.add(url)
                    reliable = False
                for link in result.links:
                    enqueue(link)
        if reliable:
            for url in await plan_deletions(tuple(sorted(present))):
                await delete_page(url)
                deleted += 1
        return RefreshResult(
            indexed=indexed,
            unchanged=unchanged,
            changed=indexed,
            deleted=deleted,
            failed=len(failed),
            failed_urls=tuple(sorted(failed)),
            discovered=len(seen),
            deletions_skipped=not reliable,
            duration_seconds=(workflows.workflow.now() - started).total_seconds(),
            status="partial" if failed else "complete",
        )
