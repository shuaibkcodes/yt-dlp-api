FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # curl-cffi is NOT in the [default] extra. Without it yt-dlp cannot impersonate
    # a browser's TLS fingerprint: Vimeo (among others) then serves a page missing
    # the player config, and extraction falls back to an API that demands an account.
    && pip install --no-cache-dir --upgrade "yt-dlp[default,curl-cffi]"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd --system --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CACHE_HOME=/tmp \
    MAX_FILE_BYTES=268435456 \
    DOWNLOAD_TIMEOUT_SECONDS=900 \
    INFO_TIMEOUT_SECONDS=60 \
    WORKER_COUNT=2 \
    WORKER_THREADS=12 \
    DOWNLOAD_CONCURRENCY=3 \
    DOWNLOAD_QUEUE_SIZE=32 \
    DOWNLOAD_QUEUE_WAIT_SECONDS=180 \
    INFO_CONCURRENCY=20 \
    INFO_QUEUE_SIZE=80 \
    INFO_QUEUE_WAIT_SECONDS=45 \
    INFO_CACHE_TTL_SECONDS=300 \
    UVICORN_LIMIT_CONCURRENCY=256

EXPOSE 8080
# One uvicorn worker keeps memory flat; the warm yt-dlp worker pool and the
# admission queues do the throttling. A larger backlog lets a burst of 20
# connections wait in the kernel instead of being refused at accept().
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --limit-concurrency ${UVICORN_LIMIT_CONCURRENCY:-256} --backlog 256 --timeout-keep-alive 10 --no-server-header"]
