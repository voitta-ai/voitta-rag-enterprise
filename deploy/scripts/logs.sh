#!/usr/bin/env bash
#
# logs.sh — tail journalctl -u voitta on the VM over IAP.

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
Usage: deploy/scripts/logs.sh [UNIT]

  UNIT  systemd unit to tail. Defaults to 'voitta'.
        'caddy' is the other one we ship.

Streams `journalctl -u <UNIT> -f` over IAP SSH. Ctrl-C to detach.
EOF
    exit 0
fi

repo_root="$(voitta_repo_root)"
voitta_load_env_terraform "${repo_root}"

PROJECT_ID="$(voitta_project_id)"
ZONE="$(voitta_zone)"
VM="$(voitta_vm_name)"
[ -n "${PROJECT_ID}" ] || voitta_die "error: project ID unset"

UNIT="${1:-voitta}"

gcloud compute ssh "${VM}" \
    --zone="${ZONE}" \
    --project="${PROJECT_ID}" \
    --tunnel-through-iap \
    --command="sudo journalctl -u ${UNIT} -f --no-pager"
