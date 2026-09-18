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

const LAMP_LABEL = {
  [SOURCE_LAMP.GOOD]: '可播',
  [SOURCE_LAMP.TRYING]: '正在尝试',
  [SOURCE_LAMP.BAD]: '不可用',
  [SOURCE_LAMP.UNKNOWN]: '未检测',
}

export function lampClass(lamp) {
  return LAMP_CLASS[lamp] || LAMP_CLASS[SOURCE_LAMP.UNKNOWN]
}

export function lampLabel(lamp) {
  return LAMP_LABEL[lamp] || LAMP_LABEL[SOURCE_LAMP.UNKNOWN]
}

/**
 * Lamp for one IPTV source.
 *
 * @param runtimeStatus this session's attempt state: trying | playing | failed | stopped | idle
 * @param probeStatus   the probe's verdict: not_live | untested | ''
 * @param working       1 = probe says it worked, 0 = probe says it failed, otherwise unknown
 */
export function iptvSourceLamp(runtimeStatus, probeStatus, working) {
  if (runtimeStatus === 'trying') return SOURCE_LAMP.TRYING
  if (runtimeStatus === 'playing') return SOURCE_LAMP.GOOD
  if (runtimeStatus === 'failed') return SOURCE_LAMP.BAD
  // 'stopped' means the user switched away from a source that was working; it
  // says nothing about playability, so it falls through to the probe.
  if (probeStatus === 'not_live') return SOURCE_LAMP.BAD
  if (working === 1) return SOURCE_LAMP.GOOD
  if (working === 0) return SOURCE_LAMP.BAD
  return SOURCE_LAMP.UNKNOWN
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
