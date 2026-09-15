# WaveFlow Security Refactor Audit

## Current File Map

- `backend/main.py`: still the largest module. It owns lifespan, CORS, IPTV subscriptions, Market, EPG, M3U8 rewrite, TS/stream proxy, RTSP/FFmpeg session state, and several in-memory caches. Radio proxy, media proxy, auth, setup, settings, media-credential and plugin routes have since been split out into `backend/routers/*`.
- `backend/database.py`: SQLite business database for settings, subscriptions, channels, EPG, Market install state, and Market sources. Security tables were added here so the first auth layer persists beside current app data.
- `backend/ssrf_guard.py`: existing SSRF checks. It is now the migration target for unified policy-driven URL validation.
- `backend/market.py`: already has its own safe fetch implementation and private-address checks; should be migrated into `infrastructure/http_client.py`.
- `backend/epg.py`: downloads XMLTV through caller-provided `httpx.AsyncClient`; should move to unified fetch policies.
- `backend/iptv_probe.py`: probe logic uses shared `httpx.AsyncClient` plus FFmpeg subprocesses; resource limits and unified request policies are needed.
- `backend/fetchers.py`: radio fetchers create their own `httpx` clients and read environment variables directly.
- `backend/adapters/*`: adapters share an `httpx.AsyncClient` contract, but final resolved media URLs still need policy validation before playback/proxying.
- `frontend/src/api/*.js`: multiple small fetch wrappers. They are being migrated to send credentials and the `X-WaveFlow-Request` CSRF header.
- `frontend/src/router/index.js`: setup/login routes, admin route metadata, and auth guard are in place. When anonymous browse is disabled, non-auth pages redirect to login.
- `electron/main/index.js` and `electron/preload/index.js`: desktop uses fixed port `18765`, loads frontend via `file://`, and does not yet perform desktop auth.
- `docker-compose.yml`: backend is currently published directly on host port `8000`; target architecture should expose backend only to the frontend container.

## Route Classification

- Public/setup/auth: `/health`, `/api/config`, `/api/setup/status`, `/api/setup/initialize`, `/api/auth/login`, `/api/auth/logout`, `/api/auth/me`, `/api/auth/desktop`.
- Browse: `/api/radio/stations`, `/api/radio/stations/{station_id}`, `/api/radio/stations/{station_id}/programme`, `/api/radio/stations/{station_id}/resolve`, `/api/radio/providers/{plugin_identity:path}/catalog/query`, `/api/radio/plugins/{plugin_identity:path}/refresh`, `/api/iptv/channels`, `/api/iptv/epg/programs/{canonical_key}`, `/api/iptv/epg/batch-current`.
- Admin: `/api/admin/subscriptions*`, `/api/admin/market*`, `/api/admin/probes/*`, `/api/admin/epg/*`, `/api/admin/media-credentials*`, `/api/admin/settings/security`, `/api/admin/plugins/*`.
- Media (current): `/api/media/radio/{station_id}/playlist.m3u8`, `/api/media/radio/{station_id}/stream`, `/api/media/channel/{channel_key}/playlist.m3u8`, `/api/media/channel/{channel_key}/stream`, `/api/media/channel/{channel_key}/resolve`, `/api/media/channel/{canonical_key}/cover`, `/api/media/proxy/playlist/{handle}`, `/api/media/proxy/chunk/{handle}`, `/api/media/proxy/stream/{handle}`, `/api/media/proxy/rtsp/{handle}`, `/api/media/proxy/rtsp-segments/{session_id}/{filename}`, `/api/iptv/subscription.m3u`, `/api/iptv/smart/{canonical_key}.m3u8`.
- Media (retired, no longer registered): `/api/{station_id}/playlist.m3u8`, `/api/{station_id}/{m3u8_name}.m3u8`, `/api/{station_id}/chunk.ts`, `/api/{station_id}/stream`, `/api/iptv/adapter/play.m3u8`, `/api/iptv/proxy/*`. Also retired: `/api/stations`, `/api/myradio/all`, `/api/yunting/*`, `/api/radio-browser/*`.

## High-Risk Entry Points

- Public raw-`target_url` proxy routes were the main SSRF/resource risk: `/api/iptv/proxy/chunk.ts`, `/api/iptv/proxy/stream`, `/api/iptv/proxy/playlist.m3u8`, `/api/iptv/proxy/rtsp.m3u8`, and radio `/api/{station_id}/playlist.m3u8?target_url=...`. **All of these are retired** — a raw upstream URL no longer appears in any public query string. The replacement is `/api/media/channel/{key}/*` plus `/api/media/proxy/*/{handle}` signed handles, so the remaining SSRF surface is handle decode plus `assert_safe_target_url` / `assert_safe_host_ips` at resolve time.
- Admin mutations require an admin session and the `X-WaveFlow-Request` CSRF header. Media routes depend on `anonymous_browse` / `anonymous_playback` through `require_browse_access` / `require_media_access`.
- CORS is no longer wide open: `allow_origins` is driven by `get_settings().allowed_origins` instead of `["*"]`.
- In-memory caches: `M3U8_CACHE` / `M3U8_CACHE_LOCKS` have been retired. `security/proxy_handles.py` (max 4096 entries) and `security/proxy_context.py` now use bounded LRU+TTL, and `ssrf_guard.py` bounds its DNS cache at 256 entries with a 5s TTL. Still partly bounded: the RTSP session dictionaries `RTSP_HLS_SESSIONS` / `_RTSP_RESERVED_SESSIONS` and `_THIN_CACHE` in `main.py`.
- Docker currently allows bypassing frontend nginx and reaching the backend directly.

## Implementation Order

1. Establish config and auth foundation: centralized mode defaults, security tables, Argon2id passwords, HttpOnly sessions, setup/login/logout/me, media credentials.
2. Keep configuration layered: fixed code rules, explicit environment overrides, admin-editable SQLite runtime settings, then mode defaults. Environment variables must preserve the difference between unset and explicitly false.
3. Add frontend boot/auth store and API client with credentials plus `X-WaveFlow-Request`.
4. Move admin routes under `/api/admin/*` or protect legacy admin routes while migrating the frontend.
5. Replace public `target_url` media URLs with signed handles, then remove legacy public proxy routes. **Done** — see "Current Progress".
6. Migrate all external HTTP calls to `infrastructure/http_client.py` policies.
7. Extract RTSP session manager and bounded caches.
8. Tighten Docker/nginx/Electron defaults.

## Current Progress

- Done: centralized `WAVEFLOW_*` configuration layering, SQLite runtime settings, auth/session/media credential tables, Argon2id password hashing, setup/login/logout/me APIs, and Admin security settings API.
- Done: frontend auth store, setup/login views, route guard, admin API client migration, and Admin page controls for editable runtime security settings.
- Done: management routes moved to `/api/admin/*`; old `/api/iptv/subscriptions*`, `/api/market*`, old probe, and old EPG management decorators were removed.
- Done: browse and playback routes now depend on `anonymous_browse` / `anonymous_playback`; tests cover anonymous-disabled behavior.
- Done: public raw `target_url` playback/proxy URLs were replaced by signed handles (`/api/media/channel/{key}/*`, `/api/media/proxy/*/{handle}`), and the legacy raw-URL public routes are no longer registered. `_validate_rtsp_proxy_request` still checks the RTSP enable flag and SSRF policy before any session starts.
- Still open: migrate outbound HTTP calls to `infrastructure/http_client.py`, add bounded cache/session managers, and finish Docker/Electron hardening.
