# Spela for a NAS (Unraid, ZimaOS, Synology…): the web app without the Windows window and tray icon.
# Built and pushed by .github/workflows/docker.yml for every release tag, from the same commit as the Windows zip.
#   docker build -t spela .
#   docker run -d --name spela --network host -v ./data:/data -v /mnt/games:/games spela

# The frontend builds once, on the build machine's own platform, whatever platform the image is for.
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
# bsdtar extracts zip, 7z and rar archives (archives.py); GNU tar can't
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools && rm -rf /var/lib/apt/lists/*
COPY backend/ /tmp/backend/
RUN pip install --no-cache-dir /tmp/backend && rm -rf /tmp/backend
COPY --from=frontend /src/dist /app/static
# 0.0.0.0: a NAS is always used from other devices. /data is created writable for any user, for `docker run --user`.
ENV SPELA_DOCKER=1 DATA_DIR=/data STAGING_DIR=/data/staging STATIC_DIR=/app/static HOST=0.0.0.0 PORT=8787 \
    PYTHONUNBUFFERED=1
RUN mkdir -m 777 /data
VOLUME /data
EXPOSE 8787
HEALTHCHECK CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/v1/health', timeout=3)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\""]
