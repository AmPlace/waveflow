<template>

  <audio ref="audioRef" hidden playsinline></audio>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import Hls from 'hls.js'
import { usePlayerStore } from '../stores/player'
import { API_BASE, isDesktop } from '../apiBase'
import { publicAsset } from '../publicAsset'
import { createRadioAudioEngine } from '../utils/radioAudioEngine'

const playerStore = usePlayerStore()
const { currentStation, isPlaying, volume, isMuted } = storeToRefs(playerStore)
const audioRef = ref(null)
const hlsRef = ref(null)
const directStreamMode = ref('')

const radioEngine = createRadioAudioEngine({
  audioRef,
  hlsRef,
  directStreamMode,
  playerStore,
  currentStation,
  volume,
  Hls,
  API_BASE,
  apiCredentials: isDesktop ? 'include' : 'same-origin',
  publicAsset,
})

function consumeRadioPlaybackIntent(fallback = 'passive') {
  return typeof playerStore.consumeRadioPlaybackIntent === 'function'
    ? playerStore.consumeRadioPlaybackIntent(fallback)
    : fallback
}

function hasActiveRadioAttempt(stationId) {
  const attempt = radioEngine.activeAttemptInfo()
  return Boolean(attempt?.active && attempt.stationId === stationId)
}

// 电台 EPG 更新时自动刷新 MediaSession 显示
watch(
  () => {
    const id = currentStation.value
    return id ? playerStore.stationMap[id]?.subtitle : undefined
  },
  (subtitle) => {
    radioEngine.updateMediaSessionForCurrentSubtitle(subtitle)
  },
)

onMounted(() => {
  if (audioRef.value) {
    audioRef.value.volume = volume.value
    audioRef.value.muted = isMuted.value
  }
  if (playerStore.isPlaying) {
    radioEngine.loadStation(currentStation.value, {
      intent: consumeRadioPlaybackIntent('passive'),
    })
  }
})

watch(currentStation, (stationId) => {
  if (!stationId) {
    radioEngine.stopRadioAttempt()
    return
  }
  if (playerStore.isPlaying && !hasActiveRadioAttempt(stationId)) {
    radioEngine.loadStation(stationId, {
      intent: consumeRadioPlaybackIntent('passive'),
    })
  }
})

watch(
  () => {
    const stationId = currentStation.value || ''
    const station = currentStation.value ? playerStore.stationMap[currentStation.value] : null
    return `${stationId}\u0000${station?.radioSourceId || ''}`
  },
  (sourceKey, previousSourceKey) => {
    const [stationId, sourceId] = String(sourceKey || '').split('\u0000')
    const [previousStationId, previousSourceId] = String(previousSourceKey || '').split('\u0000')
    // A new station changes both pieces of the key.  Its station watcher owns
    // that load; this watcher is only for an in-place source selection.
    if (!stationId || stationId !== previousStationId) return
    if (!sourceId || sourceId === previousSourceId || !playerStore.isPlaying) return
    radioEngine.loadStation(currentStation.value, {
      intent: consumeRadioPlaybackIntent('source_switch'),
    })
  },
)

watch(() => playerStore.currentIptvChannel, (channel) => {
  if (channel) radioEngine.stopRadioAttempt()
})

watch(isPlaying, (nextIsPlaying) => {
  if (!audioRef.value) return
  if (nextIsPlaying) {
    if (playerStore.currentIptvChannel) return // IPTV 模式下 AudioEngine 不播
    const stationId = currentStation.value
    if (!stationId) return

    const intent = consumeRadioPlaybackIntent('passive')
    const hasActiveAttemptForStation = hasActiveRadioAttempt(stationId)
    const hasLoadedSource = Boolean(audioRef.value.src || hlsRef.value || directStreamMode.value)

    // The station watcher owns a selection-triggered load.  During a dynamic
    // Radio resolve it is normal for no media source to exist yet; an active
    // attempt is enough to prove that this watcher must not start another one
    // or call play() against an empty audio element.
    if (!hasLoadedSource) {
      if (!hasActiveAttemptForStation) radioEngine.loadStation(stationId, { intent })
      return
    }

    radioEngine.playAudioSafely(undefined, { intent, request: true })
    return
  }
  radioEngine.pauseCurrentAudio()
})

watch(volume, (nextVolume) => {
  radioEngine.setVolume(nextVolume)
})

watch(isMuted, (nextMuted) => {
  if (audioRef.value) audioRef.value.muted = nextMuted
})

onBeforeUnmount(() => {
  radioEngine.stopRadioAttempt()
})
</script>
