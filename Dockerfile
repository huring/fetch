# Compiles a static stylesheet from the admin templates' Tailwind utility
# classes, using Tailwind's standalone CLI (a self-contained binary, no
# Node/npm needed) rather than the Play CDN <script> base.html used to load -
# see tailwind.config.js for why (backlog #26: that runtime JIT compiler is
# what made the wizard's prompt textarea feel laggy). This stage's only
# output that reaches the final image is the compiled CSS file below.
FROM debian:bookworm-slim AS cssbuild
ARG TARGETARCH=amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY tailwind.config.js .
COPY watcher/admin/templates/ ./watcher/admin/templates/
COPY watcher/admin/static/tailwind.input.css ./watcher/admin/static/tailwind.input.css
# Docker's TARGETARCH ("amd64"/"arm64") and the standalone CLI's own
# release-asset naming ("x64"/"arm64") disagree on the x86_64 name.
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) tw_arch=x64 ;; \
      arm64) tw_arch=arm64 ;; \
      *) echo "Unsupported TARGETARCH for the Tailwind standalone CLI: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-${tw_arch}" \
    && chmod +x /usr/local/bin/tailwindcss \
    && tailwindcss -i ./watcher/admin/static/tailwind.input.css -o ./watcher/admin/static/tailwind.css --minify

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
COPY --from=cssbuild /build/watcher/admin/static/tailwind.css ./watcher/admin/static/tailwind.css

RUN mkdir -p /data && chown -R watcher:watcher /data /app
USER watcher

EXPOSE 8000

HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD python -m watcher.healthcheck

ENTRYPOINT ["python", "-m", "watcher.main"]
