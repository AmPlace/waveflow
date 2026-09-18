// Stable codes for the playback states the UI branches on.
//
// ``playbackError`` stays the human-facing text, but nothing should decide
// behaviour by comparing that text: the wording is presentation and used to
// change independently of the state it described.
export const PLAYBACK_ERROR = {
  // The channel resolved to an empty source queue.
  NO_PLAYABLE_SOURCE: 'no_playable_source',
  // Every source in the queue was attempted in this cycle and none played.
  ALL_SOURCES_UNAVAILABLE: 'all_sources_unavailable',
}

const TEXT = {
  [PLAYBACK_ERROR.NO_PLAYABLE_SOURCE]: '没有可播放的源',
  [PLAYBACK_ERROR.ALL_SOURCES_UNAVAILABLE]: '所有播放源均不可用',
}

export function playbackErrorText(code) {
  return TEXT[code] || ''
}
