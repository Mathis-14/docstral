# Deployment

Single-node GKE baseline: one zonal `e2-standard-4` (4 vCPU, 16 GB), not HA.
The backend runs inside the MCP image; Mistral hosts the models and scheduling.

```text
Vibe --HTTPS/Google OAuth--> Gateway --> MCP + backend --> Vespa
Mistral Workflows <-------- worker ---------> Vespa
                           polling / incremental ingestion
```

## Runtime

| Service | CPU request | RAM request / limit | Persistent disk |
| --- | --- | --- | --- |
| Vespa | 2 | 6 / 8 GiB | 20 GiB: data and logs |
| Worker | 0.5 | 2 / 3 GiB | 10 GiB: snapshots, prepared articles and indexed state |
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
mismatch before maintenance. Keep the signing key stable and at least 32
characters long. See [MCP setup](../README.md#google-oauth-invited-users) for invitations.

## Deploy and test

Pause any existing ingestion schedule before updating. Use this workflow, not
direct manifest application, to preserve ingestion and maintenance guards.

1. Publish a stable `vX.Y.Z` release containing these manifests. Wait for **both**
   image workflows to succeed.
2. Run **Deploy to GKE** from `main`. Leave `release` empty for the latest stable
   release, or select a specific tag. Check `bootstrap` only on the first run,
   before any workloads or persistent volumes exist in the namespace.
3. The workflow verifies paired image revisions, enters maintenance, stops MCP
   and worker, removes the worker's old Kubernetes permissions, migrates Vespa,
   then starts worker and MCP. An empty corpus returns the usual abstention;
   MCP startup does not wait for ingestion. Failure never automatically clears
   maintenance. Ingestion itself leaves both runtimes running.
4. Trigger `docstral-refresh` manually in AI Studio with `{}`. The first run
   reconciles the existing corpus and initializes the indexed-page registry;
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
pending articles are retried against the new crawl. For an interrupted legacy
publication, follow [worker recovery](../apps/worker/README.md) before deploying.
Keep `bootstrap` checked only if no
runtime resources or PVCs were created; otherwise uncheck it on retry.
Maintenance is released only on success.

Rate limiting, backups and availability beyond one node remain separate work.
Reference: [Vespa persistence](https://docs.vespa.ai/en/operations/self-managed/docker-containers.html),
[GKE disks](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver),
[native Workflows workers](https://docs.mistral.ai/studio/workflows/getting-started/core_concepts/workers).
