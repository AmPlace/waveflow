# WaveFlow Module Inventory and Release Review Matrix

Status: CURRENT REVIEW BASELINE

Verified code baseline: `8e49eca1e206b0ad148a4e1b9eba74d7a5fa7a42`

Branch: `iptv`

The current working tree also contains the uncommitted
`PLUGIN-ARTIFACT-INTEGRITY-1` loader hardening and the
`DATABASE-UPGRADE-RECOVERY-1` initialization hardening with their regression
tests; the matrix below records that evidence separately from the committed
baseline.

This file is the release-hardening progress map. It does not replace the
architecture documents, provider contracts, or phase worklogs. The current
code, Git history, and reproducible tests remain authoritative.

## Scope and counts

- Top-level production domains: **8**.
- Production subsystems inventoried: **44**.
- `VERIFIED`: **9**.
- `CURRENT`: **4**.
- `PARTIAL`: **23**.
- `NOT FULLY REVIEWED`: **4**.
- `LEGACY`: **4**.
- Confirmed open P0 issues: **0**.
- Qualified release blockers: **3** (official signing, Radio distribution,
  and macOS Desktop distribution).
- Open P1/P2/P3 issues: **16 / 17 / 2**.

The status is a review status, not a claim that every path in a module is
finished. `VERIFIED` means that the named boundary has focused implementation
and regression evidence; it does not imply universal device, origin, or
deployment coverage.

## Inventory

