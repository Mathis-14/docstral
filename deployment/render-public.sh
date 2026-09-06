#!/usr/bin/env bash
# Substitute public Gateway configuration in manifests received on stdin.
set -euo pipefail

: "${MCP_PUBLIC_HOSTNAME:?Set MCP_PUBLIC_HOSTNAME}"
: "${MCP_PUBLIC_IP_NAME:?Set MCP_PUBLIC_IP_NAME}"
: "${MCP_TLS_CERT_NAME:?Set MCP_TLS_CERT_NAME}"
: "${MCP_TLS_POLICY_NAME:?Set MCP_TLS_POLICY_NAME}"

label='[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?'
tld='[a-z]([-a-z0-9]{0,61}[a-z0-9])?'
if [[ ${#MCP_PUBLIC_HOSTNAME} -gt 253 || ! "$MCP_PUBLIC_HOSTNAME" =~ ^($label\.)+$tld$ ]]; then
  echo 'MCP_PUBLIC_HOSTNAME must be a lowercase DNS hostname, without scheme or path.' >&2
  exit 1
fi
for setting in MCP_PUBLIC_IP_NAME MCP_TLS_CERT_NAME MCP_TLS_POLICY_NAME; do
  if [[ ! "${!setting}" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then
    echo "$setting must be a Google Cloud resource name (1-63 lowercase characters)." >&2
    exit 1
  fi
done

# shellcheck disable=SC2016 # envsubst needs the variable names, not their values.
envsubst '${MCP_PUBLIC_HOSTNAME} ${MCP_PUBLIC_IP_NAME} ${MCP_TLS_CERT_NAME} ${MCP_TLS_POLICY_NAME}'
