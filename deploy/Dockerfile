# syntax=docker/dockerfile:1.7
#
# voitta-rag-enterprise — single-stage runtime image (CPU base, amd64).
#
# We use a single-stage build instead of the more typical builder/runtime
# split. Reason: the Python deps (mineru, sentence-transformers, torch on
# CPU, etc.) drag in compiled extensions that need the same shared
# libraries at runtime as at build time, so a slimmer runtime stage saves
# very little real disk and risks ABI mismatch. If the image grows
# unacceptably we can revisit.
#
# Built and pushed to ghcr.io/voitta-ai/voitta-rag-enterprise on tag push;
# see .github/workflows/release.yml.

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VOITTA_DATA_DIR=/var/lib/voitta \
    VOITTA_PORT=8000

# System packages.
#   - libcairo2, libpango*, libgdk-pixbuf-2.0-0: cairosvg runtime.
#   - libgomp1: torch / numpy OpenMP runtime.
#   - fonts-dejavu-core, fonts-liberation, fonts-noto-cjk: SVG/PDF text
#     rendering with reasonable Unicode coverage out of the box.
#   - ffmpeg: video parser and a few mineru subroutines call it.
#   - poppler-utils: mineru's "txt" path uses pdftotext.
#   - curl, ca-certificates: healthcheck + secret manager fetch in #9.
#   - tini: signal-safe PID 1 for clean uvicorn shutdown.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libgomp1 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        poppler-utils \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what pip needs to resolve deps first; this layer is reused
# across rebuilds when source files change but pyproject.toml does not.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install .

# App static + scripts.
COPY static ./static
COPY scripts ./scripts

# Non-root runtime user.
RUN groupadd --system --gid 1000 voitta \
 && useradd  --system --uid 1000 --gid voitta --home-dir /app --shell /usr/sbin/nologin voitta \
 && mkdir -p "${VOITTA_DATA_DIR}" \
 && chown -R voitta:voitta "${VOITTA_DATA_DIR}" /app

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER voitta

VOLUME ["/var/lib/voitta"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${VOITTA_PORT:-8000}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "voitta_image_rag.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--ws-ping-interval", "30", \
     "--ws-ping-timeout", "90"]
