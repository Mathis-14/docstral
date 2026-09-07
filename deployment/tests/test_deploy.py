import os
import shutil
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
    name: str, tmp_path: Path, cwd: Path = ROOT, /, **overrides: str
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
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result, calls.read_text() if calls.exists() else ""


def test_oauth_origin_mismatch_stops_before_pausing_workflows(tmp_path: Path) -> None:
    result, calls = run_step(
        "Preflight and drain workflows",
        tmp_path,
        OAUTH_ORIGIN="https://other.example.com",
    )
    assert result.returncode != 0
    assert "exec -i deployment/worker -- python -" not in calls
    assert "scale" not in calls


def test_old_release_uses_current_workflow_drain_script(tmp_path: Path) -> None:
    release = tmp_path / "release"
    (release / "deployment").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "deployment/render-public.sh", release / "deployment/render-public.sh"
    )
    result, calls = run_step("Preflight and drain workflows", tmp_path, release)
    assert result.returncode == 0, result.stderr
    assert "exec -i deployment/worker -- python -" in calls


def test_failed_workflow_drain_prevents_deployment(tmp_path: Path) -> None:
    result, calls = run_step(
        "Preflight and drain workflows",
        tmp_path,
        FAIL_ON="exec -i deployment/worker -- python -",
    )
    assert result.returncode != 0
    assert "exec -i deployment/worker -- python -" in calls


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
    result, calls = run_step("Preflight and drain workflows", tmp_path, **overrides)
    assert result.returncode != 0
    assert "exec -i deployment/worker -- python -" not in calls


@pytest.mark.parametrize("resource", ["roles", "rolebindings"])
def test_missing_worker_cleanup_permission_does_not_stop_runtimes(
    tmp_path: Path, resource: str
) -> None:
    result, calls = run_step(
        "Preflight and drain workflows",
        tmp_path,
        FAIL_ON=f"auth can-i delete {resource}.rbac.authorization.k8s.io/worker",
    )
    assert result.returncode != 0
    assert "requires delete permission" in result.stdout
    assert "exec -i deployment/worker -- python -" not in calls
    assert "scale" not in calls
    assert "delete rolebinding/worker" not in calls


def test_permissions_are_checked_before_drain_and_cleanup(tmp_path: Path) -> None:
    result, _ = run_step("Preflight and drain workflows", tmp_path)
    assert result.returncode == 0, result.stderr
    result, calls = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    for resource in ("roles", "rolebindings"):
        permission = f"auth can-i delete {resource}.rbac.authorization.k8s.io/worker"
        assert calls.index(permission) < calls.index(
            "exec -i deployment/worker -- python -"
        )
    assert calls.index("exec -i deployment/worker -- python -") < calls.index(
        "--replicas=0"
    )
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
    result, calls = run_step("Start MCP", tmp_path)
    assert result.returncode == 0, result.stderr
    assert "published-snapshot" not in calls
    assert "maintenance" not in calls
    assert calls.index("scale deployment/mcp --replicas=1") < calls.index(
        "rollout status"
    )


def test_old_release_exits_maintenance_after_mcp_rollout(tmp_path: Path) -> None:
    release = tmp_path / "release"
    maintenance = release / "apps/worker/src/docstral_worker/maintenance.py"
    maintenance.parent.mkdir(parents=True)
    maintenance.touch()
    result, calls = run_step("Start MCP", tmp_path, release)
    assert result.returncode == 0, result.stderr
    assert calls.index("rollout status deployment/mcp") < calls.index(
        "docstral-worker maintenance off"
    )


def test_failed_mcp_rollout_keeps_old_release_in_maintenance(tmp_path: Path) -> None:
    release = tmp_path / "release"
    maintenance = release / "apps/worker/src/docstral_worker/maintenance.py"
    maintenance.parent.mkdir(parents=True)
    maintenance.touch()
    result, calls = run_step("Start MCP", tmp_path, release, FAIL_ON="rollout status")
    assert result.returncode != 0
    assert "maintenance off" not in calls


def test_migration_uses_the_selected_releases_manifests(tmp_path: Path) -> None:
    release = tmp_path / "release"
    shutil.copytree(ROOT / "deployment/kubernetes", release / "deployment/kubernetes")
    shutil.copyfile(
        ROOT / "deployment/render-public.sh", release / "deployment/render-public.sh"
    )
    migration = release / "deployment/kubernetes/migration/vespa-migrate.yaml"
    migration.write_text(
        migration.read_text().replace("name: vespa-migrate", "name: selected-release")
    )
    result, _ = run_step(
        "Render immutable images and stop the application runtimes",
        tmp_path,
        release,
    )
    assert result.returncode == 0, result.stderr
    job = yaml.safe_load((tmp_path / "migration.yaml").read_text())
    assert job["metadata"]["name"] == "selected-release-123-1"
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == (
        "registry.invalid/worker@sha256:" + "a" * 64
    )


def test_runtime_stays_stopped_with_selected_images_until_migration(
    tmp_path: Path,
) -> None:
    result, _ = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    deployments = {
        item["metadata"]["name"]: item["spec"]
        for item in yaml.safe_load_all((tmp_path / "runtime.yaml").read_text())
        if item["kind"] == "Deployment"
    }
    for name, digest in (("worker", "a"), ("mcp", "b")):
        assert deployments[name]["replicas"] == 0
        container = deployments[name]["template"]["spec"]["containers"][0]
        assert container["image"] == f"registry.invalid/{name}@sha256:{digest * 64}"


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
