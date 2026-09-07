import os
import sys
from time import monotonic, sleep

from mistralai.client import Mistral
from mistralai.client.models import ScheduleDefinitionOutput

WORKFLOW = "docstral-refresh"


def schedules(client: Mistral, deployment: str) -> list[ScheduleDefinitionOutput]:
    selected: list[ScheduleDefinitionOutput] = []
    token: str | None = None
    seen: set[str] = set()
    while True:
        page = client.workflows.schedules.get_schedules(
            workflow_name=WORKFLOW,
            page_size=100,
            next_page_token=token,
            timeout_ms=10000,
        )
        if page is None:
            raise RuntimeError("Schedule inventory is unavailable")
        for schedule in page.result.schedules:
            if (
                not isinstance(schedule.deployment_name, str)
                or not schedule.deployment_name
            ):
                raise RuntimeError("Every refresh schedule must target a deployment")
            if schedule.deployment_name == deployment:
                selected.append(schedule)
        next_token = page.result.next_page_token
        if not isinstance(next_token, str) or not next_token:
            return selected
        token = next_token
        if token in seen:
            raise RuntimeError("Schedule pagination repeated a cursor")
        seen.add(token)


def active_runs(client: Mistral, deployment: str) -> int:
    count = 0
    token: str | None = None
    seen: set[str] = set()
    while True:
        page = client.workflows.runs.list_runs(
            workflow_identifier=WORKFLOW,
            deployment_name=deployment,
            page_size=100,
            next_page_token=token,
            timeout_ms=10000,
        )
        if page is None:
            raise RuntimeError("Workflow inventory is unavailable")
        count += sum(
            run.status in ("RUNNING", "RETRYING_AFTER_ERROR")
            for run in page.result.executions
        )
        next_token = page.result.next_page_token
        if not isinstance(next_token, str) or not next_token:
            return count
        token = next_token
        if token in seen:
            raise RuntimeError("Run pagination repeated a cursor")
        seen.add(token)


def drain(client: Mistral, deployment: str, timeout: float = 1200) -> None:
    for schedule in schedules(client, deployment):
        client.workflows.schedules.pause_schedule(
            schedule_id=schedule.schedule_id,
            note="Deployment: wait for old executions before migration",
            timeout_ms=10000,
        )
    deadline = monotonic() + timeout
    while active_runs(client, deployment):
        if monotonic() >= deadline:
            raise TimeoutError("Old refresh runs are still active; migration refused")
        print("Waiting for old refresh runs; worker remains available", flush=True)
        sleep(min(10, max(0, deadline - monotonic())))
    if any(not schedule.paused for schedule in schedules(client, deployment)):
        raise RuntimeError("A refresh schedule was resumed during deployment")
    print("Refresh runs drained; schedules remain paused", flush=True)


def main() -> int:
    try:
        deployment = os.environ["DEPLOYMENT_NAME"].strip()
        if not deployment:
            raise ValueError("DEPLOYMENT_NAME is blank")
        with Mistral(api_key=os.environ["MISTRAL_API_KEY"]) as client:
            drain(client, deployment)
    except Exception as error:
        print(
            f"Refresh drain failed ({type(error).__name__}); migration refused. "
            "Inspect schedules and active runs in Studio.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
