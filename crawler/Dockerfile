FROM python:3.14-bookworm
ARG MEDIACRAWLER_COMMIT=d280d22
ARG XHSHOW_VERSION=0.2.0
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
    && uv pip install --python /opt/MediaCrawler/.venv/bin/python --no-cache "xhshow==$XHSHOW_VERSION" \
    && rm -rf /opt/MediaCrawler/.git
COPY upstream_overrides/media_platform/xhs/playwright_sign.py /opt/MediaCrawler/media_platform/xhs/playwright_sign.py
RUN MEDIACRAWLER_COMMIT="$MEDIACRAWLER_COMMIT" XHSHOW_VERSION="$XHSHOW_VERSION" python -c "import hashlib,json,os,pathlib; p=pathlib.Path('/opt/MediaCrawler/media_platform/xhs/playwright_sign.py'); pathlib.Path('/opt/MediaCrawler/.bridge-build.json').write_text(json.dumps({'mediacrawler_commit':os.environ['MEDIACRAWLER_COMMIT'],'xhshow_version':os.environ['XHSHOW_VERSION'],'xhs_sign_override_sha256':hashlib.sha256(p.read_bytes()).hexdigest()},sort_keys=True),encoding='utf-8')"
WORKDIR /bridge
COPY requirements.txt ./
RUN uv pip install --python /opt/MediaCrawler/.venv/bin/python --no-cache -r requirements.txt
COPY app.py worker.py contract_validation.py network_policy.py session_state.py upstream_compatibility.py worker_protocol.py entrypoint.sh ./
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium
RUN chmod +x /bridge/entrypoint.sh
EXPOSE 8090 7900
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/v1/health', timeout=3)"
CMD ["/bridge/entrypoint.sh"]
