# Radio Architecture

Status: CURRENT / NOT FULLY REVIEWED

Verified code baseline: `8e49eca1e206b0ad148a4e1b9eba74d7a5fa7a42`

Radio playback contract evidence: commit
`197cc679e7ed1e4497e6affb1fbef29bb19b5583` and its focused frontend tests.

Radio has an active Plugin/Core bridge and also retains legacy/static paths.
This document deliberately does not decide TingFM retirement or rewrite the
Radio UI.

## Current Plugin path

```text
Radio Plugin artifact/runtime
  -> radio.catalog
  -> RadioCatalogBridge validation
  -> generation/CAS atomic durable publish
  -> radio_stations + radio_station_sources
  -> /api/radio/stations projection
  -> persisted source_id selection
  -> RadioResolver.resolve_source
  -> radio.resolve_stream
  -> signed Radio media handle/playlist/stream
  -> radioAudioEngine / AudioEngine
```

The catalog bridge is implemented in `backend/radio_core.py:RadioCatalogBridge`.
The runtime refresh and bounded retry/locking live in
`backend/plugin_production.py:refresh_radio_catalog`, and persistence is in
`backend/database.py:begin_radio_catalog_refresh` and
`apply_radio_catalog`. The API filters visible owners through
`backend/routers/radio.py:_active_radio_owner_identities`.

## Identity and durable state

Radio station identity is derived from `(owner_identity, provider_key,
provider_station_id)`. A source identity additionally includes
`source_discriminator`; URL, token, and display name are not identity. The
tables are:

- `radio_stations`: station container and provider/catalog lifecycle metadata;
- `radio_station_sources`: source reference, revision, health, and expiry;
- `radio_catalog_states`: owner-scoped generation and last published generation;
- `radio_programme_snapshots`: optional provider-native programme projection.

Generation publication uses a conditional write. A late generation cannot
publish over a newer one. A failed refresh records a bounded error and does not
replace the last successful generation. Current code also applies a bounded
stale grace window before expired rows are pruned.

## Ownership and visibility

The API fails closed for owners that are not active healthy Radio Plugin
instances or do not declare the Radio catalog contract. This is an ownership
gate, not a geographical content filter. `RadioResolver` checks the persisted
owner, active scheme ownership, source revision, and descriptor before updating
source health or returning a media descriptor.

## Current Plugin providers

The repository contains bundled provider implementations for Yunting, MyRadio,
and HitFM, with installed/runtime state managed by the Plugin subsystem. The
current code and focused tests cover catalog validation, source identity,
generation handling, provider ownership, and transport descriptor bridging.
Exact real installed artifact state and full UI/playback acceptance remain
separate runtime audits; this document does not infer them from source files.

## Playback ownership and explicit-play contract

Radio playback is owned by `AudioEngine.vue` and
`radioAudioEngine.js`; it is separate from the IPTV `FullPlayer` media
engines. The Radio store carries a small playback-intent value through the
watcher boundary. Current values are `station_click`, `play_button`,
`source_switch`, `passive`, and `recovery`. The intent identifies why a play
request was made; it does not preserve a browser's transient user activation
across an asynchronous operation.

The effective Radio playback path is:

```text
station selection or Play
  -> AudioEngine watcher
  -> radioAudioEngine attempt
  -> Plugin source /resolve when required
  -> same attempt continuation with the resolved transport
  -> source load or HLS manifest setup
  -> playAudioSafely on the actual media source
```

For a persisted Plugin source, resolve/load must complete before the engine
calls `play()`. The engine must not call `play()` against an empty audio
element merely to try to retain a click gesture. A continuation after
`/resolve` remains part of the original attempt, so it cannot create a second
parallel attempt or let a late result replace a newer station. A source
fallback or recovery is a new media request within the logical station
attempt: it resets the per-source play guard and calls `play()` for the new
source. If resolve fails, the attempt is released and a later explicit Play
may create a fresh resolve/load/play attempt.

`playAudioSafely` is the Radio play boundary. It ignores stale or cancelled
attempts, deduplicates an in-flight play request, and classifies failures as
follows:

| Result | Radio behavior |
| --- | --- |
| `NotAllowedError` | Shows the browser autoplay-policy message. |
| `AbortError` | Treats the operation as interrupted, stops the request, and does not show the autoplay message. |
| `NotSupportedError` or media error code 4 | Shows the unsupported-format message. |
| `NetworkError`, `TimeoutError`, or media error code 2 | Shows the network failure message. |
| Media error code 3 | Shows the decode failure message. |
| Other active-attempt play errors | Shows the generic Radio playback failure message. |
| Stale/cancelled attempt results | Ignored without writing current Radio UI state. |

This classification applies to the Radio `play()` boundary; transport error
handlers may still select a direct/proxy fallback before presenting a final
error. Focused coverage is in
`frontend/tests/unit/audioEngineComponent.test.js`,
`frontend/tests/unit/radioAudioEngineAttempt.test.js`, and
`frontend/tests/unit/radioStore.test.js`. Static browser click and explicit
pause/Play checks passed. Dynamic Yunting HLS and MyRadio `audio_http`
browser acceptance was not run because the isolated runtime exposed no
visible dynamic Radio stations, so full provider/browser acceptance remains
open.

## Legacy and adjacent paths

`backend/main.py` still contains TingFM static stream data and older Radio
Browser/on-demand compatibility routes. They may have real consumers and are
not removed by the Plugin bridge. Their retirement status is recorded in
[legacy-and-deprecations](legacy-and-deprecations.md). No Radio legacy cleanup
or UI architecture change is part of this baseline.

## Open review items

- Complete real-source playback and browser matrix for each Radio transport.
- Confirm all current installed artifacts and their runtime health during a
  controlled acceptance run.
- Decide whether legacy/static providers have remaining user-visible duties.
- Keep provider catalog completeness, stale visibility, and logo metadata
  evidence in Radio-specific worklogs rather than treating this map as a
  complete audit.

Evidence: `backend/radio_core.py`, `backend/plugin_production.py`,
`backend/database.py`, `backend/routers/radio.py`,
`backend/tests/test_radio_core_bridge.py`,
`backend/tests/test_yunting_catalog_resilience.py`, and the RADIO worklogs.
