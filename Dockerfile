FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        gosu \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system --gid 1001 caibao \
    && adduser --system --ingroup caibao --uid 1001 caibao

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=caibao:caibao app ./app
COPY --chown=caibao:caibao alembic ./alembic
COPY --chown=caibao:caibao alembic.ini ./alembic.ini
COPY --chown=caibao:caibao scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
COPY --chown=caibao:caibao .env.example ./.env.example
RUN chmod +x ./scripts/docker-entrypoint.sh \
    && mkdir -p /data/uploads \
    && chown -R caibao:caibao /app /data

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3).read()" || exit 1

CMD ["./scripts/docker-entrypoint.sh"]
