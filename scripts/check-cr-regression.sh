#!/usr/bin/env bash
#
# Golden-snapshot regression check for the NVD generators.
#
# For every engine-managed design, generates the EDA CRs against every registry
# profile plus the containerlab topology, and compares each byte-for-byte with a
# stored snapshot. Used to prove that refactors of the shared
# generator/executor (namespace awareness, model regeneration, ...) leave the
# existing designs untouched.
#
# Usage:
#   scripts/check-cr-regression.sh --save    # (re)create the snapshots
#   scripts/check-cr-regression.sh           # compare against the snapshots
#
# Exits non-zero if any design drifts, so it can gate CI.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SNAPSHOT_DIR="${CR_SNAPSHOT_DIR:-tests/snapshots/eda_crs}"

# Prefer the project venv: on many systems bare `python` does not exist, and
# falling through to the system interpreter would miss the project's deps.
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in .venv/bin/python python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [[ -z "$PYTHON" ]]; then
    echo "No Python interpreter found; set PYTHON=<path>" >&2
    exit 1
fi

# Design directories relative to validated-designs/. Nested paths are flattened
# with '-' for the snapshot filename.
DESIGNS=(
    3-stage-evpn-vxlan
    collapsed-spine
    unconstrained-3-stage
    ai-dc/rail-optimized
)
PROFILES=(25.12 26.4 26.8)
# ai-dc needs the AI-fabric kinds, whose CR shapes were never verified against a
# live 26.4 cluster. The generator rejects that combination by design rather
# than emitting something the cluster would refuse. 26.8's shapes *are*
# verified, so it generates them.
PROFILES_ai_dc_rail_optimized=(25.12 26.8)

mode="check"
[[ "${1:-}" == "--save" ]] && mode="save"

mkdir -p "$SNAPSHOT_DIR"
rc=0

# compare <label> <generated file> <snapshot file>
compare() {
    local label="$1" generated="$2" snapshot="$3"

    if [[ ! -f "$generated" ]]; then
        echo "MISSING   ${label} (generator produced nothing)"
        rc=1
        return
    fi

    if [[ "$mode" == "save" ]]; then
        cp "$generated" "$snapshot"
        echo "SAVED     ${label}"
    elif diff -q "$snapshot" "$generated" >/dev/null 2>&1; then
        echo "UNCHANGED ${label}"
    else
        echo "DRIFT     ${label}"
        diff "$snapshot" "$generated" | head -40
        rc=1
    fi
}

for design in "${DESIGNS[@]}"; do
    design_dir="validated-designs/${design}"
    slug="${design//\//-}"
    build_dir="${design_dir}/build"

    # Per-design profile override, named PROFILES_<slug with - as _>.
    override="PROFILES_${slug//-/_}[@]"
    profiles=("${!override:-${PROFILES[@]}}")

    for profile in "${profiles[@]}"; do
        build_file="${build_dir}/eda_transaction.json"

        # Remove first: a failed run would otherwise leave the previous
        # profile's output in place and be compared as if it had succeeded.
        rm -f "$build_file"

        if ! "$PYTHON" -m automation.deploy \
                --design "$design_dir" \
                --mode eda --generate-only --eda-version "$profile" \
                >/dev/null 2>&1; then
            echo "GENFAIL   ${slug}/${profile}"
            rc=1
            continue
        fi
        compare "${slug}/${profile}" \
                "$build_file" "${SNAPSHOT_DIR}/${slug}-${profile}.json"
    done

    # The containerlab twin is profile-independent, so it is generated once.
    clab_file="${build_dir}/$("$PYTHON" - "$design_dir" <<'PY'
import sys
from pathlib import Path
from automation.core.schema_validator import load_inputs
topology, _ = load_inputs(Path(sys.argv[1]))
print(f"{topology['fabric_name']}.clab.yml")
PY
)"
    rm -f "$clab_file"
    if ! "$PYTHON" -m automation.deploy \
            --design "$design_dir" --generate-clab >/dev/null 2>&1; then
        echo "GENFAIL   ${slug}/clab"
        rc=1
    else
        compare "${slug}/clab" "$clab_file" "${SNAPSHOT_DIR}/${slug}.clab.yml"
    fi
done

exit $rc
