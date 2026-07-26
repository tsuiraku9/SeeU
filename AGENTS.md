# AGENTS.md

## Project purpose

This repository is a single-admin, self-hosted service that checks public creator
pages on Xiaohongshu, Douyin, Weibo, and Bilibili through an optional external
HTTP provider, archives newly published original content to disk, and presents
it in a token-authenticated Web UI.

## Non-negotiable boundaries

- Public creator pages only. A single administrator may complete platform
  QR/SMS/slider login interactively in the independently operated provider.
  SeeU must not accept or store platform-account passwords, Cookie
  uploads/exports, browser storage, CAPTCHA solutions, or provider browser
  profiles. Do not add proxy pools, multi-account matrices, DRM bypasses, or
  access-control bypasses.
- SeeU contains no provider implementation. It must not fetch, build, modify, or
  distribute MediaCrawler or any other platform crawler. Administrators
  independently select, license, install, and run providers that implement
  `docs/provider-http-contract.md`.
- The external provider is optional. SeeU must start without one. The built-in
  Bilibili and Weibo adapters are limited fallbacks for provider absence,
  unavailability, or execution failures; Xiaohongshu and Douyin require an
  external provider.
- A healthy Bilibili provider result may include only creator-owned original
  submissions. The fallback must re-check creator ownership and `copyright == 1`
  before archiving a video; dynamics and articles whose originality cannot be
  verified are excluded. Fallback discovery is not merged into a healthy
  provider result, so do not claim Bilibili dynamics/article coverage.
- Files under `data/archive` are canonical. Per-content `metadata.json` files
  rebuild the content index. Atomic schema-v2 account ledgers under
  `data/archive/_state/accounts` rebuild profile URLs, scheduling settings,
  baselines, completeness state, the untruncated terminal seen-content
  watermark, and durable pending-retry references; deletion writes a tombstone
  so rebuild does not restore the old monitoring configuration. Archived
  content for that slug may still create a disabled `recovered://` placeholder
  account solely to own rebuilt index rows.
- `data/state/app.db` remains operational state and is required to preserve
  crawl-run history and Web UI session state. Continuing an existing Web UI
  session requires an explicitly configured, unchanged `WEBUI_LOGIN_TOKEN`, the
  original `SESSION_SECRET`, the database state, and the unexpired client
  Cookie. A server-side backup cannot recreate a lost client Cookie.
- When `WEBUI_LOGIN_TOKEN` is empty, every application start generates a new
  token, atomically writes it to `data/state/webui-login-token.txt` with
  permissions as close to `0600` as the host filesystem permits, and
  invalidates prior Web UI sessions. Logs report only that file path, never the
  token value. An explicitly configured token removes any stale generated-token
  file.
- A first poll establishes a baseline, archives only the single newest
  historical post, and marks the remaining discovered posts as seen.
  Subsequent polls archive only newly seen posts.
- Never expose archived media as an unauthenticated static directory.
- Failed downloads must not leave a completed archive directory. Build in a
  sibling temporary directory and atomically rename only after validation
  succeeds.

## Architecture

- `backend/app`: FastAPI API, authentication, scheduler, external-provider
  client, fallback adapters, archive writer, rebuild tooling, and SQLite models.
- `backend/app/provider.py`: typed, authenticated client for the optional
  Provider HTTP contract.
- `backend/app/adapters`: URL validation plus limited Bilibili and Weibo
  public-page fallback implementations.
- `docs/provider-http-contract.md`: versioned provider requests, responses,
  errors, session handoff, media transfer, and cleanup requirements.
- `frontend/src`: React/TypeScript single-page administration and archive viewer.
- `data/archive/{platform}/{account}/{year}/{month}/{content_id}`: canonical
  archive layout.
- `data/archive/_state/accounts/{platform}/{account}.json`: canonical schema-v2
  monitoring-continuity ledger, including pending retries or an
  account-deletion tombstone.
- `data/provider-staging/{random_job_id}`: SeeU-owned temporary HTTP downloads
  awaiting validation and promotion.
- `data/state/app.db`: operational account/content indexes, normalized
  observation ledger, crawl history, and Web UI session state.
- `data/state/webui-login-token.txt`: ephemeral credential handoff used only
  when `WEBUI_LOGIN_TOKEN` is empty.
- The Web UI is published on host loopback (`127.0.0.1:8080`) by default.
  Administrators may explicitly configure another IPv4/IPv6 publication
  address and are responsible for TLS and network access controls. External
  Provider and manual-verification interfaces have their own security boundary
  and must not be published by SeeU.

## Collection contract

- The Provider validates platform URLs and returns at most the newest 500
  recognizable public original-content references. The main service records
  every observed ID without truncation and marks an account `gap_detected` if a
  saturated discovery window no longer overlaps its previous watermark.
- Provider responses explicitly contain canonical IDs and URLs,
  originality/pinned flags, aliases, content type, exact expected media slots,
  completeness, sizes, MIME types, and SHA-256 digests. SeeU rejects missing,
  unknown, or internally inconsistent contracts instead of inferring
  completeness.
- Media crosses the service boundary only through authenticated HTTP file
  endpoints. Provider host paths are invalid. SeeU streams each file into its
  own random staging directory, enforces declared and cumulative limits,
  validates Content-Type, size, SHA-256 and media magic, copies into a sibling
  temporary archive directory, and atomically promotes it.
- Missing expected media remains in the normalized observation ledger and the
  account ledger's `pending_refs` until a later successful archive clears it,
  even after it leaves the current discovery window.
- Provider failures must use diagnostic error messages and must never silently
  return a successful empty result when a session is unavailable, a page is
  blocked, or output is structurally unknown.
- Only Bilibili and Weibo may use built-in fallback adapters. They return at most
  20 public original-content references and resolve them into normalized
  content and media candidates.
- Providers must never return browser storage, Cookie values, platform
  credentials, or complete signed media URLs to SeeU.

## Development commands

- Backend tests: `python -m pytest backend/tests`
- Frontend checks: `pnpm --dir frontend test && pnpm --dir frontend build`
- Local API from `backend`: `python -m uvicorn app.main:app --reload`
- Rebuild index in Docker:
  `docker compose exec archive python -m app.cli rebuild-index`
- Rebuild index locally:
  `python -m backend.app.cli rebuild-index`
- SeeU without a bundled Provider: `docker compose up --build`

## Engineering rules

- Keep API models typed and validate every URL, path, identifier, MIME, hash,
  count, size, and enum crossing the Provider boundary.
- Add fixture-based Provider or adapter tests for every parsing and
  normalization change.
- Preserve keyboard access, visible focus, responsive layouts, and explicit
  loading/empty/error states in the Web UI.
- Never log configured/generated login tokens, Provider tokens, session
  cookies, CSRF tokens, or complete signed media URLs.
- Keep `WEBUI_LOGIN_TOKEN`, `SESSION_SECRET`, and `PROVIDER_API_TOKEN` only in
  `.env`. Initialization scripts generate only `SESSION_SECRET` and
  intentionally leave login and Provider tokens empty.
- On Windows, restrict `.env` and `data` ACLs to the real interactive user,
  SYSTEM, and Administrators with `scripts/protect-data.ps1`; never run it as a
  Codex sandbox identity. On Unix, use `chmod 600 .env` and
  `chmod -R go-rwx data`.
- Run relevant tests before handing off changes. Document platform breakage
  rather than weakening CAPTCHA, credential, DRM, access-control, or archive
  completeness boundaries.
