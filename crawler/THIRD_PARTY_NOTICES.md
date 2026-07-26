# Third-party notice

The crawler image builds MediaCrawler locally from commit `d280d22`.
MediaCrawler is copyright its contributors and is used under its
NON-COMMERCIAL LEARNING LICENSE 1.1. This integration is intended only for
personal, non-commercial learning and research. A complete copy of that
license is stored at
`../LICENSES/MediaCrawler-NON-COMMERCIAL-LEARNING-LICENSE-1.1.txt`; it also
remains available at `/opt/MediaCrawler/LICENSE` inside the locally built
image.

The image applies one narrow compatibility backport from upstream commit
`17f66121e0fcc40fc23958b995bec873d422667d` and pins `xhshow==0.2.0`. The
backport replaces the older private-method/monkey-patch signing integration
with xhshow's public GET/POST signing APIs. This keeps the MediaCrawler source
baseline at `d280d22` while incorporating the upstream fix published on
2026-07-24.

The compatibility backport at
`upstream_overrides/media_platform/xhs/playwright_sign.py` is derived from
MediaCrawler and remains subject to the upstream license above. It is excluded
from the MIT License that applies to SeeU's original source code.
