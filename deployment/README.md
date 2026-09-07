# Deployment

Single-node GKE baseline: one zonal `e2-standard-4` (4 vCPU, 16 GB), not HA.
Q&A runs inside the MCP image; Mistral hosts the models and scheduling.

```text
Vibe --HTTPS/Google OAuth--> Gateway --> MCP + Q&A --> Vespa
Mistral Workflows <-------- worker ---------> Vespa
                           polling / incremental ingestion
```

## Runtime

| Service | CPU request | RAM request / limit | Persistent disk |
| --- | --- | --- | --- |
| Vespa | 2 | 6 / 8 GiB | 20 GiB: data and logs |
| Worker | 0.5 | 2 / 3 GiB | None |
| MCP | 0.25 | 0.5 / 1 GiB | 1 GiB: encrypted OAuth state |

Initial sizing, to measure during real ingestion. Disks survive pod replacement;
they are not backups. Services remain cluster-internal; only MCP is routed
through the public HTTPS Gateway. Manifest tests require `kubectl` and `envsubst`.

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
permissions, including pod exec and deletion of the old worker Role and
RoleBinding. The worker itself has no Kubernetes API permissions or mounted
service-account token. Restrict the deployer's WIF binding to this
repository's `production` environment and manual `deploy.yml` runs on `main`.
Match GitHub's actual OIDC subject, including immutable owner/repository IDs.
Launching **Run workflow** is the deployment approval; no second review is required.

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

Set these additional variables in the `production` environment:

| Variable | Purpose |
| --- | --- |
| `MCP_PUBLIC_HOSTNAME` | Public DNS hostname, without scheme or path |
| `MCP_PUBLIC_IP_NAME` | Reserved global IPv4 address name |
| `MCP_TLS_CERT_NAME` | Global Google-managed SSL certificate name |
| `MCP_TLS_POLICY_NAME` | Global SSL policy name (TLS 1.2 minimum) |

Override `GCP_WORKLOAD_IDENTITY_PROVIDER` in the `production` environment with
a separate deployment pool; keep the repository-level provider for image builds.

Before deploying, create namespace `docstral` using `kubernetes/namespace.yaml`.
Provision the objects below; keep secret files outside Git and
never paste secret values into command arguments or logs.

| Object | Required keys |
| --- | --- |
| Secret `mistral` | `MISTRAL_API_KEY` |
| Secret `mcp-google` | `DOCSTRAL_GOOGLE_CLIENT_ID`, `DOCSTRAL_GOOGLE_CLIENT_SECRET`, `DOCSTRAL_ALLOWED_EMAILS`, `DOCSTRAL_OAUTH_SIGNING_KEY` |
| ConfigMap `runtime` | `DEPLOYMENT_NAME` (unique to this worker environment), `DOCSTRAL_OAUTH_BASE_URL` |

Optional: set `DOCSTRAL_ANSWER_MODEL` in ConfigMap `runtime` to override
`ministral-8b-2512`. Run `kubectl -n docstral edit configmap runtime`, then
`kubectl -n docstral rollout restart deployment/mcp` and check its rollout status.
Do this outside ingestion/deployment; expect a brief MCP interruption. No image
build or re-ingestion is needed; deployments preserve this operator-owned setting.