| # | Domain | Subsystem | Backend owner and production entry | Frontend owner | Persistence / dependencies | Architecture / ADR | Tests | Status | Open issues | Risk / next review |
|---:|---|---|---|---|---|---|---|---|---|---|
| 1 | Application | Bootstrap and lifespan | `backend/main.py:1131` `lifespan`; router registration at `backend/main.py:1250` | `frontend/src/main.js`, `App.vue` | SQLite init, Plugin subsystem, Automation, shared HTTP client | `architecture/overview.md` | `test_plugin_lifespan.py`, `test_automation_lifespan.py` | PARTIAL | `REV-AUTO-003`, `REV-AUTO-004` | P1; review startup failure, shutdown, and compatibility jobs first |
| 2 | Application | Automation service, Runner, Scheduler | `backend/automation.py`, `epg_tasks.py`, `market_tasks.py`, `radio_tasks.py`, `plugin_tasks.py` | none | `automation_task_config`, `automation_task_state`; single-process asyncio ownership | `architecture/epg.md`, `plugin-contract-v1.md` | `test_automation_persistence.py`, `test_automation_runner.py`, `test_automation_scheduler.py`, `test_automation_lifespan.py` | VERIFIED | `REV-AUTO-001`, `REV-AUTO-002` | P2; keep one scheduler boundary and document deployment constraint |
| 3 | Application | Bounded app/background job ownership | `backend/main.py:_track_app_background_task`, RTSP cleaner, logo refresh, speed-test handles | none | In-memory task handles plus RTSP session state | `architecture/overview.md` | `test_automation_lifespan.py`, `test_rtsp_startup.py` | PARTIAL | `REV-AUTO-003` | P2; verify every app-owned task has stop, await, and exception observation |
| 4 | Identity | Setup and first-admin initialization | `backend/routers/setup.py`, `backend/security/database.py` | `SetupView.vue`, `stores/auth.js` | `settings`, security user/session tables | `architecture/auth-and-sessions.md` | `test_access_control.py`, `test_auth_settings_foundation.py` | VERIFIED | none | Fresh/repeat/concurrent setup, post-create session failure recovery, restart, request marker, and real-process initialization accepted in `AUTH-SETTINGS-FOUNDATION-1` |
| 5 | Identity | Authentication, sessions, and password boundary | `backend/routers/auth.py`, `backend/security/sessions.py`, `passwords.py` | `api/auth.js`, `stores/auth.js`, `LoginView.vue` | Users, sessions, cookies, password hashes | `architecture/auth-and-sessions.md` | `test_access_control.py`, `test_auth_settings_foundation.py`, `authSessionExpiry.test.js` | PARTIAL | `REV-AUTH-002`, `REV-AUTH-003` | Browser session lifetime, expiry/logout/restart, anonymous policy, and stale frontend auth are accepted; Electron Desktop bootstrap and public abuse controls remain open |
| 6 | Identity | Admin authorization and route boundary | `backend/security/dependencies.py`, admin routes in `main.py` and routers | router guards in `frontend/src/router`, settings views | Session role and runtime mode | `architecture/auth-and-sessions.md` | `test_access_control.py`, `test_auth_settings_foundation.py` | VERIFIED | none | Every current `/api/admin/*` route has the backend admin dependency; 401/403/422/500 ordering and generic error behavior are covered |
| 7 | Configuration | Runtime settings and effective config | `backend/core/config.py`, `core/settings_service.py`, `routers/settings.py` | `SecuritySettings.vue`, settings API | `settings`, `app_settings`, environment | `architecture/settings-and-config.md` | `test_database_options.py`, `test_security_settings.py`, `test_auth_settings_foundation.py` | VERIFIED | none | `ENV > DB > mode default`, strict validation, atomic writes, read-failure behavior, hot metadata, and restart persistence accepted in `AUTH-SETTINGS-FOUNDATION-1` |
| 8 | Data | SQLite schema, initialization, and migrations | `backend/database.py:_SCHEMA`, `initialize()` | none | One SQLite database; transactional/idempotent forward schema updates | `architecture/database-and-migrations.md`, ADR-0007 | `test_database_upgrade_recovery.py`, `test_database_options.py`, domain database tests | VERIFIED | none | Historical upgrade, failure recovery, current-DB copy, and downgrade/restore boundary accepted in `DATABASE-UPGRADE-RECOVERY-1` |
| 9 | Content | Subscription ingest and refresh | `backend/main.py` subscription routes, `database.py:replace_subscription_channels_atomic` | `api/iptv.js`, `LiveSourcesSettings.vue` | `subscriptions`, `channels`, source URLs and headers, durable refresh state | `architecture/source-model.md` | `test_subscription_atomicity.py`, `test_subscription_refresh_empty.py`, `test_subscription_lifecycle.py`, `test_subscription_http_boundary.py` | VERIFIED | none | Safe fetch, bounded refresh-all, generation guards, last-known-good retention, sanitized failures, and restart recovery accepted in `SUBSCRIPTION-REFRESH-IDENTITY-1`; keep subscription automation out of this boundary |
| 10 | Content | M3U parsing and source normalization | `backend/m3u8_parser.py` | `utils/sourceIdentity.js` | Channel metadata, EPG hints, source type, adapter references | `architecture/source-model.md` | `test_m3u8_parser.py`, `test_normalize.py`, `test_source_id_refresh.py` | VERIFIED | none | Phase 2; preserve identity and header semantics during review |
| 11 | Content | IPTV aggregation and logical channel identity | `backend/database.py:get_aggregated_channels`, `iptv_channels.py`, `iptv_logical_gc.py` | `IptvHome.vue`, `utils/iptvViewing.js` | `iptv_logical_channels`, members, `channels` | `architecture/source-model.md` | `test_iptv_logical_channels.py`, `test_iptv_logical_gc.py`, `test_channel_data_correctness_goal2.py` | VERIFIED | none | Phase 2; verify source churn, orphan GC, and multi-subscription projection |
| 12 | Content | Probe, health, speed, and reachability | `backend/iptv_probe.py`, probe routes in `main.py` | `api/iptv.js`, `LiveSourcesSettings.vue` | Channel probe fields, live status, ffprobe metadata | `architecture/source-model.md` | `test_iptv_probe.py`, `test_probe_security.py`, `test_probe_media_tools.py`, `test_probe_persistence.py`, `test_probe_speed_revision.py`, `test_iptv_proxy_stream_response.py` | PARTIAL | `REV-PROBE-001` | HTTP/media authority split, stale-result guard, bounded subprocess cleanup, and sanitized diagnostics accepted in the current working tree; deployment/source-type real-origin matrix remains open |
| 13 | Content | EPG source refresh and atomic datasets | `backend/epg.py`, `epg_source_management.py`, `epg_tasks.py` | `EpgSourcesSettings.vue` | `epg_sources`, `epg_channels`, `epg_programs` | `architecture/epg.md` | `test_epg_parser.py`, `test_epg_refresh.py`, `test_epg_database.py`, `test_epg_automation.py` | CURRENT | `REV-EPG-001` | P2; review maintenance convergence and source failure observability |
| 14 | Content | EPG matching, bindings, and shadow runs | `epg_matcher.py`, `epg_bindings.py`, `epg_match_shadow.py`, management modules | `EpgMatchingSettings.vue`, matching API modules | logical-channel EPG binding/shadow tables | `architecture/epg.md` | `test_epg_matcher.py`, `test_epg_match_shadow.py`, `test_epg_match_shadow_apply.py`, `test_epg_binding_reconciliation.py` | CURRENT | `REV-EPG-002` | P2; complete legacy fallback cutover and rollback evidence |
| 15 | Content | EPG read/API/frontend projection | `epg_read_resolver.py`, EPG routes in `main.py` | `useEpg.js`, `epgViewing.js`, `IptvHome.vue`, `FullPlayer.vue` | Read-only projection over current and legacy bindings | `architecture/epg.md` | `test_epg_read_resolver.py`, `test_epg_management_api.py`, `useEpgLatestWins.test.js` | CURRENT | `REV-EPG-002` | P2; preserve latest-wins and compatibility route behavior |
| 16 | Content | Replay/catch-up | Provider-specific `backend/adapters/hnntv.py` and `bundled_plugins/hnntv` only | no generic replay UI/API | Provider schedule and `playseek` request semantics | `architecture/legacy-and-deprecations.md` | `test_hnntv_plugin.py` | LEGACY | `REV-REPLAY-001` | P2; do not imply a generic product capability |
| 17 | Market | Market source/package/install lifecycle | `backend/market.py`, Market routes in `main.py` | `MarketView.vue`, `api/market.js` | Market sources, package/install/subscription/assets | `docs/market-schema.md` (historical schema), `architecture/overview.md` | `test_market_lifecycle.py`, `test_market_display.py`, `test_market_plugin_lifecycle.py` | PARTIAL | `REV-MARKET-001` | P1; review package update, uninstall, cache policy, and rollback as one flow |
| 18 | Market | Market automation | `backend/market_tasks.py`, `AutomationRunner`, Market routes | no task settings UI | Automation state plus Market install status | `docs/automation-architecture.md`, `architecture/overview.md` | `test_market_tasks.py`, `test_market_automation_api.py`, `test_automation_lifespan.py` | VERIFIED | none | Phase 1 regression anchor; keep conflict group and run-token semantics |
| 19 | Market | Logo and visual asset packages | `backend/logo_resolver.py`, `logo_template.py`, `database.py:package_assets` | `useLogoVisual.js`, `channelVisual.js` | `package_assets`, logo bindings, filesystem asset store | `architecture/source-model.md` | `test_logo_package.py`, `test_logo_template.py`, `test_cover_cache.py` | PARTIAL | `REV-LOGO-001` | P2; verify missing assets, cache invalidation, and package uninstall |
| 20 | Plugin | Provider contract and SDK | `backend/waveflow_plugin_sdk`, `plugin_runtime/manifest.py`, `models.py` | Plugin settings projection only | Manifests, contracts, signed descriptor models | `architecture/plugin-contract-v1.md` | `test_plugin_runtime_contract.py`, provider plugin tests | PARTIAL | `REV-PLUGIN-002` | P1; complete ABI/version and contract compatibility matrix |
| 21 | Plugin | Runtime process, IPC, and capabilities | `backend/plugin_runtime/process.py`, `runtime.py`, `protocol.py`, `plugin_capabilities.py` | none | Runtime instances, health, IPC frames, managed/direct HTTP | `architecture/plugin-contract-v1.md`, `architecture/security-boundaries.md` | `test_plugin_runtime_process.py`, `test_plugin_capability_bridge.py`, `test_managed_http_permission.py` | PARTIAL | `REV-PLUGIN-003` | P1; release-test timeouts, cancellation, quota, and capability denial |
| 22 | Plugin | Install, permissions, ownership, and recovery | `plugin_market.py`, `plugin_production.py`, `plugin_permissions.py`, plugin routes | `PluginsSettings.vue`, `api/plugins.js` | plugin installations, artifacts, environments, approvals, ownership | `architecture/plugin-contract-v1.md` | `test_plugin_management_api.py`, `test_plugin_admin_api.py`, `test_plugin_lifespan.py`, `test_production_rollout.py` | PARTIAL | `REV-PLUGIN-003` | Artifact integrity/recovery accepted at `fc2d2bc`; signed packaged lifecycle passes on macOS arm64 / CPython 3.14, while target-runtime and real-provider upstream acceptance remain external |
| 23 | Release | Official plugin distribution and release catalog | `official_plugin_distribution.py`, `build_official_plugins.py`, `backend/official_plugins/*` | Plugin availability projection | Signed Market catalog, trust anchor, platform artifacts | `architecture/plugin-contract-v1.md` | `test_official_plugin_distribution.py`, `test_official_dependency_artifact.py` | PARTIAL | `REV-REL-001`, `REV-RADIO-001` | Official builder, test-signer catalog-to-installer flow, and signed Radio fixture pass; production signer/publish and non-host target runtime acceptance remain external |
| 24 | Provider | Legacy adapter registry and resolver fallback | `backend/adapters/*`, `backend/provider_resolver.py`, `m3u8_parser.py` | source labels and compatibility UI | Channel URL/provider identity; no separate durable Plugin contract | `architecture/legacy-and-deprecations.md` | adapter-specific tests, `test_plugin_v1` coverage | LEGACY | `REV-LEGACY-002` | P2; preserve rollback until ownership and release gates close |
| 25 | Provider | Bundled TV provider Plugins | `backend/bundled_plugins/*tv`, `plugin_production.py`, `provider_resolver.py` | `FullPlayer.vue`, IPTV views | Plugin installs, channels, source revisions, provider references | `architecture/plugin-contract-v1.md` | provider plugin tests, `test_plugin_channel_catalog.py` | PARTIAL | `REV-TV-001` | P1; finish real artifact, ownership, and representative live-source acceptance |
| 26 | Provider | Radio Plugin catalog, Core bridge, and task projection | `backend/radio_core.py`, `radio_tasks.py`, `plugin_production.py`, `routers/radio.py` | `api/radioStations.js`, `Home.vue`, `stores/player.js` | Radio catalog generations, station/source rows, Automation state | `architecture/radio.md` | `test_radio_core_bridge.py`, `test_radio_tasks.py`, `test_yunting_catalog_resilience.py`, `test_radio_catalog_closeout.py` | CURRENT | `REV-RADIO-001` | Phase 2; catalog chain passed, official distribution remains blocked |
| 27 | Provider | Radio playback and browser transport | `routers/media_proxy.py`, `radio_core.py`, `main.py` audio MIME path | `radioAudioEngine.js`, `AudioEngine.vue`, `BottomPlayer.vue` | Signed radio handles and persisted Radio source descriptors | `architecture/radio.md`, `architecture/raw-streams.md` | `test_radio_playback_transport.py`, `radioAudioEngineAttempt.test.js`, `audioEngineComponent.test.js`, `radioStore.test.js` | NOT FULLY REVIEWED | `REV-RADIO-002` | P1; focused intent/attempt continuity and play-error classification evidence is in `197cc679`; dynamic Yunting HLS and MyRadio audio HTTP browser playback, MIME, explicit pause/Play, and rapid switching passed, while browser-triggered failure classification and the remaining provider/failure matrix stay open |
| 28 | Provider | RadioBrowser compatibility route | `backend/main.py:proxy_radio_browser`, `fetchers.py` | `api/radioBrowser.js`, `Home.vue` | Request cache and legacy station projection | `architecture/legacy-and-deprecations.md` | `test_radio_tasks.py`, frontend radio tests | LEGACY | `REV-RADIO-003` | P2; decide on-demand ownership and retirement separately |
| 29 | Media | Channel resolve and media proxy orchestration | `backend/routers/media_proxy.py`, `main.py`, `provider_resolver.py` | `FullPlayer.vue`, `stores/player.js` | Source IDs/revisions, signed handles, ProxyContext | `architecture/playback-overview.md`, `security-boundaries.md` | `test_media_proxy_logic.py`, `test_media_proxy_chunk.py` | PARTIAL | `REV-MEDIA-001` | P1; review cross-format fallback and late response ownership |
| 30 | Media | Signed handles and media credentials | `backend/security/proxy_handles.py`, `proxy_context.py`, `media_credentials.py` | media credential settings and player callers | HMAC handles, bounded context registry, credential DB | `architecture/security-boundaries.md` | `test_proxy_handles.py`, `test_proxy_context_lifetime.py`, `test_access_control.py` | VERIFIED | none | Phase 1 security anchor; verify redaction and expiry at every route |
| 31 | Security | SSRF, safe redirects, DNS, and DoH | `backend/ssrf_guard.py`, `infrastructure/http_client.py` | none | DNS cache, target policy, redirect policy, managed HTTP | `architecture/security-boundaries.md`, ADR-0005 | `test_media_redirects.py`, `test_probe_security.py`, `test_ssrf_fake_ip.py` | VERIFIED | none | Phase 1 security anchor; preserve per-hop validation |
| 32 | Media | Thin HLS fetch, rewrite, cache, and segments | `backend/main.py:_thin_playlist_fetch`, `core/m3u8_rewriter.py` | `FullPlayer.vue`, hls.js path | bounded in-memory cache/locks and signed child handles | `architecture/hls.md`, ADR-0001 | `test_thin_hls_cache.py`, `test_m3u8_rewriter_handles.py`, HLS integration tests | VERIFIED | `REV-HLS-001`, `REV-HLS-002`, `REV-HLS-003`, `REV-HLS-004` | P1/P2; keep Thin-only and close security/compatibility follow-ups |
| 33 | Media | RTSP-to-HLS sessions and lifecycle | `backend/main.py`, `rtsp_playback.py` | `FullPlayer.vue` RTSP path | In-memory sessions, FFmpeg, temporary HLS files, source policy | `architecture/rtsp.md`, ADR-0002/0003/0004/0006 | `test_rtsp_policy.py`, `test_rtsp_startup.py`, `test_rtsp_watchdog.py`, `test_rtsp_source_playback.py` | VERIFIED | `REV-RTSP-001`, `REV-RTSP-002` | P1/P2; complete Apple/HEVC evidence without changing source policy globally |
| 34 | Media | HTTP-FLV, MPEG-TS, and raw relay | `backend/routers/media_proxy.py`, `main.py:serve_iptv_proxy_stream_response` | `FullPlayer.vue`, mpegts.js | Signed stream handle and stream type/header policy | `architecture/raw-streams.md` | `test_iptv_proxy_stream_response.py`, media proxy tests | NOT FULLY REVIEWED | `REV-RAW-001` | P1; real-origin codec, reconnect, and browser matrix required |
| 35 | Provider / Media | YouTube provider and iframe/player path | `backend/bundled_plugins/youtube`, `provider_resolver.py` | `FullPlayer.vue` YouTube path | Typed reference/provider resolve; no generic proxy ownership | `architecture/youtube.md` | `test_youtube_plugin.py`, `fullPlayerYoutubeOwnership.test.js` | NOT FULLY REVIEWED | `REV-YT-001` | P1; real reachability, autoplay, regional, and packaged artifact review |
| 36 | Frontend | Player selection, switch, recovery, and FullPlayer | `main.py` media routes and source resolver | `FullPlayer.vue`, `BottomPlayer.vue`, `stores/player.js`, player utilities | Selection token, source queue, volatile handles, browser engines | `architecture/player-lifecycle.md`, playback overview | FullPlayer recovery/ownership/sizing tests | PARTIAL | `REV-PLAYER-001` | P1; complete cross-format cancellation and explicit-play semantics |
| 37 | Frontend | IPTV home, channel list, and source identity UI | backend IPTV API and visual routes | `IptvHome.vue`, `api/iptv.js`, `iptvViewing.js`, `sourceIdentity.js` | Logical projection, EPG batch current, visual cache | `architecture/source-model.md`, player lifecycle | IPTV home/latest-wins/source identity tests | PARTIAL | `REV-UI-001` | P2; test empty/stale/slow responses and source fallback presentation |
| 38 | Frontend | Radio home and Radio state | `routers/radio.py` and radio media routes | `Home.vue`, `radioStations.js`, `radioAudioEngine.js`, Radio store tests | Catalog/API projection and programme snapshots | `architecture/radio.md` | `radioStations.test.js`, `radioStore.test.js`, radio audio tests | NOT FULLY REVIEWED | `REV-RADIO-004` | P1; validate provider filtering, stale states, logo fallback, and playback errors |
| 39 | Frontend | Settings, Market, EPG, and Plugin administration UI | admin routers and management APIs | `views/settings/*`, `MarketView.vue`, API modules | Admin session, task/config projections, package state | `architecture/overview.md`, `epg.md`, `plugin-contract-v1.md` | settings, EPG, Plugin, Market unit/E2E tests | PARTIAL | `REV-UI-002` | P2; complete backend contract, permission, and restart-state review |
| 40 | Platform | Electron/Desktop shell | `electron/main/index.js`, `desktop_entry.py`, desktop runtime modules | desktop Vite mode and packaged frontend | Local backend process, loopback auth, bundled Python/FFmpeg | `architecture/overview.md` | `test_desktop_plugin_runtime.py`, Desktop worklogs | PARTIAL | `REV-DESKTOP-001`, `REV-DESKTOP-002` | P1 RELEASE BLOCKER for distributable macOS; signing/notarization gate |
| 41 | Platform | Docker, NAS, and public deployment | `docker-compose.yml`, `docker-compose.dev.yml`, backend/frontend Dockerfiles | nginx/Vite public deployment | Named data volume, backend/frontend network, runtime env | `architecture/overview.md` | deployment smoke is incomplete | PARTIAL | `REV-DEPLOY-001`, `REV-DEPLOY-002` | P1; pin release images and verify fresh/upgrade/rollback deployment |
| 42 | Platform | PWA and static asset packaging | `frontend/vite.config.js`, `frontend/public/*`, `nginx.conf` | generated service worker/manifest | Workbox caches API config/stations and logos | none dedicated | frontend build only; no full PWA acceptance | PARTIAL | `REV-PWA-001` | P2; review cache invalidation, offline behavior, and API freshness |
| 43 | Release | Version, build, artifact, and signing scripts | root `package.json`, `scripts/*`, official Plugin builders | Electron/Vite production build | Release artifacts, runtime provenance, signatures | `architecture/plugin-contract-v1.md` | build/distribution and Desktop worklogs | PARTIAL | `REV-REL-001`, `REV-DESKTOP-001` | P1; run a clean release-host rehearsal |
| 44 | Compatibility | M3U export and Smart RTSP compatibility | `main.py:subscription.m3u`, `main.py:iptv_smart_playlist` | settings/source consumers; no dedicated export view | Subscription channels, legacy URL/query semantics | `architecture/legacy-and-deprecations.md`, ADR-0006 | subscription/export and RTSP tests | LEGACY | `REV-LEGACY-001` | P1 compatibility risk; inventory consumers before any retirement |

