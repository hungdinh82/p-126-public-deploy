# ---- Stage 1: Build ----
FROM python:3.11-slim AS builder

WORKDIR /app

ARG REQUIREMENTS_FILE=requirements-rag.txt
COPY requirements*.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r ${REQUIREMENTS_FILE}

# ---- Stage 2: Production ----
FROM python:3.11-slim

WORKDIR /app

# Keep the runtime dependencies outside root's home so the non-root process can
# execute their console scripts.
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# Cloud profile: use hosted providers and lexical/FTS handbook retrieval.  The
# local E5, PhoWhisper, and ZeroTTS models are intentionally not part of this
# image; a deployment can still override these defaults explicitly.
ENV PHOWHISPER_PRELOAD=false \
    ZEROTTS_PRELOAD=false \
    VIVI_RAG_LOCAL_ONLY=false \
    RAG_RETRIEVAL_MODE=sqlite \
    STT_FALLBACK_PROVIDER=off \
    TTS_PROVIDER=off

# Security: run as non-root user
RUN useradd -m appuser

# Copy application code
COPY . .

# Create data directory with correct ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.getenv('VIVI_PORT', '8787') + '/api/v1/health')" || exit 1

CMD ["uvicorn", "src.vivi.api.app:app", "--host", "0.0.0.0", "--port", "8787"]
