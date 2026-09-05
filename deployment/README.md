# Deployment

Single-node GKE baseline: one zonal `e2-standard-4` (4 vCPU, 16 GB), not HA.
The backend runs inside the MCP image; Mistral hosts the models and scheduling.

```text
Vibe --port-forward/OAuth--> MCP + backend --> Vespa
Mistral Workflows <-------- worker ---------> Vespa
                           polling / crawl / publish
```

## Runtime

| Service | CPU request | RAM request / limit | Persistent disk |
| --- | --- | --- | --- |
| Vespa | 2 | 6 / 8 GiB | 20 GiB: data and logs |
| Worker | 0.5 | 2 / 3 GiB | 10 GiB: snapshots and publication state |
| MCP | 0.25 | 0.5 / 1 GiB | 1 GiB: encrypted OAuth state |

Initial sizing, to measure during real ingestion. Disks survive pod replacement;
they are not backups. Services are cluster-internal; no public endpoint exists.
Local manifest tests require `kubectl` (with its built-in Kustomize).

## Image CI

- PR to `main`: checks and affected builds, without GCP access or publication.
- Push to `main`: publish affected images as `sha-<full commit SHA>`.
- Stable release `vX.Y.Z`: publish both images from the same commit on `main`.

Images target `linux/amd64` and pass checks and offline smoke tests before
publication. Tags are immutable; reruns verify the existing image's revision.
Each image workflow reports its commit, tag and digest in GitHub Actions.

## Prerequisites

Create the GKE cluster separately, with the PD CSI driver (`standard-rwo`),
outbound HTTPS and a control-plane endpoint reachable by GitHub's runner.
Use a dedicated node service account with repository-scoped
[`Artifact Registry Reader`](https://docs.cloud.google.com/artifact-registry/docs/integrate-gke).
No cluster creation is performed by CI.

Create a private Artifact Registry repository named `docstral` with immutable
tags. Configure [GitHub Workload Identity Federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
for trusted publication events, with builder writes limited to that repository.
No JSON service account key is required. Use a separate deployer identity with
registry read, GKE cluster discovery and namespace-scoped Kubernetes deployment
permissions (including RBAC and pod exec). Restrict its WIF binding to this
repository's protected `production` environment; require approval and `main`.

Set these GitHub Actions repository variables:

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` | Target project ID |
| `GCP_REGION` | Registry region |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full WIF provider resource name |
| `GCP_BUILD_SERVICE_ACCOUNT` | Builder service account email |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Deployer service account email |
| `GKE_CLUSTER` | Cluster name |
| `GKE_LOCATION` | Cluster zone |

Before deploying, create namespace `docstral` using `kubernetes/namespace.yaml`,
then provision these objects there. Keep secret files outside Git and never
paste secret values into command arguments or logs.

| Object | Required keys |
| --- | --- |
| Secret `mistral` | `MISTRAL_API_KEY` |
| Secret `mcp-google` | `DOCSTRAL_GOOGLE_CLIENT_ID`, `DOCSTRAL_GOOGLE_CLIENT_SECRET`, `DOCSTRAL_ALLOWED_EMAILS`, `DOCSTRAL_OAUTH_SIGNING_KEY` |
| ConfigMap `runtime` | `DEPLOYMENT_NAME` (unique to this worker environment), `DOCSTRAL_OAUTH_BASE_URL` |

For the first port-forward test, use `http://localhost:8000` as the OAuth origin
and `http://localhost:8000/auth/callback` as Google's authorized redirect URI.
Keep the signing key stable and at least 32 characters long; see the
[MCP setup](../README.md#google-oauth-invited-users) for invitations and client configuration.

## Deploy and test

Pause any existing ingestion schedule before updating. Use this workflow, not
direct manifest application, to preserve publication/maintenance guards.

1. Publish a stable `vX.Y.Z` release containing these manifests. Wait for **both**
   image workflows to succeed.
2. Run **Deploy to GKE** from `main`. Leave `release` empty for the latest stable
   release, or select a specific tag. Check `bootstrap` only on the first run,
   before any workloads or persistent volumes exist in the namespace.
3. The workflow verifies paired image revisions, enters maintenance, stops MCP
   and worker, migrates Vespa, then restarts the worker. It resumes MCP only if
   a corpus was already published. Failure never automatically clears maintenance.
4. In AI Studio, trigger `docstral-refresh` manually on the configured deployment.
   Keep scheduling disabled until this and the Vibe test succeed. See
   [worker operations](../apps/worker/README.md).

```sh
kubectl -n docstral get pods,pvc,jobs
kubectl -n docstral logs deployment/worker --tail=100
kubectl -n docstral rollout status deployment/mcp --timeout=300s
kubectl -n docstral port-forward service/mcp 8000:8000
```

Connect Vibe to `http://localhost:8000/mcp` with OAuth, log in, then ask it to call
`ask_docs` and preserve the returned sources. This exercises the remote corpus.
Readiness proves Vespa HTTP, worker health, or MCP's listening socket—not Q&A
quality. Use `k9s -n docstral` for inspection. No schedule is created by deployment.

## Failure recovery

Inspect the failed Actions step and `kubectl -n docstral logs job/<job-name>`.
Do not delete volumes or publication markers. If the worker was stopped, fix
its configuration/image and scale it to one replica before retrying deployment.
An incomplete publication must be repaired through the worker's `publish`
command; maintenance refuses to hide it. After a deployment failure, retry the
deployment with `bootstrap` unchecked; maintenance is released only on success.

Public HTTPS, rate limiting, backups and availability beyond one node remain
separate work. Reference: [Vespa persistence](https://docs.vespa.ai/en/operations/self-managed/docker-containers.html),
[GKE disks](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver),
[native Workflows workers](https://docs.mistral.ai/studio/workflows/getting-started/core_concepts/workers).
