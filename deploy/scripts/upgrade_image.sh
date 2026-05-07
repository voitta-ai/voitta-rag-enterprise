#!/usr/bin/env bash
#
# upgrade_image.sh — pull a new container image tag on the VM and
# restart the systemd unit. Does NOT touch terraform; image upgrades
# are an out-of-band concern (terraform metadata is in
# lifecycle.ignore_changes per #4).

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
Usage: deploy/scripts/upgrade_image.sh [TAG]

  TAG  Image tag to roll forward to. If omitted, defaults to
       $TF_VAR_image_tag in .env.terraform, or 'latest'.

Edits /etc/voitta/image.env on the VM, then `systemctl restart
voitta`. The systemd unit's ExecStartPre re-pulls the image, so
this picks up new content even when TAG is unchanged.
EOF
    exit 0
fi

repo_root="$(voitta_repo_root)"
voitta_load_env_terraform "${repo_root}"

PROJECT_ID="$(voitta_project_id)"
ZONE="$(voitta_zone)"
VM="$(voitta_vm_name)"
[ -n "${PROJECT_ID}" ] || voitta_die "error: project ID unset"

TAG="${1:-${TF_VAR_image_tag:-latest}}"

echo "rolling ${VM} to image_tag=${TAG}..."
gcloud compute ssh "${VM}" \
    --zone="${ZONE}" \
    --project="${PROJECT_ID}" \
    --tunnel-through-iap \
    --command="sudo sed -i.bak 's|^IMAGE_TAG=.*|IMAGE_TAG=${TAG}|' /etc/voitta/image.env && sudo systemctl restart voitta && echo restarted"

echo "done. tail logs with 'make deploy-logs' to confirm."
