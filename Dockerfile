# ── Stage 1: build ──────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

# Bring in uv without installing it system-wide
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 /uv /usr/local/bin/uv

WORKDIR /app

# Pre-compile bytecode and use copy link mode for a clean layer
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies first (better layer caching)
COPY pyproject.toml .
RUN uv pip install --system --no-cache "mcp[cli]>=1.0.0" "goodwe>=0.4.10"

# Install the package itself (no-deps: deps already above)
COPY src/ src/
RUN uv pip install --system --no-cache --no-deps .

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

RUN adduser --system --no-create-home --group appuser

WORKDIR /app

# Copy installed packages and the entry-point from the builder
COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/goodwe-mcp /usr/local/bin/goodwe-mcp

USER appuser

# ── Runtime configuration ────────────────────────────────────────────────────
# Inverter connection (required at runtime)
ENV GOODWE_HOST="" \
    GOODWE_PORT=8899 \
    GOODWE_FAMILY="" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c \
        "import urllib.request, sys; \
         r = urllib.request.urlopen('http://localhost:8000/health', timeout=4); \
         sys.exit(0 if r.status == 200 else 1)"

# Default: serve both SSE and Streamable HTTP on all interfaces
CMD ["goodwe-mcp", "--transport", "server", "--host", "0.0.0.0", "--port", "8000"]