## Open issue register

Only issues with current code, current architecture evidence, or an explicit
unclosed worklog status are listed here. Older audit findings that have a
later fix are listed separately below and are not counted as open.

| ID | Subsystem | Severity | Release classification | Evidence / observed issue | Required next action / dependency |
|---|---|---|---|---|---|
| REV-REL-001 | Official release | P1 | RELEASE BLOCKER | The official builder, verifier, platform selection, test-signer catalog, and production installer lifecycle pass end to end. The matching production Ed25519 private signing credential is intentionally unavailable in this workspace, so an official release cannot be signed here. | Obtain the approved external signer and repeat build/sign/catalog/install verification on the release host without changing the trust model. |
| REV-RADIO-001 | Official Radio distribution | P1 | RELEASE BLOCKER for Radio release | An official-style Radio package signed by the test signer passes install, ownership, runtime, restart, API projection, repair, and uninstall. Radio packages are not yet present in the production-signed release catalog. | Add approved Radio release inputs, sign and publish with the external production signer, then repeat the same lifecycle acceptance against the official catalog. |
| REV-DESKTOP-001 | Desktop distribution | P1 | RELEASE BLOCKER for macOS distribution | macOS qualification found ad-hoc signing only; Developer ID, Hardened Runtime, notarization, staple, and Gatekeeper acceptance remain unavailable. | Run on a release host with approved identity and notarization credentials. |
| REV-AUTH-002 | Desktop authentication | P1 | SHOULD FIX BEFORE DESKTOP RELEASE | Browser setup/login/session/logout and backend authorization pass, and `/api/auth/desktop` correctly requires Desktop mode, loopback, and a bootstrap secret. The Electron launcher does not set Desktop mode or generate/pass that secret, and the renderer never calls the endpoint. | Add a main/preload-owned one-shot bootstrap without exposing the secret to ordinary web content, then run packaged restart/logout acceptance. |
| REV-AUTH-003 | Authentication abuse controls | P2 | CAN DEFER for NAS/Desktop; SHOULD FIX BEFORE PUBLIC EXPOSURE | Login and pre-initialization setup use Argon2, generic credential errors, CORS allowlisting, and a request marker, but have no application-level attempt limiter. | Establish bounded reverse-proxy or application rate limiting for public deployment without introducing a second session authority. |
| REV-DEPLOY-001 | Docker/NAS deployment | P1 | SHOULD FIX BEFORE RELEASE | `docker-compose.yml` consumes `ghcr.io/amplace/waveflow-*:latest`, so deployment is not reproducible by an immutable version or digest. | Pin release tags/digests and document upgrade/rollback inputs. |
| REV-HLS-001 | Thin HLS | P1 | SHOULD FIX BEFORE RELEASE | Architecture open item: adapter child-playlist token re-resolution remains a compatibility/security boundary. | Add a focused child-playlist re-resolution contract test before changing the rewriter. |
| REV-HLS-002 | HTTP client | P1 | SHOULD FIX BEFORE RELEASE | Architecture open item: global `httpx` cookie isolation policy is not fully closed for shared clients and provider/media boundaries. | Audit cookie ownership and cross-origin propagation with a deterministic test. |
| REV-RTSP-001 | RTSP/client compatibility | P1 | SHOULD FIX BEFORE RELEASE when Apple/HEVC is supported | Desktop and focused RTSP evidence is strong, but real Safari/iOS and HEVC device A/B remain unverified. | Run the real-device matrix; do not infer a global transcode policy. |
| REV-RAW-001 | HTTP-FLV/MPEG-TS | P1 | SHOULD FIX BEFORE RELEASE when advertised | Code and focused relay tests exist, but real-origin/browser codec, reconnect, and MIME behavior are not fully reviewed. | Run representative origin and browser acceptance. |
| REV-RADIO-002 | Radio playback | P1 | SHOULD FIX BEFORE RELEASE for Radio release | Commit `197cc679` adds focused evidence for intent propagation, async resolve attempt continuity, explicit retry, fallback play reset, and `play()` error classification. Static and dynamic browser acceptance passed for Yunting HLS and MyRadio audio HTTP, including pause/explicit Play, advancing playback, correct Core MIME, and A -> B -> C rapid switching with no stale attempt error or autoplay banner. A safe browser reproduction of `NotAllowedError`, `AbortError`, or `NotSupportedError` was not run, so browser-level error classification remains partial. One transient Yunting first-playlist 403 recovered on retry and is an observation/follow-up candidate only. | Retain `NOT FULLY REVIEWED`; run targeted browser rejection cases and the remaining provider/failure-recovery matrix before closing this issue. |
| REV-YT-001 | YouTube | P1 | SHOULD FIX BEFORE RELEASE when enabled | Provider and iframe paths exist, but real reachability, autoplay, regional behavior, and packaged artifact acceptance are not claimed. | Run live/provider/package acceptance after signing inputs exist. |
| REV-AUTO-001 | Automation operations | P2 | CAN DEFER | Durable state has last-run counts/error, but generic persisted retry count, jitter, and scheduler infrastructure diagnostics are incomplete. | Add only if release operations require cross-restart retry diagnosis. |
| REV-AUTO-002 | Deployment model | P2 | CAN DEFER | Automation is a single-process asyncio scheduler; multi-worker/multi-replica active scheduling is not supported. | Keep deployment single-process or design a lease/leader boundary before scaling. |
| REV-AUTO-003 | Compatibility jobs | P2 | CAN DEFER | Manual EPG compatibility refresh remains an immediate-return/background-job compatibility surface rather than a synchronous result contract. | Decide whether to deprecate it or expose a durable job result; do not duplicate schedulers. |
| REV-AUTO-004 | Market | P3 | CAN DEFER | `market.py:run_installed_updates()` remains as a possible duplicate orchestration path while production automation uses `market_tasks.py`. | Caller audit and scoped cleanup; no deletion in this review. |
| REV-EPG-001 | EPG maintenance | P2 | CAN DEFER | A previous core correctness audit left post-commit EPG binding maintenance without a durable retry item; later source/task races were fixed, but this convergence risk remains open. | Add a bounded maintenance retry/outbox only in a separate EPG phase. |
| REV-EPG-002 | EPG compatibility | P2 | CAN DEFER | `channel_epg_map` remains a legacy compatibility fallback beside source-aware logical bindings. | Complete read cutover and rollback evidence before removing the table/path. |
| REV-HLS-003 | Thin HLS | P2 | CAN DEFER | Strict signed-URL implications of `wf_seq` injection remain an architecture open item. | Review origin signature semantics with a real signed-origin fixture. |
| REV-HLS-004 | Settings debt | P3 | CAN DEFER | Obsolete `m3u8_cache_*` terminology/settings remain an architecture cleanup item. | Remove only after a settings/data migration inventory. |
| REV-RTSP-002 | RTSP optimization | P2 | CAN DEFER | Warm-session/LRU is optional; current shared session/single-flight/quota/watchdog path is the authority. | Benchmark before adding any cache/lifecycle optimization. |
| REV-PROBE-001 | Probe | P2 | CAN DEFER | HTTP reachability and media inspection are now independent; manual Probe writes are revision-guarded and media-tool diagnostics are sanitized. Local macOS fixtures cover stale ownership, timeout/cancellation cleanup, HTTP-only quality, and subprocess failures. | Run the remaining Docker/Desktop target matrix and representative real-origin source matrix; do not infer browser playback from Probe success. |
| REV-RADIO-003 | TingFM/static Radio | P2 | CAN DEFER | Legacy/static Radio paths retain possible user-visible duties; retirement readiness was explicitly not established. | Perform ownership/consumer audit before removal. |
| REV-RADIO-004 | RadioBrowser | P2 | CAN DEFER | RadioBrowser remains a legacy/on-demand route outside the Plugin catalog; no retirement decision is recorded. | Confirm consumers and choose on-demand retention or future provider ownership. |
| REV-PLUGIN-002 | Legacy provider coverage | P2 | CAN DEFER | The final Plugin audit recorded 13 legacy schemes without current Plugin source implementations. | Preserve adapters and define migration/retirement per scheme. |
| REV-PLUGIN-003 | Plugin lifecycle | P1 | SHOULD FIX BEFORE RELEASE | The 12 read-only DB rows persisted as `unavailable/PLUGIN_CRASHED` all recovered to `active` in an isolated real-process startup with matching artifacts; no dependency, ABI, permission, platform, or artifact defect reproduced. Signed packaged install, restart, permission, disable/enable, repair, crash recovery, and uninstall pass on macOS arm64 / CPython 3.14. | Run the packaged lifecycle on every remaining supported target and execute representative real-provider upstream smoke; keep artifact integrity and runtime health as separate gates. |
| REV-TV-001 | TV providers | P1 | SHOULD FIX BEFORE RELEASE for advertised providers | Some providers remain legacy or have source-specific unsupported fallbacks; generic contract acceptance does not prove every provider's live path. | Complete provider-by-provider release acceptance and keep legacy fallback until done. |
| REV-MEDIA-001 | Cross-format player | P1 | SHOULD FIX BEFORE RELEASE | Core media security is verified, but cross-format selection/fallback/cancellation behavior is not a single complete real matrix. | Review HLS, RTSP, raw, Radio, YouTube, and late-response ownership together. |
| REV-PLAYER-001 | Frontend player lifecycle | P1 | SHOULD FIX BEFORE RELEASE | Radio-local explicit-play intent and media-error classification now have focused evidence in `197cc679`; IPTV/FullPlayer, cross-transport source-switch cancellation, and one browser acceptance pass across engines remain open. | Run desktop and iOS-compatible browser cases without bypassing autoplay policy. |
| REV-UI-001 | IPTV frontend | P2 | CAN DEFER | Latest-wins and source identity behavior has unit coverage, but full API stale/error/empty presentation is not a release matrix. | Add UI integration coverage during frontend release review. |
| REV-RADIO-005 | Radio frontend | P1 | SHOULD FIX BEFORE RELEASE for Radio release | Dynamic catalog is projected correctly in focused tests, but full stale/degraded/logo/source-filter/playback UX is not fully reviewed. | Pair with `REV-RADIO-002` in the Radio release gate. |
| REV-PWA-001 | PWA | P2 | CAN DEFER | Workbox caches `/api/stations`, `/api/config`, and logos; freshness and logout/update invalidation are not separately accepted. | Add a service-worker cache/version smoke. |
| REV-DESKTOP-002 | Desktop runtime | P2 | CAN DEFER | Packaged runtime/recovery evidence exists, but formal release-host signing is separate and no universal Desktop host matrix is claimed. | Run Windows/macOS host matrix after `REV-DESKTOP-001`. |
| REV-DEPLOY-002 | Deployment | P2 | CAN DEFER | Fresh/upgrade/rollback and persistent volume migration are not represented by one current release qualification. | Build an isolated compose/NAS acceptance fixture. |
| REV-LEGACY-001 | Smart/export compatibility | P1 | SHOULD FIX BEFORE RELEASE | Smart RTSP M3U export is active legacy compatibility and consumers have not been inventoried. | Inventory external consumers and publish a deprecation/rollback plan before removal. |
| REV-LEGACY-002 | Adapter retirement | P2 | CAN DEFER | ProviderResolver and legacy adapter fallback remain intentionally active for rollback and unowned schemes. | Retire only after signed ownership rollout and rollback gates close. |
| REV-REPLAY-001 | Catch-up product scope | P2 | CAN DEFER | Replay exists in HNNTV-specific adapter/plugin semantics, not as a generic FullPlayer product contract. | Treat any generic catch-up feature as a new product design, not an adapter cleanup. |

