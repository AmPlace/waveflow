import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const fullPlayerPath = new URL('../../src/components/FullPlayer.vue', import.meta.url)

function extractFunction(source, signature) {
  const start = source.indexOf(signature)
  assert.notEqual(start, -1, `missing function: ${signature}`)
  const bodyStart = source.indexOf('{', start)
  let depth = 0
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    if (source[index] === '}') depth -= 1
    if (depth === 0) return source.slice(start, index + 1)
  }
  throw new Error(`unterminated function: ${signature}`)
}

function createSwitchHarness() {
  const source = fs.readFileSync(fullPlayerPath, 'utf8')
  const switchSource = extractFunction(source, 'async function switchIptvSource(sourceKey)')
  const factory = new Function('deps', `
    let _playAttemptId = deps.playAttemptId
    let _recoverySeq = deps.recoverySeq
    let _recoveryInFlight = deps.recoveryInFlight
    const {
      isIptvMode, playerStore, iptvSourceOptions, sourceMenuOpen, isSourceSwitching,
      getSourceRuntimeStatus, setSourceRuntimeStatus, clearRaceLoser,
      sourceRaceKey,
      setIptvUrlIndexForAttempt, isProxyLikeEntry, tryPlayIptv,
      isAttemptActive, sourceType, markRaceLoser, fallbackToNextIptvUrl,
      playCurrentIptvUrl, markAllIptvSourcesUnavailable,
      clearStallRecoveryTimer, cancelCurrentStartup, cancelActiveProxyRace,
    } = deps
    ${switchSource}
    return {
      switchIptvSource,
      snapshot: () => ({ _playAttemptId, _recoverySeq, _recoveryInFlight }),
    }
  `)

  const statuses = new Map([[0, 'playing']])
  let stallTimerClearCount = 0
  const playerStore = {
    iptvUrlIndex: 0,
    iptvUrls: [
      { url: 'https://old.example/live.m3u8', source_id: 'old', type: 'direct', source_type: 'hls' },
      { url: 'https://new.example/live.m3u8', source_id: 'new', type: 'direct', source_type: 'hls' },
    ],
    setPlaybackError() {},
    clearPlaybackError() {},
    setLoading() {},
  }
  const harness = factory({
    playAttemptId: 10,
    recoverySeq: 7,
    recoveryInFlight: true,
    isIptvMode: { value: true },
    playerStore,
    iptvSourceOptions: { value: [{ identityKey: 'new:direct', index: 1, disabled: false }] },
    sourceMenuOpen: { value: true },
    isSourceSwitching: { value: false },
    getSourceRuntimeStatus: (index) => statuses.get(index) || 'idle',
    setSourceRuntimeStatus: (index, status) => statuses.set(index, status),
    sourceRaceKey: (entry) => `${entry.source_id}:${entry.type === 'proxy' ? 'proxy' : 'direct'}`,
    clearRaceLoser() {},
    setIptvUrlIndexForAttempt: async (index) => {
      playerStore.iptvUrlIndex = index
      return true
    },
    isProxyLikeEntry: () => false,
    tryPlayIptv: async () => {},
    isAttemptActive: () => true,
    sourceType: () => 'hls',
    markRaceLoser() {},
    fallbackToNextIptvUrl: async () => false,
    playCurrentIptvUrl: async () => {},
    markAllIptvSourcesUnavailable() {},
    clearStallRecoveryTimer: () => { stallTimerClearCount += 1 },
    cancelCurrentStartup() {},
    cancelActiveProxyRace() {},
  })
  return { harness, statuses, stallTimerClearCount: () => stallTimerClearCount }
}

test('手动切源使旧 recovery 立即失效', async () => {
  const { harness, statuses, stallTimerClearCount } = createSwitchHarness()

  await harness.switchIptvSource('new:direct')

  const state = harness.snapshot()
  assert.equal(state._recoverySeq, 8, '手动切源必须使正在运行的 recovery seq 失效')
  assert.equal(state._recoveryInFlight, false)
  assert.equal(stallTimerClearCount(), 1)
  assert.equal(statuses.get(0), 'stopped')
  assert.equal(statuses.get(1), 'trying')
})
