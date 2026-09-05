import pytest
from docstral_worker import IngestionError
from docstral_worker.kubernetes import McpDeployment, in_cluster_mcp

# Exercise the actual untyped SDK response models without contacting Kubernetes.
from kubernetes.aio import client  # type: ignore[import-untyped]
from pydantic import ValidationError


class _Apps:
    def __init__(self) -> None:
        self.replicas = 1
        self.observed = 2
        self.deployment_replicas: int | None = 0
        self.patches: list[dict[str, object]] = []

    async def read_namespaced_deployment_scale(
        self, name: str, namespace: str, *, _request_timeout: float
    ) -> object:
        assert (namespace, name, _request_timeout) == ("docstral", "mcp", 15)
        return client.V1Scale(
            # The /scale response does not populate metadata.generation.
            metadata=client.V1ObjectMeta(resource_version="version-1"),
            # Kubernetes omits spec.replicas at zero; the SDK returns None.
            spec=client.V1ScaleSpec(
                replicas=None if self.replicas == 0 else self.replicas
            ),
            status=client.V1ScaleStatus(
                selector="app=docstral-mcp", replicas=self.replicas
            ),
        )

    async def patch_namespaced_deployment_scale(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
        *,
        _request_timeout: float,
    ) -> object:
        assert (namespace, name, _request_timeout) == ("docstral", "mcp", 15)
        self.patches.append(body)
        spec = body["spec"]
        assert isinstance(spec, dict)
        replicas = spec["replicas"]
        assert isinstance(replicas, int)
        self.replicas = replicas
        return None

    async def read_namespaced_deployment(
        self, name: str, namespace: str, *, _request_timeout: float
    ) -> object:
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(resource_version="version-2", generation=2),
            spec=client.V1DeploymentSpec(
                replicas=self.deployment_replicas,
                selector=client.V1LabelSelector(match_labels={"app": "docstral-mcp"}),
                template=client.V1PodTemplateSpec(),
            ),
            status=client.V1DeploymentStatus(observed_generation=self.observed),
        )


class _Core:
    def __init__(self) -> None:
        self.calls = 0
        self.pods: list[object] = []

    async def list_namespaced_pod(
        self, namespace: str, *, label_selector: str, _request_timeout: float
    ) -> object:
        assert (namespace, label_selector, _request_timeout) == (
            "docstral",
            "app=docstral-mcp",
            15,
        )
        self.calls += 1
        return client.V1PodList(items=self.pods)


@pytest.mark.parametrize("replicas", [0, 1])
async def test_mcp_scale_targets_one_deployment_with_version_precondition(
    replicas: int,
) -> None:
    apps, core = _Apps(), _Core()
    apps.replicas = replicas
    mcp = McpDeployment(apps, core, "docstral", "mcp")
    await mcp.check()
    await mcp.stop()
    await mcp.start()
    assert core.calls == 1
    assert apps.patches == [
        {"metadata": {"resourceVersion": "version-1"}, "spec": {"replicas": 0}},
        {"metadata": {"resourceVersion": "version-1"}, "spec": {"replicas": 1}},
    ]
    assert apps.replicas == 1


@pytest.mark.parametrize("replicas", [-1, 2])
async def test_scale_rejects_invalid_replica_count(replicas: int) -> None:
    apps = _Apps()
    apps.replicas = replicas
    with pytest.raises(ValidationError, match="replicas"):
        await McpDeployment(apps, _Core(), "docstral", "mcp").check()
    assert apps.patches == []


async def test_stop_does_not_default_missing_deployment_replicas() -> None:
    apps, core = _Apps(), _Core()
    apps.deployment_replicas = None
    with pytest.raises(ValidationError, match="replicas"):
        await McpDeployment(apps, core, "docstral", "mcp").stop()
    assert core.calls == 0


@pytest.mark.parametrize("pending", ["pods", "controller"])
async def test_stop_waits_for_pod_termination_and_controller_observation(
    pending: str,
) -> None:
    apps, core = _Apps(), _Core()
    if pending == "pods":
        core.pods = [client.V1Pod()]
    else:
        apps.observed = 1
    with pytest.raises(IngestionError, match="did not terminate"):
        await McpDeployment(apps, core, "docstral", "mcp", timeout=0.01).stop()
    assert len(apps.patches) == 1


async def test_cluster_factory_never_uses_local_kubeconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(Exception, match="Service host/port is not set"):
        async with in_cluster_mcp("docstral", "mcp"):
            pytest.fail("used local Kubernetes credentials")


async def test_stop_refuses_a_concurrent_scale_up() -> None:
    apps = _Apps()
    apps.deployment_replicas = 1
    with pytest.raises(IngestionError, match="scaled up"):
        await McpDeployment(apps, _Core(), "docstral", "mcp").stop()
