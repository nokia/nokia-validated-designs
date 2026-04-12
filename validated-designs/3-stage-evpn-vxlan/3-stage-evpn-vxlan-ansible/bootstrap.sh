#!/usr/bin/env bash
# Bootstrap SR Linux nodes with a self-signed TLS certificate and JSON-RPC server.
#
# Reads ansible_host addresses from inventory.yml and uses gnmic (gNMI) to
# configure each node -- gNMI is always available on SR Linux, whereas the
# JSON-RPC server (needed by the nokia.srlinux Ansible collection) may not be.
#
# A single self-signed certificate is generated locally with openssl and pushed
# to every node's TLS server-profile via gNMI set.
#
# Usage:
#   ./bootstrap.sh                      # all nodes from inventory.yml
#   ./bootstrap.sh leaf1 spine1         # specific nodes only
#
# Environment variables:
#   GNMI_PORT     gNMI port on the nodes (default: 57410)
#   JSONRPC_PORT  HTTPS port for JSON-RPC (default: 443)
#   SRL_USER      SR Linux username (default: admin)
#   SRL_PASS      SR Linux password (default: NokiaSrl1!)
#
# Prerequisites: gnmic, openssl, curl, python3 (with PyYAML)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INVENTORY="${SCRIPT_DIR}/inventory.yml"

GNMI_PORT="${GNMI_PORT:-57410}"
SRL_USER="${SRL_USER:-admin}"
SRL_PASS="${SRL_PASS:-NokiaSrl1!}"

PROFILE_NAME="self-signed-tls"
JSONRPC_PORT="${JSONRPC_PORT:-443}"

TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

# ---------------------------------------------------------------------------
# Parse inventory
# ---------------------------------------------------------------------------
get_hosts() {
    python3 -c "
import yaml, sys
with open('${INVENTORY}') as f:
    inv = yaml.safe_load(f)
for group in inv.get('all', {}).get('children', {}).values():
    for name, attrs in group.get('hosts', {}).items():
        print(name, attrs.get('ansible_host', name))
"
}

# ---------------------------------------------------------------------------
# Generate a single self-signed certificate (shared across all nodes)
# ---------------------------------------------------------------------------
generate_cert() {
    openssl req -x509 -newkey rsa:4096 -nodes \
        -keyout "${TMPDIR}/key.pem" -out "${TMPDIR}/cert.pem" \
        -days 365 -subj "/CN=srlinux-ansible" 2>/dev/null

    KEY_ESCAPED=$(awk '{printf "%s\\n", $0}' "${TMPDIR}/key.pem")
    CERT_ESCAPED=$(awk '{printf "%s\\n", $0}' "${TMPDIR}/cert.pem")
}

# ---------------------------------------------------------------------------
# gnmic wrapper
# ---------------------------------------------------------------------------
run_gnmic() {
    local host="$1"; shift
    gnmic -a "${host}:${GNMI_PORT}" --skip-verify \
        -u "${SRL_USER}" -p "${SRL_PASS}" -e json_ietf "$@"
}

# ---------------------------------------------------------------------------
# Bootstrap a single node
# ---------------------------------------------------------------------------
bootstrap_node() {
    local name="$1" host="$2"
    echo "--- ${name} (${host}) ---"

    # 1. Push TLS server-profile with cert+key and JSON-RPC server config
    echo "  Configuring TLS profile and JSON-RPC server..."
    if ! run_gnmic "${host}" set \
        --update-path "/system/tls/server-profile[name=${PROFILE_NAME}]" \
        --update-value "{
            \"key\": \"${KEY_ESCAPED}\",
            \"certificate\": \"${CERT_ESCAPED}\",
            \"authenticate-client\": false
        }" \
        --update-path /system/json-rpc-server \
        --update-value "{
            \"admin-state\": \"enable\",
            \"network-instance\": [{
                \"name\": \"mgmt\",
                \"http\": {\"admin-state\": \"enable\"},
                \"https\": {
                    \"admin-state\": \"enable\",
                    \"tls-profile\": \"${PROFILE_NAME}\",
                    \"port\": ${JSONRPC_PORT}
                }
            }]
        }" >/dev/null 2>&1; then
        echo "  ERROR: gNMI set failed for ${name}"
        return 1
    fi
    echo "  TLS profile '${PROFILE_NAME}' and JSON-RPC server configured"

    # 2. Verify JSON-RPC is reachable
    echo -n "  Verifying JSON-RPC... "
    local ok=false
    for _ in $(seq 1 10); do
        if curl -sk -o /dev/null -w '%{http_code}' \
            "https://${host}:${JSONRPC_PORT}/jsonrpc" \
            -H 'Content-Type: application/json' \
            -d '{"jsonrpc":"2.0","id":1,"method":"get","params":{"commands":[{"path":"/system/name","datastore":"running"}]}}' \
            -u "${SRL_USER}:${SRL_PASS}" 2>/dev/null | grep -q '200'; then
            ok=true
            break
        fi
        sleep 1
    done
    if $ok; then
        echo "OK (https://${host}:${JSONRPC_PORT})"
    else
        echo "FAILED"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
declare -A HOST_MAP
while read -r name host; do
    HOST_MAP["${name}"]="${host}"
done < <(get_hosts)

if [[ $# -gt 0 ]]; then
    TARGETS=("$@")
else
    TARGETS=("${!HOST_MAP[@]}")
fi

IFS=$'\n' TARGETS=($(sort <<<"${TARGETS[*]}")); unset IFS

echo "Bootstrapping ${#TARGETS[@]} node(s) with JSON-RPC server"
echo "  TLS profile: ${PROFILE_NAME}"
echo "  gNMI port: ${GNMI_PORT}  JSON-RPC HTTPS port: ${JSONRPC_PORT}"
echo ""

echo "Generating self-signed TLS certificate..."
generate_cert
echo ""

FAILED=()
for name in "${TARGETS[@]}"; do
    host="${HOST_MAP[${name}]:-}"
    if [[ -z "${host}" ]]; then
        echo "ERROR: '${name}' not found in inventory"
        FAILED+=("${name}")
        continue
    fi
    if ! bootstrap_node "${name}" "${host}"; then
        FAILED+=("${name}")
    fi
    echo ""
done

if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "FAILED nodes: ${FAILED[*]}"
    exit 1
fi
echo "All ${#TARGETS[@]} node(s) bootstrapped successfully."