## Closed findings carried forward

These findings came from earlier audits but have later code/test evidence and
are not open release issues at this baseline:

- Automation startup orphan handles: fixed in `d3873bd`.
- Market update/uninstall race, concurrent first setup, and private-cache
  authority revocation: fixed in `536e137`.
- Subscription parent/channel atomicity: fixed in `471029b`.
- EPG stale source-task reconciliation: fixed in `c80636c`.
- Wide cold-start lifecycle: fixed in `71cbd2f`; production Wide HLS was then
  removed in `f20f26f`.
- Legacy lifecycle task ownership, shutdown draining, and retry cancellation:
  fixed in `2036d51`.
- Plugin artifact store ownership, recurring deletion, exact repair, and
  restart recovery: fixed and accepted in `fc2d2bc`; later real recovery
  evidence confirmed persisted digests, active lifecycle state, unchanged
  ownership, and stable subsequent restarts. The older
  `REAL-PLUGIN-INSTALLATION-INTEGRITY-AUDIT-1` finding is historical evidence,
  not a current blocker.
- Yunting complete catalog, official image mapping, bounded fanout, and stale
  snapshot retention: fixed and accepted in `d062b97`.
- `REV-DB-001` old-to-current upgrade and rollback evidence: closed by
  `DATABASE-UPGRADE-RECOVERY-1`; the implementation remains uncommitted at this
  review baseline.
