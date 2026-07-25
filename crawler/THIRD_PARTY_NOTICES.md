# Third-party notice

The crawler image builds MediaCrawler locally from commit `d280d22`.
MediaCrawler is copyright its contributors and is used under its
NON-COMMERCIAL LEARNING LICENSE 1.1. This integration is intended only for
personal, non-commercial learning and research. The upstream license remains
available at `/opt/MediaCrawler/LICENSE` inside the locally built image.

The image applies one narrow compatibility backport from upstream commit
`17f66121e0fcc40fc23958b995bec873d422667d` and pins `xhshow==0.2.0`. The
backport replaces the older private-method/monkey-patch signing integration
with xhshow's public GET/POST signing APIs. This keeps the MediaCrawler source
baseline at `d280d22` while incorporating the upstream fix published on
2026-07-24.
