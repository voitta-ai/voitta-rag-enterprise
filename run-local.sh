#!/bin/sh
# Local single-user launch with the managed (no-Docker) Qdrant binary.
set -e
cd "$(dirname "$0")"
export VOITTA_SINGLE_USER=true
export VOITTA_DATA_DIR="$HOME/voitta-local-data"
export VOITTA_QDRANT_MODE=managed
export VOITTA_QDRANT_BINARY="$PWD/.local/bin/qdrant"
export VOITTA_ROOT_PATH="$HOME/voitta-files"
exec .venv/bin/python -m uvicorn voitta_rag_enterprise.main:app \
  --host 127.0.0.1 --port 8000 \
  --ws-ping-interval 30 --ws-ping-timeout 90
