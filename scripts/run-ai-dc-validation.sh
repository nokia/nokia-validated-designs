#!/usr/bin/env bash
# Validate a running AI DC rail-optimized deployment.
#
# Checks that traffic is carried where the design says it should be: GPU rail
# connectivity and ECMP across the RoCEv2 backend, and GPU-to-storage
# connectivity across the EVPN-VXLAN frontend.
#
# The fabric is deployed and converged before this runs; the topology is
# discovered from the live nodes, so no design arguments are needed.
#
# Usage:
#   scripts/run-ai-dc-validation.sh [<clab topology>] [pytest args...]
#
# Examples:
#   scripts/run-ai-dc-validation.sh
#   scripts/run-ai-dc-validation.sh path/to/dc1.clab.yml -k backend
#   scripts/run-ai-dc-validation.sh path/to/dc1.clab.yml --html=report.html

set -euo pipefail

cd "$(dirname "$0")/.."

DEFAULT_TOPO="validated-designs/ai-dc/rail-optimized/build/dc1.clab.yml"

TOPO="$DEFAULT_TOPO"
if [[ $# -gt 0 && "$1" != -* ]]; then
    TOPO="$1"
    shift
fi

if [[ ! -f "$TOPO" ]]; then
    echo "Topology file not found: $TOPO" >&2
    echo "Generate it with:" >&2
    echo "  python -m automation.deploy --design validated-designs/ai-dc/rail-optimized \\" >&2
    echo "      --mode eda --generate-clab" >&2
    exit 1
fi

echo "Validating AI DC fabric from $TOPO"

exec uv run pytest tests/test_ai_dc_connectivity.py \
    -m ai_dc \
    --clab-topo "$TOPO" \
    -v "$@"
