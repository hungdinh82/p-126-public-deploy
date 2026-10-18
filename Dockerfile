# ---- Stage 1: Build ----
FROM python:3.11-slim AS builder

WORKDIR /app

ARG REQUIREMENTS_FILE=requirements/requirements-rag.txt
COPY requirements/ ./requirements/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r ${REQUIREMENTS_FILE}

# ---- Stage 2: Production ----
FROM python:3.11-slim

WORKDIR /app

# Keep the runtime dependencies outside root's home so the non-root process can
# execute their console scripts.
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# Public web and hardware profiles both use the pinned local E5 retriever.
ENV PHOWHISPER_PRELOAD=false ZEROTTS_PRELOAD=false RAG_RETRIEVAL_MODE=sqlite_local

# Security: run as non-root user
RUN useradd -m appuser

# Copy application code
COPY . .
RUN python -m src.vivi.cli.prepare_embeddings \
        --revision 761b726dd34fb83930e26aab4e9ac3899aa1fa78 \
    && echo "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39  models/multilingual-e5-small-int8/tokenizer.json" | sha256sum -c - \
    && echo "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193  models/multilingual-e5-small-int8/model_quantized.onnx" | sha256sum -c -

# Create data directory with correct ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.getenv('VIVI_PORT', '8787') + '/api/v1/health')" || exit 1

CMD ["uvicorn", "src.vivi.api.app:app", "--host", "0.0.0.0", "--port", "8787"]
