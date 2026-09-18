// One source-status model shared by the IPTV and Radio source menus.
//
// The lamp answers exactly one question -- "is this source known to be
// playable?" -- and nothing else.  "Which source is selected / currently
// playing" is a separate dimension and is expressed by the active row, never by
// the lamp.  Keeping them apart is what lets green + active mean "playing and
// healthy" while an unselected source can still show green because it is known
// to work.
//
// A lamp is derived from one precedence order:
//   1. what happened in this session (trying / played / failed)
//   2. otherwise what the probe concluded
//   3. otherwise unknown
export const SOURCE_LAMP = {
  GOOD: 'good',
  TRYING: 'trying',
  BAD: 'bad',
  UNKNOWN: 'unknown',
}

const LAMP_CLASS = {
  [SOURCE_LAMP.GOOD]: 'bg-emerald-500',
  [SOURCE_LAMP.TRYING]: 'bg-amber-400',
  [SOURCE_LAMP.BAD]: 'bg-red-500',
  [SOURCE_LAMP.UNKNOWN]: 'bg-neutral-300 dark:bg-neutral-600',
}

export function lampClass(lamp) {
  return LAMP_CLASS[lamp] || LAMP_CLASS[SOURCE_LAMP.UNKNOWN]
}

// The two pieces of evidence are named apart because they are different
// measurements: this session's playback attempt, and the channel's stored
// speed-test result (channels.probe_status / is_working).  Only the latter is a
// real speed test, so only it says 测速.  The lamp and the text are produced
// together so they can never disagree.
const TEXT = {
  // This session is a playback attempt, not a speed test: no probe request is
  // issued.  Only the stored channel result (原测速) comes from an actual test.
  SESSION_PLAYING: '本次可播',
  SESSION_TRYING: '正在尝试',
  SESSION_FAILED: '本次失败',
  STORED_GOOD: '原测速可用',
  STORED_BAD: '原测速失败',
  STORED_NOT_LIVE: '原测速未开播',
  UNTESTED: '未测速',
}

/**
 * Lamp and status text for one IPTV source, derived from one precedence order.
 *
 * @param runtimeStatus this session's attempt state: trying | playing | failed | stopped | idle
 * @param probeStatus   the channel's stored speed test: not_live | untested | online | offline | ...
 * @param working       1 = stored test says it worked, 0 = it says it failed, otherwise unknown
 */
export function iptvSourceStatus(runtimeStatus, probeStatus, working) {
  if (runtimeStatus === 'trying') {
    return { lamp: SOURCE_LAMP.TRYING, text: TEXT.SESSION_TRYING }
  }
  if (runtimeStatus === 'playing') {
    return { lamp: SOURCE_LAMP.GOOD, text: TEXT.SESSION_PLAYING }
  }
  if (runtimeStatus === 'failed') {
    return { lamp: SOURCE_LAMP.BAD, text: TEXT.SESSION_FAILED }
  }
  // 'stopped' means the user switched away from a source that was working; it
  // says nothing about playability, so it falls through to the stored test.
  if (probeStatus === 'not_live') {
    return { lamp: SOURCE_LAMP.BAD, text: TEXT.STORED_NOT_LIVE }
  }
  if (working === 1) return { lamp: SOURCE_LAMP.GOOD, text: TEXT.STORED_GOOD }
  if (working === 0) return { lamp: SOURCE_LAMP.BAD, text: TEXT.STORED_BAD }
  return { lamp: SOURCE_LAMP.UNKNOWN, text: TEXT.UNTESTED }
}

/**
 * Lamp for one Radio source.  Radio has no per-attempt runtime state, so the
 * persisted health is the only input; anything unknown stays gray rather than
 * claiming the source works.
 */
export function radioSourceLamp(healthStatus) {
  const health = String(healthStatus || '').trim().toLowerCase()
  if (health === 'healthy') return SOURCE_LAMP.GOOD
  if (health === 'failed' || health === 'unhealthy' || health === 'error') return SOURCE_LAMP.BAD
  return SOURCE_LAMP.UNKNOWN
}

export function radioSourceText(healthStatus) {
  const lamp = radioSourceLamp(healthStatus)
  if (lamp === SOURCE_LAMP.GOOD) return TEXT.STORED_GOOD
  if (lamp === SOURCE_LAMP.BAD) return TEXT.STORED_BAD
  return TEXT.UNTESTED
}
