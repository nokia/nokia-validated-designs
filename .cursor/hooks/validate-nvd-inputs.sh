#!/usr/bin/env bash
# postToolUse hook: validate NVD design inputs after the agent edits them.
#
# No matcher is configured in hooks.json because the tool name for an edit
# varies (Write, StrReplace, ...). Filtering happens here instead: the payload
# is only handed to Python when it mentions a design input path, so unrelated
# tool calls cost one string match rather than an interpreter start.
#
# Always exits 0 — a broken validator must never block the agent.

set -uo pipefail

payload=$(cat)

case "$payload" in
  *validated-designs*) ;;
  *) exit 0 ;;
esac

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

# The validator needs jsonschema + pyyaml + the automation package, so prefer
# the project venv. A bare python3 usually lacks them; the script exits quietly
# in that case rather than reporting a spurious failure.
python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  python_bin=$(command -v python3) || exit 0
fi

printf '%s' "$payload" | "$python_bin" "$repo_root/.cursor/hooks/validate_nvd_inputs.py"
exit 0