- `REV-AUTH-001` first-admin initialization evidence: closed by
  `AUTH-SETTINGS-FOUNDATION-1`; SQLite is the authority, concurrent setup has
  one winner, and post-create session failure remains recoverable through
  normal login.
- `REV-SETTINGS-001` precedence and restart evidence: closed by
  `AUTH-SETTINGS-FOUNDATION-1`; effective settings are `ENV > DB > mode
  default`, writes are atomic and strictly validated, and read failures no
  longer discard persisted security policy.

### SUBSCRIPTION-REFRESH-IDENTITY-1 closeout

- `REV-SUB-001` safe subscription fetch boundary: closed in the current
  working tree. Regular subscription reads use the shared SSRF/redirect/TLS,
  timeout, response-size, and sanitized-error boundary.
- `REV-SUB-002` durable refresh state and restart recovery: closed in the
  current working tree. Attempt/success timestamps, status, sanitized error,
  generation guards, last-known-good channel retention, and `running` to
  `interrupted` startup recovery are covered.
- `REV-SUB-003` refresh-all failure isolation: closed in the current working
  tree. Per-subscription failures produce stable result entries and do not
  abort later subscriptions; cancellation remains propagating.
- `REV-SUB-004` refresh-all concurrency: closed in the current working tree.
  Regular sources use a four-way bound while Market refreshes retain their
  serialized lifecycle boundary.
