# AGENTS.md

## Project purpose

This repository is a single-admin, self-hosted service that uses a locally built MediaCrawler sidecar to check public creator pages on Xiaohongshu, Douyin, Weibo, and Bilibili, archives newly published original content to disk, and presents it in a token-authenticated Web UI.

## Non-negotiable boundaries

- Public creator pages only. A single administrator may complete platform QR/SMS/slider login interactively and persist the resulting local browser profile. Do not accept or store platform-account passwords, accept manual Cookie uploads/exports, automate CAPTCHA solving, run proxy pools or multi-account matrices, bypass DRM, or bypass access controls.
- The primary provider is the locally built MediaCrawler sidecar pinned to commit `d280d22`. The provider may generate platform request signatures internally. MediaCrawler is used under its NON-COMMERCIAL LEARNING LICENSE 1.1, so this integration is limited to personal, non-commercial learning and research.
- The MediaCrawler sidecar is the primary discovery and media-staging path for all four platforms. The built-in Bilibili and Weibo adapters are limited fallbacks for provider unavailability or provider execution failures.
- At pinned commit `d280d22`, a healthy Bilibili provider run discovers creator video submissions only and treats only detail records with `copyright == 1` as original. The fallback must re-check creator ownership and the same originality marker before archiving a video; dynamics and articles whose originality cannot be verified are excluded. Fallback discovery is not merged into a healthy provider result, so do not claim Bilibili dynamics/article coverage.
- Files under `data/archive` are canonical. Per-content `metadata.json` files rebuild the content index. Atomic schema-v2 account ledgers under `data/archive/_state/accounts` rebuild profile URLs, per-account scheduling settings, baselines, completeness state, the untruncated terminal seen-content watermark, and durable pending-retry references; deletion writes a tombstone so rebuild does not restore the old monitoring configuration. Archived content for that slug may still create a disabled `recovered://` placeholder account solely to own rebuilt index rows. `data/state/app.db` remains operational state and is required to preserve crawl-run history and Web UI session state. Continuing an existing Web UI session requires an explicitly configured, unchanged `WEBUI_LOGIN_TOKEN`, the original `SESSION_SECRET`, the operational database state, and the unexpired client Cookie; a server-side backup cannot recreate a lost client Cookie. When `WEBUI_LOGIN_TOKEN` is empty, every application start generates a new token, atomically writes it to `data/state/webui-login-token.txt` with permissions as close to `0600` as the host filesystem permits, and invalidates prior Web UI sessions. Logs report only that file path, never the token value. An explicitly configured token removes any stale generated-token file.
- A first poll establishes a baseline, archives only the single newest historical post, and marks the remaining discovered posts as seen. Subsequent polls archive only newly seen posts.
- Never expose archived media as an unauthenticated static directory.
- Failed downloads must not leave a completed archive directory. Build in a sibling temporary directory and atomically rename only after validation succeeds.

## Architecture

- `backend/app`: FastAPI API, authentication, scheduler, provider client, fallback adapters, archive writer, rebuild tooling, and SQLite models.
- `backend/app/provider.py`: typed client for the Docker-internal crawler bridge.
- `backend/app/adapters`: URL validation plus the limited Bilibili and Weibo public-page fallback implementations.
- `crawler/app.py`: Docker-internal FastAPI bridge for platform sessions, discovery, staging, explicit-contract validation, and cleanup.
- `crawler/worker.py`: configures and runs the pinned MediaCrawler implementation with isolated per-platform browser profiles, captures platform fields before upstream JSONL serialization can discard them, and atomically emits `bridge-contract.json` schema v1 for each successful job.
- `/opt/MediaCrawler`: runtime location of the pinned third-party provider inside the crawler image; it is not exposed as a host service.
- `frontend/src`: React/TypeScript single-page administration and archive viewer.
- `data/archive/{platform}/{account}/{year}/{month}/{content_id}`: canonical archive layout.
- `data/archive/_state/accounts/{platform}/{account}.json`: canonical schema-v2 monitoring-continuity ledger, including pending retries or an account-deletion tombstone.
- `data/browser/mediacrawler/{platform}`: persistent per-platform browser profiles.
- `data/provider-staging/{job_id}`: temporary provider downloads awaiting main-service validation and promotion.
- `data/provider-state`: platform-session status and QR handoff state.
- `data/state/app.db`: operational account/content indexes, the normalized observation ledger, crawl history, and Web UI session state.
- `data/state/webui-login-token.txt`: ephemeral credential handoff used only when `WEBUI_LOGIN_TOKEN` is empty; it must be atomically replaced with restrictive permissions and removed when an explicit token is configured.
- The Web UI must remain published on host loopback (`127.0.0.1:8080` by default). `WEBUI_PORT` changes both the Uvicorn container listen port and the host-published loopback port; `APP_PORT` is a deprecated compatibility fallback used only when `WEBUI_PORT` is absent. The crawler bridge and browser debug interfaces are not published. noVNC has no application authentication and must also remain on host loopback; its default address is `127.0.0.1:7900`. `NOVNC_PORT` changes only the host-published port while websockify stays on container port `7900`.

