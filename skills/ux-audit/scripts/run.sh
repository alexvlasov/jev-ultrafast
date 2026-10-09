#!/usr/bin/env bash
# Runs ux_audit.py inside the jev-ultrafast project, even when this skill is reached through a symlink.
set -euo pipefail
HERE="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
ENV_ARGS=()
[ -f "$REPO/.env" ] && ENV_ARGS=(--env-file "$REPO/.env")
exec uv run --quiet --project "$REPO" "${ENV_ARGS[@]}" python "$HERE/ux_audit.py" "$@"
