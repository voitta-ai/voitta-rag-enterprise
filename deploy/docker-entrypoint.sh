#!/usr/bin/env bash
#
# voitta-rag-enterprise container entrypoint.
#
# Two responsibilities, in order:
#
#   1. Resolve any environment variable whose value is `sm://<name>`
#      against Google Secret Manager. The lookup uses the GCE metadata
#      server for both the project ID and an OAuth access token, so no
#      key file ships in the image and no extra arguments need to be
#      passed in. Strict failure mode: a `sm://` ref that we can't
#      resolve aborts the container, which puts systemd into a clean
#      restart loop until the operator fixes the secret.
#
#   2. Honor $PORT for platforms that hand it in (Cloud Run, Heroku-
#      style hosts) by mirroring it into $VOITTA_PORT, then rewrite
#      uvicorn's --port flag if the in-image CMD is being used.
#
# Local `docker run` without GCP credentials keeps working: a value
# without the `sm://` prefix is passed through unchanged.

set -euo pipefail

# ----- Helpers --------------------------------------------------------

log_err() { printf '%s\n' "entrypoint: error: $*" >&2; }
log_inf() { printf '%s\n' "entrypoint: $*" >&2; }

# Returns 0 if any env var has a sm:// value. Used to short-circuit
# all metadata-server traffic when the operator runs the image
# locally with plain env values.
have_sm_refs() {
    while IFS= read -r line; do
        case "${line#*=}" in
            sm://*) return 0 ;;
        esac
    done < <(env)
    return 1
}

# Curl the GCE metadata server. Adds the required header. 0 = success
# AND the body is on stdout; non-zero = real error already logged.
metadata_get() {
    local path="$1"
    curl --fail --silent --show-error \
        --header "Metadata-Flavor: Google" \
        --max-time 5 \
        "http://metadata.google.internal/computeMetadata/v1/${path}"
}

# Fetch a single secret's latest version. Echo plaintext on stdout.
# Errors are non-zero return + stderr message. We use a single python
# call to base64-decode + jq is not in the runtime image.
fetch_secret() {
    local name="$1" project="$2" token="$3"
    local url body
    url="https://secretmanager.googleapis.com/v1/projects/${project}/secrets/${name}/versions/latest:access"

    body="$(curl --fail --silent --show-error \
        --max-time 10 \
        --header "Authorization: Bearer ${token}" \
        --header "Content-Type: application/json" \
        "${url}")" || {
        log_err "Secret Manager fetch failed for ${name} (curl exit $?)"
        return 1
    }

    python3 -c '
import base64, json, sys
data = json.loads(sys.stdin.read())
payload = data.get("payload", {}).get("data", "")
sys.stdout.write(base64.b64decode(payload).decode("utf-8"))
' <<< "${body}" || {
        log_err "Secret Manager response decode failed for ${name}"
        return 1
    }
}

# ----- 1. Resolve sm:// references -----------------------------------

if have_sm_refs; then
    log_inf "resolving sm:// env-var references"

    project="$(metadata_get "project/project-id")" || {
        log_err "could not read project/project-id from metadata server"
        log_err "(are we running on GCE? sm:// refs require a metadata server)"
        exit 1
    }

    token_json="$(metadata_get "instance/service-accounts/default/token")" || {
        log_err "could not read service-account token from metadata server"
        exit 1
    }

    access_token="$(python3 -c '
import json, sys
sys.stdout.write(json.loads(sys.stdin.read())["access_token"])
' <<< "${token_json}")" || {
        log_err "service-account token parse failed"
        exit 1
    }

    # Iterate environment, NUL-safe just in case a value contains
    # newlines. printenv -0 isn't portable; this approach reads each
    # `key=value` line via env, then re-reads the value with
    # `printenv "$key"` so multi-line values stay intact.
    while IFS= read -r line; do
        key="${line%%=*}"
        value="${line#*=}"
        case "${value}" in
            sm://*) ;;
            *) continue ;;
        esac

        # Re-read the actual value (env truncates at first newline if
        # we relied on it). For sm:// refs this is single-line anyway,
        # but the discipline keeps the template stable when more
        # complex env values flow through.
        value="$(printenv "${key}" 2>/dev/null || true)"
        secret_name="${value#sm://}"
        if [[ -z "${secret_name}" ]]; then
            log_err "${key} is set to bare sm:// with no secret name"
            exit 1
        fi

        plaintext="$(fetch_secret "${secret_name}" "${project}" "${access_token}")" || {
            log_err "could not resolve ${key}=sm://${secret_name}"
            exit 1
        }

        export "${key}=${plaintext}"
        log_inf "resolved ${key} from sm://${secret_name}"
    done < <(env)
fi

# ----- 2. Port mapping -----------------------------------------------

if [[ -z "${VOITTA_PORT:-}" && -n "${PORT:-}" ]]; then
    export VOITTA_PORT="${PORT}"
fi

# When running uvicorn with the in-image CMD, rewrite `--port 8000` to
# the configured VOITTA_PORT so a single env var controls both ends.
# Anything else: exec untouched.
if [[ "${1:-}" == "uvicorn" ]]; then
    args=("$@")
    for i in "${!args[@]}"; do
        if [[ "${args[$i]}" == "--port" && -n "${VOITTA_PORT:-}" ]]; then
            args[$((i + 1))]="${VOITTA_PORT}"
        fi
    done
    exec "${args[@]}"
fi

exec "$@"
