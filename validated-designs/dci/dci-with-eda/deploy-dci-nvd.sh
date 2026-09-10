#!/bin/bash
# © 2026 Nokia
# Licensed under the BSD 3-Clause License
# SPDX-License-Identifier: BSD-3-Clause
#
# Deploy the EDA-managed DCI validated design.
#
#   ./deploy-dci-nvd.sh [--skip-clab]
#
# Manifests go in through the EDA transaction API (POST /core/transaction/v2)
# rather than kubectl, so the validation webhooks and the intent engine's own
# dependency ordering are exercised, and a rejected CR fails the script instead
# of sitting unreconciled in etcd.
#
# Requires: containerlab, kubectl (pointing at the EDA cluster), jq, python3
# with PyYAML, and license-srlinux.txt in this directory.
set -uo pipefail
cd "$(dirname "$0")"

EDA_HOST=${EDA_HOST:-srv0201.wdslab.be}
EDA_PORT=${EDA_PORT:-9445}
EDA_URL="https://${EDA_HOST}:${EDA_PORT}"
NS=dci
TOPO=dci-with-eda.clab.yaml
MANIFESTS=eda-manifests

SRL_NODES=(leaf1 leaf2 leaf3 leaf4 leaf5 leaf6 leaf7 leaf8
           spine1 spine2 spine3 spine4 dcgw1 dcgw2 dcgw3 dcgw4 p1 p2)

