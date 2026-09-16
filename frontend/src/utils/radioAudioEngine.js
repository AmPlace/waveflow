export function createRadioAudioEngine({
  audioRef,
  hlsRef,
  directStreamMode,
  playerStore,
  currentStation,
  volume,
  Hls,
  API_BASE,
  apiCredentials = 'same-origin',
  publicAsset,
  getNavigator = () => globalThis.navigator,
  getMediaMetadata = () => globalThis.MediaMetadata,
  createAudio = () => new Audio(),
  fetchImpl = (...args) => fetch(...args),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  logger = console,
}) {
  let attemptSeq = 0
  let activeAttempt = null
  let mainAudioErrorCleanup = null

  const RADIO_PLAYBACK_INTENTS = new Set([
    'station_click',
    'play_button',
    'source_switch',
    'passive',
    'recovery',
  ])

  function normalizePlaybackIntent(intent, fallback = 'passive') {
    const value = String(intent || '').trim()
    return RADIO_PLAYBACK_INTENTS.has(value) ? value : fallback
  }

  function persistedRadioStation(stationId) {
    const station = playerStore.stationMap[stationId]
    if (!station?.radioStationId || !station?.radioSourceId) return null
    return station
  }

  function radioResolveUrl(station) {
    const query = new URLSearchParams({ source_id: String(station.radioSourceId) })
    return `${API_BASE}/api/radio/stations/${encodeURIComponent(station.radioStationId)}/resolve?${query}`
  }

  function radioMediaUrl(station, path) {
    const query = new URLSearchParams({ source_id: String(station.radioSourceId) })
    return `${API_BASE}/api/media/radio/${encodeURIComponent(station.radioStationId)}/${path}?${query}`
  }

  function configureCoreCredentials(xhr, url) {
    // Desktop credentials belong to the Core origin, never a direct provider.
    xhr.withCredentials = false
    if (apiCredentials !== 'include' || !API_BASE) return
    try {
      xhr.withCredentials = new URL(url, `${API_BASE}/`).origin === new URL(API_BASE).origin
    } catch {}
  }

  function isAttemptActive(attempt) {
    return Boolean(
      attempt
        && activeAttempt === attempt
        && !attempt.cancelled
        && currentStation.value === attempt.stationId
        && !playerStore.currentIptvChannel,
    )
  }

  function addAttemptCleanup(attempt, cleanup) {
    if (!attempt || typeof cleanup !== 'function') return () => {}
    attempt.cleanups.add(cleanup)
    return () => attempt.cleanups.delete(cleanup)
  }

  function runAttemptCleanups(attempt) {
    if (!attempt) return
    const cleanups = Array.from(attempt.cleanups)
    attempt.cleanups.clear()
    for (const cleanup of cleanups) {
      try { cleanup() } catch {}
    }
  }

  function removeMainAudioErrorHandler() {
    if (!mainAudioErrorCleanup) return
    mainAudioErrorCleanup()
    mainAudioErrorCleanup = null
  }

  function bindMainAudioError(attempt) {
    removeMainAudioErrorHandler()
    const el = audioRef.value
    if (!el || !attempt) return
    const onError = () => handleAudioError(attempt)
    el.addEventListener('error', onError)
    mainAudioErrorCleanup = () => el.removeEventListener('error', onError)
    addAttemptCleanup(attempt, () => {
      if (mainAudioErrorCleanup) removeMainAudioErrorHandler()
    })
  }

  function invalidateActiveAttempt() {
    const attempt = activeAttempt
    if (!attempt) return
    attempt.cancelled = true
    if (activeAttempt === attempt) activeAttempt = null
    runAttemptCleanups(attempt)
  }

  function beginAttempt(stationId, intent = 'passive') {
    invalidateActiveAttempt()
    const attempt = {
      id: ++attemptSeq,
      stationId,
      intent: normalizePlaybackIntent(intent),
      cancelled: false,
      cleanups: new Set(),
      mainHls: null,
      playRequested: true,
      playPromise: null,
      playGeneration: 0,
      played: false,
    }
    activeAttempt = attempt
    return attempt
  }

  function prepareAttemptMedia(attempt) {
    if (!attempt) return
    // A recovery/fallback source is a new media playback request even though
    // it remains within the same logical station attempt.
    attempt.playGeneration += 1
    attempt.playPromise = null
    attempt.played = false
  }

  function destroyCurrentHls() {
    if (!hlsRef.value) return
    hlsRef.value.destroy()
    hlsRef.value = null
  }

  function destroyAttemptHls(attempt) {
    if (!attempt?.mainHls) return
    const hls = attempt.mainHls
    attempt.mainHls = null
    if (hlsRef.value === hls) hlsRef.value = null
    try { hls.destroy() } catch {}
  }

  function resetAudioSource() {
    const el = audioRef.value
    if (!el) return
    removeMainAudioErrorHandler()
    el.pause()
    el.removeAttribute('src')
    el.load()
  }

  function setMainAudioSrc(attempt, url) {
    if (!isAttemptActive(attempt) || !audioRef.value) return false
    prepareAttemptMedia(attempt)
    bindMainAudioError(attempt)
    audioRef.value.src = url
    audioRef.value.load()
    return true
  }

  function updateSystemMediaSession(stationId, attempt = activeAttempt) {
    if (!isAttemptActive(attempt)) return
    const navigatorRef = getNavigator()
    if (!navigatorRef || !('mediaSession' in navigatorRef)) return
    const meta = playerStore.stationMap[stationId] || {}
    const finalLogo = publicAsset(meta.logoUrl || '/logos/default.png')

    try {
      const MediaMetadataCtor = getMediaMetadata()
      navigatorRef.mediaSession.metadata = new MediaMetadataCtor({
        title: meta.name || '未知频率',
        artist: meta.subtitle || 'WaveFlow Radio',
        album: 'Live Stream',
        artwork: [{ src: finalLogo, sizes: '512x512', type: 'image/png' }],
      })
    } catch (e) {
      logger.warn('MediaSession 写入失败，跳过元数据更新', e)
    }

    navigatorRef.mediaSession.setActionHandler('play', () => playerStore.togglePlay(true))
    navigatorRef.mediaSession.setActionHandler('pause', () => playerStore.togglePlay(false))
  }

  function clearRadioMediaSession() {
    const navigatorRef = getNavigator()
    if (!navigatorRef || !('mediaSession' in navigatorRef)) return
    const session = navigatorRef.mediaSession
    try { session.metadata = null } catch {}
    try { session.playbackState = 'none' } catch {}
    for (const action of ['play', 'pause']) {
      try { session.setActionHandler(action, null) } catch {}
    }
  }

  function updateMediaSessionForCurrentSubtitle(subtitle) {
    const attempt = activeAttempt
    if (subtitle && isAttemptActive(attempt)) updateSystemMediaSession(attempt.stationId, attempt)
  }

  function classifyPlaybackError(error, audio = audioRef.value) {
    const name = String(error?.name || '')
    if (name === 'NotAllowedError') return 'not_allowed'
    if (name === 'AbortError') return 'aborted'
    if (name === 'NotSupportedError') return 'not_supported'
    if (name === 'NetworkError' || name === 'TimeoutError') return 'network'

    const mediaCode = Number(audio?.error?.code)
    if (mediaCode === 2) return 'network'
    if (mediaCode === 3) return 'decode'
    if (mediaCode === 4) return 'not_supported'
    return 'generic'
  }

  function playbackErrorMessage(kind) {
    if (kind === 'not_allowed') return '浏览器阻止自动播放，请手动点击播放。'
    if (kind === 'not_supported') return '当前浏览器不支持此音频格式，请尝试其他源。'
    if (kind === 'network') return '音频网络连接失败，请稍后重试。'
    if (kind === 'decode') return '音频解码失败，请尝试其他源。'
    return '音频播放失败，请稍后重试。'
  }

  function playAudioSafely(attempt = activeAttempt, options = {}) {
    if (!isAttemptActive(attempt) || !audioRef.value) return Promise.resolve(false)

    if (options.intent) attempt.intent = normalizePlaybackIntent(options.intent, attempt.intent)
    if (options.request === true) attempt.playRequested = true
    if (!attempt.playRequested) return Promise.resolve(false)
    if (attempt.playPromise) return attempt.playPromise
    if (attempt.played) return Promise.resolve(true)

    const generation = ++attempt.playGeneration
    let playPromise
    playPromise = (async () => {
      try {
        await audioRef.value.play()
        if (
          !isAttemptActive(attempt)
          || generation !== attempt.playGeneration
          || !attempt.playRequested
        ) return false

        attempt.played = true
        playerStore.clearPlaybackError()
        playerStore.setLoading(false)
        playerStore.togglePlay(true, { intent: 'passive' })
        updateSystemMediaSession(attempt.stationId, attempt)
        return true
      } catch (error) {
        if (
          !isAttemptActive(attempt)
          || generation !== attempt.playGeneration
          || !attempt.playRequested
        ) return false

        const kind = classifyPlaybackError(error)
        const navigatorRef = getNavigator()
        logger.warn('Radio audio.play rejected.', {
          kind,
          name: String(error?.name || ''),
          intent: attempt.intent,
          userActivation: {
            isActive: Boolean(navigatorRef?.userActivation?.isActive),
            hasBeenActive: Boolean(navigatorRef?.userActivation?.hasBeenActive),
          },
          muted: Boolean(audioRef.value.muted),
        })

        if (kind === 'aborted') {
          attempt.playRequested = false
          playerStore.setLoading(false)
          playerStore.togglePlay(false, { intent: 'passive' })
          return false
        }

        attempt.playRequested = false
        playerStore.setPlaybackError(playbackErrorMessage(kind))
        playerStore.togglePlay(false, { intent: 'passive' })
        return false
      } finally {
        if (attempt.playPromise === playPromise) attempt.playPromise = null
      }
    })()
    attempt.playPromise = playPromise
    return playPromise
  }

  function handleAudioError(attempt = activeAttempt) {
    if (!isAttemptActive(attempt)) return
    attempt.intent = 'recovery'
    if (directStreamMode.value === 'proxy') {
      playerStore.setPlaybackError('后端中转音频流连接失败，请稍后重试。')
      playerStore.togglePlay(false, { intent: 'passive' })
      return
    }
    playerStore.setPlaybackError('电台音频加载失败，请检查后端代理或稍后重试。')
    playerStore.togglePlay(false, { intent: 'passive' })
  }

  function loadStation(stationId, options = {}) {
    if (!audioRef.value || !stationId) return null

    const continuation = Boolean(options.attempt)
    const attempt = options.attempt || beginAttempt(stationId, options.intent)
    const radioStation = persistedRadioStation(stationId)
    if (!radioStation) {
      invalidateActiveAttempt()
      playerStore.setPlaybackError('电台目录已更新，请从当前目录重新选择电台。')
      playerStore.togglePlay(false, { intent: 'passive' })
      return attempt
    }
    const playlistUrl = radioMediaUrl(radioStation, 'playlist.m3u8')

    if (!continuation) {
      destroyCurrentHls()
      resetAudioSource()
      // 统一绑定 audio error handler 到当前 attempt。
      // 模板原有的 @error 已移至 bindMainAudioError，确保所有路径（包括原生 HLS）
      // 都能捕获 audio element 的 error 事件。
      bindMainAudioError(attempt)
    }
    if (!isAttemptActive(attempt)) return attempt
    if (!continuation) {
      playerStore.clearPlaybackError()
      playerStore.setLoading(true)
      directStreamMode.value = ''
    }

    if (radioStation && !options.radioTransport) {
      const controller = new AbortController()
      const timer = setTimer(() => controller.abort(), 10_000)
      const removeCleanup = addAttemptCleanup(attempt, () => controller.abort())
      ;(async () => {
        try {
          const response = await fetchImpl(radioResolveUrl(radioStation), { signal: controller.signal, credentials: apiCredentials })
          if (!isAttemptActive(attempt)) return
          if (!response.ok) throw new Error('Radio source resolve failed')
          const resolved = await response.json()
          const transport = String(resolved?.source_type || '').trim().toLowerCase()
          if (!['audio_http', 'hls'].includes(transport)) throw new Error('Unsupported Radio transport')
          if (isAttemptActive(attempt)) loadStation(stationId, { radioTransport: transport, attempt })
        } catch (error) {
          if (!isAttemptActive(attempt)) return
          logger.warn('Radio source resolve failed.', error)
          // A failed resolve is a terminal attempt.  Releasing it lets an
          // explicit Play action start a fresh resolve instead of treating the
          // failed, source-less attempt as still loading.
          invalidateActiveAttempt()
          playerStore.setPlaybackError('电台播放源解析失败，请稍后重试。')
          playerStore.togglePlay(false, { intent: 'passive' })
        } finally {
          clearTimer(timer)
          removeCleanup()
        }
      })()
      return attempt
    }

    if (radioStation && options.radioTransport === 'audio_http') {
      directStreamMode.value = 'proxy'
      if (setMainAudioSrc(attempt, radioMediaUrl(radioStation, 'stream'))) playAudioSafely(attempt)
      return attempt
    }

    if (Hls?.isSupported()) {
      async function startHlsWithFallback() {
        if (!isAttemptActive(attempt)) return
        prepareAttemptMedia(attempt)
        if (!isAttemptActive(attempt)) return

        const hls = new Hls({
          xhrSetup: configureCoreCredentials,
          enableWorker: true,
          lowLatencyMode: true,
          autoStartLoad: true,
          startFragPrefetch: true,
          liveSyncDurationCount: 2,
          liveMaxLatencyDurationCount: 5,
          maxBufferLength: 10,
        })
        attempt.mainHls = hls
        hlsRef.value = hls
        hls.loadSource(playlistUrl)
        hls.attachMedia(audioRef.value)

        const onManifestParsed = () => { if (isAttemptActive(attempt)) playAudioSafely(attempt) }
        const onHlsError = (_event, data) => {
          if (!isAttemptActive(attempt) || !data?.fatal) return
          attempt.intent = 'recovery'
          logger.warn('HLS 播放发生致命错误。', data)

          playerStore.setPlaybackError('HLS 播放发生错误，请稍后重试。')
          playerStore.togglePlay(false, { intent: 'passive' })
        }
        hls.on(Hls.Events.MANIFEST_PARSED, onManifestParsed)
        hls.on(Hls.Events.ERROR, onHlsError)
        addAttemptCleanup(attempt, () => {
          hls.off?.(Hls.Events.MANIFEST_PARSED, onManifestParsed)
          hls.off?.(Hls.Events.ERROR, onHlsError)
        })

      }

      startHlsWithFallback()
      return attempt
    }

    if (audioRef.value.canPlayType('application/vnd.apple.mpegurl')) {
      async function startSafariHls() {
        if (!isAttemptActive(attempt)) return
        if (!isAttemptActive(attempt)) return

        if (!setMainAudioSrc(attempt, playlistUrl)) return
        const onLoaded = () => playAudioSafely(attempt)
        audioRef.value.addEventListener('loadedmetadata', onLoaded, { once: true })
        addAttemptCleanup(attempt, () => audioRef.value?.removeEventListener('loadedmetadata', onLoaded))
      }

      startSafariHls()
      return attempt
    }

    logger.warn('当前浏览器不支持 HLS 播放，或 hls.js 尚未加载完成。')
    if (!isAttemptActive(attempt)) return attempt
    playerStore.setPlaybackError('当前浏览器不支持 HLS 播放。')
    playerStore.togglePlay(false, { intent: 'passive' })
    return attempt
  }

  function stopRadioAttempt() {
    invalidateActiveAttempt()
    destroyCurrentHls()
    resetAudioSource()
    directStreamMode.value = ''
    clearRadioMediaSession()
  }

  function pauseCurrentAudio() {
    if (activeAttempt) {
      activeAttempt.playRequested = false
      activeAttempt.playGeneration += 1
      activeAttempt.playPromise = null
      activeAttempt.played = false
    }
    audioRef.value?.pause()
  }

  function setVolume(nextVolume) {
    if (!audioRef.value) return
    audioRef.value.volume = nextVolume
  }

  function activeAttemptInfo() {
    return activeAttempt
      ? {
          id: activeAttempt.id,
          stationId: activeAttempt.stationId,
          intent: activeAttempt.intent,
          active: isAttemptActive(activeAttempt),
        }
      : null
  }

  return {
    activeAttemptInfo,
    handleAudioError,
    invalidateActiveAttempt,
    loadStation,
    pauseCurrentAudio,
    playAudioSafely,
    setVolume,
    stopRadioAttempt,
    updateMediaSessionForCurrentSubtitle,
  }
}
