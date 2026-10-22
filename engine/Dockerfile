FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODESYNC_DATA_DIR=/data

RUN useradd --create-home --uid 1000 codesync \
    && mkdir /data \
    && chown codesync:codesync /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

USER codesync
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'))"

CMD ["sh", "-c", "uvicorn codesync.server.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
