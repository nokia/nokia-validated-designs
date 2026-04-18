#!/usr/bin/env bash
# Run the SR Linux JSON-RPC acceptance tests.
# Each parameterization spins up a fresh single-node containerlab, pushes the
# generated config, and tears the lab down. Requires containerlab + docker.

set -euo pipefail

cd "$(dirname "$0")/.."

exec uv run pytest tests/system/ --run-system -v "$@"
