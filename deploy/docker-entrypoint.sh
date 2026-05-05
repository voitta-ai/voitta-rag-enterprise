#!/usr/bin/env bash
#
# voitta-rag-enterprise container entrypoint.
#
# For #1 (Dockerfile-only) this is a thin shim that just exec's whatever
# CMD was passed. The Secret-Manager `sm://...` env-var resolution lands
# in a later issue (#9) — when that ships it'll be added here, before the
# final `exec`, so the rest of this file can stay unchanged.
#
# Honor $PORT for platforms that pass it (Cloud Run, Heroku-style hosts):
# if VOITTA_PORT is unset and PORT is set, mirror PORT into VOITTA_PORT so
# both the app config and the uvicorn `--port` flag pick it up.

set -euo pipefail

if [[ -z "${VOITTA_PORT:-}" && -n "${PORT:-}" ]]; then
    export VOITTA_PORT="${PORT}"
fi

# When running uvicorn with the in-image CMD (Dockerfile default), rewrite
# `--port 8000` to the configured VOITTA_PORT so a single env var controls
# both ends. For any other command, exec it untouched.
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
