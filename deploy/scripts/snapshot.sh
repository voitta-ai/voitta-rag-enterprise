#!/usr/bin/env bash
#
# snapshot.sh — take an ad-hoc snapshot of the data disk outside
# the daily policy from #11. Useful before risky ops (re-embed,
# secret rotation, model migration).

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
Usage: deploy/scripts/snapshot.sh

Creates a snapshot named '<disk>-manual-YYYYMMDD-HHMMSS' in the
same region as the disk. Snapshots inherit the disk's CMEK setting
(if enable_cmek=true in #7).
EOF
    exit 0
fi

repo_root="$(voitta_repo_root)"
voitta_load_env_terraform "${repo_root}"

PROJECT_ID="$(voitta_project_id)"
ZONE="$(voitta_zone)"
DISK="$(voitta_data_disk_name)"
[ -n "${PROJECT_ID}" ] || voitta_die "error: project ID unset"

NAME="${DISK}-manual-$(date +%Y%m%d-%H%M%S)"
echo "snapshotting ${DISK} -> ${NAME}..."
gcloud compute disks snapshot "${DISK}" \
    --zone="${ZONE}" \
    --project="${PROJECT_ID}" \
    --snapshot-names="${NAME}"
echo "done."
