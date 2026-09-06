# Public HTTPS

One-time operator setup; GitHub deploys the Gateway afterward, not DNS or TLS
prerequisites. Load balancer, IP and DNS are billed separately.

## Setup

Use the [deployment variables](README.md#prerequisites) and set `KUBE_CONTEXT`
to the target cluster; always select the project/context explicitly.

1. Verify the domain's contact email. Enable the cluster's `HttpLoadBalancing`
   add-on and Gateway API (`standard`).
2. Reserve a global IPv4 address. Create a global Google-managed SSL certificate
   for `MCP_PUBLIC_HOSTNAME` and a global SSL policy (`MODERN`, minimum TLS 1.2).
   Follow [GKE Gateway TLS](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-gateway).
   Point the hostname's Cloud DNS `A` record to that IP; preserve NS/SOA and
   remove conflicting addresses, including `AAAA`.
3. Grant the deployer the namespaced Gateway permissions, from the repository root:

   ```sh
   kubectl --context "$KUBE_CONTEXT" apply -f deployment/kubernetes/public-deployer-rbac.yaml
   kubectl --context "$KUBE_CONTEXT" -n docstral create rolebinding github-public-deployer \
     --role=github-public-deployer --user="$GCP_DEPLOY_SERVICE_ACCOUNT" \
     --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
   ```

4. Add `https://<MCP_PUBLIC_HOSTNAME>/auth/callback` to the existing Google Web
   OAuth client. Preserve the OAuth Secret, signing key and PVC.
5. Set `runtime.DOCSTRAL_OAUTH_BASE_URL` to `https://<MCP_PUBLIC_HOSTNAME>` and
   the four `MCP_*` GitHub `production` variables. Outside ingestion/deployment,
   configure these values, then [deploy the release](README.md#deploy-and-test)
   with `bootstrap=false` on an existing installation.

## Verify

- Wait for certificate `ACTIVE`, Gateway `Programmed=True`, route
  `Accepted=True`/`ResolvedRefs=True`, all attached policies accepted and backend
  healthy. Confirm backend access logs are disabled before OAuth (codes in URLs).
- Over valid HTTPS, `/healthz` returns `200 ok`; unauthenticated `POST /mcp`
  returns `401` with public `WWW-Authenticate` metadata. Do not bypass TLS checks.
- Stop local MCP/port-forward. Add `https://<hostname>/mcp` to Vibe with Streamable
  HTTP and a new name, then log in through Google. An address in
  `DOCSTRAL_ALLOWED_EMAILS` must call `ask_docs` successfully with sources;
  a non-invited account must not. Repeat in Vibe Work with a custom MCP connector.

Deployment success is not public readiness; the real Q&A test is required.
For failures, inspect DNS/TLS, Gateway conditions and pod logs; follow
[deployment recovery](README.md#failure-recovery) without bypassing OAuth.
