# syntax=docker/dockerfile:1.7
#
# Production image for Voitta RAG Enterprise.
#
# Single replica per deployment — the app holds an embedded Qdrant + a
# SQLite DB on a mounted PD, so we do not multi-stage to minimise size.
# We DO multi-stage to keep build tooling out of the runtime layer.
#
# What the build does:
#
# 1. ``base`` installs OS libs the wheels can't carry (cairo, poppler,
#    libgl) and the Python deps.
# 2. ``warm`` runs scripts/prewarm_models.py against ``HF_HOME`` so the
#    embedder + MinerU weights are baked into the image. First boot of a
#    fresh pod does no network downloads — works in egress-restricted
#    customer environments.
# 3. ``runtime`` strips build tooling, drops to a non-root UID, and runs
#    uvicorn against the FastAPI entrypoint. The same process serves the
#    UI, REST, WS, and MCP — there is no separate MCP container.
#
# Build:   docker build -t voitta-rag-enterprise:dev .
# Smoke:   docker run --rm -p 8000:8000 \
#              -e VOITTA_DEV_USER=you@localhost \
#              -v $(pwd)/local-data:/data \
#              voitta-rag-enterprise:dev
#          curl http://localhost:8000/healthz   # → {"ok": true}

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1 — base: OS libs + Python deps
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    HF_HOME=/opt/hf-cache \
    MINERU_MODEL_SOURCE=huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libjpeg62-turbo \
        poppler-utils \
        fonts-liberation \
        # CAD render path (cad_step_parser / cad_fcstd_parser → OCP + VTK).
        # The cadquery-ocp pip wheel is self-contained (bundles OpenCASCADE
        # 7.x), but the vtk wheel dlopens the host's OpenGL stack at runtime:
        #   - libegl1 + libgl1-mesa-dri: EGL surface with Mesa Gallium
        #     llvmpipe — VTK's preferred software-OpenGL path on a no-GPU
        #     host, and what production headless renders actually use.
        #   - libosmesa6: belt-and-braces fallback. VTK ≥ 9.3 ships a
        #     bundled OSMesa, but the apt build links cleanly when EGL is
        #     absent (e.g. cut-down container hosts).
        # Keep these in the slim image — they total ~30 MB and the render
        # endpoint silently degrades to "no renderer found" otherwise.
        libegl1 \
        libgl1-mesa-dri \
        libosmesa6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps in a separate layer from the app source so iterating
# on the source doesn't bust the dep cache. ``ml`` extras pulls in
# sentence-transformers + transformers; ``dev`` is intentionally NOT here.
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
# mineru[all] + the transformers/torch stack is too large for pip's default
# backtracking resolver — a clean (uncached) install explodes into
# ResolutionTooDeep (200k rounds) regardless of constraints. The legacy resolver
# takes first-compatible instead of exhaustively backtracking, so it converges;
# ``-c constraints.txt`` then pins the three packages whose newest releases
# would otherwise be installed in conflict (see constraints.txt). This pairing
# is verified to install cleanly. The legacy resolver is deprecated-but-present
# in current pip; a full pinned lockfile is the longer-term hardening.
RUN pip install --use-deprecated=legacy-resolver -c constraints.txt -e ".[ml]"

# SPA assets — copied after pip install so iterating on UI doesn't bust
# the dep cache. ``main.py`` only registers the root route when this dir
# is present at import time.
COPY static ./static

# ---------------------------------------------------------------------------
# Stage 2 — warm: pre-fetch model weights into HF_HOME
# ---------------------------------------------------------------------------
FROM base AS warm
COPY scripts/prewarm_models.py /tmp/prewarm.py
RUN python /tmp/prewarm.py && rm /tmp/prewarm.py

# ---------------------------------------------------------------------------
# Stage 3 — runtime: drop build tooling, run as non-root
# ---------------------------------------------------------------------------
FROM base AS runtime

# Pull the warmed model cache from the previous stage. HF_HOME points at
# /opt/hf-cache, so transformers, sentence-transformers, fastembed, and
# MinerU all land there — single cache root, no need to chase ~/.cache.
COPY --from=warm /opt/hf-cache /opt/hf-cache

# MinerU writes its config to ~/mineru.json at warm time (HOME=/root).
# Carry that file over to the runtime user's home so MinerU finds the
# pre-warmed weights instead of re-downloading on first parse.
COPY --from=warm /root/mineru.json /tmp/mineru.json

# Strip the toolchain we only needed for pip-installing native wheels.
# ``git`` stays — the GitHub sync feature shells out to ``git ls-remote``
# and ``git clone`` at runtime.
RUN apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. UID 1000 keeps the PD bind-mount permissions
# predictable when an operator chowns the volume out of band.
RUN useradd --uid 1000 --create-home --shell /bin/bash voitta \
    && mkdir -p /data \
    && mv /tmp/mineru.json /home/voitta/mineru.json \
    && chown -R voitta:voitta /data /opt/hf-cache /home/voitta
USER voitta
ENV HF_HOME=/opt/hf-cache \
    HOME=/home/voitta \
    VOITTA_DATA_DIR=/data \
    VOITTA_PORT=8000

WORKDIR /app
EXPOSE 8000

# Single uvicorn worker — the app's queue + GPU lock assume one writer.
# Indexing parallelism lives inside the asyncio worker pool, not in
# uvicorn-level forking.
CMD ["python", "-m", "uvicorn", "voitta_rag_enterprise.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "30", "--ws-ping-timeout", "90"]
