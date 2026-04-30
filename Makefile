.PHONY: install run dev mcp test lint typecheck clean ui-dev ui-build help doctor seed-users rebuild-index reembed

PYTHON ?= python3

# Single source of truth: .env. Recipes that need ports source it at run time
# and fall back to compiled-in defaults if the var is unset.
DOTENV := set -a; [ -f .env ] && . ./.env; set +a;

help:
	@echo "Targets:"
	@echo "  install    - install package in editable mode with dev extras"
	@echo "  run        - run web + MCP on the same port (.env VOITTA_PORT, default 8000)"
	@echo "  dev        - same, with --reload"
	@echo "  mcp        - (legacy) run only the MCP server on its own port"
	@echo "  test           - run pytest"
	@echo "  lint           - ruff check"
	@echo "  typecheck      - mypy"
	@echo "  clean          - remove build artefacts and caches"
	@echo "  doctor         - print resolved settings + probe deps"
	@echo "  seed-users     - import users.txt"
	@echo "  rebuild-index  - drop CAS+Qdrant, re-extract every file"
	@echo "  reembed        - re-enqueue stale embed jobs after a model upgrade"
	@echo "  ui-dev     - vite dev server (deferred; Stage 5 ships a vanilla SPA)"
	@echo "  ui-build   - vite build (deferred; Stage 5 ships a vanilla SPA)"

install:
	$(PYTHON) -m pip install -e ".[dev]"

# --ws-ping-interval 30 / --ws-ping-timeout 90: under heavy indexing the
# loop can momentarily get behind on sending pongs (24 workers all
# publishing through call_soon_threadsafe + the WS pump batching). The
# default 20s timeout was killing the connection on perfectly healthy
# but busy servers. 90s is generous; if a real network problem hits,
# the client's reconnect logic still kicks in within ~30s.
UVICORN_FLAGS := --host 0.0.0.0 --port "$${VOITTA_PORT:-8000}" --ws-ping-interval 30 --ws-ping-timeout 90

run:
	@$(DOTENV) \
	$(PYTHON) -m uvicorn voitta_image_rag.main:app $(UVICORN_FLAGS)

dev:
	@$(DOTENV) \
	$(PYTHON) -m uvicorn voitta_image_rag.main:app $(UVICORN_FLAGS) --reload

mcp:
	@$(DOTENV) \
	$(PYTHON) -m voitta_image_rag.mcp_server

doctor:
	$(PYTHON) -m scripts.doctor

seed-users:
	$(PYTHON) -m scripts.seed_users

rebuild-index:
	$(PYTHON) -m scripts.rebuild_index

reembed:
	$(PYTHON) -m scripts.reembed_stale

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy src

ui-dev:
	@echo "ui-dev: Stage 5 (Vite + Solid). Not yet scaffolded."

ui-build:
	@echo "ui-build: Stage 5 (Vite + Solid). Not yet scaffolded."

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
