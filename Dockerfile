# ---- Stage 1: Build ----
FROM python:3.11-slim AS builder

WORKDIR /app

ARG REQUIREMENTS_FILE=requirements.txt
COPY requirements*.txt ./
RUN pip install --no-cache-dir --user -r ${REQUIREMENTS_FILE}

# ---- Stage 2: Production ----
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PHOWHISPER_PRELOAD=false ZEROTTS_PRELOAD=false

# Security: run as non-root user
RUN useradd -m appuser

# Copy application code
COPY . .

# Create data directory with correct ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/api/v1/health')" || exit 1

CMD ["uvicorn", "src.vivi.api.app:app", "--host", "0.0.0.0", "--port", "8787"]