- Focused subscription, Market, and logical-channel evidence totals 72
  passing tests. The implementation is uncommitted at this review baseline;
  the code and tests remain the authoritative evidence.

### AUTH-SETTINGS-FOUNDATION-1 closeout

- Browser session expiry now uses one effective lifetime for both SQLite and
  the cookie. Valid sessions survive restart; logout revocation also survives
  restart.
- Setup mutation requires the shared request marker, and CORS uses the
  environment-owned origin allowlist instead of accepting every origin.
- Every current `/api/admin/*` route has `require_admin` in its dependency
  graph. FastAPI's body parser may return `422` before auth for malformed JSON;
  valid requests follow the documented `401`/`403`/`422` boundary.
- Frontend API `401` handling clears an authenticated stale user, redirects
  protected views, and preserves anonymous browse pages.
- Desktop backend auth is fail-closed and focused-tested, but the Electron
  launcher/renderer bootstrap remains `REV-AUTH-002`.

### PLUGIN-ARTIFACT-INTEGRITY-1 closeout

- `REV-PLUGIN-001` is closed as an artifact-integrity finding. The historical
  deletion root cause and exact trusted repair were accepted at `fc2d2bc`; the
  current read-only store check still finds 25/25 referenced artifact files and
  no staged artifacts.
- The official release loader now rejects incomplete artifact-reference
  coverage, duplicate package or identity/version metadata, duplicate platform
  declarations, duplicate trust keys, and mismatched official manifest signer
  metadata. The current 10-package catalog passes these checks.
