#!/usr/bin/env bash
#
# populate_secrets.sh — push values from .env.secrets into the
# Secret Manager resources that terraform declared.
#
# Usage:
#   make deploy-secrets                                 # default
#   ENV_FILE=.env.secrets.staging ./deploy/scripts/populate_secrets.sh
#
# Idempotent: each run adds a NEW version of every secret. To rotate,
# edit .env.secrets, re-run, and restart the container so it picks up
# the latest version. Empty values are skipped (no-op for that
# secret) — useful when only a subset has been provisioned.
#
# Compatible with macOS bash 3.2 (no mapfile, no `declare -A`).

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: populate_secrets.sh [--help]

Reads ENV_FILE (default: .env.secrets in the repo root) and adds a
new version of each secret named in `terraform output secret_ids`
whose corresponding value is non-empty.

Requires: gcloud (authenticated to the customer's project),
terraform (run from deploy/terraform).

Environment overrides:
  ENV_FILE     Path to .env.secrets file (default: .env.secrets).
  TF_DIR       Path to terraform root (default: deploy/terraform).
EOF
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
fi

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${repo_root}/.env.secrets}"
TF_DIR="${TF_DIR:-${repo_root}/deploy/terraform}"

if [ ! -f "${ENV_FILE}" ]; then
    echo "error: ${ENV_FILE} not found. Copy .env.secrets.example and fill in values." >&2
    exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: gcloud not on PATH." >&2
    exit 1
fi

# Pull project from .env.terraform if present.
ENV_TF="${repo_root}/.env.terraform"
if [ -f "${ENV_TF}" ]; then
    # shellcheck disable=SC1090
    set -a; . "${ENV_TF}"; set +a
fi
PROJECT_ID="${TF_VAR_project_id:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "${PROJECT_ID}" ]; then
    echo "error: project ID unset. Set TF_VAR_project_id in .env.terraform or run 'gcloud config set project <id>'." >&2
    exit 1
fi

# Pull secret IDs from terraform output. Newline-separated to a tmp
# file so we can iterate without bash 4 mapfile.
SECRETS_LIST="$(mktemp -t voitta-secrets.XXXXXX)"
trap 'rm -f "${SECRETS_LIST}"' EXIT

(
    cd "${TF_DIR}"
    terraform output -json secret_ids 2>/dev/null
) | python3 -c 'import json,sys; [print(s) for s in json.load(sys.stdin)]' \
    > "${SECRETS_LIST}"

if [ ! -s "${SECRETS_LIST}" ]; then
    echo "error: no secrets in terraform output. Run 'terraform apply' first." >&2
    exit 1
fi

# Look up a key's value in ENV_FILE without an associative array.
# Strips surrounding single/double quotes and any leading/trailing
# whitespace on the value.
lookup_value() {
    local key="$1"
    local raw
    raw="$(grep -E "^[[:space:]]*${key}=" "${ENV_FILE}" | tail -n1 || true)"
    [ -z "${raw}" ] && return 0
    raw="${raw#*=}"
    # Trim surrounding spaces.
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    # Trim a single layer of matched quotes.
    case "${raw}" in
        \"*\") raw="${raw%\"}"; raw="${raw#\"}" ;;
        \'*\') raw="${raw%\'}"; raw="${raw#\'}" ;;
    esac
    printf '%s' "${raw}"
}

added=0
skipped=0
while IFS= read -r secret; do
    [ -z "${secret}" ] && continue
    value="$(lookup_value "${secret}")"
    if [ -z "${value}" ]; then
        echo "skip ${secret} (empty in ${ENV_FILE})"
        skipped=$((skipped + 1))
        continue
    fi
    printf '%s' "${value}" \
        | gcloud secrets versions add "${secret}" \
            --project "${PROJECT_ID}" \
            --data-file=- \
        > /dev/null
    echo "added ${secret} (new version)"
    added=$((added + 1))
done < "${SECRETS_LIST}"

echo
echo "done: ${added} added, ${skipped} skipped"
echo "restart the app container to pick up new versions:"
echo "  ./deploy/scripts/upgrade_image.sh   # or systemctl restart voitta on the VM"
