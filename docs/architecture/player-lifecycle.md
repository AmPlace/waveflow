# Player Lifecycle

Status: VERIFIED / PARTIAL

Verified code baseline: `8e49eca1e206b0ad148a4e1b9eba74d7a5fa7a42`

The selection and teardown invariants below are verified by current code and
focused frontend/backend tests. Browser autoplay policy, recovery quality on
all clients, and real Apple-device behavior are not fully reviewed here.

## Selection state

`frontend/src/stores/player.js` owns the IPTV selection queue:

- `iptvSelectionToken` increments for every new selection and invalidates late
  asynchronous work.
- `pendingIptvChannel` exposes the user's selection immediately while the
  candidate queue and provider resolves are still being assembled.
- `currentIptvChannel` is published when the queue is ready enough for the
  player surface; the source entries retain `source_id` and source type.
- `activeIptvResolveControllers` cancels provider resolve requests that no
  longer belong to the current selection.
- `iptvUrls` and `iptvUrlIndex` represent the current ordered attempt queue.

`playIptvChannel` does not trust a result merely because its request completed.
Every state mutation that depends on an asynchronous resolve checks the
selection token first.

## Channel switch invariant

The switch path is:

```text
row / FullPlayer selection
  -> playIptvChannel
  -> pendingIptvChannel
  -> FullPlayer pending watcher
  -> beginIptvChannelSwitch
  -> cancel startup and proxy races
  -> destroy old HLS/MPEG-TS/YouTube engines
  -> load the new candidate queue
```

The old engine is torn down as soon as the new selection is published. The
switch does not wait for an adapter network resolve to complete. This prevents
old audio/video, HLS callbacks, or recovery timers from operating on the new
channel.

`_playAttemptId` is a second guard inside FullPlayer. It protects an individual
attempt from late `play`, error, event, and cleanup callbacks after a fallback
or teardown.

## Candidate resolution

The queue is built in this order:

1. Persisted source entries are sorted by health/probe status and latency.
2. YouTube and adapter references enter provider-resolution handling.
3. Plain HLS sources may be tried directly and then through a channel-scoped
   proxy fallback.
4. RTSP and sources with request-header/proxy requirements use the proxy path.
5. A provider result can add a direct resolved URL or a proxy candidate, but
   the original `source_id` remains the candidate identity.

The adapter `AbortController` is canceled when the selection token changes.
Direct/proxy hedged races are cleaned up after a winner or cancellation, and
loser cleanup has an explicit bounded lifetime.

## First-play timing

For the production HLS path, `tryPlayIptv` attaches hls.js and waits for
`Hls.Events.FRAG_BUFFERED` before invoking `video.play()`. `FRAG_LOADED` means
that a fragment arrived; it does not prove that the media element has a safe
buffer to start from.

MPEG-TS/HTTP-FLV uses mpegts.js and its own media/error events. YouTube uses a
separate IFrame API or iframe lifecycle and does not share the HTMLMediaElement
HLS startup path. Radio uses `frontend/src/utils/radioAudioEngine.js` and is a
separate audio ownership mode.

## Errors and recovery

FullPlayer contains transport-specific HLS and MPEG-TS error handlers,
reconnect/watchdog paths, and source fallback. The code observes waiting,
stalled, progress, and media-frame signals, with iOS-specific frame observation
where available.

Radio's separate audio ownership path now has a focused explicit-play and
media-error contract in [radio.md](radio.md), backed by commit
`197cc679e7ed1e4497e6affb1fbef29bb19b5583` and Radio frontend tests. That
contract does not change the IPTV/FullPlayer lifecycle or preserve transient
browser activation across async work.

For FullPlayer/IPTV and other non-Radio transports, this baseline still does
not claim that every `play()` rejection is classified correctly on every
browser. The explicit user-gesture versus restore/autoplay boundary and the
cross-transport browser matrix remain open. Do not broaden an autoplay
message to cover network, decode, unsupported-format, cancellation, or
provider errors without a dedicated test.

## Ownership and cleanup rules

- A new selection owns all new resolve controllers, timers, and media engines.
- Old callbacks must check the selection/attempt token before mutating UI or
  store state.
- Engine destruction is idempotent and releases hls.js, mpegts.js, YouTube,
  event handlers, and playback watchdogs.
- Backend RTSP startup is shared by session identity; canceling one HTTP waiter
  must not cancel other waiters, while shutdown cancels and drains the shared
  startup owner.
- Source fallback must not turn a stale provider result into a new durable
  source record.

## Focused evidence

- `frontend/tests/unit/iptvSelectionLatestWins.test.js`
- `frontend/tests/unit/playerAdapterResolve.test.js`
- `frontend/tests/unit/fullPlayerEffectiveTransport.test.js`
- `frontend/tests/unit/fullPlayerRecoveryOwnership.test.js`
- `backend/tests/test_rtsp_startup.py`
- `backend/tests/test_rtsp_watchdog.py`
