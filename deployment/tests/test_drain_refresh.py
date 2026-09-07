from collections.abc import Iterator

import httpx
import pytest
from drain_refresh import drain
from mistralai.client import Mistral


class ControlPlane:
    def __init__(self) -> None:
        self.paused = False
        self.active = False
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/pause"):
            self.paused = True
            return httpx.Response(204)
        if path.endswith("/schedules"):
            return httpx.Response(
                200,
                json={
                    "schedules": [
                        {
                            "schedule_id": "hourly",
                            "workflow_name": "docstral-refresh",
                            "input": {},
                            "paused": self.paused,
                            "deployment_name": "test",
                        }
                    ]
                },
            )
        assert path.endswith("/runs")
        executions = (
            [
                {
                    "workflow_name": "docstral-refresh",
                    "execution_id": "old",
                    "root_execution_id": "old",
                    "status": "RUNNING",
                    "start_time": "2026-09-07T00:00:00Z",
                    "end_time": None,
                }
            ]
            if self.active
            else []
        )
        return httpx.Response(200, json={"executions": executions})


@pytest.fixture
def control() -> Iterator[tuple[ControlPlane, Mistral]]:
    boundary = ControlPlane()
    with httpx.Client(transport=httpx.MockTransport(boundary.handle)) as http:
        with Mistral(api_key="test-key", client=http) as client:
            yield boundary, client


def test_deployment_pauses_scheduling_before_waiting_for_old_runs(
    control: tuple[ControlPlane, Mistral],
) -> None:
    boundary, client = control
    drain(client, "test", timeout=0)
    assert boundary.paused
    assert boundary.calls.index(
        "/v1/workflows/schedules/hourly/pause"
    ) < boundary.calls.index("/v1/workflows/runs")
    assert not any(path.endswith("/resume") for path in boundary.calls)


def test_active_old_run_refuses_migration_and_keeps_schedule_paused(
    control: tuple[ControlPlane, Mistral],
) -> None:
    boundary, client = control
    boundary.active = True
    with pytest.raises(TimeoutError, match="migration refused"):
        drain(client, "test", timeout=0)
    assert boundary.paused


def test_deployment_does_not_pause_another_deployments_schedule(
    control: tuple[ControlPlane, Mistral],
) -> None:
    boundary, client = control
    drain(client, "other", timeout=0)
    assert not boundary.paused
