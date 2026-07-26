FROM node:22-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/tsconfig*.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
RUN corepack enable && pnpm install --frozen-lockfile
RUN pnpm build

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY backend ./backend
COPY --from=frontend-builder /build/frontend/dist ./frontend_dist
COPY docker-entrypoint.sh /usr/local/bin/archive-entrypoint
RUN chmod 0755 /usr/local/bin/archive-entrypoint \
    && mkdir -p /app/data/state /app/data/archive /app/data/provider-staging
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os, urllib.request; port = os.environ.get('WEBUI_PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3)"
ENTRYPOINT ["/usr/local/bin/archive-entrypoint"]
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port \"${WEBUI_PORT:-8080}\" --proxy-headers --forwarded-allow-ips 127.0.0.1"]
