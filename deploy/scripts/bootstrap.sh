#!/usr/bin/env bash
#
# bootstrap.sh — sanity-check first-boot health on the VM.
# cloud-init handles the actual setup; this just curls /healthz over
# IAP and reports pass/fail. Useful right after `make deploy-apply`
# to confirm the unit came up.

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
Usage: deploy/scripts/bootstrap.sh

SSHs into the VM via IAP and curls http://localhost:8000/healthz.
Exits 0 on healthy, non-zero otherwise. Idempotent — safe to re-run.
EOF
    exit 0
fi

repo_root="$(voitta_repo_root)"
voitta_load_env_terraform "${repo_root}"

PROJECT_ID="$(voitta_project_id)"
ZONE="$(voitta_zone)"
VM="$(voitta_vm_name)"
[ -n "${PROJECT_ID}" ] || voitta_die "error: project ID unset"

echo "checking ${VM} in ${ZONE} (${PROJECT_ID})..."
# `--ssh-flag` keeps each retry quick. ConnectTimeout=10s caps the
# wait if the VM hasn't come up yet.
gcloud compute ssh "${VM}" \
    --zone="${ZONE}" \
    --project="${PROJECT_ID}" \
    --tunnel-through-iap \
    --ssh-flag="-o ConnectTimeout=10" \
    --command='curl -fsS http://localhost:8000/healthz && echo' \
    || voitta_die "healthz failed. Check 'make deploy-logs' for details."

echo "OK"