- `REV-PLUGIN-003` remains a target-runtime and real-provider acceptance item,
  not a missing artifact feature. All 12 persisted `PLUGIN_CRASHED` rows
  recovered under isolated real-process startup, and the host packaged lifecycle
  passed without reproducing a production defect.
- `REV-REL-001` remains `IMPLEMENTATION READY / EXTERNAL SIGNING CREDENTIAL
  REQUIRED`; test-signer catalog-to-installer acceptance passed and the
  production private key is intentionally outside the workspace.
- `REV-RADIO-001` remains `IMPLEMENTATION READY / EXTERNAL SIGNING-PUBLISH
  BLOCKED`; an official-style signed Radio fixture passed the production
  lifecycle, but Radio packages are not in the production-signed release.

### DATABASE-UPGRADE-RECOVERY-1 closeout

- WaveFlow has one SQLite authority shared by application and security state.
  FastAPI lifespan awaits initialization before starting Plugin ownership or
  Automation.
- Fresh, oldest supported, intermediate, recent Radio, real current-DB copy,
  repeated restart, concurrent initialization, busy writer, malformed schema,
  corruption, and injected migration failure cases have reproducible evidence.
- Schema DDL and additive compatibility work now commit in one transaction;
  the foreign-key-sensitive Radio table rebuild is a separate atomic,
  idempotent transaction. Initialization failures propagate and connections
  are closed.
