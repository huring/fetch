FROM python:3.12-slim

ENV TZ=Europe/Stockholm \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 watcher
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher/ ./watcher/

RUN mkdir -p /data && chown -R watcher:watcher /data /app
USER watcher

EXPOSE 8000

HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD python -m watcher.healthcheck

ENTRYPOINT ["python", "-m", "watcher.main"]