Complete the one-time [public HTTPS setup](https.md) before deploying this release.
Use `https://<MCP_PUBLIC_HOSTNAME>` as the OAuth origin; deployment rejects a
mismatch before stopping runtimes. Keep the signing key stable and at least 32
characters long. See [MCP setup](#google-oauth-invited-users) for invitations.

## Deploy and test

Before deployment, manually pause the `docstral-refresh` schedules targeting the
production deployment in Mistral Studio. Wait until its executions are no longer
`RUNNING` or `RETRYING_AFTER_ERROR`, keeping the old worker available to finish them.
Do not start manual refreshes or resume scheduling during deployment. Pausing a
schedule does not block manual API triggers.

The deployment workflow does not pause schedules or wait for ingestion executions.
It stops application pods before migrating; deploying during ingestion can
interrupt it and leave an old execution for incompatible new worker code to resume.

The workflow keeps its deployment tooling at the workspace root and checks out
the selected release separately under `release/`. Manifests and migrations come
from that release. Releases that still use `maintenance.py` leave maintenance
only after MCP rollout succeeds.
The workflow does not change schedule state.

1. Publish a stable `vX.Y.Z` release containing these manifests. Wait for **both**
   image workflows to succeed.
2. Complete the manual scheduling and execution checks above, then run
   **Deploy to GKE** from `main`. Leave `release` empty for the latest stable
   release, or select a specific tag. Check `bootstrap` only on the first run,
   before any workloads or persistent volumes exist in the namespace.
3. The workflow verifies paired images and prerequisites, stops MCP and worker,
   waits for their pods to terminate, removes legacy worker permissions, migrates
   Vespa and starts both runtimes. A failed migration prevents runtime startup.
4. Trigger `docstral-refresh` manually in AI Studio with `{}`. The first run
   reconciles the existing corpus and confirms pages in Vespa;
   subsequent runs update only added or changed articles and delete absent ones.
   Keep scheduling disabled until the refresh and Vibe test succeed. See
   [worker operations](../apps/worker/README.md).

```sh
kubectl -n docstral get pods,pvc,jobs
kubectl -n docstral logs deployment/worker --tail=100
kubectl -n docstral rollout status deployment/mcp --timeout=300s
```

After DNS and TLS are ready, connect Vibe to `https://<MCP_PUBLIC_HOSTNAME>/mcp`,
log in and call `ask_docs` with sources; follow the [public checks](https.md#verify).
Pod readiness is not public HTTPS readiness or Q&A quality. Use `k9s -n docstral`
for inspection. Deployment never creates a schedule or waits for certificate issuance.

## Failure recovery

Inspect the failed Actions step and `kubectl -n docstral logs job/<job-name>`.
Do not delete volumes or indexed state. If the worker was stopped, fix
its configuration/image and scale it to one replica before retrying deployment.
For failed ingestion, start a fresh `docstral-refresh` invocation with `{}`;
unconfirmed pages are repaired from current HTML. Complete any interrupted
legacy publication with its original release before this migration. The new
worker does not consume the old local markers or snapshots.
Keep `bootstrap` checked only if no
runtime resources or PVCs were created; otherwise uncheck it on retry.
Resume scheduling only after a successful fresh refresh and unchanged run.
The worker no longer mounts `worker-data`; deployment does not delete an existing
PVC, so historical snapshots remain available for operator inspection.

Rate limiting, backups and availability beyond one node remain separate work.
Reference: [Vespa persistence](https://docs.vespa.ai/en/operations/self-managed/docker-containers.html),
[GKE disks](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver),
[native Workflows workers](https://docs.mistral.ai/studio/workflows/getting-started/core_concepts/workers).

## Google OAuth (invited users)

Create a **Web application** OAuth client in
[Google Auth Platform](https://console.cloud.google.com/auth/clients), with
`http://localhost:8000/auth/callback` as the authorized redirect URI.

Fill the OAuth settings from [.env.example](../.env.example) in `.env`.
Only verified addresses in `DOCSTRAL_ALLOWED_EMAILS` can use the tool;
Google's test-user list is not the access control for these identity-only scopes.

```sh
# Server terminal (stop any existing MCP first)
uv run --env-file .env docstral-mcp --auth google
# Another terminal
vibe mcp add docstral-google --url http://localhost:8000/mcp --transport streamable-http
```

In Vibe, use `/mcp login docstral-google` if needed, then ask `ask_docs` a question
and request all sources. Test outside the repository to avoid local-file context.

Keep `FASTMCP_HOME` (`data/oauth`) and the secret signing key across restarts;
Docker storage must be writable by UID 1000. This remains local, with one MCP
instance. For remote access, see [GKE HTTPS setup](https.md).
Invitations do not cap API spending. See [FastMCP OAuth](https://gofastmcp.com/integrations/google).

## Docker images

Build from the repository root (AMD64 is the GKE deployment target):

```sh
docker build --platform linux/amd64 \
  -f deployment/docker/mcp.Dockerfile -t docstral-mcp:local .
docker build --platform linux/amd64 \
  -f deployment/docker/worker.Dockerfile -t docstral-worker:local .
docker run --rm --platform linux/amd64 docstral-worker:local --help
```

With Docker Desktop and an indexed Vespa listening on the host's port 8080:

```sh
uv run --env-file .env docker run --rm --platform linux/amd64 \
  --publish 127.0.0.1:8000:8000 --env MISTRAL_API_KEY \
  docstral-mcp:local --vespa-endpoint http://host.docker.internal:8080
```

This serves `/mcp` without authentication. Images run as UID/GID 1000 and contain
no corpus or secrets. Mount worker data at `/app/data`; run local ingestion on
the host. See [native refresh](../apps/worker/README.md) and
[deployment](#deploy-and-test). Cluster provisioning is separate.
