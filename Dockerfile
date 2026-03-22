# ── Stage 1: Frontend build ──────────────────────────
FROM node:20-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Backend runtime ─────────────────────────
FROM python:3.11-slim AS backend

LABEL maintainer="Rudy1995T" \
      description="Bittensor Subnet Alpha Trading Bot — Backend"

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/
COPY .env.example ./

# Create data directories
RUN mkdir -p /app/data/logs /app/data/exports

# Non-root user
RUN useradd --create-home trader \
    && chown -R trader:trader /app
USER trader

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8081/health || exit 1

EXPOSE 8081

ENTRYPOINT ["python", "-m", "app.main"]

# ── Stage 3: Frontend runtime ────────────────────────
FROM node:20-slim AS frontend

WORKDIR /app/frontend
COPY --from=frontend-build /app/frontend/.next ./.next
COPY --from=frontend-build /app/frontend/node_modules ./node_modules
COPY frontend/package.json ./
COPY frontend/public ./public
COPY frontend/next.config.js ./

RUN useradd --create-home trader \
    && chown -R trader:trader /app
USER trader

EXPOSE 3000

CMD ["npm", "run", "start"]
