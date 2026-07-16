FROM python:3.11-bookworm
ARG MEDIACRAWLER_COMMIT=d280d22
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 \
    PATH=/opt/MediaCrawler/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends git xvfb x11vnc novnc websockify chromium ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/google/chrome && ln -s /usr/bin/chromium /opt/google/chrome/chrome
RUN git clone https://github.com/NanmiCoder/MediaCrawler.git /opt/MediaCrawler && cd /opt/MediaCrawler && git checkout "$MEDIACRAWLER_COMMIT" && test "$(git rev-parse --short=7 HEAD)" = "$MEDIACRAWLER_COMMIT"
RUN pip install --no-cache-dir uv \
    && cd /opt/MediaCrawler \
    && uv sync --frozen \
    && rm -rf /opt/MediaCrawler/.git
WORKDIR /bridge
COPY requirements.txt ./
RUN uv pip install --python /opt/MediaCrawler/.venv/bin/python --no-cache -r requirements.txt
COPY app.py worker.py entrypoint.sh ./
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
RUN chmod +x /bridge/entrypoint.sh
EXPOSE 8090 7900
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/v1/health', timeout=3)"
CMD ["/bridge/entrypoint.sh"]