- The historical `refresh_generation` compatibility migration targeted
  `channels` instead of `subscriptions`; current initialization repairs the
  required subscription column without deleting the harmless legacy column.
- `REV-DB-001` is closed. Application rollback across a schema change requires
  the matching pre-upgrade SQLite backup, recorded in ADR-0007 and the database
  upgrade runbook; arbitrary old-binary/new-database compatibility is not
  claimed.

## Release hardening review order

The phases follow dependency and risk, not directory order.

### Phase 1 - Release foundations and safety

1. SQLite schema/upgrade and persistent data recovery.
2. Setup, auth, sessions, admin authorization, and runtime settings.
3. Signed handles, media credentials, SSRF, redirects, DNS/DoH.
4. Plugin artifact integrity, permissions, activation, and recovery.
5. Official signing inputs and immutable deployment image/version inputs.

Exit condition: no P0/P1 foundation blocker, and fresh/upgrade/restart paths
preserve data and fail closed on invalid artifacts or targets.

### Phase 2 - Source, content, and provider correctness

1. Subscription import/refresh and M3U/source identity.
2. IPTV logical aggregation and probe state.
3. EPG source datasets, matching, maintenance, and compatibility reads.
4. Market package/install/update/uninstall and visual assets.
5. TV provider Plugins and Radio catalog/provider distribution.

Exit condition: source identity, package identity, catalog generations, and
failure retention are stable across restart and partial upstream failure.

### Phase 3 - Media and player acceptance

1. Thin HLS and RTSP focused invariants.
2. HTTP-FLV/MPEG-TS real-origin matrix.
3. Radio playback, MIME/headers, provider descriptors, and browser behavior.
4. YouTube provider/iframe behavior and any provider-specific replay path.
5. FullPlayer source switch, explicit-play, cancellation, and late response.

Exit condition: each advertised transport has a bounded fallback/error
contract and a real browser/source acceptance record.

### Phase 4 - Product integration and platform

1. IPTV Home, Radio Home, Settings, Market, EPG, and Plugin UI contracts.
2. PWA service-worker cache/update behavior.
3. Electron/Desktop packaged runtime and loopback auth.
4. Docker/NAS fresh install, upgrade, rollback, volume persistence, and logs.

Exit condition: API/UI state, authorization, cache invalidation, and platform
startup/shutdown agree with the backend contracts.

### Phase 5 - Cross-module release rehearsal

Run the complete backend/frontend test suites, frontend build and E2E, Python
compile, clean release build/sign/package, isolated fresh install, upgrade,
restart/offline recovery, representative live-source smoke, and
`git diff --check` on the release candidate.

## Fixed review checklist

Each future subsystem review should record only applicable items, with code,
test, and runtime evidence:

1. Production call path.
2. Authority and data ownership.
3. Persistence and migration.
4. State and lifecycle.
5. Concurrency and race behavior.
6. Cancellation and cleanup.
7. Error recovery.
8. Cache and invalidation.
9. Security boundary.
10. Backward compatibility.
11. Performance and hot path.
12. Duplicate or dead code.
13. Frontend/backend contract.
14. Restart and reconnect.
15. Real-world smoke.
16. Tests and excluded tests.
17. Known limitations and release classification.

## Baseline coverage gaps

Architecture Baseline v1 directly documents source identity, playback,
player lifecycle, RTSP, Thin HLS, raw streams, Radio, YouTube, security, EPG,
legacy paths, the Plugin contract, and the database migration policy. It does
not yet have one architecture document per setup/auth or Market release
operations, Electron packaging, Docker/NAS deployment, or frontend product
surfaces. This matrix records those modules and their review state without
inventing new architecture contracts. Dedicated architecture documents are
only needed when a future implementation changes an ownership or lifecycle
boundary.

The prior `overview.md` statement that a naked EPG loop remains separately
scheduled was stale relative to current `backend/epg_tasks.py` and
`backend/main.py`; the index was corrected in this phase.

## Radio/FM concurrency status

No local Radio/FM worklog currently has an active `IN PROGRESS` state. The
latest Yunting catalog phase is complete and accepted at `d062b97`; the latest
Radio distribution phase is complete with a signing blocker. Radio catalog
visibility is therefore `CURRENT`, while Radio playback and official package
distribution remain release-gated. TingFM/static retirement and RadioBrowser
retirement are explicitly not part of this matrix's implementation scope.

## Current next review

The next in-workspace review is Phase 2 **Probe, health, and reachability**
(`REV-PROBE-001`). Subscription import/refresh and source identity
(`REV-SUB-001` through `REV-SUB-004`) are accepted in the current working tree;
the local setup, browser-session, admin-route, and settings-precedence gate is
also accepted. Desktop auth bootstrap
(`REV-AUTH-002`) and public abuse controls (`REV-AUTH-003`) remain explicit
platform/deployment follow-ups. External signed distribution, target-runtime,
and representative provider gates remain release blockers but do not prevent
the next source-correctness review. Do not start Radio retirement or remove
legacy adapters before those distribution gates close.

## Gate

`RELEASE REVIEW MATRIX READY` means the inventory, open issue IDs, review
ordering, and evidence boundaries are recorded. It does not mean the release
itself is ready; the P1 blockers above remain open.
