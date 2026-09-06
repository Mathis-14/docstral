"""Exercise real workflow shell with only the Kubernetes boundary replaced."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML fixture data is untyped.

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ENV = {
    "MCP_PUBLIC_HOSTNAME": "mcp.example.com",
    "MCP_PUBLIC_IP_NAME": "example-mcp-ip",
    "MCP_TLS_CERT_NAME": "example-mcp-cert",
    "MCP_TLS_POLICY_NAME": "example-mcp-tls",
}

KUBECTL = r"""
kubectl() {
  printf '%s\n' "$*" >> "$CALLS"
  if [[ -n "$FAIL_ON" && "$*" == *"$FAIL_ON"* ]]; then return 1; fi
  case "$*" in
    *"get configmap runtime"*) printf '%s' "$OAUTH_ORIGIN" ;;
    *"get jobs"*) printf '%s\n' "$JOBS" ;;
    *"get deployment,statefulset,pvc"*) printf '%s\n' "$RESOURCES" ;;
    *"get deployment worker mcp"*) echo deployment/worker; echo deployment/mcp ;;
    *"get pods"*) echo pod/worker-old; echo pod/mcp-old ;;
    "create -f"*) echo job/vespa-migrate-test ;;
    "kustomize "*) command kubectl "$@" ;;
  esac
}
"""


def run_step(
    name: str, tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == name
    )
    assert isinstance(script, str)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", KUBECTL + script],
        env={
            "PATH": os.environ["PATH"],
            "CALLS": str(calls),
            "FAIL_ON": "",
            "BOOTSTRAP": "false",
            "JOBS": '{"items": []}',
            "RESOURCES": '{"items": []}',
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_WORKSPACE": str(ROOT),
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "WORKER_IMAGE": "registry.invalid/worker@sha256:" + "a" * 64,
            "MCP_IMAGE": "registry.invalid/mcp@sha256:" + "b" * 64,
            **PUBLIC_ENV,
            "OAUTH_ORIGIN": "https://mcp.example.com",
            **overrides,
        },
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"MCP_PUBLIC_HOSTNAME": ""},
        {"MCP_PUBLIC_HOSTNAME": "https://mcp.example.com"},
        {"MCP_PUBLIC_HOSTNAME": "mcp.example.com/mcp"},
        {"MCP_PUBLIC_HOSTNAME": "*.example.com"},
        {"MCP_PUBLIC_HOSTNAME": "a" * 64 + ".example.com"},
        {"MCP_PUBLIC_HOSTNAME": "\nkind: Secret"},
        {"MCP_PUBLIC_IP_NAME": ""},
        {"MCP_TLS_CERT_NAME": "../certificate"},
        {"MCP_TLS_POLICY_NAME": "bad policy"},
        {"OAUTH_ORIGIN": "http://localhost:8000"},
        {"OAUTH_ORIGIN": "https://other.example.com"},
        {"OAUTH_ORIGIN": "https://mcp.example.com/mcp"},
        {"FAIL_ON": "get gateways"},
    ],
)
def test_public_preflight_failure_does_not_acquire_maintenance(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    result, calls = run_step("Preflight and acquire maintenance", tmp_path, **overrides)
    assert result.returncode != 0
    assert "maintenance on" not in calls
    assert "scale" not in calls


def test_public_preflight_accepts_origin_with_trailing_slash(tmp_path: Path) -> None:
    result, calls = run_step(
        "Preflight and acquire maintenance",
        tmp_path,
        OAUTH_ORIGIN="https://mcp.example.com/",
    )
    assert result.returncode == 0, result.stderr
    assert "maintenance on" in calls


def test_existing_runtime_requires_maintenance(tmp_path: Path) -> None:
    result, calls = run_step(
        "Preflight and acquire maintenance", tmp_path, FAIL_ON="maintenance on"
    )
    assert result.returncode != 0
    assert "maintenance on" in calls


@pytest.mark.parametrize(
    "overrides",
    [
        {"BOOTSTRAP": "true", "RESOURCES": '{"items": [{}]}'},
        {"JOBS": '{"items": [{"metadata": {"name": "vespa-migrate-1"}}]}'},
    ],
)
def test_preflight_rejects_existing_bootstrap_or_active_migration(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    result, calls = run_step("Preflight and acquire maintenance", tmp_path, **overrides)
    assert result.returncode != 0
    assert "maintenance on" not in calls


@pytest.mark.parametrize("resource", ["roles", "rolebindings"])
def test_missing_worker_cleanup_permission_does_not_stop_runtimes(
    tmp_path: Path, resource: str
) -> None:
    result, calls = run_step(
        "Preflight and acquire maintenance",
        tmp_path,
        FAIL_ON=f"auth can-i delete {resource}.rbac.authorization.k8s.io/worker",
    )
    assert result.returncode != 0
    assert "requires delete permission" in result.stdout
    assert "maintenance on" not in calls
    assert "scale" not in calls
    assert "delete rolebinding/worker" not in calls


def test_permissions_are_checked_before_drain_and_cleanup(tmp_path: Path) -> None:
    result, _ = run_step("Preflight and acquire maintenance", tmp_path)
    assert result.returncode == 0, result.stderr
    result, calls = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    for resource in ("roles", "rolebindings"):
        permission = f"auth can-i delete {resource}.rbac.authorization.k8s.io/worker"
        assert calls.index(permission) < calls.index("maintenance on")
    assert calls.index("maintenance on") < calls.index("--replicas=0")
    assert calls.index("--replicas=0") < calls.index("--for=delete")
    cleanup = "delete rolebinding/worker role/worker --ignore-not-found"
    assert calls.index("--for=delete") < calls.index(cleanup)
    assert calls.index(cleanup) < calls.index("apply -f")


def test_failed_drain_prevents_apply(tmp_path: Path) -> None:
    result, calls = run_step(
        "Render immutable images and stop the application runtimes",
        tmp_path,
        FAIL_ON="--for=delete",
    )
    assert result.returncode != 0
    assert "apply -f" not in calls


def test_failed_worker_permission_cleanup_prevents_apply(tmp_path: Path) -> None:
    result, calls = run_step(
        "Render immutable images and stop the application runtimes",
        tmp_path,
        FAIL_ON="delete rolebinding/worker",
    )
    assert result.returncode != 0
    assert "apply -f" not in calls


def test_gateway_dry_run_failure_does_not_stop_runtimes(tmp_path: Path) -> None:
    result, calls = run_step(
        "Render immutable images and stop the application runtimes",
        tmp_path,
        FAIL_ON="--dry-run=server",
    )
    assert result.returncode != 0
    assert "scale" not in calls
    assert "apply -f" not in calls


def test_failed_migration_does_not_start_worker(tmp_path: Path) -> None:
    result, calls = run_step(
        "Migrate Vespa before starting the worker",
        tmp_path,
        FAIL_ON="condition=complete",
    )
    assert result.returncode != 0
    assert "scale" not in calls


def test_mcp_starts_without_a_corpus_marker(tmp_path: Path) -> None:
    result, calls = run_step("Start MCP and release maintenance", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "published-snapshot" not in calls
    assert calls.index("scale deployment/mcp --replicas=1") < calls.index(
        "rollout status"
    )
    assert calls.index("rollout status") < calls.index("maintenance off")


def test_failed_mcp_rollout_keeps_maintenance(tmp_path: Path) -> None:
    result, calls = run_step(
        "Start MCP and release maintenance",
        tmp_path,
        FAIL_ON="rollout status",
    )
    assert result.returncode != 0
    assert "maintenance off" not in calls


def test_kustomize_renders_the_migration_overlay(tmp_path: Path) -> None:
    result, _ = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(tmp_path / "migration")],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    job = yaml.safe_load(rendered.stdout)
    assert job["metadata"]["name"] == "vespa-migrate-123-1"
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == (
        "registry.invalid/worker@sha256:" + "a" * 64
    )
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "worker-data"


def test_runtime_wiring_preserves_ingestion_and_auth(tmp_path: Path) -> None:
    result, _ = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    rendered = (tmp_path / "runtime.yaml").read_text()
    assert "${MCP_" not in rendered
    resources = {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(rendered)
    }
    for name, digest in (("worker", "a"), ("mcp", "b")):
        deployment = resources["Deployment", name]["spec"]
        assert deployment["replicas"] == 0
        assert deployment["strategy"]["type"] == "Recreate"
        container = deployment["template"]["spec"]["containers"][0]
        assert container["image"] == f"registry.invalid/{name}@sha256:{digest * 64}"
    mcp = resources["Deployment", "mcp"]["spec"]["template"]["spec"]
    assert mcp["automountServiceAccountToken"] is False
    assert mcp["containers"][0]["args"][:2] == ["--auth", "google"]
    env = {entry["name"]: entry for entry in mcp["containers"][0]["env"]}
    assert env["DOCSTRAL_ANSWER_MODEL"]["valueFrom"]["configMapKeyRef"] == {
        "name": "runtime",
        "key": "DOCSTRAL_ANSWER_MODEL",
        "optional": True,
    }
    assert mcp["volumes"][0]["persistentVolumeClaim"]["claimName"] == "mcp-auth"
    for probe in ("startupProbe", "readinessProbe"):
        assert mcp["containers"][0][probe]["httpGet"] == {
            "path": "/healthz",
            "port": "http",
        }
    gateway = resources["Gateway", "mcp"]["spec"]
    assert gateway["gatewayClassName"] == "gke-l7-global-external-managed"
    assert gateway["addresses"] == [
        {"type": "NamedAddress", "value": PUBLIC_ENV["MCP_PUBLIC_IP_NAME"]}
    ]
    assert gateway["listeners"] == [
        {
            "name": "https",
            "hostname": PUBLIC_ENV["MCP_PUBLIC_HOSTNAME"],
            "port": 443,
            "protocol": "HTTPS",
            "tls": {
                "mode": "Terminate",
                "options": {
                    "networking.gke.io/pre-shared-certs": PUBLIC_ENV[
                        "MCP_TLS_CERT_NAME"
                    ]
                },
            },
            "allowedRoutes": {"namespaces": {"from": "Same"}},
        }
    ]
    route = resources["HTTPRoute", "mcp"]["spec"]
    assert route["parentRefs"] == [{"name": "mcp", "sectionName": "https"}]
    assert route["hostnames"] == [PUBLIC_ENV["MCP_PUBLIC_HOSTNAME"]]
    assert route["rules"] == [{"backendRefs": [{"name": "mcp", "port": 8000}]}]
    assert resources["GCPBackendPolicy", "mcp"]["spec"] == {
        "default": {"timeoutSec": 120, "logging": {"enabled": False}},
        "targetRef": {"group": "", "kind": "Service", "name": "mcp"},
    }
    assert resources["GCPGatewayPolicy", "mcp"]["spec"] == {
        "default": {"sslPolicy": PUBLIC_ENV["MCP_TLS_POLICY_NAME"]},
        "targetRef": {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": "mcp",
        },
    }
    assert resources["HealthCheckPolicy", "mcp"]["spec"] == {
        "default": {
            "config": {
                "type": "HTTP",
                "httpHealthCheck": {
                    "portSpecification": "USE_FIXED_PORT",
                    "port": 8000,
                    "requestPath": "/healthz",
                },
            }
        },
        "targetRef": {"group": "", "kind": "Service", "name": "mcp"},
    }
    worker = resources["Deployment", "worker"]["spec"]["template"]["spec"]
    assert (
        worker["serviceAccountName"]
        == resources["ServiceAccount", "worker"]["metadata"]["name"]
    )
    assert worker["automountServiceAccountToken"] is False
    assert (
        resources["ServiceAccount", "worker"]["automountServiceAccountToken"] is False
    )
    assert ("Role", "worker") not in resources
    assert ("RoleBinding", "worker") not in resources
    worker_env = {entry["name"]: entry for entry in worker["containers"][0]["env"]}
    assert "POD_NAMESPACE" not in worker_env
    assert "MCP_DEPLOYMENT" not in worker_env
    assert worker_env["DOCSTRAL_DATA_DIR"]["value"] == "/app/data"
    assert worker_env["VESPA_ENDPOINT"]["value"] == "http://vespa:8080"
    assert worker["volumes"][0]["persistentVolumeClaim"]["claimName"] == "worker-data"
    for (kind, _), resource in resources.items():
        if kind == "Service":
            assert resource["spec"].get("type", "ClusterIP") == "ClusterIP"
        assert kind not in {"Secret", "Namespace"}


def test_public_renderer_does_not_expand_other_environment_variables() -> None:
    result = subprocess.run(
        ["bash", "deployment/render-public.sh"],
        cwd=ROOT,
        input="${MCP_PUBLIC_HOSTNAME} ${MISTRAL_API_KEY}",
        env={
            "PATH": os.environ["PATH"],
            **PUBLIC_ENV,
            "MISTRAL_API_KEY": "do-not-read",
        },
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stdout == "mcp.example.com ${MISTRAL_API_KEY}"


def test_public_resource_names_stay_strings_after_yaml_rendering(
    tmp_path: Path,
) -> None:
    result, _ = run_step(
        "Render immutable images and stop the application runtimes",
        tmp_path,
        MCP_PUBLIC_IP_NAME="true",
        MCP_TLS_CERT_NAME="null",
        MCP_TLS_POLICY_NAME="on",
    )
    assert result.returncode == 0, result.stderr
    resources = {
        item["kind"]: item
        for item in yaml.safe_load_all((tmp_path / "runtime.yaml").read_text())
    }
    gateway = resources["Gateway"]["spec"]
    assert gateway["addresses"][0]["value"] == "true"
    assert gateway["listeners"][0]["tls"]["options"] == {
        "networking.gke.io/pre-shared-certs": "null"
    }
    assert resources["GCPGatewayPolicy"]["spec"]["default"]["sslPolicy"] == "on"


def test_public_deployer_has_only_namespaced_network_permissions() -> None:
    role = yaml.safe_load(
        (ROOT / "deployment/kubernetes/public-deployer-rbac.yaml").read_text()
    )
    assert role["kind"] == "Role"
    assert role["metadata"]["namespace"] == "docstral"
    for rule in role["rules"]:
        assert rule["verbs"] == ["get", "list", "watch", "create", "patch"]
    assert role["rules"][0]["apiGroups"] == ["gateway.networking.k8s.io"]
    assert role["rules"][0]["resources"] == ["gateways", "httproutes"]
    assert role["rules"][1]["apiGroups"] == ["networking.gke.io"]
    assert role["rules"][1]["resources"] == [
        "gcpgatewaypolicies",
        "gcpbackendpolicies",
        "healthcheckpolicies",
    ]
