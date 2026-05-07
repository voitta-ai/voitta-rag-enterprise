#!/usr/bin/env bash
#
# Shared helpers sourced by every deploy/scripts/*.sh helper.
# bash-3.2 compatible (no associative arrays, no mapfile).

set -euo pipefail

# repo_root, env_terraform, env_secrets resolved relative to the
# script that's sourcing us — works no matter where the caller cd'd
# into.
voitta_repo_root() {
    local script_path="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
    cd "$(dirname "${script_path}")/../.." && pwd
}

# Source .env.terraform when present so TF_VAR_* are visible. Quiet on
# missing — most scripts fall back to gcloud config.
voitta_load_env_terraform() {
    local repo_root="$1"
    if [ -f "${repo_root}/.env.terraform" ]; then
        # shellcheck disable=SC1090,SC1091
        set -a; . "${repo_root}/.env.terraform"; set +a
    fi
}

voitta_project_id() {
    if [ -n "${TF_VAR_project_id:-}" ]; then
        echo "${TF_VAR_project_id}"
    else
        gcloud config get-value project 2>/dev/null
    fi
}

voitta_zone() {
    echo "${TF_VAR_zone:-us-central1-a}"
}

voitta_vm_name() {
    echo "${TF_VAR_name_prefix:-voitta-rag}-vm"
}

voitta_data_disk_name() {
    echo "${TF_VAR_name_prefix:-voitta-rag}-data"
}

voitta_die() {
    printf '%s\n' "$@" >&2
    exit 1
}
