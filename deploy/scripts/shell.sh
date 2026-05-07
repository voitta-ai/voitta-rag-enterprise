#!/usr/bin/env bash
#
# shell.sh — open an interactive SSH session on the VM over IAP.

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
Usage: deploy/scripts/shell.sh

Opens an interactive shell on the VM via IAP. No SSH key on disk —
auth is OS Login + IAM. The operator's gcloud identity must have
roles/iap.tunnelResourceAccessor + roles/compute.osLogin (or be a
project Owner).
EOF
    exit 0
fi

repo_root="$(voitta_repo_root)"
voitta_load_env_terraform "${repo_root}"

PROJECT_ID="$(voitta_project_id)"
ZONE="$(voitta_zone)"
VM="$(voitta_vm_name)"
[ -n "${PROJECT_ID}" ] || voitta_die "error: project ID unset"

exec gcloud compute ssh "${VM}" \
    --zone="${ZONE}" \
    --project="${PROJECT_ID}" \
    --tunnel-through-iap
