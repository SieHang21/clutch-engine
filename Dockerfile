# syntax=docker/dockerfile:1
# =========================================================================
# Clutch (ক্ল্যাচ) - Production Multi-Stage Dockerfile
# Python 3.12-slim base optimized for layer caching and minimal image footprint
# =========================================================================

# --- Stage 1: Build Dependencies ---
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build essentials if needed for C-extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Optimize layer caching: copy requirements first
COPY requirements.txt .

# Install dependencies into a dedicated prefix
RUN pip install --no-cache-dir --user -r requirements.txt


# --- Stage 2: Final Production Runtime ---
FROM python:3.12-slim AS runner

WORKDIR /app

# Set production Python flags
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/appuser/.local/bin:$PATH

# Install minimal runtime system utilities (e.g. curl for container healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user
RUN groupadd -g 1001 appuser && \
    useradd -u 1001 -g appuser -m -s /bin/bash appuser

# Copy installed Python packages from builder stage
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application source code
COPY --chown=appuser:appuser . .

# Ensure invoices directory exists with write permissions for appuser
RUN mkdir -p /app/invoices && chown -R appuser:appuser /app/invoices

# Switch to non-root user
USER appuser

# Expose API port
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command to run FastAPI application with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