## Collection contract

- The sidecar validates platform URLs and returns at most the newest 500 recognizable public original-content references. The main service records every observed ID without truncation and marks an account `gap_detected` if a saturated discovery window no longer overlaps its previous watermark.
- `crawler/worker.py` must capture canonical IDs and URLs, originality/pinned flags, content type, exact expected media slots, and unsupported-media state in memory before the pinned provider's lossy JSONL storage step, then atomically emit `bridge-contract.json` schema v1 after successful provider execution. The bridge must reject a missing, unknown, or internally inconsistent contract instead of inferring completeness from filenames or a downloaded-file count.
- Staging results cross the service boundary as a manifest containing relative paths, expected/downloaded file counts, a completeness flag, sizes, MIME types, and SHA-256 digests. The main service must reject an incomplete manifest, validate every file, copy into a sibling temporary archive directory, and atomically promote it. Missing expected media must remain eligible for retry and must never be published as complete.
- Failed or incomplete references must remain in the normalized observation ledger and the account ledger's `pending_refs` until a later successful archive clears them, even after they fall outside the current 500-item discovery window.
- Provider failures must use diagnostic error messages and must never silently return a successful empty result when a platform session is unavailable, the page is blocked, or the provider output is structurally unknown.
- When the provider is unavailable or fails during execution, only Bilibili and Weibo may use their built-in fallback adapters. Those adapters must validate and normalize profile URLs, return at most 20 public original-content references, and resolve references into normalized content and media candidates.
- The bridge must never return browser storage or Cookie values to the main service or Web UI.

## Development commands

- Backend tests: `python -m pytest backend/tests`
- Frontend checks: `pnpm --dir frontend test && pnpm --dir frontend build`
- Local API from `backend`: `python -m uvicorn app.main:app --reload`
- Rebuild index in Docker: `docker compose exec archive python -m app.cli rebuild-index`
- Rebuild index locally from the repository root: `python -m backend.app.cli rebuild-index`
- Full stack: `docker compose up --build`

## Engineering rules

- Keep API models typed and validate all URL, path, and enum inputs.
- Add fixture-based provider, bridge, or adapter tests for every parsing and normalization change.
- Preserve keyboard access, visible focus, responsive layouts, and explicit loading/empty/error states in the Web UI.
- Never log configured, generated, or submitted login tokens, session cookies, CSRF tokens, or complete signed media URLs. Startup logs may report the generated-token file path only.
- Keep an explicitly configured `WEBUI_LOGIN_TOKEN` and `SESSION_SECRET` only in `.env`; initialization scripts generate only `SESSION_SECRET` and intentionally leave the login token empty for runtime generation. The sole separate credential file is `data/state/webui-login-token.txt`, written atomically by the application only for an automatically generated token and deleted when an explicit token is configured. On Windows, restrict `.env` and `data` ACLs to the real interactive user, SYSTEM, and Administrators with `scripts/protect-data.ps1`; never run it as a Codex sandbox identity, and use an elevated PowerShell when the real user does not own existing files. On Unix, use `chmod 600 .env` and `chmod -R go-rwx data`.
- Run relevant tests before handing off changes. Document platform breakage rather than weakening the remaining CAPTCHA, credential, DRM, and access-control boundaries.
