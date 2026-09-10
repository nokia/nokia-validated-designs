#!/bin/bash
# © 2026 Nokia
# Licensed under the BSD 3-Clause License
# SPDX-License-Identifier: BSD-3-Clause
#
# Tear down the EDA-managed DCI validated design.
#
#   ./destroy-dci-nvd.sh [--keep-clab]
#
# Deleting the dci namespace removes every CR of the design in one go; EDA
# unconfigures the nodes as the intents disappear. That only matters if you are
# keeping the lab, so with --keep-clab we wait for the namespace to actually go
# away rather than racing containerlab.
set -uo pipefail
cd "$(dirname "$0")"

NS=dci
TOPO=dci-with-eda.clab.yaml

echo "=== Removing the $NS namespace from EDA ==="
kubectl delete namespace.core.eda.nokia.com "$NS" -n eda-system --ignore-not-found --wait=true

for _ in $(seq 1 60); do
  kubectl get namespace.core.eda.nokia.com "$NS" -n eda-system >/dev/null 2>&1 || break
  sleep 5
done

if [ "${1:-}" != "--keep-clab" ]; then
  echo "=== Destroying the containerlab topology ==="
  sudo containerlab destroy -t "$TOPO" --cleanup
fi

echo "=== Done ==="
