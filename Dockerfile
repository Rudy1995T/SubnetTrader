FROM python:3.11-slim

LABEL maintainer="Rudy1995T" \
      description="Bittensor Subnet Alpha Trading Bot"

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Create data directories
RUN mkdir -p /app/data/logs /app/data/exports

# Non-root user
RUN useradd --create-home trader
RUN chown -R trader:trader /app
USER trader

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8080/health'); r.raise_for_status()" || exit 1

EXPOSE 8080

ENTRYPOINT ["python", "-m", "app.main"]
