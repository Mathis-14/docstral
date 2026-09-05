# Runtime image CI

Build and publish the MCP and worker images. No deployment or corpus update.

## Runs

- PR to `main`: checks and affected builds, without GCP access or publication.
- Push to `main`: publish affected images as `sha-<full commit SHA>`.
- Stable release `vX.Y.Z`: publish both images from the same commit on `main`.

Images target `linux/amd64` and pass checks and offline smoke tests before
publication. Tags are immutable; reruns verify the existing image's revision.
Each image workflow reports its commit, tag and digest in GitHub Actions.

## Setup

Create a private Artifact Registry repository named `docstral` with immutable
tags. Configure [GitHub Workload Identity Federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
for trusted publication events, with builder writes limited to that repository.
No JSON service account key is required.

Set these GitHub Actions repository variables:

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` | Target project ID |
| `GCP_REGION` | Registry region |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full WIF provider resource name |
| `GCP_BUILD_SERVICE_ACCOUNT` | Builder service account email |