say() { echo; echo "=== $* ==="; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- transactions

eda_token() {
  local secret
  secret=$(kubectl get secret -n eda-system eda-api-client-secret \
    -o jsonpath='{.data.clientKey}' | base64 -d)
  printf 'grant_type=password&client_id=eda-api-server&client_secret=%s&username=%s&password=%s' \
    "$secret" "${EDA_USER:-admin}" "${EDA_PASS:-admin}" |
    curl -ks -X POST \
      "$EDA_URL/core/httpproxy/v1/keycloak/realms/eda/protocol/openid-connect/token" \
      -H "Content-Type: application/x-www-form-urlencoded" --data-binary @- |
    jq -r '.access_token // empty'
}

# Submit YAML files as one transaction and block until EDA reports the
# authoritative result. "state: complete" only means the engine stopped working
# on it, so the summary's success flag is what actually decides pass/fail.
apply_manifests() {
  local desc=$1; shift
  local token body resp id state ok
  token=$(eda_token) || die "token fetch failed"
  [ -n "$token" ] || die "token fetch failed - is the API reachable at $EDA_URL?"

  body=$(mktemp); resp=$(mktemp)
  python3 - "$@" > "$body" <<'PY'
import json, sys, yaml
crs = []
for path in sys.argv[1:]:
    with open(path) as fh:
        crs += [{"type": {"replace": {"value": d}}} for d in yaml.safe_load_all(fh) if d]
json.dump({"description": "dci-nvd", "dryRun": False, "resultType": "normal", "crs": crs},
          sys.stdout)
PY
  printf '%-28s %3s CRs ... ' "$desc" "$(jq '.crs|length' "$body")"

  curl -ks -o "$resp" -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    "$EDA_URL/core/transaction/v2" --data-binary @"$body"

  id=$(jq -r '.id // empty' "$resp")
  [ -n "$id" ] || { echo "REJECTED"; jq . "$resp" >&2; die "$desc rejected before execution"; }

  for _ in $(seq 1 300); do
    state=$(curl -ks -H "Authorization: Bearer $token" \
      "$EDA_URL/core/transaction/v2/state/$id" | jq -r '.state // empty')
    [ "$state" = "complete" ] && break
    sleep 2
  done
  ok=$(curl -ks -H "Authorization: Bearer $token" \
    "$EDA_URL/core/transaction/v2/result/summary/$id" | jq -r '.success')
  [ "$ok" = "true" ] || die "$desc failed (transaction $id) - inspect with:
  kubectl get transactionresults -n eda-system transaction-$(printf '%09d' "$id") -o yaml"
  echo "ok (transaction $id)"
  rm -f "$body" "$resp"
}

# ---------------------------------------------------------------- lab

if [ "${1:-}" != "--skip-clab" ]; then
  [ -f license-srlinux.txt ] || die "license-srlinux.txt missing (needed by the six 7250 nodes)"

  say "Allowing the containerlab bridge to reach the kind (EDA) bridge"
  sudo iptables -I DOCKER-USER 2 \
    -o "$(sudo docker network inspect kind -f '{{.Id}}' | cut -c 1-12 | awk '{print "br-"$1}')" \
    -m comment --comment "allow communications to kind bridge (EDA)" -j ACCEPT

  say "Deploying containerlab topology"
  sudo containerlab deploy -t "$TOPO" --reconfigure || die "containerlab deploy failed"
fi

# ---------------------------------------------------------------- bootstrap

say "Bootstrapping EDA (namespace, init, node user, node profile, pools)"
apply_manifests "namespace"    "$MANIFESTS/00-namespace.yaml"
apply_manifests "init + users" "$MANIFESTS/01-init.yaml" "$MANIFESTS/02-nodeuser.yaml"
apply_manifests "node profile" "$MANIFESTS/03-nodeprofile.yaml"
apply_manifests "pools"        "$MANIFESTS/04-pools.yaml"

# Onboarding is a commit-confirmed transaction, and EDA batches every pending
# node of the namespace into ONE of them. On a busy host the slowest node blows
# the confirm window and takes the whole batch down with it, after which the
# retry backoff doubles (77s, 2m, 4m, ... 34m) and the lab appears wedged.
# One node at a time is slower to start but finishes sooner.
say "Onboarding nodes (one at a time - see comment above)"
for node in "${SRL_NODES[@]}"; do
  python3 - "$MANIFESTS/05-toponodes.yaml" "$node" > /tmp/dci-toponode.yaml <<'PY'
import sys, yaml
path, want = sys.argv[1], sys.argv[2]
for doc in yaml.safe_load_all(open(path)):
    if doc and doc["metadata"]["name"] == want:
        print(yaml.safe_dump(doc, sort_keys=False))
PY
  apply_manifests "toponode $node" /tmp/dci-toponode.yaml
  state=""
  for _ in $(seq 1 60); do
    state=$(kubectl get toponode -n "$NS" "$node" -o jsonpath='{.status.node-state}' 2>/dev/null)
    [ "$state" = "Synced" ] && break
    sleep 10
  done
  [ "$state" = "Synced" ] || echo "WARNING: $node is $state, not Synced"
done
rm -f /tmp/dci-toponode.yaml

synced=$(kubectl get toponodes -n "$NS" \
  -o jsonpath='{range .items[*]}{.status.node-state}{"\n"}{end}' | grep -c Synced)
echo "$synced/${#SRL_NODES[@]} nodes Synced"
[ "$synced" -eq "${#SRL_NODES[@]}" ] || die "not every node onboarded; fix that before layering services"

# ---------------------------------------------------------------- design

say "Applying the design"
apply_manifests "interfaces + LAGs"  "$MANIFESTS/10-interfaces.yaml" "$MANIFESTS/11-lags.yaml"
apply_manifests "topolinks"          "$MANIFESTS/12-topolinks.yaml"
apply_manifests "fabrics"            "$MANIFESTS/20-fabrics.yaml"
# Policies come before the WAN layer, not after it: the iBGP groups name them
# as import/export policies and the interconnects reference them again, and an
# unresolved Policy reference fails the whole transaction.
apply_manifests "routing policies"   "$MANIFESTS/24-policies.yaml"
apply_manifests "wan label blocks"   "$MANIFESTS/30-wan-labelblocks.yaml"
apply_manifests "wan interfaces"     "$MANIFESTS/31-wan-interfaces.yaml"
apply_manifests "wan isis"           "$MANIFESTS/32-wan-isis.yaml"
apply_manifests "wan ldp"            "$MANIFESTS/33-wan-ldp.yaml"
apply_manifests "wan ibgp"           "$MANIFESTS/34-wan-ibgp.yaml"
apply_manifests "services dc1"       "$MANIFESTS/50-services-dc1.yaml"
apply_manifests "services dc2"       "$MANIFESTS/51-services-dc2.yaml"
apply_manifests "dci interconnects"  "$MANIFESTS/60-interconnects.yaml"
# Last, because it patches a subtree the ethernet segments must already own.
apply_manifests "configlets"         "$MANIFESTS/70-configlets.yaml"

say "Done"
echo "UI:    $EDA_URL"
echo "state: kubectl get toponodes -n $NS"
echo "tests: cd tests && pytest -v -m connectivity"
