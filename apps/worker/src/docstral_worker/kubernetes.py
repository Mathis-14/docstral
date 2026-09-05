"""Control only the configured MCP Deployment using the official Kubernetes SDK."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError


class _Object(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class _Metadata(_Object):
    resource_version: str


class _DeploymentMetadata(_Metadata):
    generation: int


class _ScaleSpec(_Object):
    replicas: Literal[0, 1]


class _ScaleStatus(_Object):
    selector: str = Field(min_length=1)


class _Scale(_Object):
    metadata: _Metadata
    spec: _ScaleSpec
    status: _ScaleStatus


class _DeploymentStatus(_Object):
    observed_generation: int | None = None


class _Deployment(_Object):
    metadata: _DeploymentMetadata
    spec: _ScaleSpec
    status: _DeploymentStatus


class _Pods(_Object):
    items: tuple[object, ...]


class _Apps(Protocol):
    async def read_namespaced_deployment_scale(
        self, name: str, namespace: str, *, _request_timeout: float
    ) -> object: ...
    async def patch_namespaced_deployment_scale(
        self,
        name: str,
        namespace: str,
        body: dict[str, object],
        *,
        _request_timeout: float,
    ) -> object: ...
    async def read_namespaced_deployment(
        self, name: str, namespace: str, *, _request_timeout: float
    ) -> object: ...


class _Core(Protocol):
    async def list_namespaced_pod(
        self, namespace: str, *, label_selector: str, _request_timeout: float
    ) -> object: ...


class McpDeployment:
    def __init__(
        self,
        apps: _Apps,
        core: _Core,
        namespace: str,
        name: str,
        *,
        timeout: float = 120,
    ) -> None:
        self.apps = apps
        self.core = core
        self.namespace = namespace
        self.name = name
        self.timeout = timeout

    async def check(self) -> None:
        await self._scale()

    async def _scale(self) -> _Scale:
        return _Scale.model_validate(
            await self.apps.read_namespaced_deployment_scale(
                self.name,
                self.namespace,
                _request_timeout=15,
            )
        )

    async def _set_replicas(self, scale: _Scale, replicas: int) -> None:
        await self.apps.patch_namespaced_deployment_scale(
            self.name,
            self.namespace,
            {
                "metadata": {"resourceVersion": scale.metadata.resource_version},
                "spec": {"replicas": replicas},
            },
            _request_timeout=15,
        )

    async def stop(self) -> None:
        scale = await self._scale()
        await self._set_replicas(scale, 0)
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    deployment = _Deployment.model_validate(
                        await self.apps.read_namespaced_deployment(
                            self.name,
                            self.namespace,
                            _request_timeout=15,
                        )
                    )
                    if deployment.spec.replicas != 0:
                        raise IngestionError(
                            "MCP was scaled up during publication shutdown"
                        )
                    pods = _Pods.model_validate(
                        await self.core.list_namespaced_pod(
                            self.namespace,
                            label_selector=scale.status.selector,
                            _request_timeout=15,
                        )
                    )
                    observed = deployment.status.observed_generation
                    if (
                        observed is not None
                        and observed >= deployment.metadata.generation
                        and not pods.items
                    ):
                        return
                    await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise IngestionError(
                "MCP pods did not terminate before publication timeout"
            ) from exc

    async def start(self) -> None:
        await self._set_replicas(await self._scale(), 1)


@asynccontextmanager
async def in_cluster_mcp(namespace: str, name: str) -> AsyncIterator[McpDeployment]:
    # The official SDK has no py.typed marker; validate its responses at this boundary.
    from kubernetes.aio import client, config  # type: ignore[import-untyped]

    config.load_incluster_config()  # Never load the operator's local kubeconfig.
    async with client.ApiClient() as api:
        yield McpDeployment(
            cast(_Apps, client.AppsV1Api(api)),
            cast(_Core, client.CoreV1Api(api)),
            namespace,
            name,
        )
