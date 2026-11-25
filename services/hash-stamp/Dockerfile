# QorTime isolated auxiliary hash-stamp service (qortime.aux.hash_stamp.v1 only).
# Build context = repo root:
#   docker build -f services/hash-stamp/Dockerfile -t <image> .
# Signing seed is injected at runtime via Secret Manager (QOR_HASH_STAMP_SEED_FILE or QOR_HASH_STAMP_SEED),
# never baked into this image.
FROM python:3.12-slim

RUN groupadd --gid 10001 appuser \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
 && mkdir -p /app /data \
 && chown -R 10001:10001 /app /data

WORKDIR /app
COPY --chown=10001:10001 pyproject.toml README.md LICENSE ./
COPY --chown=10001:10001 qor_receipt ./qor_receipt

RUN pip install --no-cache-dir ".[referee]"

USER 10001:10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QOR_HASH_STAMP_AUTOSTART=1 \
    QOR_HASH_STAMP_JSONL=/data/hash_stamps.jsonl

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn qor_receipt.hash_stamp_app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --limit-concurrency 32 --timeout-keep-alive 30"]
