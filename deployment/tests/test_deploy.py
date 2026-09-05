"""Exercise real workflow shell with only the Kubernetes boundary replaced."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML fixture data is untyped.

ROOT = Path(__file__).resolve().parents[2]

KUBECTL = r"""
kubectl() {
  printf '%s\n' "$*" >> "$CALLS"
  if [[ -n "$FAIL_ON" && "$*" == *"$FAIL_ON"* ]]; then return 1; fi
  case "$*" in
    *"get jobs"*) printf '%s\n' "$JOBS" ;;
    *"get deployment,statefulset,pvc"*) printf '%s\n' "$RESOURCES" ;;
    *"get deployment worker mcp"*) echo deployment/worker; echo deployment/mcp ;;
    *"get pods"*) echo pod/worker-old; echo pod/mcp-old ;;
    *".published-snapshot"*) printf '%s\n' "$PUBLISHED" ;;
    "create -f"*) echo job/vespa-migrate-test ;;
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
            "PUBLISHED": "yes",
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
            **overrides,
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result, calls.read_text()


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


def test_old_pods_terminate_before_applying_resources(tmp_path: Path) -> None:
    result, calls = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert calls.index("--replicas=0") < calls.index("--for=delete")
    assert calls.index("--for=delete") < calls.index("apply -f")


def test_failed_drain_prevents_apply(tmp_path: Path) -> None:
    result, calls = run_step(
        "Render immutable images and stop the application runtimes",
        tmp_path,
        FAIL_ON="--for=delete",
    )
    assert result.returncode != 0
    assert "apply -f" not in calls


def test_failed_migration_does_not_start_worker(tmp_path: Path) -> None:
    result, calls = run_step(
        "Migrate Vespa before starting the worker",
        tmp_path,
        FAIL_ON="condition=complete",
    )
    assert result.returncode != 0
    assert "scale" not in calls


@pytest.mark.parametrize("published", ["yes", "no"])
def test_resume_requires_published_corpus(tmp_path: Path, published: str) -> None:
    result, calls = run_step(
        "Restore serving only for a published corpus", tmp_path, PUBLISHED=published
    )
    assert result.returncode == 0, result.stderr
    assert ("scale deployment/mcp" in calls) == (published == "yes")
    assert "maintenance off" in calls
    if published == "yes":
        assert calls.index("rollout status") < calls.index("maintenance off")


def test_failed_mcp_rollout_keeps_maintenance(tmp_path: Path) -> None:
    result, calls = run_step(
        "Restore serving only for a published corpus",
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


def test_runtime_wiring_preserves_publication_and_auth(tmp_path: Path) -> None:
    result, _ = run_step(
        "Render immutable images and stop the application runtimes", tmp_path
    )
    assert result.returncode == 0, result.stderr
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(tmp_path / "runtime")],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    resources = {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(rendered.stdout)
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
    assert mcp["volumes"][0]["persistentVolumeClaim"]["claimName"] == "mcp-auth"
    worker = resources["Deployment", "worker"]["spec"]["template"]["spec"]
    assert (
        worker["serviceAccountName"]
        == resources["ServiceAccount", "worker"]["metadata"]["name"]
    )
    assert worker["volumes"][0]["persistentVolumeClaim"]["claimName"] == "worker-data"
    assert resources["Role", "worker"]["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments/scale"],
            "resourceNames": ["mcp"],
            "verbs": ["get", "patch"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["mcp"],
            "verbs": ["get"],
        },
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
    ]
    for (kind, _), resource in resources.items():
        if kind == "Service":
            assert resource["spec"].get("type", "ClusterIP") == "ClusterIP"
        assert kind not in {"Secret", "Namespace"}
