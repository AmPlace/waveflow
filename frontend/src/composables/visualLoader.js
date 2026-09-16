/**
 * Lazy visual metadata request coordination.
 *
 * Core owns source-scoped TTL/cache.  This loader only bounds browser-side
 * concurrency and shares in-flight requests; it intentionally has no second
 * metadata TTL cache keyed by canonical channel.
 */
const MAX_CONCURRENT = 4
const _inflight = new Map()
let _currentAbortController = null
let _pendingQueue = []
let _activeCount = 0

export function abortPendingVisualRequests() {
  _currentAbortController?.abort()
  _currentAbortController = null
  while (_pendingQueue.length) _pendingQueue.shift()?.()
  _inflight.clear()
}

function _getAbortController() {
  if (!_currentAbortController || _currentAbortController.signal.aborted) {
    _currentAbortController = new AbortController()
  }
  return _currentAbortController
}

export async function loadVisual(channelKey, fetchFn) {
  if (!channelKey) return {}
  const existing = _inflight.get(channelKey)
  if (existing) return existing.catch(() => ({}))
  const ctrl = _getAbortController()
  const promise = _enqueue(fetchFn, ctrl.signal)
  _inflight.set(channelKey, promise)
  try {
    return await promise
  } catch (error) {
    if (error?.name !== 'AbortError' && error?.message !== '请求已取消') return {}
    return {}
  } finally {
    if (_inflight.get(channelKey) === promise) _inflight.delete(channelKey)
  }
}

async function _enqueue(fetchFn, signal) {
  while (_activeCount >= MAX_CONCURRENT) await new Promise(resolve => _pendingQueue.push(resolve))
  _activeCount++
  try {
    return await fetchFn(signal)
  } finally {
    _activeCount--
    _pendingQueue.shift()?.()
  }
}
