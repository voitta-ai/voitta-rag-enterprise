#!/usr/bin/env bash
#
# init.sh — one-time prerequisites on the operator's workstation.
# Enables required APIs in the customer's GCP project and reminds
# the operator to authenticate.

# shellcheck disable=SC1091
. "$(dirname "$0")/_lib.sh"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'EOF'
Usage: deploy/scripts/init.sh

Enables the GCP APIs that terraform / runtime need:
  - compute.googleapis.com
  - secretmanager.googleapis.com
  - cloudkms.googleapis.com
  - iap.googleapis.com
  - logging.googleapis.com
  - monitoring.googleapis.com
  - artifactregistry.googleapis.com (only used if mirroring image)

Reads project ID from TF_VAR_project_id (in .env.terraform) or from
gcloud config. Run `gcloud auth application-default login` first.
EOF
    exit 0
fi

repo_root="$(voitta_repo_root)"
voitta_load_env_terraform "${repo_root}"

PROJECT_ID="$(voitta_project_id)" || true
[ -n "${PROJECT_ID}" ] || voitta_die "error: project ID unset (set TF_VAR_project_id in .env.terraform)"

# Sanity-check ADC. If `gcloud auth application-default print-access-token`
# can mint one, terraform's google provider will work too.
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
    voitta_die "error: ADC not configured. Run 'gcloud auth application-default login' first."
fi

echo "project: ${PROJECT_ID}"
echo "enabling required APIs (idempotent; takes ~30s)..."
gcloud services enable \
    compute.googleapis.com \
    secretmanager.googleapis.com \
    cloudkms.googleapis.com \
    iap.googleapis.com \
    logging.googleapis.com \
    monitoring.googleapis.com \
    artifactregistry.googleapis.com \
    --project "${PROJECT_ID}"

echo
echo "done. Next steps:"
echo "  1. Copy .env.terraform.example -> .env.terraform and fill in values."
echo "  2. Copy .env.secrets.example -> .env.secrets and fill in values."
echo "  3. make deploy-plan && make deploy-apply"
