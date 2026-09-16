<template>
  <Teleport to="body">
    <Transition name="ios-sheet">
      <div
        v-show="isPlayerExpanded"
        ref="playerRootRef"
        tabindex="-1"
        class="full-player fixed inset-0 z-50 overflow-y-auto"
        :class="{ 'theme-dark': isFullPlayerDark, 'safari-chrome-refresh': isSafariChromeRefreshing, 'full-player--mobile-layout': isMobileLayout, 'full-player--mobile-iptv': isMobileLayout && isIptvMode, 'full-player--theater': isTheaterLayout }"
        @keydown.capture="handlePlayerKeyboard"
        @focusin.capture="handleOverlayFocusIn"
        @focusout.capture="handleOverlayFocusOut"
      >
        <div ref="playerLayoutRef" class="player-layout" :style="playerLayoutStyle">
          <button
            type="button"
            class="desktop-collapse-btn"
            aria-label="收起播放器"
            @click="playerStore.collapsePlayer()"
          >
            <svg viewBox="0 0 24 24" fill="none"><path d="M19 12H5M12 5l-7 7 7 7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <main ref="playerMainRef" class="player-main">
            <section
              ref="mediaSurfaceRef"
              tabindex="-1"
              class="media-card"
              :class="{ 'media-surface--overlay-hidden': isDesktopOverlayHidden }"
              @pointerenter="handleOverlayActivity"
              @pointermove="handleOverlayActivity"
              @mousemove="handleOverlayActivity"
              @pointerdown="handlePlayerPointerDown"
              @touchstart="handlePlayerTouchStart"
              @click.capture="handlePlayerControlClick"
              @pointerleave="scheduleOverlayHide"
            >
              <div class="mobile-live-pill" aria-hidden="true">
                <div class="pill-logo">
                  <img
                    v-if="currentArtworkUrl"
                    :src="currentArtworkUrl"
                    :alt="currentStationName"
                    @error="useDefaultLogo"
                  />
                  <span v-else>{{ currentStationName.slice(0, 1) }}</span>
                </div>
                <span class="mini-eq active"></span>
              </div>

              <button
                type="button"
                class="overlay-btn overlay-back"
                :class="{ hidden: !mobileOverlayVisible }"
                aria-label="收起播放器"
                @click.stop="playerStore.collapsePlayer()"
              >
                <svg viewBox="0 0 24 24" fill="none"><path d="M19 12H5M12 5l-7 7 7 7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>

              <!-- <button
                type="button"
                class="overlay-btn overlay-info"
                aria-label="频道信息"
              >
                <svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="2"/><path d="M12 10.8v5.2M12 7.8h.01" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
              </button> -->

              <video
                v-if="isIptvMode"
                v-show="activeIptvEngine !== 'youtube'"
                ref="iptvVideoRef"
                class="media-video"
                playsinline
                preload="auto"
                :muted="iptvMuted"
                @error="handleIptvError"
                @playing="onVideoEvent('playing')"
                @pause="onVideoEvent('pause')"
                @waiting="onVideoEvent('waiting')"
                @stalled="onVideoStalled"
                @loadedmetadata="updateMediaAspectFromVideo"
                @timeupdate="onVideoTimeUpdate"
              ></video>
              <div
                v-if="isIptvMode"
                ref="youtubeHostRef"
                class="youtube-player-host"
                :class="{ active: activeIptvEngine === 'youtube' }"
              ></div>

              <div v-else class="radio-art-stage">
                <div class="radio-art">
                  <img
                    v-if="currentArtworkUrl"
                    :src="currentArtworkUrl"
                    :alt="currentStationName"
                    @error="useDefaultLogo"
                  />
                  <span v-else>{{ currentStationData?.logoText || '?' }}</span>
                </div>
              </div>

              <Transition name="video-loading">
                <div
                  v-if="showOverlayLoadingSpinner"
                  class="video-loading-indicator"
                  role="status"
                  aria-label="正在加载"
                >
                  <span class="video-loading-spinner" aria-hidden="true"></span>
                </div>
              </Transition>

              <Transition name="video-failure">
                <div
                  v-if="showPlaybackFailureIndicator"
                  class="video-playback-failure"
                  role="alert"
                  :aria-label="overlayPlaybackStatusText"
                >
                  <svg class="video-playback-failure-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.8" />
                    <path d="M12 7.7v5.1M12 16.2h.01" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" />
                  </svg>
                  <span class="video-playback-failure-label">{{ overlayPlaybackStatusText }}</span>
                  <button
                    type="button"
                    class="video-overlay-button video-overlay-button--main video-playback-failure-retry"
                    aria-label="重新播放"
                    @click.stop="retryPlayback"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path d="M20 11a8 8 0 1 1-2.34-5.66L20 7.68" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
                      <path d="M20 4v3.68h-3.68" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </button>
                </div>
              </Transition>

              <div
                class="video-overlay desktop-video-overlay"
                :class="{ 'is-hidden': isDesktopOverlayHidden }"
                aria-label="播放控制"
              >
                <div
                  v-if="isIptvMode"
                  class="program-progress video-overlay-progress"
                  :class="{ empty: !hasCurrentEpgProgram }"
                  role="progressbar"
                  aria-label="节目进度"
                  aria-valuemin="0"
                  aria-valuemax="100"
                  :aria-valuenow="hasCurrentEpgProgram ? currentProgram.progress : 0"
                >
                  <div class="progress-track">
                    <div v-if="hasCurrentEpgProgram" class="progress-fill" :style="{ width: currentProgramProgressPercent }"></div>
                  </div>
                  <div v-if="hasCurrentEpgProgram" class="progress-times">
                    <span>{{ currentProgram.start }}</span>
                    <span>{{ currentProgram.end }}</span>
                  </div>
                </div>

                <div class="video-overlay-controls">
                  <div class="video-overlay-transport">
                    <button type="button" class="video-overlay-button video-overlay-button--side" aria-label="上一个" @click="playPrev">
                      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M5.5 5.5h2.6v13H5.5zm4.8 6.5 8.2 6.1V5.9z"/></svg>
                    </button>
                    <button
                      type="button"
                      class="video-overlay-button video-overlay-button--main"
                      :aria-label="isPlaying ? '暂停' : '播放'"
                      @click="playerStore.togglePlay()"
                    >
                      <svg v-if="isPlaying" viewBox="0 0 24 24" fill="none"><path d="M8.5 5.5v13M15.5 5.5v13" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></svg>
                      <svg v-else viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.6v12.8c0 .75.83 1.2 1.46.78l9.65-6.39a.95.95 0 0 0 0-1.58L9.46 4.82A.94.94 0 0 0 8 5.6Z"/></svg>
                    </button>
                    <button type="button" class="video-overlay-button video-overlay-button--side" aria-label="下一个" @click="playNext">
                      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.9 5.5h2.6v13h-2.6zM5.5 18.1l8.2-6.1-8.2-6.1z"/></svg>
                    </button>
                    <span
                      class="video-overlay-status"
                      :class="overlayPlaybackStateClass"
                      role="status"
                      aria-live="polite"
                    >
                      {{ overlayPlaybackStatusText }}
                    </span>
                  </div>

                  <div class="video-overlay-utility">
                    <div class="video-overlay-volume-panel">
                      <button
                        type="button"
                        class="video-overlay-button video-overlay-button--utility"
                        :aria-label="overlayVolumeButtonLabel"
                        @click="toggleOverlayMute"
                      >
                        <svg v-if="volumeIconState === 'muted'" viewBox="0 0 24 24" fill="none">
                          <path d="M4 10v4a1 1 0 0 0 1 1h3l4.2 3.15A.5.5 0 0 0 13 17.75V6.25a.5.5 0 0 0-.8-.4L8 9H5a1 1 0 0 0-1 1Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
                          <path d="m21 3-18 18" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
                        </svg>
                        <svg v-else-if="volumeIconState === 'low'" viewBox="0 0 24 24" fill="none">
                          <path d="M4 10v4a1 1 0 0 0 1 1h3l4.2 3.15A.5.5 0 0 0 13 17.75V6.25a.5.5 0 0 0-.8-.4L8 9H5a1 1 0 0 0-1 1Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
                          <path d="M16 10a3 3 0 0 1 0 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                        </svg>
                        <svg v-else viewBox="0 0 24 24" fill="none">
                          <path d="M4 10v4a1 1 0 0 0 1 1h3l4.2 3.15A.5.5 0 0 0 13 17.75V6.25a.5.5 0 0 0-.8-.4L8 9H5a1 1 0 0 0-1 1Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
                          <path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                        </svg>
                      </button>

                      <div v-if="!isIOS" class="video-overlay-volume">
                        <input
                          type="range"
                          min="0"
                          max="1"
                          step="0.01"
                          :value="effectiveVolume"
                          aria-label="音量"
                          @pointerdown="beginOverlayPinnedInteraction"
                          @pointerup="endOverlayPinnedInteraction"
                          @pointercancel="endOverlayPinnedInteraction"
                          @focus="beginOverlayPinnedInteraction"
                          @blur="endOverlayPinnedInteraction"
                          @input="setOverlayVolume($event.target.value)"
                        />
                      </div>
                    </div>

                    <button
                      v-if="isIptvMode && iptvSourceOptions.length"
                      ref="desktopSourceButtonRef"
                      type="button"
                      class="video-overlay-button video-overlay-button--utility"
                      :aria-expanded="sourceMenuOpen"
                      aria-controls="iptv-source-menu"
                      aria-label="切换播放源"
                      @click.stop="toggleSourceMenu"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 7 7.5-3 7.5 3-7.5 3-7.5-3Z"/><path d="m4.5 12 7.5 3 7.5-3"/><path d="m4.5 17 7.5 3 7.5-3"/></svg>
                    </button>
                    <button
                      v-if="!isIptvMode && currentRadioSourceOptions.length > 1"
                      ref="desktopSourceButtonRef"
                      type="button"
                      class="video-overlay-button video-overlay-button--utility"
                      :aria-expanded="sourceMenuOpen"
                      aria-controls="radio-source-menu"
                      aria-label="切换电台播放源"
                      @click.stop="toggleSourceMenu"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 7 7.5-3 7.5 3-7.5 3-7.5-3Z"/><path d="m4.5 12 7.5 3 7.5-3"/><path d="m4.5 17 7.5 3 7.5-3"/></svg>
                    </button>

                    <button
                      v-if="isDesktopLayout"
                      type="button"
                      class="video-overlay-button video-overlay-button--utility"
                      :class="{ active: isRailLayout, disabled: isFullscreen }"
                      :aria-expanded="isRailLayout"
                      :aria-disabled="isFullscreen"
                      :disabled="isFullscreen"
                      :title="isFullscreen ? '全屏中不可显示频道列表' : (isRailLayout ? '隐藏频道列表' : '显示频道列表')"
                      :aria-label="isFullscreen ? '全屏中不可显示频道列表' : (isRailLayout ? '隐藏频道列表' : '显示频道列表')"
                      @click="toggleDesktopRail"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 6h2M5 12h2M5 18h2M11 6h8M11 12h8M11 18h8"/></svg>
                    </button>

                    <button type="button" class="video-overlay-button video-overlay-button--utility" aria-label="全屏" :aria-pressed="isFullscreen" @click="toggleFullscreen">
                      <svg viewBox="0 0 24 24" fill="none"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" fill="currentColor"/></svg>
                    </button>
                  </div>
                </div>
              </div>
              <div
                v-if="isMobileLayout"
                class="video-overlay mobile-video-overlay"
                :class="{ 'is-hidden': isMobileOverlayHidden, 'is-loading': isLoading, 'is-switching': isSourceSwitching }"
                aria-label="移动端播放控制"
              >
                <div
                  v-if="isIptvMode && hasCurrentEpgProgram"
                  class="program-progress mobile-video-overlay-progress"
                  role="progressbar"
                  aria-label="节目进度"
                  aria-valuemin="0"
                  aria-valuemax="100"
                  :aria-valuenow="currentProgram.progress"
                >
                  <div class="progress-track">
                    <div class="progress-fill" :style="{ width: currentProgramProgressPercent }"></div>
                  </div>
                  <div class="progress-times">
                    <span>{{ currentProgram.start }}</span>
                    <span>{{ currentProgram.end }}</span>
                  </div>
                </div>

                <div class="mobile-video-overlay-transport">
                  <button type="button" class="mobile-video-overlay-button mobile-video-overlay-button--side" aria-label="上一个" @click="playPrev">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M5.5 5.5h2.6v13H5.5zm4.8 6.5 8.2 6.1V5.9z"/></svg>
                  </button>
                  <button
                    type="button"
                    class="mobile-video-overlay-button mobile-video-overlay-button--main"
                    :aria-label="isPlaying ? '暂停' : '播放'"
                    @click="playerStore.togglePlay()"
                  >
                    <svg v-if="isPlaying" viewBox="0 0 24 24" fill="none"><path d="M8.5 5.5v13M15.5 5.5v13" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></svg>
                    <svg v-else viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.6v12.8c0 .75.83 1.2 1.46.78l9.65-6.39a.95.95 0 0 0 0-1.58L9.46 4.82A.94.94 0 0 0 8 5.6Z"/></svg>
                  </button>
                  <button type="button" class="mobile-video-overlay-button mobile-video-overlay-button--side" aria-label="下一个" @click="playNext">
                    <svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.9 5.5h2.6v13h-2.6zM5.5 18.1l8.2-6.1-8.2-6.1z"/></svg>
                  </button>
                </div>

                <div class="mobile-video-overlay-bottom">
                  <span
                    class="mobile-video-overlay-status"
                    :class="overlayPlaybackStateClass"
                    role="status"
                    aria-live="polite"
                  >
                    {{ overlayPlaybackStatusText }}
                  </span>
                  <div class="mobile-video-overlay-utility">
                    <button
                      type="button"
                      class="mobile-video-overlay-button mobile-video-overlay-button--utility"
                      :aria-label="iptvMuted ? '取消静音' : '静音'"
                      @click="toggleMute()"
                    >
                      <svg v-if="iptvMuted" viewBox="0 0 24 24" fill="none"><path d="M4 10v4a1 1 0 0 0 1 1h3l4.2 3.15A.5.5 0 0 0 13 17.75V6.25a.5.5 0 0 0-.8-.4L8 9H5a1 1 0 0 0-1 1Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m21 3-18 18" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>
                      <svg v-else viewBox="0 0 24 24" fill="none"><path d="M4 10v4a1 1 0 0 0 1 1h3l4.2 3.15A.5.5 0 0 0 13 17.75V6.25a.5.5 0 0 0-.8-.4L8 9H5a1 1 0 0 0-1 1Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
                    </button>
                    <button
                      v-if="isIptvMode && iptvSourceOptions.length"
                      ref="sourceButtonRef"
                      type="button"
                      class="mobile-video-overlay-button mobile-video-overlay-button--utility"
                      :aria-expanded="sourceMenuOpen"
                      aria-controls="iptv-source-menu"
                      aria-label="切换播放源"
                      @click.stop="toggleSourceMenu"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 7 7.5-3 7.5 3-7.5 3-7.5-3Z"/><path d="m4.5 12 7.5 3 7.5-3"/><path d="m4.5 17 7.5 3 7.5-3"/></svg>
                    </button>
                    <button
                      v-if="!isIptvMode && currentRadioSourceOptions.length > 1"
                      ref="sourceButtonRef"
                      type="button"
                      class="mobile-video-overlay-button mobile-video-overlay-button--utility"
                      :aria-expanded="sourceMenuOpen"
                      aria-controls="radio-source-menu"
                      aria-label="切换电台播放源"
                      @click.stop="toggleSourceMenu"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 7 7.5-3 7.5 3-7.5 3-7.5-3Z"/><path d="m4.5 12 7.5 3 7.5-3"/><path d="m4.5 17 7.5 3 7.5-3"/></svg>
                    </button>
                    <button type="button" class="mobile-video-overlay-button mobile-video-overlay-button--utility" aria-label="全屏" :aria-pressed="isFullscreen" @click="toggleFullscreen">
                      <svg viewBox="0 0 24 24" fill="none"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" fill="currentColor"/></svg>
                    </button>
                  </div>
                </div>
              </div>
            </section>

            <section ref="nowPanelRef" class="now-panel">
              <div v-if="isMobileLayout && isIptvMode" class="mobile-now-playing">
                <span class="mobile-now-playing__logo">
                  <img v-if="currentArtworkUrl" :src="currentArtworkUrl" :alt="currentStationName" @error="useDefaultLogo" />
                  <span v-else>{{ currentStationName.slice(0, 2) }}</span>
                </span>
                <div class="mobile-now-playing__copy">
                  <div class="mobile-now-playing__identity">
                    <h1>{{ currentStationName }}</h1>
                    <span class="mobile-now-playing__state">{{ currentChannelSubtitle }}</span>
                  </div>
                  <p v-if="hasProgrammeMetadata" class="mobile-now-playing__programme" aria-live="polite">
                    <span class="mobile-now-playing__current">{{ epgViewingText }}</span>
                    <span v-if="nextProgramSummary" class="mobile-now-playing__next">下一节目 · {{ nextProgramSummary }}</span>
                    <span v-if="showProgramRemaining" class="mobile-now-playing__remaining">剩余 {{ currentProgram.remaining }} 分钟</span>
                  </p>
                </div>
              </div>
              <div v-else class="now-metadata" :class="{ 'now-metadata--without-programme': !hasProgrammeMetadata }">
                <div class="now-identity">
                  <h1>{{ currentStationName }}</h1>
                  <p class="channel-subtitle">{{ currentChannelSubtitle }}</p>
                </div>
                <div v-if="hasProgrammeMetadata" class="now-programme">
                  <p
                    class="now-program-title"
                    :class="`now-program-title--${epgViewingState}`"
                    aria-live="polite"
                  >
                    {{ epgViewingText }}
                  </p>
                  <p v-if="nextProgramSummary" class="now-program-next mobile-now-program-next">
                    下一节目 · {{ nextProgramSummary }}
                  </p>
                  <p v-if="showProgramRemaining" class="now-program-remaining mobile-now-program-remaining">
                    剩余 {{ currentProgram.remaining }} 分钟
                  </p>
                  <div v-if="nextProgramSummary" class="desktop-now-program-next" aria-label="下一节目">
                    <span class="desktop-now-program-next__label">下一节目</span>
                    <span class="desktop-now-program-next__title">{{ nextProgramSummary }}</span>
                  </div>
                  <p v-if="showProgramRemaining" class="now-program-remaining desktop-now-program-meta">
                    剩余 {{ currentProgram.remaining }} 分钟
                  </p>
                </div>
              </div>
            </section>

            <section class="mobile-panel">
              <div v-if="isMobileLayout && isIptvMode" class="mobile-panel-header">
                <div class="panel-tabs" ref="mobileTabsRef">
                  <button
                    type="button"
                    :class="{ active: activePlayerPanel === 'channels' }"
                    @click="activePlayerPanel = 'channels'"
                  >
                    频道列表
                  </button>
                  <button
                    type="button"
                    :class="{ active: activePlayerPanel === 'schedule' }"
                    @click="activePlayerPanel = 'schedule'"
                  >
                    节目单
                  </button>
                  <span class="tab-indicator" :style="mobileTabIndicatorStyle"></span>
                </div>
                <div class="mobile-panel-sort">
                  <button
                    type="button"
                    class="sort-btn"
                    :class="{ active: channelSortMode !== 'original' }"
                    @click="nextSortMode"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" aria-hidden="true">
                      <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="16" y2="12"/><line x1="4" y1="18" x2="12" y2="18"/>
                    </svg>
                    {{ currentSortLabel }}
                  </button>
                </div>
              </div>
              <div v-else class="panel-tabs" ref="mobileTabsRef">
                <button
                  type="button"
                  :class="{ active: activePlayerPanel === 'channels' }"
                  @click="activePlayerPanel = 'channels'"
                >
                  频道列表
                </button>
                <button
                  type="button"
                  :class="{ active: activePlayerPanel === 'schedule' }"
                  @click="activePlayerPanel = 'schedule'"
                >
                  节目单
                </button>
                <span class="tab-indicator" :style="mobileTabIndicatorStyle"></span>
              </div>

              <Transition name="panel-slide" mode="out-in">
                <div v-if="activePlayerPanel === 'channels'" key="channels" class="channel-panel">
                  <div v-if="!(isMobileLayout && isIptvMode)" class="channel-sort-bar">
                    <button
                      type="button"
                      class="sort-btn"
                      :class="{ active: channelSortMode !== 'original' }"
                      @click="nextSortMode"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
                        <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="16" y2="12"/><line x1="4" y1="18" x2="12" y2="18"/>
                      </svg>
                      {{ currentSortLabel }}
                    </button>
                  </div>
                  <button
                    v-for="item in displayChannelRows"
                    :key="item.key"
                    type="button"
                    class="channel-row"
                    :class="{ active: item.active, disabled: item.disabled }"
                    :disabled="item.disabled"
                    @click="handleChannelRowClick(item)"
                  >
                    <span class="channel-logo">
                      <img v-if="item.logo" :src="item.logo" :alt="item.name" @error="useDefaultLogo" />
                      <span v-else>{{ item.name.slice(0, 2) }}</span>
                    </span>
                    <span class="channel-copy">
                      <span class="channel-title">
                        {{ item.name }}
                        <span v-if="item.live" class="live-dot"></span>
                      </span>
                      <span class="channel-subtitle">{{ item.summary }}</span>
                    </span>
                    <svg
                      v-if="item.playing"
                      class="eq-icon active"
                      viewBox="0 0 24 24"
                      aria-hidden="true"
                    >
                      <rect x="4" y="8" width="2.5" height="8" rx="1.25" />
                      <rect x="8.5" y="5" width="2.5" height="14" rx="1.25" />
                      <rect x="13" y="3" width="2.5" height="18" rx="1.25" />
                      <rect x="17.5" y="6" width="2.5" height="12" rx="1.25" />
                    </svg>
                  </button>
                </div>

                <div v-else key="schedule" class="schedule-panel" :class="{ 'schedule-panel--empty': !hasScheduleData }">
                  <div v-if="hasScheduleData" class="schedule-content">
                    <div v-if="epgDateOptions.length" class="schedule-date-list">
                      <button
                        v-for="item in epgDateOptions"
                        :key="item.value"
                        type="button"
                        class="schedule-date-chip"
                        :class="{ active: item.active }"
                        @click="selectEpgDate(item.value)"
                      >
                        <span>{{ item.label }}</span>
                        <small>{{ item.weekday }}</small>
                      </button>
                    </div>
                    <div v-else class="schedule-date">
                      <span>今天</span>
                    </div>
                    <div class="timeline">
                      <div v-for="program in displaySchedule" :key="`${program.time}-${program.title}`" class="timeline-row" :class="{ current: program.current, past: program.past }">
                        <span class="timeline-time">{{ program.time }}</span>
                        <span class="timeline-dot"></span>
                        <span class="timeline-title">
                          {{ program.title }}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </Transition>
            </section>
          </main>

          <aside class="side-panel">
            <div class="panel-tabs" ref="desktopTabsRef">
              <button
                type="button"
                :class="{ active: activePlayerPanel === 'channels' }"
                @click="activePlayerPanel = 'channels'"
              >
                频道列表
              </button>
              <button
                type="button"
                :class="{ active: activePlayerPanel === 'schedule' }"
                @click="activePlayerPanel = 'schedule'"
              >
                节目单
              </button>
              <span class="tab-indicator" :style="desktopTabIndicatorStyle"></span>
            </div>

            <Transition name="panel-slide" mode="out-in">
              <div v-if="activePlayerPanel === 'channels'" key="channels" class="channel-panel desktop-panel-scroll">
                <div class="channel-sort-bar">
                  <button
                    type="button"
                    class="sort-btn"
                    :class="{ active: channelSortMode !== 'original' }"
                    @click="nextSortMode"
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
                      <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="16" y2="12"/><line x1="4" y1="18" x2="12" y2="18"/>
                    </svg>
                    {{ currentSortLabel }}
                  </button>
                </div>
                <button
                  v-for="item in displayChannelRows"
                  :key="item.key"
                  type="button"
                  class="channel-row"
                  :class="{ active: item.active, disabled: item.disabled }"
                  :disabled="item.disabled"
                  @click="handleChannelRowClick(item)"
                >
                  <span class="channel-logo" :class="`channel-logo--${railLogoVisualMode(item)}`">
                    <img
                      v-if="item.logo"
                      :src="item.logo"
                      :alt="item.name"
                      :class="`channel-logo-image--${railLogoVisualMode(item)}`"
                      @load="classifyRailLogo(item, $event)"
                      @error="useDefaultLogo"
                    />
                    <span v-else>{{ item.name.slice(0, 2) }}</span>
                  </span>
                  <span class="channel-copy">
                    <span class="channel-title">
                      <span class="channel-title-text">{{ item.name }}</span>
                      <span v-if="item.live" class="live-dot"></span>
                    </span>
                    <span class="channel-subtitle">{{ item.summary }}</span>
                  </span>
                  <svg
                    v-if="item.playing"
                    class="eq-icon active"
                    viewBox="0 0 24 24"
                    aria-hidden="true"
                  >
                    <rect x="4" y="8" width="2.5" height="8" rx="1.25" />
                    <rect x="8.5" y="5" width="2.5" height="14" rx="1.25" />
                    <rect x="13" y="3" width="2.5" height="18" rx="1.25" />
                    <rect x="17.5" y="6" width="2.5" height="12" rx="1.25" />
                  </svg>
                </button>
              </div>

              <div v-else key="schedule" class="schedule-panel desktop-panel-scroll">
                <div v-if="hasScheduleData" class="schedule-content">
                  <div v-if="epgDateOptions.length" class="schedule-date-list">
                    <button
                      v-for="item in epgDateOptions"
                      :key="item.value"
                      type="button"
                      class="schedule-date-chip"
                      :class="{ active: item.active }"
                      @click="selectEpgDate(item.value)"
                    >
                      <span>{{ item.label }}</span>
                      <small>{{ item.weekday }}</small>
                    </button>
                  </div>
                  <div v-else class="schedule-date">
                    <span>今天</span>
                  </div>
                  <div class="timeline">
                    <div v-for="program in displaySchedule" :key="`${program.time}-${program.title}`" class="timeline-row" :class="{ current: program.current, past: program.past }">
                      <span class="timeline-time">{{ program.time }}</span>
                      <span class="timeline-dot"></span>
                      <span class="timeline-title">
                        {{ program.title }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </Transition>
          </aside>
        </div>
      </div>
    </Transition>
  </Teleport>

  <Teleport :to="sourceMenuTeleportTarget">
    <Transition name="fade">
      <div
        v-show="sourceMenuOpen"
        :id="isIptvMode ? 'iptv-source-menu' : 'radio-source-menu'"
        class="fixed z-[80] overflow-hidden rounded-2xl border border-neutral-200 bg-white/95 p-1.5 text-left shadow-xl shadow-black/10 backdrop-blur dark:border-white/10 dark:bg-neutral-800/95"
        :class="{ 'source-menu-in-fullscreen': isFullscreen }"
        :style="sourceMenuStyle"
        @click.stop
      >
        <div class="flex items-center justify-between px-2.5 py-2">
          <span class="text-xs font-medium text-neutral-400 dark:text-neutral-500">播放源</span>
          <span class="text-xs text-neutral-400 dark:text-neutral-500">{{ isIptvMode ? currentIptvSourceLabel : currentRadioSourceLabel }}</span>
        </div>
        <div class="overscroll-contain overflow-y-auto [-webkit-overflow-scrolling:touch]" :style="{ maxHeight: sourceMenuListMaxHeight }">
          <button
            v-for="source in iptvSourceOptions"
            :key="source.identityKey || `${source.index}-${source.url}`"
            type="button"
            class="flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left transition-colors hover:bg-neutral-100 dark:hover:bg-neutral-700/70"
            :class="{
              'bg-neutral-100 dark:bg-neutral-700/70': source.active,
              'cursor-not-allowed opacity-45 hover:bg-transparent dark:hover:bg-transparent': source.disabled,
            }"
            :disabled="source.disabled"
            @click="switchIptvSource(source.identityKey)"
          >
            <span
              class="size-2.5 shrink-0 rounded-full"
              :class="source.statusClass"
            ></span>
            <span
              class="flex h-6 w-10 shrink-0 items-center justify-center rounded-full text-[0.68rem] font-medium"
              :class="source.type === 'proxy'
                ? 'bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300'
                : 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300'"
            >
              {{ source.typeLabel }}
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium text-neutral-800 dark:text-neutral-100">
                {{ source.title }}
              </p>
              <p class="mt-0.5 truncate text-xs text-neutral-400 dark:text-neutral-500">
                {{ source.meta }}
              </p>
            </div>
          </button>
          <button
            v-for="source in (isIptvMode ? [] : currentRadioSourceOptions)"
            :key="source.source_id"
            type="button"
            class="flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left transition-colors hover:bg-neutral-100 dark:hover:bg-neutral-700/70"
            :class="{ 'bg-neutral-100 dark:bg-neutral-700/70': source.active }"
            @click="switchRadioSource(source.source_id)"
          >
            <span class="size-2.5 shrink-0 rounded-full bg-emerald-500"></span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium text-neutral-800 dark:text-neutral-100">{{ source.title }}</p>
              <p class="mt-0.5 truncate text-xs text-neutral-400 dark:text-neutral-500">{{ source.meta }}</p>
            </div>
          </button>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { computed, ref, watch, onBeforeUnmount, onMounted, nextTick } from 'vue'
import { storeToRefs } from 'pinia'
import mpegts from 'mpegts.js'
import Hls from 'hls.js'
import { usePlayerStore } from '../stores/player'
import { fetchAggregatedChannels } from '../api/iptv'
import { useEpg } from '../composables/useEpg'
import { useLogoVisual } from '../composables/useLogoVisual'
import { formatEpgClock } from '../utils/epgViewing'
import { API_BASE } from '../apiBase'
import { publicAsset } from '../publicAsset'
import { calculateFullPlayerSizing } from '../utils/fullPlayerSizing'
import {
  extractSourceIdFromUrl,
  channelIdentity,
  isChannelAllNotLive,
  isChannelAllUnsupported,
  isChannelAllUrlsBlocked,
  isSourceExplicitlyDisabled,
  sourceRaceKey,
  sourceTransport,
  startupRaceCandidateKind,
} from '../utils/sourceIdentity'
import { useToastStore } from '../stores/toast'
import { IPTV_CHANNEL_SORT_MODES, sortIptvChannels } from '../utils/iptvChannelList'

const playerStore = usePlayerStore()
const { isPlayerExpanded, currentStation, isPlaying, isLoading, volume, isMuted, stationMap, stationList } = storeToRefs(playerStore)
const toastStore = useToastStore()
const {
  logoVisualMode: railLogoVisualMode,
  classifyLogo: classifyRailLogo,
} = useLogoVisual({
  getLogoUrl: (item) => item?.logo,
  getDisplayName: (item) => item?.name,
  getIdentityKey: (item) => item?.key || item?.name,
  getVisualKey: (item) => `${item?.key || item?.name}|${item?.logo || ''}`,
  enableWide: true,
})

const iptvVideoRef = ref(null)
const iptvHlsRef = ref(null)
const iptvMpegtsRef = ref(null)
const youtubeHostRef = ref(null)
const playerRootRef = ref(null)
const playerLayoutRef = ref(null)
const playerMainRef = ref(null)
const mediaSurfaceRef = ref(null)
const nowPanelRef = ref(null)
const activeIptvEngine = ref('video')
const sourceButtonRef = ref(null)
const desktopSourceButtonRef = ref(null)
const sourceMenuOpen = ref(false)
const isSourceSwitching = ref(false)
const sourceMenuStyle = ref({
  left: '12px',
  top: '64px',
  width: 'calc(100vw - 24px)',
  maxHeight: '320px',
})
const sourceMenuListMaxHeight = ref('260px')
const iptvSourceRuntimeStatus = ref({})
const activePlayerPanel = ref('channels')
const channelSortMode = computed(() => playerStore.iptvChannelSortMode)
const mobileTabsRef = ref(null)
const desktopTabsRef = ref(null)
const tabIndicatorRevision = ref(0)
const DEFAULT_MEDIA_ASPECT_VALUE = 16 / 9
const DEFAULT_MEDIA_ASPECT_RATIO = '16 / 9'
const ULTRAWIDE_MEDIA_ASPECT = 2
const MAX_ADAPTIVE_LANDSCAPE_ASPECT = 2.4
const KEYBOARD_VOLUME_STEP = 0.05

function tabIndicatorStyle(tabsRef) {
  tabIndicatorRevision.value
  if (!tabsRef) return { opacity: 0 }
  const buttons = tabsRef.querySelectorAll('button')
  const idx = activePlayerPanel.value === 'channels' ? 0 : 1
  const btn = buttons[idx]
  if (!btn || btn.offsetWidth <= 0) return { opacity: 0 }
  const isDesktopRailTabs = tabsRef === desktopTabsRef.value
  const indicatorInset = isDesktopRailTabs ? 10 : 0
  return {
    transform: `translateX(${btn.offsetLeft + indicatorInset}px)`,
    width: `${Math.max(28, btn.offsetWidth - indicatorInset * 2)}px`,
    opacity: 1,
  }
}

const mobileTabIndicatorStyle = computed(() => tabIndicatorStyle(mobileTabsRef.value))
const desktopTabIndicatorStyle = computed(() => tabIndicatorStyle(desktopTabsRef.value))
let tabIndicatorRaf = 0

function scheduleTabIndicatorUpdate() {
  if (tabIndicatorRaf) return
  tabIndicatorRaf = window.requestAnimationFrame(() => {
    tabIndicatorRaf = window.requestAnimationFrame(() => {
      tabIndicatorRaf = 0
      tabIndicatorRevision.value += 1
    })
  })
}

function nextSortMode() {
  const idx = IPTV_CHANNEL_SORT_MODES.findIndex(m => m.key === channelSortMode.value)
  playerStore.setIptvChannelSortMode(IPTV_CHANNEL_SORT_MODES[(idx + 1) % IPTV_CHANNEL_SORT_MODES.length].key)
}
const currentSortLabel = computed(() => IPTV_CHANNEL_SORT_MODES.find(m => m.key === channelSortMode.value)?.label || '默认排序')
const epgNow = ref(Date.now())
const mobileOverlayVisible = ref(true)
const mediaAspectRatio = ref(DEFAULT_MEDIA_ASPECT_RATIO)
const mediaAspectValue = ref(DEFAULT_MEDIA_ASPECT_VALUE)
const mediaStageAspectRatio = ref(null)
const mediaFrameWidth = ref(null)
const mediaFrameHeight = ref(null)
const sidePanelWidth = ref(null)
const playerLayoutWidth = ref(null)
const layoutMode = ref('desktop')
const desktopRailPreference = ref('auto')
const desktopRailLayout = ref('theater')
const DEFAULT_LOGO_URL = publicAsset('/logos/default.png')
const isFullPlayerDark = ref(document.documentElement.classList.contains('dark'))
const isSafariChromeRefreshing = ref(false)
const isFullscreen = ref(false)
const desktopOverlayVisible = ref(true)
const overlayControlsFocused = ref(false)
const overlayVolumeInteracting = ref(false)
let themeObserver = null
let epgTickTimer = null
let epgRefreshAfterEndTimer = null
let mobileOverlayTimer = null
let overlayLoadingTimer = null
let mediaLayoutObserver = null
let mediaLayoutRaf = 0
let hoverCapabilityQuery = null
let finePointerCapabilityQuery = null
let overlayHideTimer = null
let lastOverlayInputModality = 'pointer'
let pendingPointerFullscreenTrigger = null
let pendingPointerCommandControl = null
let pointerCommandFocusCleanupTimer = null

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
playerStore.initializeMuted(isIOS)  // iOS 静音绕过自动播放限制
const iptvMuted = isMuted
const lastNonZeroVolume = ref(Number(volume.value) > 0 ? Number(volume.value) : 1)

const effectiveVolume = computed(() => (
  iptvMuted.value ? 0 : Math.min(1, Math.max(0, Number(volume.value) || 0))
))

const volumeIconState = computed(() => {
  const current = effectiveVolume.value
  if (current <= 0) return 'muted'
  if (current < 0.5) return 'low'
  return 'high'
})

const overlayVolumeButtonLabel = computed(() => (
  effectiveVolume.value > 0 ? '静音' : '取消静音'
))

const playerLayoutStyle = computed(() => {
  const style = {
    '--media-aspect-ratio': mediaAspectRatio.value,
  }
  if (mediaStageAspectRatio.value) style['--stage-aspect-ratio'] = mediaStageAspectRatio.value
  if (mediaFrameWidth.value) style['--media-frame-width'] = `${mediaFrameWidth.value.toFixed(3)}px`
  if (mediaFrameHeight.value) style['--media-height'] = `${mediaFrameHeight.value.toFixed(3)}px`
  if (sidePanelWidth.value) style['--side-panel-width'] = `${sidePanelWidth.value.toFixed(3)}px`
  if (playerLayoutWidth.value) style['--layout-width'] = `${playerLayoutWidth.value.toFixed(3)}px`
  return style
})

const isMobileLayout = computed(() => layoutMode.value === 'mobile')
const isDesktopLayout = computed(() => layoutMode.value === 'desktop')
const isRailLayout = computed(() => isDesktopLayout.value && desktopRailLayout.value === 'rail')
const isTheaterLayout = computed(() => isDesktopLayout.value && desktopRailLayout.value === 'theater')
const sourceMenuTeleportTarget = computed(() => (
  isFullscreen.value && mediaSurfaceRef.value ? mediaSurfaceRef.value : 'body'
))

function useDefaultLogo(event) {
  const img = event?.target
  if (!img || img.dataset.logoFallback === '1') return
  img.dataset.logoFallback = '1'
  img.src = DEFAULT_LOGO_URL
}

function toggleMute() {
  playerStore.setMuted(!iptvMuted.value)
  syncCurrentAudioMute()
}

function syncCurrentAudioMute() {
  if (activeIptvEngine.value === 'youtube') {
    syncYoutubeAudioState()
    return
  }
  if (iptvVideoRef.value) iptvVideoRef.value.muted = iptvMuted.value
}

function setOverlayVolume(value) {
  const next = Math.min(1, Math.max(0, Number(value) || 0))
  if (next > 0) {
    lastNonZeroVolume.value = next
    playerStore.setVolume(next)
    if (iptvMuted.value) playerStore.setMuted(false)
  } else {
    playerStore.setVolume(0)
    playerStore.setMuted(true)
  }
  syncCurrentAudioMute()
}

function toggleOverlayMute() {
  if (effectiveVolume.value > 0) {
    lastNonZeroVolume.value = effectiveVolume.value
    playerStore.setMuted(true)
  } else {
    const restore = Math.min(1, Math.max(0.01, Number(lastNonZeroVolume.value) || 1))
    playerStore.setVolume(restore)
    playerStore.setMuted(false)
  }
  syncCurrentAudioMute()
}

function supportsCustomFullscreen(target = mediaSurfaceRef.value) {
  return document.fullscreenEnabled === true
    && typeof target?.requestFullscreen === 'function'
    && typeof document.exitFullscreen === 'function'
}

async function toggleFullscreen() {
  if (document.fullscreenElement) {
    pendingPointerFullscreenTrigger = null
    if (typeof document.exitFullscreen !== 'function') {
      isFullscreen.value = false
      return
    }
    try {
      await document.exitFullscreen()
    } catch {
      // The platform remains authoritative after an exit failure.
    }
    handleFullscreenChange()
    return
  }

  isFullscreen.value = false
  const target = mediaSurfaceRef.value
  if (supportsCustomFullscreen(target)) {
    try {
      await target.requestFullscreen()
      handleFullscreenChange()
      return
    } catch {
      pendingPointerFullscreenTrigger = null
      isFullscreen.value = false
    }
  }

  // iOS versions without Element.requestFullscreen only expose the native
  // video fullscreen API. Keep this as a feature-detected fallback.
  pendingPointerFullscreenTrigger = null
  const video = iptvVideoRef.value
  if (typeof video?.webkitEnterFullscreen === 'function') {
    try {
      video.webkitEnterFullscreen()
    } catch {
      // Native fullscreen support is optional and may reject independently.
    }
  }
  handleFullscreenChange()
} // 跟踪当前播放的 URL，防止重复设置

function handleFullscreenChange() {
  const fullscreenElement = document.fullscreenElement
  isFullscreen.value = Boolean(
    supportsCustomFullscreen()
    && fullscreenElement
    && mediaSurfaceRef.value
    && (fullscreenElement === mediaSurfaceRef.value || mediaSurfaceRef.value.contains(fullscreenElement)),
  )
  if (isFullscreen.value) {
    const trigger = pendingPointerFullscreenTrigger
    pendingPointerFullscreenTrigger = null
    if (lastOverlayInputModality === 'pointer' && trigger && document.activeElement === trigger) {
      focusPlayerShortcutScope()
    } else if (!mediaSurfaceRef.value?.contains?.(document.activeElement)) {
      focusPlayerShortcutScope()
    }
    handleOverlayActivity()
  } else {
    pendingPointerFullscreenTrigger = null
    if (document.activeElement === mediaSurfaceRef.value) {
      focusPlayerShortcutScope()
    }
    scheduleOverlayHide()
  }
  if (sourceMenuOpen.value) nextTick(() => updateSourceMenuPosition())
}

function resetMediaAspect() {
  mediaAspectRatio.value = DEFAULT_MEDIA_ASPECT_RATIO
  mediaAspectValue.value = DEFAULT_MEDIA_ASPECT_VALUE
  scheduleMediaFrameSizeUpdate()
}

function adaptiveMediaAspect(width, height) {
  const aspect = width / height
  if (!Number.isFinite(aspect) || aspect <= 0) return DEFAULT_MEDIA_ASPECT_VALUE
  if (aspect < ULTRAWIDE_MEDIA_ASPECT) return DEFAULT_MEDIA_ASPECT_VALUE
  return Math.min(MAX_ADAPTIVE_LANDSCAPE_ASPECT, aspect)
}

function formatAspectRatio(aspect) {
  return `${aspect.toFixed(6)} / 1`
}

function setMediaAspect(width, height) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return
  const aspect = adaptiveMediaAspect(width, height)
  mediaAspectValue.value = aspect
  mediaAspectRatio.value = aspect === DEFAULT_MEDIA_ASPECT_VALUE
    ? DEFAULT_MEDIA_ASPECT_RATIO
    : formatAspectRatio(aspect)
  scheduleMediaFrameSizeUpdate()
}

function updateMediaAspectFromVideo() {
  const video = iptvVideoRef.value
  if (!video) return
  setMediaAspect(video.videoWidth, video.videoHeight)
}

function scheduleMediaFrameSizeUpdate() {
  if (mediaLayoutRaf) return
  mediaLayoutRaf = window.requestAnimationFrame(() => {
    mediaLayoutRaf = 0
    updateMediaFrameSize()
  })
}

function updateMediaFrameSize() {
  const viewportWidth = window.innerWidth
  const viewportHeight = window.innerHeight
  const hasHover = typeof window.matchMedia === 'function'
    ? window.matchMedia('(hover: hover)').matches
    : false
  const hasFinePointer = typeof window.matchMedia === 'function'
    ? window.matchMedia('(pointer: fine)').matches
    : false
  const nowPanelHeight = nowPanelRef.value
    ? Math.ceil(nowPanelRef.value.getBoundingClientRect().height)
    : 150
  const sizing = calculateFullPlayerSizing({
    viewportWidth,
    viewportHeight,
    hasHover,
    hasFinePointer,
    railPreference: desktopRailPreference.value,
    mediaAspect: mediaAspectValue.value,
    nowPanelHeight,
  })
  layoutMode.value = sizing.mode
  desktopRailLayout.value = sizing.desktopLayout || 'theater'
  if (sizing.mode === 'mobile') {
    mediaFrameWidth.value = null
    mediaFrameHeight.value = null
    mediaStageAspectRatio.value = null
    sidePanelWidth.value = null
    playerLayoutWidth.value = null
  } else {
    mediaFrameWidth.value = sizing.videoWidth
    mediaFrameHeight.value = sizing.stageHeight
    mediaStageAspectRatio.value = sizing.stageAspectRatio
    sidePanelWidth.value = sizing.railWidth
    playerLayoutWidth.value = sizing.layoutWidth
  }
  scheduleTabIndicatorUpdate()
}

function toggleDesktopRail() {
  if (!isDesktopLayout.value || isFullscreen.value) return
  desktopRailPreference.value = isRailLayout.value ? 'hidden' : 'shown'
  handleOverlayActivity()
  scheduleMediaFrameSizeUpdate()
}

function handlePointerCapabilityChange() {
  scheduleMediaFrameSizeUpdate()
}

function observePointerCapabilities() {
  if (typeof window.matchMedia !== 'function') return
  hoverCapabilityQuery = window.matchMedia('(hover: hover)')
  finePointerCapabilityQuery = window.matchMedia('(pointer: fine)')
  hoverCapabilityQuery.addEventListener?.('change', handlePointerCapabilityChange)
  finePointerCapabilityQuery.addEventListener?.('change', handlePointerCapabilityChange)
  hoverCapabilityQuery.addListener?.(handlePointerCapabilityChange)
  finePointerCapabilityQuery.addListener?.(handlePointerCapabilityChange)
}

function stopObservingPointerCapabilities() {
  hoverCapabilityQuery?.removeEventListener?.('change', handlePointerCapabilityChange)
  finePointerCapabilityQuery?.removeEventListener?.('change', handlePointerCapabilityChange)
  hoverCapabilityQuery?.removeListener?.(handlePointerCapabilityChange)
  finePointerCapabilityQuery?.removeListener?.(handlePointerCapabilityChange)
  hoverCapabilityQuery = null
  finePointerCapabilityQuery = null
}

function updateSourceMenuPosition() {
  const desktop = isDesktopLayout.value
  const button = (desktop ? desktopSourceButtonRef.value : sourceButtonRef.value) ||
    (desktop ? sourceButtonRef.value : desktopSourceButtonRef.value)
  if (!button) return
  const rect = button.getBoundingClientRect()
  const gutter = 12
  const width = Math.min(320, window.innerWidth - gutter * 2)
  const maxHeight = Math.min(340, Math.max(180, window.innerHeight - gutter * 2))
  const belowTop = rect.bottom + 8
  const aboveTop = rect.top - maxHeight - 8
  const top = belowTop + maxHeight <= window.innerHeight - gutter
    ? belowTop
    : Math.max(gutter, aboveTop)
  const left = Math.min(
    window.innerWidth - width - gutter,
    Math.max(gutter, rect.right - width),
  )

  sourceMenuStyle.value = {
    left: `${left}px`,
    top: `${top}px`,
    width: `${width}px`,
    maxHeight: `${maxHeight}px`,
  }
  sourceMenuListMaxHeight.value = `${Math.max(120, maxHeight - 48)}px`
}

async function toggleSourceMenu() {
  handleOverlayActivity()
  sourceMenuOpen.value = !sourceMenuOpen.value
  if (sourceMenuOpen.value) {
    await nextTick()
    updateSourceMenuPosition()
  }
}

function closeSourceMenu() {
  sourceMenuOpen.value = false
}

function clearMobileOverlayTimer() {
  if (!mobileOverlayTimer) return
  clearTimeout(mobileOverlayTimer)
  mobileOverlayTimer = null
}

function showMobileOverlayControls() {
  if (isDesktopLayout.value) return
  mobileOverlayVisible.value = true
  clearMobileOverlayTimer()
  if (overlayMustStayVisible.value) return
  mobileOverlayTimer = setTimeout(() => {
    if (!overlayMustStayVisible.value && isMobileLayout.value) {
      mobileOverlayVisible.value = false
    }
    mobileOverlayTimer = null
  }, 2800)
}

function scheduleMobileOverlayHide() {
  clearMobileOverlayTimer()
  if (overlayMustStayVisible.value || !isMobileLayout.value) {
    mobileOverlayVisible.value = true
    return
  }
  mobileOverlayTimer = setTimeout(() => {
    mobileOverlayTimer = null
    if (!overlayMustStayVisible.value && isMobileLayout.value) {
      mobileOverlayVisible.value = false
    }
  }, 900)
}

const overlayMustStayVisible = computed(() => (
  !isPlaying.value
  || isLoading.value
  || isSourceSwitching.value
  || Boolean(playerStore.playbackError)
  || sourceMenuOpen.value
  || overlayControlsFocused.value
  || overlayVolumeInteracting.value
))

const isDesktopOverlayHidden = computed(() => (
  isDesktopLayout.value
  && !desktopOverlayVisible.value
  && !overlayMustStayVisible.value
))

const isMobileOverlayHidden = computed(() => (
  isMobileLayout.value
  && !mobileOverlayVisible.value
  && !overlayMustStayVisible.value
))

function clearOverlayHideTimer() {
  if (!overlayHideTimer) return
  clearTimeout(overlayHideTimer)
  overlayHideTimer = null
}

function applyOverlayVisibility() {
  clearOverlayHideTimer()
  desktopOverlayVisible.value = true
  if (overlayMustStayVisible.value || !isDesktopLayout.value) return
  overlayHideTimer = setTimeout(() => {
    overlayHideTimer = null
    if (!overlayMustStayVisible.value && isDesktopLayout.value) {
      desktopOverlayVisible.value = false
    }
  }, 2800)
}

function scheduleOverlayHide() {
  clearOverlayHideTimer()
  if (isMobileLayout.value) {
    scheduleMobileOverlayHide()
    return
  }
  if (overlayMustStayVisible.value || !isDesktopLayout.value) {
    desktopOverlayVisible.value = true
    return
  }
  overlayHideTimer = setTimeout(() => {
    overlayHideTimer = null
    if (!overlayMustStayVisible.value && isDesktopLayout.value) {
      desktopOverlayVisible.value = false
    }
  }, 900)
}

function isInteractiveKeyboardTarget(target) {
  return Boolean(target?.closest?.(
    'input, textarea, select, option, button, a, [contenteditable="true"], [role="slider"], [role="menu"], [role="menuitem"]',
  ))
}

function isKeyboardTextEntryTarget(target) {
  return Boolean(target?.closest?.(
    'input, textarea, select, [contenteditable="true"]',
  ))
}

function isMediaSurfaceTarget(target) {
  return Boolean(target && mediaSurfaceRef.value?.contains?.(target))
}

function isMediaPlayerControlTarget(target) {
  if (!isMediaSurfaceTarget(target)) return false
  return Boolean(target?.closest?.('button, input, select, textarea, [role="slider"]'))
}

function getPlayerCommandControl(target) {
  const control = target?.closest?.('button')
  if (!control || !isMediaSurfaceTarget(control)) return null
  if (control.getAttribute('aria-controls')?.endsWith('-source-menu')) return null
  return control
}

function getFullscreenTrigger(target) {
  return target?.closest?.('button[aria-label="全屏"]') || null
}

function shouldShowOverlayForKeyboard(event, code, isInteractiveTarget) {
  const target = event?.target
  if (isMediaPlayerControlTarget(target)) return true
  if (!isMediaSurfaceTarget(target) && target !== playerRootRef.value) return false
  if (isInteractiveTarget) return false
  return ['Space', 'KeyK', 'KeyM', 'KeyF', 'PageUp', 'PageDown'].includes(code)
}

function clearPointerCommandFocusCleanup() {
  if (pointerCommandFocusCleanupTimer === null) return
  clearTimeout(pointerCommandFocusCleanupTimer)
  pointerCommandFocusCleanupTimer = null
}

function focusPlayerShortcutScope() {
  const target = isFullscreen.value ? mediaSurfaceRef.value : playerRootRef.value
  target?.focus?.({ preventScroll: true })
}

function movePointerCommandFocus(control) {
  if (!isPlayerExpanded.value || !document.contains(control)) return
  if (document.activeElement !== control) return
  if (isDesktopLayout.value) focusPlayerShortcutScope()
  else control.blur?.()
}

function schedulePointerCommandFocusCleanup(control) {
  clearPointerCommandFocusCleanup()
  movePointerCommandFocus(control)
  pointerCommandFocusCleanupTimer = setTimeout(() => {
    pointerCommandFocusCleanupTimer = null
    movePointerCommandFocus(control)
  }, 0)
}

function trackPointerInteraction(event) {
  lastOverlayInputModality = 'pointer'
  pendingPointerFullscreenTrigger = getFullscreenTrigger(event?.target)
  pendingPointerCommandControl = getPlayerCommandControl(event?.target)
}

function handlePlayerPointerDown(event) {
  trackPointerInteraction(event)
  overlayControlsFocused.value = false
  handleOverlayActivity()
  if (!isDesktopLayout.value || isInteractiveKeyboardTarget(event?.target)) return
  focusPlayerShortcutScope()
}

function handlePlayerTouchStart(event) {
  trackPointerInteraction(event)
  overlayControlsFocused.value = false
  handleOverlayActivity()
}

function handlePlayerControlClick(event) {
  const control = getPlayerCommandControl(event?.target)
  if (!control || control !== pendingPointerCommandControl) return
  pendingPointerCommandControl = null
  schedulePointerCommandFocusCleanup(control)
}

function handlePlayerKeyboard(event) {
  // Keep this in the existing FullPlayer capture path so shortcuts do not
  // become a second global listener with a separate cleanup/lifecycle.
  lastOverlayInputModality = 'keyboard'
  pendingPointerCommandControl = null
  clearPointerCommandFocusCleanup()
  if (
    !isPlayerExpanded.value
    || !isDesktopLayout.value
    || event?.isComposing
    || event?.ctrlKey
    || event?.metaKey
    || event?.altKey
  ) return

  const code = String(event?.code || '')
  const key = String(event?.key || '').toLowerCase()
  const isVolumeShortcut = code === 'ArrowUp' || code === 'ArrowDown'
  const isPlaybackShortcut = code === 'Space' || code === 'KeyK'
  const isMuteShortcut = code === 'KeyM'
  const isFullscreenShortcut = code === 'KeyF'
  const isChannelShortcut = code === 'PageUp' || code === 'PageDown'
  const isInteractiveTarget = isInteractiveKeyboardTarget(event?.target)
  const isMediaControlTarget = isMediaPlayerControlTarget(event?.target)

  if (isMediaControlTarget) {
    overlayControlsFocused.value = true
  }
  if (shouldShowOverlayForKeyboard(event, code, isInteractiveTarget)) {
    handleOverlayActivity()
  }

  if (key === 'escape') {
    // Escape should close the open source surface even when focus is on one
    // of its menu buttons, but must remain native for text-entry controls.
    if (!sourceMenuOpen.value || isKeyboardTextEntryTarget(event?.target)) return
    closeSourceMenu()
    event.preventDefault()
    return
  }

  if (isInteractiveTarget) return

  if (!isVolumeShortcut && !isPlaybackShortcut && !isMuteShortcut && !isFullscreenShortcut && !isChannelShortcut) return

  // Toggle/channel actions are one-shot. Volume remains repeatable so a held
  // ArrowUp/ArrowDown behaves like a normal desktop volume control.
  event.preventDefault()
  if (event.repeat && !isVolumeShortcut) return

  if (isVolumeShortcut) {
    const delta = code === 'ArrowUp' ? KEYBOARD_VOLUME_STEP : -KEYBOARD_VOLUME_STEP
    setOverlayVolume(effectiveVolume.value + delta)
  } else if (isPlaybackShortcut) {
    playerStore.togglePlay()
  } else if (isMuteShortcut) {
    toggleOverlayMute()
  } else if (isFullscreenShortcut) {
    void toggleFullscreen()
  } else if (isChannelShortcut) {
    if (code === 'PageUp') playPrev()
    else playNext()
  }
}

function handleOverlayActivity() {
  desktopOverlayVisible.value = true
  applyOverlayVisibility()
  showMobileOverlayControls()
}

function beginOverlayPinnedInteraction() {
  overlayVolumeInteracting.value = true
  handleOverlayActivity()
}

function endOverlayPinnedInteraction() {
  overlayVolumeInteracting.value = false
  applyOverlayVisibility()
}

function handleOverlayFocusIn(event) {
  const target = event?.target
  overlayControlsFocused.value = lastOverlayInputModality === 'keyboard' && isMediaPlayerControlTarget(target)
  if (target === playerRootRef.value || isMediaSurfaceTarget(target) || overlayControlsFocused.value) {
    handleOverlayActivity()
  }
}

function handleOverlayFocusOut() {
  nextTick(() => {
    overlayControlsFocused.value = lastOverlayInputModality === 'keyboard' &&
      isMediaPlayerControlTarget(document.activeElement)
    scheduleOverlayHide()
  })
}

function syncFullPlayerTheme() {
  const shouldUseDark = document.documentElement.classList.contains('dark') ||
    document.body.classList.contains('dark')
  isFullPlayerDark.value = shouldUseDark
}

function setFullPlayerChromeOpen(open) {
  const appEl = document.getElementById('app')
  document.documentElement.classList.toggle('full-player-open', open)
  document.body.classList.toggle('full-player-open', open)
  appEl?.classList.toggle('full-player-open', open)

  if (open) {
    syncFullPlayerTheme()
  } else {
    window.__waveflowSyncThemeChrome?.()
  }
}

function handleThemeChromeSync(event) {
  if (typeof event?.detail?.isDarkMode === 'boolean') {
    isFullPlayerDark.value = event.detail.isDarkMode
  } else {
    syncFullPlayerTheme()
  }
  if (event?.detail?.refreshChrome === false) return
  refreshSafariChrome()
}

function refreshSafariChrome() {
  if (!isPlayerExpanded.value) return
  if (!isIOS) return
  if (document.visibilityState !== 'visible') return
  isSafariChromeRefreshing.value = true
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      isSafariChromeRefreshing.value = false
    })
  })
}

const displayIptvChannel = computed(() => playerStore.pendingIptvChannel || playerStore.currentIptvChannel)

const isIptvMode = computed(() => Boolean(playerStore.currentIptvChannel))

const currentStationData = computed(() => stationMap.value[currentStation.value])

const currentStationName = computed(() => {
  if (displayIptvChannel.value) return displayIptvChannel.value.name
  return currentStationData.value?.name || '未选择频道'
})

const statusText = computed(() => {
  if (playerStore.playbackError) return playerStore.playbackError
  if (isLoading.value) return '正在连接'
  if (displayIptvChannel.value) return displayIptvChannel.value.group_name || 'IPTV'
  const subtitle = currentStationData.value?.subtitle
  if (subtitle) return subtitle
  return 'WaveFlow'
})

const channelList = computed(() => stationList.value)

const iptvChannelList = ref([])
const iptvChannelContext = computed(() => playerStore.iptvChannelContext)
let iptvListRequestSeq = 0
let iptvListController = null

const currentArtworkUrl = computed(() => {
  if (displayIptvChannel.value) return displayIptvChannel.value.logo_url || ''
  return currentStationData.value?.logoUrl || ''
})

const currentChannelSubtitle = computed(() => {
  if (displayIptvChannel.value) return displayIptvChannel.value.group_name || '直播频道'
  return statusText.value
})

const currentProgram = computed(() => {
  const epg = playerStore.currentEpgProgram
  if (epg) {
    const startD = new Date(epg.start)
    const stopD = new Date(epg.stop)
    const now = epgNow.value
    const total = stopD.getTime() - startD.getTime()
    const elapsed = now - startD.getTime()
    const remainingMs = stopD.getTime() - now
    const progress = total > 0 ? Math.max(0, Math.min(100, Math.round((elapsed / total) * 100))) : 0
    return {
      title: epg.title,
      start: startD.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      end: stopD.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      remaining: Math.max(0, Math.ceil(remainingMs / 60000)),
      progress,
    }
  }
  return { title: currentStationName.value, start: '', end: '', remaining: 0, progress: 0 }
})

const hasCurrentEpgProgram = computed(() => Boolean(playerStore.currentEpgProgram))

const currentProgramProgressPercent = computed(() => `${currentProgram.value.progress}%`)

const epgViewingState = computed(() => {
  if (_epgLoading.value && !hasCurrentEpgProgram.value) return 'loading'
  if (_epgError.value) return 'error'
  if (hasCurrentEpgProgram.value) return 'current'
  return 'empty'
})

const epgViewingText = computed(() => {
  if (epgViewingState.value === 'current') return currentProgram.value.title
  return ''
})

const nextProgramSummary = computed(() => {
  if (!hasCurrentEpgProgram.value || _epgLoading.value || _epgError.value) return ''
  const title = String(_epgNext.value?.title || '').trim()
  if (!title) return ''
  const start = formatEpgClock(_epgNext.value?.start)
  return start ? `${title} ${start}` : title
})

const hasProgrammeMetadata = computed(() => (
  isIptvMode.value && (
    hasCurrentEpgProgram.value
    || Boolean(nextProgramSummary.value)
  )
))

function localDateString(date = new Date()) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function epgDateLabel(value) {
  const today = localDateString()
  const date = new Date(`${value}T00:00:00`)
  const diffDays = Math.round((date - new Date(`${today}T00:00:00`)) / 86400000)
  if (diffDays === -1) return '昨天'
  if (diffDays === 0) return '今天'
  if (diffDays === 1) return '明天'
  return `${date.getMonth() + 1}/${date.getDate()}`
}

function epgDateWeekday(value) {
  return new Date(`${value}T00:00:00`).toLocaleDateString('zh-CN', { weekday: 'short' })
}

const epgDateOptions = computed(() => {
  const dates = _epgAvailableDates.value || []
  return dates.map((value) => ({
    value,
    label: epgDateLabel(value),
    weekday: epgDateWeekday(value),
    active: value === _epgSelectedDate.value,
  }))
})

const fullPlayerStatusText = computed(() => {
  if (playerStore.playbackError) return playerStore.playbackError
  if (isLoading.value) return '正在连接'
  if (isPlaying.value) return '直播中'
  return '已暂停'
})

const showProgramRemaining = computed(() => (
  hasCurrentEpgProgram.value
  && !playerStore.playbackError
  && !isLoading.value
  && isPlaying.value
  && currentProgram.value.remaining > 0
))

const playbackStateClass = computed(() => {
  if (playerStore.playbackError) return 'error'
  if (isLoading.value) return 'loading'
  if (isPlaying.value) return 'playing'
  return 'idle'
})

const isTransientPlaybackMessage = computed(() => (
  /切换备用|源不可用.*切换|正在恢复|正在重连|卡顿|缓冲|重试/.test(String(playerStore.playbackError || ''))
))

const overlayPlaybackStatusText = computed(() => {
  const error = String(playerStore.playbackError || '')
  if (isSourceSwitching.value) return '正在切换源'
  if (error) {
    if (/切换备用|源不可用.*切换/.test(error)) return '正在切换源'
    if (/正在恢复|正在重连|卡顿|缓冲|重试/.test(error)) return '正在重连'
    return error
  }
  if (isLoading.value) return '正在加载'
  if (isPlaying.value) return '直播中'
  return '已暂停'
})

const overlayPlaybackStateClass = computed(() => (
  isSourceSwitching.value || (isLoading.value && isTransientPlaybackMessage.value)
    ? 'loading'
    : playbackStateClass.value
))

const canRetryPlayback = computed(() => (
  isIptvMode.value
    ? Boolean(playerStore.currentIptvChannel || playerStore.pendingIptvChannel)
    : Boolean(currentStation.value)
))

const showPlaybackFailureIndicator = computed(() => (
  Boolean(playerStore.playbackError)
  && !isLoading.value
  && !isTransientPlaybackMessage.value
  && canRetryPlayback.value
))

async function retryPlayback() {
  if (isLoading.value) return
  if (isIptvMode.value) {
    const channel = playerStore.currentIptvChannel || playerStore.pendingIptvChannel
    if (channel) await playIptvChannelFromFullPlayer(channel)
    return
  }
  const stationId = currentStation.value
  if (!stationId) return
  playerStore.currentStation = ''
  await nextTick()
  playerStore.switchStation(stationId)
}

const isOverlayLoadingCandidate = computed(() => {
  if (!isLoading.value) return false
  if (playerStore.playbackError && !isTransientPlaybackMessage.value) return false
  return Boolean(
    isPlaying.value
    || playerStore.pendingIptvChannel
    || playerStore.currentIptvChannel
    || currentStation.value,
  )
})

const showOverlayLoadingSpinner = ref(false)

function clearOverlayLoadingTimer() {
  if (!overlayLoadingTimer) return
  clearTimeout(overlayLoadingTimer)
  overlayLoadingTimer = null
}

watch(isOverlayLoadingCandidate, (shouldShow) => {
  clearOverlayLoadingTimer()
  if (!shouldShow) {
    showOverlayLoadingSpinner.value = false
    return
  }
  overlayLoadingTimer = setTimeout(() => {
    overlayLoadingTimer = null
    showOverlayLoadingSpinner.value = isOverlayLoadingCandidate.value
  }, 240)
}, { immediate: true })

const isPlaybackConfirmed = computed(() => (
  !playerStore.playbackError && !isLoading.value && isPlaying.value
))

const displaySchedule = computed(() => {
  const schedule = _epgSchedule.value || []
  if (schedule.length) {
    // EPG 有数据时用 EPG schedule
    return schedule.map(p => {
      const startD = new Date(p.start)
      return {
        time: startD.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
        title: p.title,
        current: p.status === 'current',
        past: p.status === 'past',
      }
    })
  }
  // Radio 的节目面板仍保留电台名 fallback；IPTV 没有节目数据时保持空面板，
  // 不用占位文案伪造节目单。
  if (isIptvMode.value) return []
  return currentProgram.value.title
    ? [{ time: '', title: currentProgram.value.title, current: false }]
    : []
})

const hasScheduleData = computed(() => (
  displaySchedule.value.length > 0 || epgDateOptions.value.length > 0
))

function normalizeGroupName(value) {
  return String(value || '').trim().toLowerCase()
}

function channelRowSummary(ch, active) {
  if (isIptvAllNotLive(ch)) return '未开播'
  if (isIptvUnavailable(ch)) return '已禁用'
  if (active) {
    if (hasCurrentEpgProgram.value) {
      const remaining = currentProgram.value.remaining
      const suffix = remaining > 0 ? ` · 剩余 ${remaining} 分钟` : ''
      return `当前：${currentProgram.value.title}${suffix}`
    }
    return '直播中'
  }
  return ch.group_name || '直播频道'
}

const displayChannelRows = computed(() => {
  if (isIptvMode.value) {
    const context = iptvChannelContext.value
    const allChannels = context
      ? context.channels
      : (iptvChannelList.value.length
        ? iptvChannelList.value
        : (displayIptvChannel.value ? [displayIptvChannel.value] : []))
    let channels = allChannels.slice()
    const current = displayIptvChannel.value
    const currentIdentity = channelIdentity(current)
    if (currentIdentity && !channels.some((ch) => channelIdentity(ch) === currentIdentity)) {
      channels.unshift(current)
    }
    channels = sortIptvChannels(channels, channelSortMode.value)
    return channels.map((ch, index) => {
      const active = isCurrentIptv(ch)
      const playing = active && isPlaybackConfirmed.value
      return {
        key: `iptv-${channelIdentity(ch) || ch.canonical_key || ch.name}`,
        name: ch.name,
        logo: ch.logo_url || '',
        live: playing,
        playing,
        active,
        disabled: isIptvUnavailable(ch),
        channel: ch,
        summary: channelRowSummary(ch, active),
        select: () => playIptvChannelFromFullPlayer(ch),
      }
    })
  }

  return channelList.value.map((station, index) => {
    const active = currentStation.value === station.id
    const playing = active && isPlaybackConfirmed.value
    return {
      key: `radio-${station.id}`,
      name: station.name,
      logo: station.logoUrl || '',
      live: playing,
      playing,
      active,
      stationId: station.id,
      summary: active
        ? `当前：${station.subtitle || station.name}`
        : `${station.subtitle || '直播电台'}`,
      select: () => playerStore.switchStation(station.id),
    }
  })
})

async function playIptvChannelFromFullPlayer(channel) {
  if (!channel?.urls?.length) {
    playerStore.setPlaybackError('频道没有可用播放源')
    return
  }
  if (isIptvUnavailable(channel)) {
    playerStore.setPlaybackError('频道已禁用')
    return
  }
  // not_live 是上次测速结果，不阻断；toast 提示后照常进入起播链路（与 IptvHome 一致）。
  if (isIptvAllNotLive(channel)) {
    toastStore.info('该频道上次检测未开播，正在尝试播放')
  }

  _manualIptvStartPending += 1
  try {
    await playerStore.playIptvChannel(channel, { progressive: true })
  } catch (e) {
    _manualIptvStartPending = Math.max(0, _manualIptvStartPending - 1)
    playerStore.setPlaybackError(e?.message || '频道起播失败')
    return
  }
  const selectionToken = playerStore.iptvSelectionToken
  const selectedChannelKey = (value) => String(
    value?.logical_channel_id || value?.canonical_key || value?.name || '',
  )
  nextTick(() => {
    _manualIptvStartPending = Math.max(0, _manualIptvStartPending - 1)
    if (
      selectionToken !== playerStore.iptvSelectionToken
      || selectedChannelKey(playerStore.currentIptvChannel) !== selectedChannelKey(channel)
      || !iptvVideoRef.value
      || !playerStore.currentIptvChannel
    ) {
      return
    }
    _hardIptvSwitchTeardown = false
    resetRacedLosers()
    _playSelectionToken = selectionToken
    const attemptId = ++_playAttemptId
    playCurrentIptvUrl(attemptId).catch((e) => {
      console.warn('[IPTV] 列表切台起播失败:', e?.message)
    })
  })
}

function handleChannelRowClick(item) {
  if (import.meta.env.DEV) {
    console.log('channel row clicked', item)
    console.log(typeof item?.select)
  }

  if (typeof item?.select === 'function') {
    item.select()
    return
  }

  if (item?.channel) {
    playIptvChannelFromFullPlayer(item.channel)
    return
  }

  if (item?.stationId) {
    playerStore.switchStation(item.stationId)
  }
}

function sourceTargetUrl(entry) {
  return entry?.original_url || entry?.url || ''
}

function sourceHost(url) {
  if (!url) return '未知地址'
  try {
    return new URL(url).host
  } catch {
    return url.replace(/^https?:\/\//, '').split('/')[0] || url
  }
}

function sourceStatusClass(status) {
  if (status === 'playing') return 'bg-emerald-500'
  if (status === 'trying') return 'bg-amber-400'
  if (status === 'failed') return 'bg-red-500'
  return 'bg-neutral-300 dark:bg-neutral-600'
}

function sourceStatusLabel(status) {
  if (status === 'playing') return '当前可播'
  if (status === 'trying') return '正在尝试'
  if (status === 'failed') return '本次失败'
  if (status === 'stopped') return '已停止'
  return '未尝试'
}

function syncIptvMediaSession(playbackState = isPlaying.value ? 'playing' : 'paused') {
  if (!isIptvMode.value || !('mediaSession' in navigator)) return
  try {
    const session = navigator.mediaSession
    if (typeof MediaMetadata === 'function') {
      session.metadata = new MediaMetadata({
        title: currentProgram.value?.title || currentStationName.value,
        artist: currentStationName.value,
        artwork: currentArtworkUrl.value ? [{ src: currentArtworkUrl.value }] : [],
      })
    }
    session.playbackState = playbackState
    registerMediaSessionAction('play', () => {
      if (activeIptvEngine.value === 'youtube') {
        try { _youtubePlayer?.playVideo?.() } catch {}
      }
      resumeIptvFromMediaSession()
    })
    registerMediaSessionAction('pause', () => {
      if (activeIptvEngine.value === 'youtube') {
        try { _youtubePlayer?.pauseVideo?.() } catch {}
      }
      playerStore.togglePlay(false)
    })
    registerMediaSessionAction('stop', () => {
      if (activeIptvEngine.value === 'youtube') destroyYoutubePlayer()
      playerStore.togglePlay(false)
      session.playbackState = 'none'
    })
  } catch (e) {
    console.warn('[IPTV] MediaSession 更新失败:', e)
  }
}

function clearIptvMediaSession() {
  if (!('mediaSession' in navigator)) return
  const session = navigator.mediaSession
  try { session.metadata = null } catch {}
  try { session.playbackState = 'none' } catch {}
  for (const action of ['play', 'pause', 'stop']) {
    try { session.setActionHandler(action, null) } catch {}
  }
}

function registerMediaSessionAction(action, handler) {
  if (!('mediaSession' in navigator)) return
  try {
    navigator.mediaSession.setActionHandler(action, handler)
  } catch (e) {
    console.warn(`[IPTV] MediaSession action ${action} 注册失败:`, e?.message || e)
  }
}

async function resumeIptvFromMediaSession() {
  if (!isIptvMode.value) return
  playerStore.togglePlay(true)

  if (activeIptvEngine.value === 'youtube') {
    try { _youtubePlayer?.playVideo?.() } catch {}
    syncIptvMediaSession('playing')
    return
  }

  const video = iptvVideoRef.value
  if (video && !video.paused && !video.ended) {
    syncIptvMediaSession('playing')
    return
  }

  try {
    if (await resumeSoftPausedIptv('media-session-resume')) return
    const recovered = await recoverIptvPlayback('media-session-resume', {
      force: true,
      statusText: '正在恢复播放...',
    })
    syncIptvMediaSession(recovered ? 'playing' : (isPlaying.value ? 'playing' : 'paused'))
  } catch (e) {
    if (e?.message !== 'cancelled') {
      console.warn('[IPTV] MediaSession resume reload failed:', e?.message || e)
    }
    syncIptvMediaSession(isPlaying.value ? 'playing' : 'paused')
  }
}

const iptvSourceOptions = computed(() => {
  const selectedEntry = playerStore.iptvUrls[playerStore.iptvUrlIndex]
  const selectedKey = selectedEntry ? sourceRaceKey(selectedEntry) : ''
  return playerStore.iptvUrls.map((entry, index) => {
    const targetUrl = sourceTargetUrl(entry)
    const isProxySource = entry.type === 'proxy' || entry.via_proxy
    const st = sourceType(entry)
    const typeLabel = st === 'youtube' ? 'YT' : isProxySource ? '代理' : '直连'
    const host = sourceHost(targetUrl)
    const recommendedLabel = String(entry.recommended_display_label || '').trim()
    const working = Number(entry.is_working)
    const latency = Number(entry.latency_ms) > 0 ? `${entry.latency_ms}ms` : ''
    const runtimeKey = sourceRaceKey(entry)
    const runtimeStatus = iptvSourceRuntimeStatus.value[runtimeKey] || 'idle'
    const probeStatus = entry.probe_status || ''
    const disabled = isSourceExplicitlyDisabled(entry) || st === 'unsupported_youtube_url'
    const health = probeStatus === 'not_live'
      ? '未开播'
      : probeStatus === 'untested'
        ? '未检测'
        : working === 1
          ? '检测可用'
          : working === 0
            ? '检测不可用'
            : '未检测'
    const healthLabel = runtimeStatus === 'idle' || health !== '未检测' ? health : ''
    const ua = entry.custom_ua ? 'UA' : ''
    const transcode = entry.rtsp_compat ? '转码' : ''
    const speed = Number(entry.speed_mbps) > 0 ? `${Number(entry.speed_mbps).toFixed(1)}M/s` : ''
    const quality = [entry.resolution, entry.video_codec, speed].filter(Boolean).join(' ')
    const meta = [sourceStatusLabel(runtimeStatus), healthLabel, quality, latency, ua, transcode].filter(Boolean).join(' · ')

    return {
      index,
      url: entry.url,
      source_id: entry.source_id || '',
      transport: sourceTransport(entry),
      identityKey: sourceRaceKey(entry),
      type: st === 'youtube' ? 'youtube' : isProxySource ? 'proxy' : entry.type,
      typeLabel,
      title: recommendedLabel || host || `线路 ${index + 1}`,
      meta,
      status: runtimeStatus,
      statusClass: sourceStatusClass(runtimeStatus),
      active: selectedKey ? runtimeKey === selectedKey : index === playerStore.iptvUrlIndex,
      disabled,
    }
  })
})

const currentRadioSourceOptions = computed(() => {
  if (isIptvMode.value) return []
  const station = currentStationData.value
  return (station?.radioSources || []).map((source) => ({
    source_id: source.source_id,
    active: source.source_id === station.radioSourceId,
    title: `${source.provider_key || 'radio'} · ${source.provider_station_id || source.source_id}`,
    meta: [source.health_status, source.lifecycle_state].filter(Boolean).join(' · ') || '可用',
  }))
})

const currentIptvSourceLabel = computed(() => {
  const current = iptvSourceOptions.value[playerStore.iptvUrlIndex]
  return current ? `${current.index + 1}/${iptvSourceOptions.value.length}` : '未选择'
})

const currentRadioSourceLabel = computed(() => {
  const current = currentRadioSourceOptions.value.find((source) => source.active)
  return current ? current.title : '未选择'
})

async function loadIptvChannels() {
  const seq = ++iptvListRequestSeq
  if (iptvListController) iptvListController.abort()
  const controller = new AbortController()
  iptvListController = controller
  const context = playerStore.iptvChannelContext
  const contextToken = context?.token || 0
  const currentGroup = String(displayIptvChannel.value?.group_name || '').trim()
  const group = context ? context.group : currentGroup
  const search = context ? context.search : ''
  try {
    let data = await fetchAggregatedChannels({ group, search, signal: controller.signal })
    if (seq !== iptvListRequestSeq) return
    if (!context && group && !(data.channels || []).length) {
      data = await fetchAggregatedChannels({ signal: controller.signal })
      if (seq !== iptvListRequestSeq) return
    }
    if (context) {
      playerStore.refreshIptvChannelContext({
        token: contextToken,
        group,
        search,
        channels: data.channels || [],
      })
      return
    }
    if (playerStore.iptvChannelContext || seq !== iptvListRequestSeq) return
    iptvChannelList.value = data.channels || []
  } catch (e) {
    if (seq !== iptvListRequestSeq || e?.name === 'AbortError' || e?.status === 0) return
    console.error('加载 IPTV 频道失败:', e)
  } finally {
    if (seq === iptvListRequestSeq) iptvListController = null
  }
}

function isCurrentIptv(ch) {
  const current = playerStore.pendingIptvChannel || playerStore.currentIptvChannel
  return Boolean(current && channelIdentity(current) === channelIdentity(ch))
}

function isIptvAllNotLive(ch) {
  return isChannelAllNotLive(ch)
}

function isIptvUnavailable(ch) {
  return isChannelAllUrlsBlocked(ch) || isChannelAllUnsupported(ch)
}

// 切换到 IPTV 模式时加载频道列表
watch(isIptvMode, (isIptv) => {
  if (isIptv) loadIptvChannels()
})

onMounted(() => {
  if (isIptvMode.value) loadIptvChannels()
})

function playAdjacentVisibleChannel(offset) {
  const rows = displayChannelRows.value.filter((item) => typeof item?.select === 'function' && !item.disabled)
  if (rows.length <= 1) return
  const activeIndex = rows.findIndex((item) => item.active)
  const currentIndex = activeIndex >= 0 ? activeIndex : 0
  const nextIndex = (currentIndex + offset + rows.length) % rows.length
  rows[nextIndex]?.select?.()
}

function playPrev() {
  if (isIptvMode.value) {
    playAdjacentVisibleChannel(-1)
    return
  }
  const list = stationList.value
  if (!list.length) return
  const idx = list.findIndex((s) => s.id === currentStation.value)
  const prevIdx = idx <= 0 ? list.length - 1 : idx - 1
  playerStore.switchStation(list[prevIdx].id)
}

function playNext() {
  if (isIptvMode.value) {
    playAdjacentVisibleChannel(1)
    return
  }
  const list = stationList.value
  if (!list.length) return
  const idx = list.findIndex((s) => s.id === currentStation.value)
  const nextIdx = idx >= list.length - 1 ? 0 : idx + 1
  playerStore.switchStation(list[nextIdx].id)
}

// ── IPTV 视频播放 ──

const HLS_ABR_PATCH_KEY = '__waveflowAbrNullGuard'

function patchHlsAbrNullGuard(hls) {
  const abr = hls?.abrController
  if (!abr || abr[HLS_ABR_PATCH_KEY]) return
  const original = abr._abandonRulesCheck
  if (typeof original !== 'function') return
  abr._abandonRulesCheck = (...args) => {
    if (!abr.hls) {
      try {
        abr.clearTimer?.()
      } catch {}
      return
    }
    return original.apply(abr, args)
  }
  abr[HLS_ABR_PATCH_KEY] = true
}

function clearHlsInternalTimers(hls) {
  const controllers = [
    hls?.abrController,
    hls?.streamController,
    hls?.levelController,
    hls?.audioStreamController,
    hls?.subtitleStreamController,
  ]
  for (const controller of controllers) {
    try {
      controller?.clearTimer?.()
    } catch {}
  }
}

function trackHlsSource(hls) {
  patchHlsAbrNullGuard(hls)
}

function releaseTrackedHls(hls) {
  clearRuntimeHandlerCleanup(hls)
  clearHlsInternalTimers(hls)
}

function destroyIptvHls() {
  if (iptvHlsRef.value) {
    releaseTrackedHls(iptvHlsRef.value)
    iptvHlsRef.value.destroy()
    iptvHlsRef.value = null
  }
}

function destroyIptvMpegts() {
  if (!iptvMpegtsRef.value) return
  const player = iptvMpegtsRef.value
  iptvMpegtsRef.value = null
  clearRuntimeHandlerCleanup(player)
  try {
    player.destroy()
  } catch (e) {
    const message = e?.message || ''
    if (!message.includes('removeAllListeners')) {
      console.warn('[IPTV] mpegts cleanup failed:', e)
    }
  }
}

function destroyIptvEngines() {
  destroyIptvHls()
  destroyIptvMpegts()
  destroyYoutubePlayer()
}

function resetIptvVideo() {
  if (!iptvVideoRef.value) return
  stopPlaybackWatchdogs()
  resetMediaAspect()
  iptvVideoRef.value.pause()
  iptvVideoRef.value.removeAttribute('src')
  iptvVideoRef.value.load()
}

function clearPauseReleaseTimer() {
  _pauseGraceSeq++
  if (_pauseReleaseTimer) {
    clearTimeout(_pauseReleaseTimer)
    _pauseReleaseTimer = null
  }
}

function releaseSoftPausedConnection(seq) {
  _pauseReleaseTimer = null
  if (seq !== _pauseGraceSeq || !isIptvMode.value || isPlaying.value || !_softPausedAt) return
  _softPauseReleased = true

  if (iptvHlsRef.value) {
    try {
      iptvHlsRef.value.stopLoad?.()
    } catch (e) {
      console.warn('[IPTV] HLS pause grace stopLoad failed:', e?.message || e)
    }
    releaseTrackedHls(iptvHlsRef.value)
  }

  if (iptvMpegtsRef.value) {
    try {
      iptvMpegtsRef.value.pause?.()
    } catch (e) {
      console.warn('[IPTV] MPEG-TS pause grace release failed:', e?.message || e)
    }
  }
}

function scheduleSoftPauseRelease() {
  clearPauseReleaseTimer()
  const seq = _pauseGraceSeq
  _pauseReleaseTimer = setTimeout(() => releaseSoftPausedConnection(seq), PAUSE_GRACE_RELEASE_MS)
}

function pauseIptvPlaybackPreservingFrame() {
  _playAttemptId++
  _recoverySeq++
  _recoveryInFlight = false
  cancelCurrentStartup()
  cancelActiveProxyRace()
  clearStallRecoveryTimer()
  stopPlaybackProgressWatch()
  stopVideoFrameWatch()

  const video = iptvVideoRef.value
  if (video && !video.paused) {
    try {
      video.pause()
    } catch {}
  }
  _softPausedAt = Date.now()
  _softPauseReleased = false
  scheduleSoftPauseRelease()
  playerStore.setLoading(false)
  syncIptvMediaSession('paused')
}

async function resumeSoftPausedIptv(reason = 'resume') {
  if (_softResumePromise) return await _softResumePromise

  const run = async () => {
    const video = iptvVideoRef.value
    if (!video || !_softPausedAt || _softPauseReleased) return false
    if (Date.now() - _softPausedAt > PAUSE_GRACE_RELEASE_MS) return false

    clearPauseReleaseTimer()
    const attemptId = ++_playAttemptId
    _recoverySeq++
    _recoveryInFlight = false
    clearStallRecoveryTimer()
    const sourceIndex = playerStore.iptvUrlIndex
    const entry = playerStore.iptvUrls[sourceIndex]
    const sourceUrl = entry?.url || iptvHlsRef.value?.__waveflowSourceUrl || ''
    const usingProxy = Boolean(entry?.via_proxy)

    try {
      playerStore.clearPlaybackError()
      playerStore.setLoading(false)

      if (iptvHlsRef.value) {
        await video.play()
        if (!isAttemptActive(attemptId)) return false
        attachRuntimeHlsErrorHandlers(iptvHlsRef.value, sourceUrl, usingProxy, attemptId, sourceIndex)
      } else if (iptvMpegtsRef.value) {
        await video.play()
        if (!isAttemptActive(attemptId)) return false
        attachRuntimeMpegtsErrorHandlers(iptvMpegtsRef.value, sourceUrl, usingProxy, attemptId, sourceIndex)
      } else if (video.src || video.currentSrc) {
        await video.play()
        if (!isAttemptActive(attemptId)) return false
      } else {
        return false
      }

      _softPausedAt = 0
      _softPauseReleased = false
      markVideoProgress(video)
      startPlaybackProgressWatch(video)
      startVideoFrameWatch(video)
      playerStore.togglePlay(true)
      setSourceRuntimeStatus(sourceIndex, 'playing')
      syncIptvMediaSession('playing')
      console.warn('[IPTV] soft pause resumed', { reason, sourceIndex })
      return true
    } catch (e) {
      if (!isAttemptActive(attemptId)) return false
      if (isAutoplayBlockedError(e)) {
        _softPausedAt = 0
        _softPauseReleased = true
        playerStore.setPlaybackError('浏览器阻止自动播放，请点击播放按钮继续。')
        playerStore.setLoading(false)
        playerStore.togglePlay(false)
        syncIptvMediaSession('paused')
        return false
      }
      console.warn('[IPTV] soft pause resume failed:', e?.message || e)
      _softPausedAt = 0
      _softPauseReleased = true
      return false
    }
  }

  _softResumePromise = run()
  try {
    return await _softResumePromise
  } finally {
    _softResumePromise = null
  }
}

function clearYoutubeStartupTimer() {
  if (!_youtubeStartupTimer) return
  clearTimeout(_youtubeStartupTimer)
  _youtubeStartupTimer = null
}

function destroyYoutubePlayer(resetEngine = true) {
  clearYoutubeStartupTimer()
  if (_youtubePlayer) {
    try {
      _youtubePlayer.stopVideo?.()
    } catch {}
    try {
      _youtubePlayer.destroy?.()
    } catch (e) {
      console.warn('[IPTV] YouTube cleanup failed:', e)
    }
    _youtubePlayer = null
  }
  if (youtubeHostRef.value) youtubeHostRef.value.innerHTML = ''
  if (resetEngine && activeIptvEngine.value === 'youtube') {
    activeIptvEngine.value = 'video'
  }
}

function syncYoutubeAudioState() {
  if (!_youtubePlayer) return
  try {
    _youtubePlayer.setVolume?.(Math.round(volume.value * 100))
    if (iptvMuted.value || volume.value <= 0) _youtubePlayer.mute?.()
    else _youtubePlayer.unMute?.()
  } catch {}
}

async function loadYoutubeIframeApi(timeoutMs = 3000) {
  const now = Date.now()
  if (window.YT?.Player) {
    _youtubeApiReachable = true
    _youtubeApiCheckedAt = now
    return true
  }
  if (_youtubeApiPromise && now - _youtubeApiCheckedAt < 60_000) return _youtubeApiPromise
  if (_youtubeApiReachable === false && now - _youtubeApiCheckedAt < 60_000) return false

  _youtubeApiCheckedAt = now
  _youtubeApiPromise = new Promise((resolve) => {
    let settled = false
    const previousReady = window.onYouTubeIframeAPIReady
    const finish = (ok) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      _youtubeApiReachable = ok
      _youtubeApiCheckedAt = Date.now()
      _youtubeApiPromise = null
      resolve(ok)
    }

    window.onYouTubeIframeAPIReady = () => {
      if (typeof previousReady === 'function') {
        try { previousReady() } catch {}
      }
      finish(Boolean(window.YT?.Player))
    }

    const timer = setTimeout(() => finish(Boolean(window.YT?.Player)), timeoutMs)

    if (!document.querySelector('script[data-waveflow-youtube-api="1"]')) {
      const script = document.createElement('script')
      script.src = 'https://www.youtube.com/iframe_api'
      script.async = true
      script.dataset.waveflowYoutubeApi = '1'
      script.onerror = () => {
        script.remove()
        finish(false)
      }
      document.head.appendChild(script)
    }
  })

  return _youtubeApiPromise
}

function preflightYoutubeApiForQueue(urls) {
  if (!urls?.some((entry) => sourceType(entry) === 'youtube')) return
  loadYoutubeIframeApi().catch(() => false)
}

async function handleActiveYoutubeFailure(reason, attemptId, sourceIndex) {
  if (!isAttemptActive(attemptId)) return
  console.warn('[IPTV] YouTube 播放中断，切备用源:', reason)
  setSourceRuntimeStatus(sourceIndex, 'failed')
  destroyYoutubePlayer()
  const nextAttemptId = ++_playAttemptId
  if (await fallbackToNextIptvUrl(nextAttemptId)) {
    return await playCurrentIptvUrl(nextAttemptId)
  }
  markAllIptvSourcesUnavailable(nextAttemptId)
}

const YOUTUBE_URL_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'youtu.be',
  'www.youtu.be',
  'youtube-nocookie.com',
  'www.youtube-nocookie.com',
])

const YOUTUBE_EMBED_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'youtube-nocookie.com',
  'www.youtube-nocookie.com',
])

function isYoutubeInputHost(host) {
  return YOUTUBE_URL_HOSTS.has(host)
}

function isValidYoutubeHttpUrl(parsed) {
  return parsed.protocol === 'https:'
    && !parsed.username
    && !parsed.password
    && (!parsed.port || parsed.port === '443')
}

function isAllowedYoutubeEmbedUrl(value) {
  try {
    const parsed = new URL(String(value || '').trim())
    return parsed.protocol === 'https:'
      && YOUTUBE_EMBED_HOSTS.has(parsed.hostname.toLowerCase())
      && parsed.pathname.toLowerCase().startsWith('/embed/')
      && !parsed.username
      && !parsed.password
      && (!parsed.port || parsed.port === '443')
  } catch {
    return false
  }
}

function youtubePlaybackErrorMessage(code) {
  const numericCode = Number(code)
  if (numericCode === 2) return 'YouTube 视频参数无效'
  if (numericCode === 5) return 'YouTube 播放器不支持此视频'
  if (numericCode === 100) return 'YouTube 视频不可用'
  if ([101, 150, 153].includes(numericCode)) return 'YouTube 视频不允许嵌入'
  return 'YouTube 播放失败'
}

async function startYoutubeCandidate(entry, attemptId = 0, sourceIndex = -1) {
  if (!isAttemptActive(attemptId)) throw cancelledError()
  const videoId = youtubeVideoId(entry)
  const liveEmbedUrl = youtubeLiveEmbedUrl(entry)
  if (!videoId && !liveEmbedUrl) throw new Error('YouTube video_id 缺失')
  if (liveEmbedUrl && !isAllowedYoutubeEmbedUrl(liveEmbedUrl)) {
    throw new Error('YouTube embed URL 不受支持')
  }

  const setRuntimeStatus = (status) => {
    if (sourceIndex >= 0) setSourceRuntimeStatus(sourceIndex, status)
    else setSourceRuntimeStatusByEntry(entry, status)
  }

  setRuntimeStatus('trying')
  playerStore.setLoading(true)
  cancelCurrentStartup()
  cancelActiveProxyRace()
  destroyIptvHls()
  destroyIptvMpegts()
  destroyYoutubePlayer(false)
  resetIptvVideo()

  if (!isAttemptActive(attemptId)) throw cancelledError()
  if (!youtubeHostRef.value) throw new Error('YouTube 播放容器未就绪')

  activeIptvEngine.value = 'youtube'
  resetMediaAspect()
  youtubeHostRef.value.innerHTML = ''

  const reachable = await loadYoutubeIframeApi()
  if (!isAttemptActive(attemptId)) throw cancelledError()
  if (!reachable || !window.YT?.Player) {
    setRuntimeStatus('failed')
    throw new Error('YouTube API 不可达')
  }

  console.log(videoId
    ? `[START] YouTube:${videoId}`
    : `[START] YouTubeLive:${entry.youtube_channel_id || liveEmbedUrl}`)

  return new Promise((resolve, reject) => {
    let settled = false
    let confirmed = false
    let playerInstance = null
    let startupTimer = null
    let cancelStartup = null
    const clearOwnedStartup = () => {
      if (startupTimer) {
        clearTimeout(startupTimer)
        if (_youtubeStartupTimer === startupTimer) _youtubeStartupTimer = null
        startupTimer = null
      }
      if (_cancelCurrentStartup === cancelStartup) _cancelCurrentStartup = null
    }
    const cleanupFailure = () => {
      clearOwnedStartup()
      if (_youtubePlayer === playerInstance) {
        destroyYoutubePlayer()
        return
      }
      try { playerInstance?.stopVideo?.() } catch {}
      try { playerInstance?.destroy?.() } catch {}
    }
    const safeReject = (err) => {
      if (settled) return
      settled = true
      cleanupFailure()
      if (isAttemptActive(attemptId)) setRuntimeStatus('failed')
      reject(err)
    }
    const safeResolve = () => {
      if (settled || !isAttemptActive(attemptId)) return
      settled = true
      confirmed = true
      clearOwnedStartup()
      setRuntimeStatus('playing')
      playerStore.togglePlay(true)
      playerStore.clearPlaybackError()
      playerStore.setLoading(false)
      syncIptvMediaSession('playing')
      resolve()
    }

    cancelStartup = () => safeReject(cancelledError())
    _cancelCurrentStartup = cancelStartup
    startupTimer = setTimeout(() => {
      safeReject(new Error('YouTube 起播超时'))
    }, 8000)
    _youtubeStartupTimer = startupTimer

    const playerOptions = {
      width: '100%',
      height: '100%',
      playerVars: {
        autoplay: 1,
        playsinline: 1,
        controls: 1,
        rel: 0,
        modestbranding: 1,
      },
      events: {
        onReady: () => {
          if (!isAttemptActive(attemptId)) {
            safeReject(cancelledError())
            return
          }
          syncYoutubeAudioState()
          try {
            _youtubePlayer?.playVideo?.()
          } catch (e) {
            safeReject(e)
          }
        },
        onStateChange: (event) => {
          if (!isAttemptActive(attemptId)) return
          const state = event?.data
          if (state === window.YT.PlayerState.PLAYING) {
            safeResolve()
            return
          }
          if (state === window.YT.PlayerState.BUFFERING) {
            if (!confirmed) playerStore.setLoading(true)
            return
          }
          if (state === window.YT.PlayerState.PAUSED) {
            playerStore.togglePlay(false)
            syncIptvMediaSession('paused')
            return
          }
          if (state === window.YT.PlayerState.ENDED) {
            playerStore.togglePlay(false)
            syncIptvMediaSession('none')
            if (!confirmed) safeReject(new Error('YouTube 直播已结束'))
          }
        },
        onError: (event) => {
          const code = Number(event?.data)
          const err = new Error(youtubePlaybackErrorMessage(code))
          err.youtubeCode = Number.isFinite(code) ? code : null
          if (!confirmed) {
            safeReject(err)
            return
          }
          handleActiveYoutubeFailure(err.message, attemptId, sourceIndex)
        },
      },
    }

    if (videoId) {
      playerOptions.videoId = videoId
      playerInstance = new window.YT.Player(youtubeHostRef.value, playerOptions)
      _youtubePlayer = playerInstance
      return
    }

    const iframe = document.createElement('iframe')
    const embedUrl = new URL(liveEmbedUrl)
    embedUrl.searchParams.set('enablejsapi', '1')
    if (window.location?.origin) embedUrl.searchParams.set('origin', window.location.origin)
    iframe.src = embedUrl.toString()
    iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share'
    iframe.allowFullscreen = true
    iframe.referrerPolicy = 'strict-origin-when-cross-origin'
    iframe.style.width = '100%'
    iframe.style.height = '100%'
    iframe.style.border = '0'
    youtubeHostRef.value.appendChild(iframe)
    playerInstance = new window.YT.Player(iframe, { events: playerOptions.events })
    _youtubePlayer = playerInstance
  })
}

function canUseHls() {
  if (typeof Hls === 'undefined') return false
  if (Hls.isSupported()) return true
  if (window.ManagedMediaSource) {
    console.log('[IPTV] 使用 ManagedMediaSource (hls.js on iOS)')
    return true
  }
  console.log('[IPTV] 无 MSE/MMS，回退到原生 HLS')
  return false
}

function isHlsUrl(url) {
  return /\.m3u8(\?|$)/i.test(url)
}

function isMpegTsUrl(url) {
  return /\/api\/iptv\/proxy\/stream(\?|$)/i.test(url)
    || /\/(?:rtp|udp)\//i.test(url)
    || /%2F(?:rtp|udp)%2F/i.test(url)
}

function isHttpFlvUrl(url) {
  return /\.flv(?:[?#]|$)/i.test(url) || /[?&]stream_type=http_flv(?:&|$)/i.test(url)
}

function isMpegTsEngineType(type) {
  return type === 'mpegts' || type === 'http_flv'
}

function isChannelProxyPlaylistUrl(url) {
  try {
    const parsed = new URL(url, window.location.origin)
    return /\/api\/media\/channel\/.+\/playlist\.m3u8$/i.test(parsed.pathname)
  } catch {
    return false
  }
}

function sourceTypeFromProxyRedirect(url) {
  try {
    const parsed = new URL(url, window.location.origin)
    if (/\/api\/media\/proxy\/stream\//i.test(parsed.pathname)) {
      return parsed.searchParams.get('stream_type') === 'http_flv' ? 'http_flv' : 'mpegts'
    }
    if (/\/api\/media\/proxy\/rtsp\//i.test(parsed.pathname)) return 'hls'
  } catch {}
  return ''
}

function isKnownRtspProxyPlaylist(url, usingProxy, sourceType) {
  return Boolean(
    usingProxy
    && isChannelProxyPlaylistUrl(url)
    && String(sourceType || '').trim().toLowerCase() === 'rtsp',
  )
}

async function preflightProxyPlaylistTransport(url, usingProxy, signal = null) {
  if (!usingProxy || !isChannelProxyPlaylistUrl(url)) return null
  return await new Promise((resolve) => {
    const xhr = new XMLHttpRequest()
    let settled = false
    let abortHandler = null
    const settle = (value) => {
      if (settled) return
      settled = true
      if (abortHandler) signal?.removeEventListener('abort', abortHandler)
      try { xhr.abort() } catch {}
      resolve(value)
    }
    if (signal?.aborted) {
      settle(null)
      return
    }
    if (signal) {
      abortHandler = () => settle(null)
      signal.addEventListener('abort', abortHandler, { once: true })
    }
    xhr.open('GET', url, true)
    xhr.setRequestHeader('Accept', 'application/vnd.apple.mpegurl,application/x-mpegURL,*/*')
    xhr.timeout = 3500
    xhr.onreadystatechange = () => {
      if (xhr.readyState < 2 || settled) return
      const finalUrl = xhr.responseURL || url
      const sourceType = sourceTypeFromProxyRedirect(finalUrl)
      if (sourceType) {
        console.log('[IPTV] proxy playlist redirect', {
          source_id: extractSourceIdFromUrl(url),
          source_type: sourceType,
        })
        settle({ url: finalUrl, sourceType })
        return
      }
      settle(null)
    }
    xhr.onerror = () => settle(null)
    xhr.ontimeout = () => settle(null)
    xhr.send()
  })
}

function mpegtsPlayerType(type, url = '') {
  return type === 'http_flv' || isHttpFlvUrl(url) ? 'flv' : 'mse'
}

function playbackEngineType(type, url = '') {
  const normalized = String(type || '').trim().toLowerCase()
  if (normalized === 'rtsp' && (isHlsUrl(url) || sourceTypeFromProxyRedirect(url) === 'hls')) return 'hls'
  return normalized
}

function youtubeUrlParts(parsed) {
  return parsed.protocol === 'youtube:'
    ? [parsed.hostname, ...parsed.pathname.split('/')].filter(Boolean)
    : parsed.pathname.split('/').filter(Boolean)
}

function parseYoutubeVideoId(url) {
  try {
    const parsed = new URL(url)
    const host = parsed.hostname.toLowerCase()
    const parts = youtubeUrlParts(parsed)
    let id = ''
    if (parsed.protocol === 'youtube:') {
      if (parts.length === 1) id = parts[0] || ''
      else if (parts.length >= 2 && ['live', 'embed', 'shorts'].includes(parts[0])) id = parts[1]
    } else if ((host === 'youtu.be' || host === 'www.youtu.be') && isValidYoutubeHttpUrl(parsed)) {
      id = parts[0] || ''
    } else if (isYoutubeInputHost(host) && isValidYoutubeHttpUrl(parsed) && !['youtu.be', 'www.youtu.be'].includes(host)) {
      if (parts[0]?.toLowerCase() === 'watch') id = parsed.searchParams.get('v') || ''
      if (!id && parts.length >= 2 && ['live', 'embed', 'shorts'].includes(parts[0]?.toLowerCase())) {
        id = parts[1]
      }
    }
    return /^[a-zA-Z0-9_-]{11}$/.test(id) ? id : ''
  } catch {
    return ''
  }
}

function parseYoutubeChannelId(url) {
  try {
    const parsed = new URL(url)
    const host = parsed.hostname.toLowerCase()
    const parts = youtubeUrlParts(parsed)
    let id = ''
    if (parsed.protocol === 'youtube:') {
      if (/^UC[a-zA-Z0-9_-]{20,}$/.test(parts[0] || '')) id = parts[0]
      else if (parts.length >= 2 && parts[0] === 'channel') id = parts[1]
    } else if (isYoutubeInputHost(host) && isValidYoutubeHttpUrl(parsed) && !['youtu.be', 'www.youtu.be'].includes(host)) {
      if (parts.length >= 2 && parts[0] === 'channel') id = parts[1]
    }
    return /^UC[a-zA-Z0-9_-]{20,}$/.test(id) ? id : ''
  } catch {
    return ''
  }
}

function isYoutubeLiveChannelUrl(url) {
  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'youtube:' && !isYoutubeUrl(url)) return false
    const parts = youtubeUrlParts(parsed)
    return parts[parts.length - 1] === 'live'
  } catch {
    return false
  }
}

function isYoutubeUrl(url) {
  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'youtube:') return true
    const host = parsed.hostname.toLowerCase()
    return isYoutubeInputHost(host)
  } catch {
    return false
  }
}

// source_type 优先，兜底回 URL 猜测
function sourceType(entry) {
  const url = entry?.url || ''
  const inferred = parseYoutubeVideoId(url)
    ? 'youtube'
    : parseYoutubeChannelId(url) && isYoutubeLiveChannelUrl(url)
      ? 'youtube'
      : isYoutubeUrl(url)
      ? 'unsupported_youtube_url'
      : isHlsUrl(url)
        ? 'hls'
        : isHttpFlvUrl(url)
          ? 'http_flv'
          : isMpegTsUrl(url) ? 'mpegts' : 'hls'
  const declared = String(entry?.source_type || '').trim().toLowerCase()
  return declared && declared !== 'hls' ? declared : inferred
}

function youtubeVideoId(entry) {
  return entry?.youtube_video_id || parseYoutubeVideoId(entry?.original_url || entry?.url || '')
}

function youtubeLiveEmbedUrl(entry) {
  if (entry?.youtube_live_embed_url) return entry.youtube_live_embed_url
  const original = entry?.original_url || entry?.url || ''
  const channelId = entry?.youtube_channel_id || parseYoutubeChannelId(original)
  if (!channelId || !isYoutubeLiveChannelUrl(original)) return ''
  return `https://www.youtube.com/embed/live_stream?channel=${encodeURIComponent(channelId)}&autoplay=1&playsinline=1&controls=1&rel=0`
}

function canUseMpegTs() {
  try {
    return Boolean(mpegts?.getFeatureList?.().mseLivePlayback || mpegts?.isSupported?.())
  } catch {
    return false
  }
}

// 设置面板候选：起播并发数。建议未来高级设置暴露为 1-12，默认 6。
const STARTUP_RACE_LIMIT = 6
const MPEGTS_EOF_RECONNECT_DELAY_MS = 300
const MPEGTS_ERROR_RECONNECT_DELAY_MS = 1200
const MPEGTS_RECONNECT_WINDOW_MS = 60_000
const MPEGTS_RECONNECT_LIMIT = 3
const RECOVERY_LOADING_DELAY_MS = 400
// stall 检测阈值：直通模式下上游单 segment 时长可达 10-11s，hls.js 自身的
// bufferStalled 自愈需要等下一个 segment（5-8s）。把硬阈值放宽，否则刚 stall
// 就被强制 recover/reload 反而打断 hls.js 自愈，触发周期性卡顿。
const RECOVERY_SOFT_RECOVER_MS = 6000
const RECOVERY_HARD_RELOAD_MS = 12000
const RECOVERY_CURRENT_RETRY_LIMIT = 2
const RECOVERY_CURRENT_TTL_MS = 4000
const RECOVERY_FALLBACK_TTL_MS = 5000
const RECOVERY_RETRY_DELAYS_MS = [300, 800]
const RECOVERY_PROGRESS_WATCH_INTERVAL_MS = 750
const PAUSE_GRACE_RELEASE_MS = 30_000
// 设置面板候选：直连起播超过该时长仍无真实赢家时，启动代理兜底组。
// 建议未来高级设置暴露为 0-8000ms；0 表示 direct/proxy 同时抢跑。
const HEDGED_PROXY_DELAY_MS = 2500
// 设置面板候选：临时失败源跳过时长，仅内存态，不写数据库。
// 建议未来高级设置暴露为 0-300s；0 表示不记录临时 loser。
const RACE_LOSER_TTL_MS = 45_000
// 不建议开放：HLS 真胜出确认窗口属于状态机安全参数，避免用户调回误判。
const RACE_HLS_CONFIRM_MS = 700
const RACE_HLS_CONFIRM_RETRY_MS = 500

let _playAttemptId = 0
let _playSelectionToken = playerStore.iptvSelectionToken
let _recoverySeq = 0
let _recoveryInFlight = false
let _pauseReleaseTimer = null
let _pauseGraceSeq = 0
let _softPausedAt = 0
let _softPauseReleased = false
let _softResumePromise = null
// URL -> expireAt。只做本轮/短期启动避让；频道切换和手动选源会清空或删除。
let _racedLosers = new Map()
let _mpegtsRecoveries = new Map()
let _cleanupActiveRace = null
let _cancelCurrentStartup = null
let _suppressIptvUrlWatch = 0
let _manualIptvStartPending = 0
let _youtubePlayer = null
let _youtubeStartupTimer = null
let _youtubeApiPromise = null
let _youtubeApiReachable = null
let _youtubeApiCheckedAt = 0
let _componentDisposed = false
let _hardIptvSwitchTeardown = false
function isAttemptActive(attemptId) {
  return !_componentDisposed
    && attemptId === _playAttemptId
    && _playSelectionToken === playerStore.iptvSelectionToken
}

function cancelledError() {
  return new Error('cancelled')
}

function isAutoplayBlockedError(error) {
  const name = String(error?.name || '')
  return name === 'NotAllowedError'
}

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function jitter(ms, spread = 120) {
  return Math.max(0, ms + Math.round((Math.random() - 0.5) * spread))
}

async function withAttemptTimeout(promise, attemptId, timeoutMs, message) {
  if (!timeoutMs) return await promise
  let timer = null
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          if (isAttemptActive(attemptId)) _playAttemptId++
          reject(new Error(message))
        }, timeoutMs)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function clearCurrentHlsIf(hls) {
  if (iptvHlsRef.value === hls) iptvHlsRef.value = null
}

function clearCurrentMpegtsIf(player) {
  if (iptvMpegtsRef.value === player) iptvMpegtsRef.value = null
}

function cancelActiveProxyRace() {
  if (!_cleanupActiveRace) return
  const cleanup = _cleanupActiveRace
  _cleanupActiveRace = null
  cleanup()
}

function cancelCurrentStartup() {
  if (!_cancelCurrentStartup) return
  const cancel = _cancelCurrentStartup
  _cancelCurrentStartup = null
  cancel()
}

function markAllIptvSourcesUnavailable(attemptId) {
  if (!isAttemptActive(attemptId)) return
  playerStore.setPlaybackError('所有播放源均不可用')
  playerStore.isPlaying = false
  playerStore.isLoading = false
}

function recordMpegtsReconnect(sourceUrl) {
  const now = Date.now()
  const record = _mpegtsRecoveries.get(sourceUrl)
  if (!record || now - record.firstAt > MPEGTS_RECONNECT_WINDOW_MS) {
    const nextRecord = { firstAt: now, count: 1 }
    _mpegtsRecoveries.set(sourceUrl, nextRecord)
    return nextRecord.count
  }

  record.count += 1
  _mpegtsRecoveries.set(sourceUrl, record)
  return record.count
}

function setSourceRuntimeStatus(index, status) {
  if (index < 0) return
  const runtimeKey = sourceRaceKey(playerStore.iptvUrls[index])
  if (!runtimeKey) return
  iptvSourceRuntimeStatus.value = {
    ...iptvSourceRuntimeStatus.value,
    [runtimeKey]: status,
  }
}

function getSourceRuntimeStatus(index) {
  if (index < 0) return 'idle'
  const runtimeKey = sourceRaceKey(playerStore.iptvUrls[index])
  return runtimeKey ? (iptvSourceRuntimeStatus.value[runtimeKey] || 'idle') : 'idle'
}

function setSourceRuntimeStatusByUrl(url, status) {
  const requestSourceId = extractSourceIdFromUrl(url || '')
  const index = requestSourceId
    ? playerStore.iptvUrls.findIndex((entry) => entry.source_id === requestSourceId && entry.url === url)
    : playerStore.iptvUrls.findIndex((entry) => entry.url === url)
  setSourceRuntimeStatus(index, status)
}

function setSourceRuntimeStatusByEntry(entry, status) {
  const index = playerStore.iptvUrls.indexOf(entry)
  setSourceRuntimeStatus(index, status)
}

function isRecoveryActive(seq) {
  return seq === _recoverySeq && isIptvMode.value && isPlaying.value
}

async function refreshVolatileEntryForRecovery(entry, attemptId, reason) {
  if (!entry?.adapter_volatile_url || !entry?.adapter_source_url) return false
  const retryCount = entry._volatileRetryCount || 0
  if (retryCount >= RECOVERY_CURRENT_RETRY_LIMIT) return false

  console.warn('[IPTV] recovery 重新 resolve 当前源...', {
    reason,
    retry: retryCount + 1,
    limit: RECOVERY_CURRENT_RETRY_LIMIT,
  })
  playerStore.setPlaybackError('正在获取新的播放地址...')
  const resolved = await playerStore.reResolveAdapterUrl(entry.adapter_source_url)
  if (!isAttemptActive(attemptId)) return false
  if (!resolved?.url) return false
  entry.url = resolved.url
  if (resolved.source_type) entry.source_type = resolved.source_type
  entry._volatileRetryCount = retryCount + 1
  return true
}

async function recoverIptvPlayback(reason = 'stalled', options = {}) {
  if (!isIptvMode.value || !playerStore.currentIptvChannel || !isPlaying.value) return false
  if (_recoveryInFlight && !options.force) return false

  const seq = ++_recoverySeq
  _recoveryInFlight = true
  clearStallRecoveryTimer()
  cancelCurrentStartup()
  cancelActiveProxyRace()

  const startIndex = playerStore.iptvUrlIndex
  const currentEntry = playerStore.iptvUrls[startIndex]
  const currentRetryLimit = options.currentRetryLimit ?? RECOVERY_CURRENT_RETRY_LIMIT
  const currentTtlMs = options.currentTtlMs ?? RECOVERY_CURRENT_TTL_MS
  const fallbackTtlMs = options.fallbackTtlMs ?? RECOVERY_FALLBACK_TTL_MS
  const allowFallbackRace = options.allowFallbackRace !== false
  const statusText = options.statusText || '直播卡顿，正在重新连接当前源'

  playerStore.setLoading(true)
  playerStore.setPlaybackError(statusText)
  setSourceRuntimeStatus(startIndex, 'trying')
  console.warn('[IPTV] recovery start', { reason, startIndex, currentRetryLimit, currentTtlMs })

  try {
    for (let i = 0; i < currentRetryLimit; i += 1) {
      if (!isRecoveryActive(seq)) return false
      const delayMs = jitter(RECOVERY_RETRY_DELAYS_MS[i] ?? RECOVERY_RETRY_DELAYS_MS[RECOVERY_RETRY_DELAYS_MS.length - 1])
      if (delayMs) await wait(delayMs)
      if (!isRecoveryActive(seq)) return false

      const attemptId = ++_playAttemptId
      if (currentEntry?.adapter_volatile_url && currentEntry?.adapter_source_url) {
        try {
          await refreshVolatileEntryForRecovery(currentEntry, attemptId, reason)
        } catch (e) {
          console.warn('[IPTV] recovery resolve 当前源失败:', e?.message || e)
        }
      }

      try {
        await withAttemptTimeout(
          playCurrentIptvUrl(attemptId, {
            allowStartupRace: false,
            allowFallback: false,
            allowCurrentRetry: false,
            allowVolatileResolve: false,
            preserveFrame: true,
            startupTimeoutMs: currentTtlMs,
          }),
          attemptId,
          currentTtlMs + 300,
          '当前源重连超时',
        )
        if (!isRecoveryActive(seq)) return false
        playerStore.clearPlaybackError()
        playerStore.setLoading(false)
        syncIptvMediaSession('playing')
        return true
      } catch (e) {
        if (!isRecoveryActive(seq)) return false
        console.warn('[IPTV] recovery 当前源重连失败:', {
          retry: i + 1,
          limit: currentRetryLimit,
          reason: e?.message || e,
        })
        setSourceRuntimeStatus(startIndex, 'failed')
      }
    }

    if (currentEntry?.url && (currentEntry.type === 'proxy' || currentEntry.via_proxy)) {
      markRaceLoser(currentEntry)
    }

    if (!allowFallbackRace || !isRecoveryActive(seq)) return false

    const fallbackAttemptId = ++_playAttemptId
    if (await setIptvUrlIndexForAttempt(startIndex, fallbackAttemptId)) {
      if (await fallbackToNextIptvUrl(fallbackAttemptId)) {
        playerStore.setPlaybackError('当前源不可用，正在尝试备用源...')
        await playCurrentIptvUrl(fallbackAttemptId, {
          allowStartupRace: true,
          startupTimeoutMs: fallbackTtlMs,
          raceTimeoutMs: fallbackTtlMs,
        })
        return isRecoveryActive(seq)
      }
    }
    markAllIptvSourcesUnavailable(fallbackAttemptId)
    return false
  } finally {
    if (seq === _recoverySeq) _recoveryInFlight = false
  }
}

async function fallbackToNextIptvUrl(attemptId) {
  if (!isAttemptActive(attemptId)) return false
  _suppressIptvUrlWatch++
  const hasNext = playerStore.iptvFallbackNext()
  await nextTick()
  _suppressIptvUrlWatch = Math.max(0, _suppressIptvUrlWatch - 1)
  return hasNext && isAttemptActive(attemptId)
}

async function setIptvUrlIndexForAttempt(index, attemptId) {
  if (!isAttemptActive(attemptId) || index < 0 || playerStore.iptvUrlIndex === index) {
    return isAttemptActive(attemptId)
  }
  _suppressIptvUrlWatch++
  playerStore.iptvUrlIndex = index
  await nextTick()
  _suppressIptvUrlWatch = Math.max(0, _suppressIptvUrlWatch - 1)
  return isAttemptActive(attemptId)
}

async function switchIptvSource(sourceKey) {
  if (!isIptvMode.value || !sourceKey) return
  const option = iptvSourceOptions.value.find((item) => item.identityKey === sourceKey)
  const index = option
    ? playerStore.iptvUrls.findIndex((entry) => sourceRaceKey(entry) === sourceKey)
    : -1
  if (index < 0) return
  if (option?.disabled) {
    playerStore.setPlaybackError('播放源已禁用或不受支持')
    sourceMenuOpen.value = false
    return
  }
  sourceMenuOpen.value = false

  const previousIndex = playerStore.iptvUrlIndex
  if (previousIndex !== index) isSourceSwitching.value = true
  if (previousIndex !== index && ['trying', 'playing'].includes(getSourceRuntimeStatus(previousIndex))) {
    setSourceRuntimeStatus(previousIndex, 'stopped')
  }
  const entry = playerStore.iptvUrls[index]
  if (entry?.url) clearRaceLoser(entry)

  const attemptId = ++_playAttemptId
  _recoverySeq++
  _recoveryInFlight = false
  clearStallRecoveryTimer()
  cancelCurrentStartup()
  cancelActiveProxyRace()
  if (!(await setIptvUrlIndexForAttempt(index, attemptId))) {
    isSourceSwitching.value = false
    return
  }
  setSourceRuntimeStatus(index, 'trying')

  if (isProxyLikeEntry(entry)) {
    try {
      const entryType = sourceType(entry)
      const playbackType = entryType === 'adapter' && entry?.adapter_transport_pending
        ? 'hls'
        : entryType
      await tryPlayIptv(entry.url, true, entry.custom_ua || '', attemptId, index, playbackType)
      if (!isAttemptActive(attemptId)) return
      setSourceRuntimeStatus(index, 'playing')
      playerStore.clearPlaybackError()
      playerStore.setLoading(false)
    } catch (e) {
      if (!isAttemptActive(attemptId)) return
      setSourceRuntimeStatus(index, 'failed')
      if (entry?.url) markRaceLoser(entry)
      console.warn('[IPTV] 手动切换代理源失败:', e?.message)
      if (await fallbackToNextIptvUrl(attemptId)) {
        return await playCurrentIptvUrl(attemptId)
      }
      markAllIptvSourcesUnavailable(attemptId)
    }
    return
  }

  await playCurrentIptvUrl(attemptId, { allowStartupRace: false })
}

function switchRadioSource(sourceId) {
  if (isIptvMode.value || !currentStation.value) return
  if (playerStore.selectRadioSource(currentStation.value, sourceId)) {
    sourceMenuOpen.value = false
  }
}

function clearRuntimeHandlerCleanup(target) {
  const cleanup = target?.__waveflowRuntimeCleanup
  if (!cleanup) return
  try {
    cleanup()
  } catch (e) {
    console.warn('[IPTV] runtime handler cleanup failed:', e?.message || e)
  }
}

function attachRuntimeHlsErrorHandlers(hls, sourceUrl, usingProxy, attemptId, sourceIndex = -1) {
  clearRuntimeHandlerCleanup(hls)
  let fragFail = 0
  let levelLoadFail = 0
  let stallRecovery = 0
  // fatal NETWORK_ERROR 我们让 hls.js 自己 startLoad 一次再观察；如果它一直
  // 收 fatal，就需要切源，而不是死循环 startLoad。这里维护独立计数。
  let fatalNetworkRecover = 0
  let fatalMediaRecover = 0
  const fatalNetworkThreshold = usingProxy ? 4 : 3
  const fatalMediaThreshold = 2
  let switching = false
  // 提高代理源容忍度：单次 frag 失败几乎一定能由 hls.js 自带重试在下一段恢复。
  // 之前 threshold=2 经常因为一次 502/网络抖动就把整个播放栈 destroy。
  const fragThreshold = usingProxy ? 4 : 3
  const levelThreshold = usingProxy ? 4 : 3
  const stallThreshold = 3
  const setRuntimeStatus = (status) => {
    if (sourceIndex >= 0) setSourceRuntimeStatus(sourceIndex, status)
    else setSourceRuntimeStatusByUrl(sourceUrl, status)
  }

  const cleanup = () => {
    hls.off(Hls.Events.FRAG_LOADED, onFragLoaded)
    hls.off(Hls.Events.ERROR, onError)
    if (hls.__waveflowRuntimeCleanup === cleanup) hls.__waveflowRuntimeCleanup = null
  }

  const switchToFallback = async (reason) => {
    if (switching || !isAttemptActive(attemptId)) return
    switching = true
    console.warn(`[IPTV] HLS 播放中断，进入恢复流程: ${reason}`)
    cleanup()
    setRuntimeStatus('failed')
    releaseTrackedHls(hls)
    hls.destroy()
    clearCurrentHlsIf(hls)
    return await recoverIptvPlayback(`HLS:${reason}`, { force: true })
  }

  const onFragLoaded = () => {
    fragFail = 0
    levelLoadFail = 0
    stallRecovery = 0
    fatalNetworkRecover = 0
    fatalMediaRecover = 0
  }

  const onError = (_, data) => {
    if (!isAttemptActive(attemptId)) return
    const details = data.details
    const fatal = !!data.fatal
    const type = data.type
    // 调试日志：保持单行结构化，便于 grep；不打印 URL 与 token。
    console.log(
      `[ERR:runtime] details=${details} fatal=${fatal} type=${type}` +
      ` http=${data.response?.code ?? '-'} fragSn=${data.frag?.sn ?? '-'}` +
      ` level=${data.level ?? '-'} fragFail=${fragFail} levelFail=${levelLoadFail}`
    )

    // ── 非 fatal 错误：让 hls.js 自带的内部重试先工作，仅累计计数 ────────────
    if (!fatal) {
      if (
        details === Hls.ErrorDetails.FRAG_LOAD_ERROR ||
        details === Hls.ErrorDetails.FRAG_LOAD_TIMEOUT ||
        details === Hls.ErrorDetails.KEY_LOAD_ERROR ||
        details === Hls.ErrorDetails.KEY_LOAD_TIMEOUT
      ) {
        fragFail++
        if (fragFail >= fragThreshold) switchToFallback(details)
        return
      }
      if (
        details === Hls.ErrorDetails.LEVEL_LOAD_ERROR ||
        details === Hls.ErrorDetails.LEVEL_LOAD_TIMEOUT ||
        details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR ||
        details === Hls.ErrorDetails.MANIFEST_LOAD_TIMEOUT
      ) {
        levelLoadFail++
        if (levelLoadFail >= levelThreshold) switchToFallback(details)
        return
      }
      if (details === Hls.ErrorDetails.LEVEL_PARSING_ERROR) {
        // playlist 中混入坏内容（HTML/空 body）。攒几次再切，因为偶发刷新失败
        // 后端会临时返回 503/502；hls.js 会自动重试 root playlist。
        levelLoadFail++
        if (levelLoadFail >= levelThreshold) switchToFallback(details)
        return
      }
      if (
        details === Hls.ErrorDetails.BUFFER_STALLED_ERROR ||
        details === Hls.ErrorDetails.BUFFER_NUDGE_ON_STALL
      ) {
        // hls.js 自己会做小幅 currentTime nudge，这里不做副作用，只计数。
        stallRecovery++
        if (stallRecovery >= stallThreshold) switchToFallback(details)
        return
      }
      // 其它非 fatal 错误：忽略，交给 hls.js 内部重试。
      return
    }

    // ── fatal 错误：先尝试 hls.js 内部恢复，再决定是否切源 ──────────────────
    // LEVEL_PARSING_ERROR 即便被标 fatal，本质是 playlist 内容问题，不是网络
    // 问题。继续 startLoad 不会修复，必须按 parsing 路径计数 + 切源。
    if (details === Hls.ErrorDetails.LEVEL_PARSING_ERROR) {
      levelLoadFail++
      if (levelLoadFail >= levelThreshold) switchToFallback(details)
      return
    }
    if (type === Hls.ErrorTypes.NETWORK_ERROR) {
      // 永久 503/404 时不能死循环 startLoad —— 计数并在阈值后切源。
      fatalNetworkRecover++
      if (fatalNetworkRecover >= fatalNetworkThreshold) {
        switchToFallback(details || type)
        return
      }
      try {
        hls.startLoad?.()
      } catch (e) {
        switchToFallback(details || type)
      }
      return
    }
    if (type === Hls.ErrorTypes.MEDIA_ERROR) {
      fatalMediaRecover++
      if (fatalMediaRecover >= fatalMediaThreshold) {
        switchToFallback(details || type)
        return
      }
      try {
        hls.recoverMediaError?.()
      } catch (e) {
        switchToFallback(details || type)
      }
      return
    }
    switchToFallback(details || type)
  }

  hls.on(Hls.Events.FRAG_LOADED, onFragLoaded)
  hls.on(Hls.Events.ERROR, onError)
  hls.__waveflowRuntimeCleanup = cleanup
}

function attachRuntimeMpegtsErrorHandlers(player, sourceUrl, usingProxy, attemptId, sourceIndex = -1) {
  clearRuntimeHandlerCleanup(player)
  let switching = false
  let completeWatchTimer = null
  const setRuntimeStatus = (status) => {
    if (sourceIndex >= 0) setSourceRuntimeStatus(sourceIndex, status)
    else setSourceRuntimeStatusByUrl(sourceUrl, status)
  }

  const clearCompleteWatchTimer = () => {
    if (!completeWatchTimer) return
    clearTimeout(completeWatchTimer)
    completeWatchTimer = null
  }

  const cleanup = () => {
    clearCompleteWatchTimer()
    player.off(mpegts.Events.ERROR, onError)
    player.off(mpegts.Events.LOADING_COMPLETE, onComplete)
    if (player.__waveflowRuntimeCleanup === cleanup) player.__waveflowRuntimeCleanup = null
  }

  const switchToFallback = async (reason) => {
    if (switching || !isAttemptActive(attemptId)) return
    switching = true
    console.warn(`[IPTV] MPEG-TS 播放中断，进入恢复流程: ${reason}`)
    cleanup()
    setRuntimeStatus('failed')
    clearCurrentMpegtsIf(player)
    try {
      player.destroy()
    } catch (e) {
      const message = e?.message || ''
      if (!message.includes('removeAllListeners')) {
        console.warn('[IPTV] mpegts runtime cleanup failed:', e)
      }
    }
    return await recoverIptvPlayback(`MPEG-TS:${reason}`, { force: true })
  }

  const destroyCurrentPlayer = (label) => {
    clearCurrentMpegtsIf(player)
    try {
      player.destroy()
    } catch (e) {
      const message = e?.message || ''
      if (!message.includes('removeAllListeners')) {
        console.warn(`[IPTV] mpegts ${label} cleanup failed:`, e)
      }
    }
  }

  const scheduleReconnectCurrentSource = (reason, options = {}) => {
    if (switching || !isAttemptActive(attemptId)) return
    const destroyBeforeDelay = options.destroyBeforeDelay !== false
    const delayMs = options.delayMs ?? MPEGTS_ERROR_RECONNECT_DELAY_MS
    const video = iptvVideoRef.value
    console.warn('[IPTV] MPEG-TS 播放中断，准备重连当前源', {
      reason,
      currentTime: Number.isFinite(video?.currentTime) ? video.currentTime : null,
      readyState: video?.readyState,
      networkState: video?.networkState,
    })

    switching = true
    cleanup()
    setRuntimeStatus('trying')
    playerStore.setPlaybackError('直播连接断开，正在重新连接当前源')
    playerStore.setLoading(true)

    if (destroyBeforeDelay) {
      destroyCurrentPlayer('reconnect')
    }

    completeWatchTimer = setTimeout(async () => {
      completeWatchTimer = null
      if (!isAttemptActive(attemptId)) return
      if (!destroyBeforeDelay) {
        destroyCurrentPlayer('reconnect')
      }

      await recoverIptvPlayback(`MPEG-TS:${reason}`, { force: true })
    }, delayMs)
  }

  const onError = (type, detail, info) => {
    scheduleReconnectCurrentSource(`${type || 'mpegts error'}:${detail || info?.msg || ''}`, {
      destroyBeforeDelay: true,
      delayMs: MPEGTS_ERROR_RECONNECT_DELAY_MS,
    })
  }

  const onComplete = () => {
    if (switching || !isAttemptActive(attemptId)) return
    if (completeWatchTimer) return

    scheduleReconnectCurrentSource('mpegts EOF', {
      destroyBeforeDelay: false,
      delayMs: MPEGTS_EOF_RECONNECT_DELAY_MS,
    })
  }

  player.on(mpegts.Events.ERROR, onError)
  player.on(mpegts.Events.LOADING_COMPLETE, onComplete)
  player.__waveflowRuntimeCleanup = cleanup
}

async function tryPlayIptv(url, usingProxy = false, customUa = '', attemptId = 0, sourceIndex = -1, playbackSourceType = '', options = {}) {
  if (!isAttemptActive(attemptId)) throw cancelledError()
  if (!iptvVideoRef.value) throw new Error('播放器未就绪')
  clearPauseReleaseTimer()
  _softPausedAt = 0
  _softPauseReleased = false

  const setRuntimeStatus = (status) => {
    if (sourceIndex >= 0) setSourceRuntimeStatus(sourceIndex, status)
    else setSourceRuntimeStatusByUrl(url, status)
  }

  setRuntimeStatus('trying')

  cancelCurrentStartup()
  const preflightController = new AbortController()
  let preflightCancelled = false
  const cancelPreflight = () => {
    preflightCancelled = true
    preflightController.abort()
  }
  _cancelCurrentStartup = cancelPreflight
  cancelActiveProxyRace()
  destroyIptvEngines()
  activeIptvEngine.value = 'video'
  if (options.preserveFrame) {
    stopPlaybackWatchdogs()
    resetMediaAspect()
  } else {
    resetIptvVideo()
  }
  playerStore.setLoading(true)
  const suppliedTransport = options.effectiveTransport?.url && options.effectiveTransport?.sourceType
    ? options.effectiveTransport
    : null
  let effectiveUrl = suppliedTransport?.url || url
  let resolvedSourceType = playbackEngineType(
    suppliedTransport?.sourceType || playbackSourceType || sourceType({ url }),
    effectiveUrl,
  )
  let proxyTransport = suppliedTransport
  const knownRtspProxy = isKnownRtspProxyPlaylist(url, usingProxy, playbackSourceType)
  if (!suppliedTransport && !knownRtspProxy) {
    try {
      proxyTransport = await preflightProxyPlaylistTransport(url, usingProxy, preflightController.signal)
    } finally {
      if (_cancelCurrentStartup === cancelPreflight) _cancelCurrentStartup = null
    }
  } else if (_cancelCurrentStartup === cancelPreflight) {
    _cancelCurrentStartup = null
  }
  if (preflightCancelled || !isAttemptActive(attemptId)) throw cancelledError()
  if (proxyTransport?.url && proxyTransport.sourceType) {
    effectiveUrl = proxyTransport.url
    resolvedSourceType = playbackEngineType(proxyTransport.sourceType, effectiveUrl)
  }

  const useHls = resolvedSourceType === 'hls'
  const useMpegTs = isMpegTsEngineType(resolvedSourceType) || (!useHls && isMpegTsUrl(effectiveUrl))

  if (!useHls && !useMpegTs) {
    setRuntimeStatus('failed')
    throw new Error('不支持的播放格式')
  }

  if (useMpegTs && !canUseMpegTs()) {
    setRuntimeStatus('failed')
    throw new Error(`当前浏览器不支持 ${resolvedSourceType === 'http_flv' ? 'HTTP-FLV' : 'MPEG-TS'} 播放`)
  }

  console.log(`[START] ${usingProxy ? '(proxy) ' : ''}${effectiveUrl.slice(0, 80)}...`)

  return new Promise((resolve, reject) => {
    let settled = false
    let settling = false
    let timer = null
    let hlsInstance = null
    let mpegtsInstance = null
    const hlsEventFns = []
    const mpegtsEventFns = []
    let mpegtsProgressTimer = null
    let nativeCleanup = null  // 原生 HLS listener cleanup
    let cancelStartup = null

    const clearStartupCancel = () => {
      if (_cancelCurrentStartup === cancelStartup) _cancelCurrentStartup = null
    }

    const cleanupPending = () => {
      clearTimeout(timer)
      timer = null
      clearInterval(mpegtsProgressTimer)
      mpegtsProgressTimer = null
      for (const [evt, fn] of hlsEventFns) hlsInstance?.off(evt, fn)
      hlsEventFns.length = 0
      for (const [evt, fn] of mpegtsEventFns) mpegtsInstance?.off(evt, fn)
      mpegtsEventFns.length = 0
      if (nativeCleanup) {
        nativeCleanup()
        nativeCleanup = null
      }
    }

    const cleanupFailure = () => {
      cleanupPending()
      if (hlsInstance) {
        releaseTrackedHls(hlsInstance)
        hlsInstance.destroy()
        clearCurrentHlsIf(hlsInstance)
        hlsInstance = null
      }
      if (mpegtsInstance) {
        try {
          mpegtsInstance.destroy()
        } catch (e) {
          console.warn('[IPTV] mpegts failure cleanup failed:', e)
        }
        clearCurrentMpegtsIf(mpegtsInstance)
        mpegtsInstance = null
      }
    }

    cancelStartup = () => {
      if (settled) return
      settled = true
      cleanupFailure()
      clearStartupCancel()
      reject(cancelledError())
    }
    _cancelCurrentStartup = cancelStartup

    const safeResolve = async () => {
      if (settled || settling) return
      if (!isAttemptActive(attemptId)) {
        settled = true
        cleanupFailure()
        clearStartupCancel()
        reject(cancelledError())
        return
      }
      settling = true
      cleanupPending()
      try {
        if (!iptvVideoRef.value) throw new Error('播放器未就绪')
        iptvVideoRef.value.volume = volume.value
        updateMediaAspectFromVideo()
        await iptvVideoRef.value.play()
        if (settled) return
        if (!isAttemptActive(attemptId)) {
          settled = true
          cleanupFailure()
          clearStartupCancel()
          reject(cancelledError())
          return
        }
        if (hlsInstance) attachRuntimeHlsErrorHandlers(hlsInstance, url, usingProxy, attemptId, sourceIndex)
        if (mpegtsInstance) attachRuntimeMpegtsErrorHandlers(mpegtsInstance, url, usingProxy, attemptId, sourceIndex)
        playerStore.togglePlay(true)
        setRuntimeStatus('playing')
        settled = true
        clearStartupCancel()
        resolve()
      } catch (e) {
        if (settled) return
        settled = true
        cleanupFailure()
        if (isAttemptActive(attemptId)) setRuntimeStatus('failed')
        clearStartupCancel()
        reject(e)
      }
    }

    const safeReject = (err) => {
      if (settled) return
      if (!isAttemptActive(attemptId)) {
        settled = true
        cleanupFailure()
        clearStartupCancel()
        reject(cancelledError())
        return
      }
      settled = true
      cleanupFailure()
      setRuntimeStatus('failed')
      clearStartupCancel()
      reject(err)
    }

    // MPEG-TS / HTTP-FLV over mpegts.js path
    if (useMpegTs) {
      const player = mpegts.createPlayer({
        type: mpegtsPlayerType(resolvedSourceType, effectiveUrl),
        isLive: true,
        cors: true,
        url: effectiveUrl,
      }, {
        enableWorker: true,
        lazyLoad: false,
        liveBufferLatencyChasing: true,
        statisticsInfoReportInterval: 1000,
      })
      mpegtsInstance = player
      iptvMpegtsRef.value = player

      const video = iptvVideoRef.value
      const timeSnapshot = video.currentTime || 0
      const onMediaInfo = () => {
        try { video.play()?.catch?.(() => {}) } catch {}
      }
      const onStats = (stats) => {
        if ((stats?.decodedFrames || 0) > 0) safeResolve()
      }
      const onMpegtsError = (type, detail, info) => {
        safeReject(new Error(`${type || 'mpegts error'}:${detail || info?.msg || ''}`))
      }
      const onVideoError = () => safeReject(new Error('MPEG-TS 视频错误'))

      player.on(mpegts.Events.MEDIA_INFO, onMediaInfo)
      player.on(mpegts.Events.STATISTICS_INFO, onStats)
      player.on(mpegts.Events.ERROR, onMpegtsError)
      mpegtsEventFns.push([mpegts.Events.MEDIA_INFO, onMediaInfo])
      mpegtsEventFns.push([mpegts.Events.STATISTICS_INFO, onStats])
      mpegtsEventFns.push([mpegts.Events.ERROR, onMpegtsError])
      video.addEventListener('error', onVideoError)
      nativeCleanup = () => {
        video.removeEventListener('error', onVideoError)
      }

      player.attachMediaElement(video)
      player.load()
      mpegtsProgressTimer = setInterval(() => {
        if ((video.currentTime || 0) - timeSnapshot > 0.1) safeResolve()
      }, 250)
      timer = setTimeout(() => safeReject(new Error('MPEG-TS 加载超时')), options.startupTimeoutMs ?? 12_000)
      return
    }

    // HLS path
    if (canUseHls()) {
      const hlsConfig = {
        enableWorker: true, lowLatencyMode: false, liveDurationInfinity: true,
        // Thin 直通模式下，前端拿到的是上游原始滑动窗口，常见
        // 6 segments * 10s ≈ 60s。liveSyncDuration 必须明显小于窗口才能留出
        // 安全 buffer 余量；让 hls.js 从靠近 edge 的位置起播，但同时通过
        // maxLiveSyncPlaybackRate 允许轻微加速追赶 live edge。
        liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 6,
        liveSyncOnStallIncrease: 1,
        maxLiveSyncPlaybackRate: 1.2, nudgeOffset: 0.1, nudgeMaxRetry: 5,
        // maxBufferHole 保持 hls.js 默认 0.5：只跨过亚秒级浮点抖动，不跨真实缺段，
        // 避免在稳定源上误跳真实内容。maxFragLookUpTolerance 放宽片段对齐查找容忍。
        maxBufferLength: 30, maxBufferHole: 0.5, maxFragLookUpTolerance: 1,
        highBufferWatchdogPeriod: 3,
      }
      if (isIOS) {
        Object.assign(hlsConfig, {
          liveSyncDurationCount: 4,
          liveMaxLatencyDurationCount: 8,
          maxBufferLength: 60,
          maxMaxBufferLength: 90,
          liveSyncOnStallIncrease: 2,
        })
      }
      if (customUa) hlsConfig.xhrSetup = (xhr) => { xhr.setRequestHeader('User-Agent', customUa) }
      const hls = new Hls(hlsConfig)
      trackHlsSource(hls)
      hlsInstance = hls
      iptvHlsRef.value = hls
      let fragFail = 0
      const threshold = usingProxy ? 2 : 3

      // Network completion is not playback readiness: wait until hls.js has
      // appended the first fragment before calling video.play(). Calling it
      // from FRAG_LOADED can start with an empty SourceBuffer and immediately
      // enter a waiting/rebuffer cycle on live RTSP-backed HLS.
      const onFragBuffered = () => { fragFail = 0; safeResolve() }
      hls.on(Hls.Events.FRAG_BUFFERED, onFragBuffered)
      hlsEventFns.push([Hls.Events.FRAG_BUFFERED, onFragBuffered])

      const onError = (_, d) => {
        console.log(`[ERR] ${d.details} fatal:${d.fatal}`)
        if (!d.fatal && d.details === Hls.ErrorDetails.FRAG_LOAD_ERROR) {
          fragFail++
          if (fragFail >= threshold) safeReject(new Error(d.details))
          return
        }
        if (d.fatal || d.type === Hls.ErrorTypes.NETWORK_ERROR) {
          safeReject(new Error(d.details))
        }
      }
      hls.on(Hls.Events.ERROR, onError)
      hlsEventFns.push([Hls.Events.ERROR, onError])

      hls.loadSource(effectiveUrl)
      hls.attachMedia(iptvVideoRef.value)
      timer = setTimeout(() => safeReject(new Error('HLS 加载超时')), options.startupTimeoutMs ?? 10_000)
      return
    }

    // Safari native HLS
    if (iptvVideoRef.value.canPlayType('application/vnd.apple.mpegurl')) {
      const video = iptvVideoRef.value
      video.src = effectiveUrl
      const onLoaded = () => safeResolve()
      const onErr = () => safeReject(new Error('原生 HLS 错误'))
      video.addEventListener('loadedmetadata', onLoaded)
      video.addEventListener('error', onErr)
      nativeCleanup = () => {
        video.removeEventListener('loadedmetadata', onLoaded)
        video.removeEventListener('error', onErr)
      }
      timer = setTimeout(() => safeReject(new Error('原生 HLS 超时')), options.startupTimeoutMs ?? 10_000)
      return
    }

    safeReject(new Error('当前浏览器不支持 HLS 播放'))
  })
}

function pruneRaceLosers(now = Date.now()) {
  for (const [key, expireAt] of _racedLosers.entries()) {
    if (!expireAt || expireAt <= now) _racedLosers.delete(key)
  }
}

function isRaceLoser(entry) {
  const key = sourceRaceKey(entry)
  if (!key) return false
  const expireAt = _racedLosers.get(key)
  if (!expireAt) return false
  if (expireAt <= Date.now()) {
    _racedLosers.delete(key)
    return false
  }
  return true
}

function markRaceLoser(entry) {
  const key = sourceRaceKey(entry)
  if (!key) return
  _racedLosers.set(key, Date.now() + RACE_LOSER_TTL_MS)
}

function clearRaceLoser(entry) {
  const key = sourceRaceKey(entry)
  if (!key) return
  _racedLosers.delete(key)
}

function resetRacedLosers() { _racedLosers.clear() }

function isProxyLikeEntry(entry) {
  return entry?.type === 'proxy' || Boolean(entry?.via_proxy)
}

function entrySourceId(entry) {
  return entry?.source_id || extractSourceIdFromUrl(entry?.url || '') || ''
}

function raceHealthRank(entry) {
  const status = String(entry?.probe_status || '').toLowerCase()
  const working = Number(entry?.is_working)
  if (working === 1 || status === 'online') return 0
  if (status === 'not_live') return 3
  if (working === 0 || ['timeout', 'error', 'offline'].includes(status)) return 2
  return 1
}

function raceLatencyRank(entry) {
  const latency = Number(entry?.latency_ms)
  return Number.isFinite(latency) && latency > 0 ? latency : Number.POSITIVE_INFINITY
}

function raceCandidateKind(entry) {
  return startupRaceCandidateKind(entry, {
    sourceType: sourceType(entry),
    hlsSupported: canUseHls(),
    mpegTsSupported: canUseMpegTs(),
  })
}

function sortedRaceCandidates(urls, startIndex, proxyLike) {
  pruneRaceLosers()
  return urls
    .map((entry, index) => ({ entry, index, kind: raceCandidateKind(entry) }))
    .filter(({ entry, index, kind }) => {
      if (index < startIndex || !kind) return false
      if (isProxyLikeEntry(entry) !== proxyLike) return false
      if (isRaceLoser(entry)) return false
      return true
    })
    .sort((a, b) => {
      const healthDelta = raceHealthRank(a.entry) - raceHealthRank(b.entry)
      if (healthDelta) return healthDelta
      const latencyDelta = raceLatencyRank(a.entry) - raceLatencyRank(b.entry)
      if (latencyDelta) return latencyDelta
      return a.index - b.index
    })
    .slice(0, STARTUP_RACE_LIMIT)
}

function collectDirectRacers(urls, startIndex = 0) {
  return sortedRaceCandidates(urls, startIndex, false)
}

function collectProxyRacers(urls, startIndex = 0) {
  return sortedRaceCandidates(urls, startIndex, true)
}

async function playCurrentIptvUrl(attemptId = 0, options = {}) {
  if (!isAttemptActive(attemptId)) return
  if (!iptvVideoRef.value || !playerStore.currentIptvChannel) return
  const allowStartupRace = options.allowStartupRace !== false
  const allowFallback = options.allowFallback !== false
  const allowCurrentRetry = options.allowCurrentRetry !== false
  const allowVolatileResolve = options.allowVolatileResolve !== false
  const urls = playerStore.iptvUrls
  const idx = playerStore.iptvUrlIndex
  preflightYoutubeApiForQueue(urls)
  if (idx >= urls.length) {
    markAllIptvSourcesUnavailable(attemptId)
    return
  }
  const entry = urls[idx]
  const st = sourceType(entry)
  if (st === 'unsupported_youtube_url') {
    setSourceRuntimeStatus(idx, 'failed')
    console.warn('[IPTV] 不支持的 YouTube URL:', entry.url)
    if (!allowFallback) throw new Error('不支持的 YouTube URL')
    if (allowFallback && await fallbackToNextIptvUrl(attemptId)) {
      return await playCurrentIptvUrl(attemptId, options)
    }
    markAllIptvSourcesUnavailable(attemptId)
    return
  }
  const directRacers = allowStartupRace && st !== 'youtube' ? collectDirectRacers(urls, idx) : []
  const proxyRacers = allowStartupRace && st !== 'youtube' ? collectProxyRacers(urls, idx) : []
  if (directRacers.length + proxyRacers.length > 1) {
    const raced = await runHedgedRace(directRacers, proxyRacers, attemptId, {
      timeoutMs: options.raceTimeoutMs,
      playCurrentOptions: options,
    })
    if (raced !== false) return raced
    if (!isAttemptActive(attemptId)) return
  }

  console.log(`[START] ${entry.type}:${entry.url.slice(0, 60)}...`)
  try {
    setSourceRuntimeStatus(idx, 'trying')
    if (st === 'youtube') {
      await startYoutubeCandidate(entry, attemptId, idx)
    } else {
      const playbackType = st === 'adapter' && isProxyLikeEntry(entry) && entry?.adapter_transport_pending
        ? 'hls'
        : st
      await tryPlayIptv(entry.url, isProxyLikeEntry(entry), entry.custom_ua || '', attemptId, idx, playbackType, {
        startupTimeoutMs: options.startupTimeoutMs,
        preserveFrame: options.preserveFrame,
      })
    }
    if (!isAttemptActive(attemptId)) return
    setSourceRuntimeStatus(idx, 'playing')
    clearRaceLoser(entry)
    playerStore.clearPlaybackError()
    playerStore.setLoading(false)
  } catch (e) {
    if (!isAttemptActive(attemptId)) return
    if (isAutoplayBlockedError(e)) {
      setSourceRuntimeStatus(idx, 'trying')
      playerStore.setPlaybackError('浏览器阻止自动播放，请点击播放按钮继续。')
      playerStore.setLoading(false)
      playerStore.togglePlay(false)
      return
    }
    setSourceRuntimeStatus(idx, 'failed')
    console.warn('[IPTV] 失败:', e?.message)
    if (allowCurrentRetry && isMpegTsEngineType(st) && allowStartupRace === false) {
      const reconnectCount = recordMpegtsReconnect(entry.url)
      if (reconnectCount <= MPEGTS_RECONNECT_LIMIT) {
        console.warn('[IPTV] MPEG-TS 重连起播失败，继续重试当前源', {
          reconnectCount,
          limit: MPEGTS_RECONNECT_LIMIT,
          reason: e?.message || 'startup failed',
        })
        setSourceRuntimeStatus(idx, 'trying')
        playerStore.setPlaybackError('直播连接断开，正在重新连接当前源')
        playerStore.setLoading(true)
        await new Promise(resolve => setTimeout(resolve, MPEGTS_ERROR_RECONNECT_DELAY_MS))
        if (!isAttemptActive(attemptId)) return
        const nextAttemptId = ++_playAttemptId
        return await playCurrentIptvUrl(nextAttemptId, { ...options, allowStartupRace: false })
      }
    }
    // Volatile adapter: re-resolve for a fresh URL before giving up
    if (allowVolatileResolve && entry.adapter_volatile_url && entry.adapter_source_url) {
      const retryCount = entry._volatileRetryCount || 0
      if (retryCount < 2) {
        try {
          console.warn('[IPTV] volatile adapter URL 失败，重新 resolve... (retry ' + (retryCount + 1) + '/2)')
          setSourceRuntimeStatus(idx, 'trying')
          playerStore.setPlaybackError('直连失败，正在获取新地址...')
          playerStore.setLoading(true)
          const resolved = await playerStore.reResolveAdapterUrl(entry.adapter_source_url)
          if (!isAttemptActive(attemptId)) return
          if (resolved && resolved.url) {
            entry.url = resolved.url
            if (resolved.source_type) entry.source_type = resolved.source_type
            entry._volatileRetryCount = retryCount + 1
            const nextAttemptId = ++_playAttemptId
            return await playCurrentIptvUrl(nextAttemptId, { ...options, allowStartupRace: false })
          }
        } catch (reErr) {
          console.warn('[IPTV] volatile adapter re-resolve 失败:', reErr?.message)
        }
      }
    }
    if (!allowFallback) throw e
    if (await fallbackToNextIptvUrl(attemptId)) {
      return await playCurrentIptvUrl(attemptId, options)
    }
    markAllIptvSourcesUnavailable(attemptId)
  }
}

function probeBufferAhead(video) {
  if (!video?.buffered?.length) return 0
  const current = video.currentTime || 0
  for (let i = 0; i < video.buffered.length; i += 1) {
    const start = video.buffered.start(i)
    const end = video.buffered.end(i)
    if (current >= start && current <= end) return Math.max(0, end - current)
    if (current < start) return Math.max(0, end - start)
  }
  return 0
}

function hlsProbeHasRealProgress(video, snapshotTime) {
  return Boolean(
    video?.buffered?.length > 0
    && probeBufferAhead(video) > 0.3
    && video.readyState >= 2
    && (video.currentTime || 0) - snapshotTime > 0.1,
  )
}

async function runHedgedRace(directRacers, proxyRacers, attemptId = 0, options = {}) {
  if (!isAttemptActive(attemptId)) return
  const playCurrentOptions = options.playCurrentOptions || {}
  const directUrlSet = new Set(directRacers.map(({ entry }) => entry?.url).filter(Boolean))
  const stage2ProxyRacers = proxyRacers.filter(({ entry }) => entry?.url && !directUrlSet.has(entry.url))
  const allCandidates = [...directRacers, ...stage2ProxyRacers]
  if (allCandidates.length < 2) return false

  cancelCurrentStartup()
  cancelActiveProxyRace()
  destroyIptvEngines()
  resetIptvVideo()
  playerStore.setLoading(true)

  console.log(`[RACE:hedged] direct=${directRacers.length} proxy=${stage2ProxyRacers.length} delay=${HEDGED_PROXY_DELAY_MS}ms`)

  let failCount = 0
  let raceTimer = null
  let proxyTimer = null
  let settled = false
  const racers = []

  const cleanupRacer = (racer) => {
    if (!racer || racer.cleaned) return
    racer.cleaned = true
    racer.abortController?.abort()
    racer.abortController = null
    for (const timer of racer.timers || []) clearTimeout(timer)
    if (racer.interval) clearInterval(racer.interval)
    for (const cleanup of racer.cleanups || []) {
      try { cleanup() } catch {}
    }
    try {
      if (racer.kind === 'hls' && racer.engine) {
        racer.engine.stopLoad?.()
        racer.engine.detachMedia?.()
        racer.engine.destroy()
      } else if (isMpegTsEngineType(racer.kind) && racer.engine) {
        racer.engine.unload?.()
        racer.engine.detachMediaElement?.()
        racer.engine.destroy()
      }
    } catch (e) {
      const message = e?.message || ''
      if (!message.includes('removeAllListeners')) {
        console.warn('[RACE:hedged] cleanup failed:', e)
      }
    }
    if (racer.video?.parentNode) racer.video.remove()
  }

  const result = await new Promise((resolve) => {
    const finish = (value) => {
      if (settled) return
      settled = true
      clearTimeout(raceTimer)
      clearTimeout(proxyTimer)
      for (const racer of racers) {
        if (value && racer !== value.racer) {
          const racerIndex = playerStore.iptvUrls.indexOf(racer.entry)
          if (getSourceRuntimeStatus(racerIndex) === 'trying') {
            setSourceRuntimeStatus(racerIndex, 'stopped')
          }
        }
        cleanupRacer(racer)
      }
      if (_cleanupActiveRace === cancelRace) _cleanupActiveRace = null
      resolve(value)
    }

    const failRacer = (racer) => {
      if (settled || racer.cleaned || racer.failed) return
      racer.failed = true
      failCount++
      markRaceLoser(racer.entry)
      setSourceRuntimeStatusByEntry(racer.entry, 'failed')
      cleanupRacer(racer)
      if (failCount >= allCandidates.length) finish(null)
    }

    const winRacer = (racer, label) => {
      if (!isAttemptActive(attemptId)) { finish(null); return }
      if (settled || racer.cleaned || racer.failed) return
      console.log('[RACE:hedged] winner', {
        label,
        source_id: entrySourceId(racer.entry),
        request_source_id: extractSourceIdFromUrl(racer.entry?.url || ''),
        transport: sourceTransport(racer.entry),
        url: (racer.entry?.url || '').slice(0, 80),
      })
      setSourceRuntimeStatusByEntry(racer.entry, 'trying')
      finish({
        racer,
        entry: racer.entry,
        index: racer.index,
        effectiveTransport: racer.effectiveTransport,
      })
    }

    const armHlsConfirmation = (racer, label) => {
      if (settled || racer.cleaned || racer.failed || racer.confirming) return
      racer.confirming = true
      try { racer.video.play()?.catch?.(() => {}) } catch {}
      const snapshotTime = racer.video.currentTime || 0
      const firstTimer = setTimeout(() => {
        if (settled || racer.cleaned || racer.failed) return
        if (hlsProbeHasRealProgress(racer.video, snapshotTime)) {
          winRacer(racer, label)
          return
        }
        const retryTimer = setTimeout(() => {
          if (settled || racer.cleaned || racer.failed) return
          if (hlsProbeHasRealProgress(racer.video, snapshotTime)) winRacer(racer, label)
          else failRacer(racer)
        }, RACE_HLS_CONFIRM_RETRY_MS)
        racer.timers.push(retryTimer)
      }, RACE_HLS_CONFIRM_MS)
      racer.timers.push(firstTimer)
    }

    const cancelRace = () => finish(null)
    _cleanupActiveRace = cancelRace

    raceTimer = setTimeout(() => {
      for (const { entry } of allCandidates) {
        markRaceLoser(entry)
        setSourceRuntimeStatusByEntry(entry, 'failed')
      }
      finish(null)
    }, options.timeoutMs ?? 12_000)

    const startRacers = (candidates, phaseLabel) => {
      if (settled || !candidates.length) return
      candidates.forEach(({ entry }) => setSourceRuntimeStatusByEntry(entry, 'trying'))
      candidates.forEach(({ entry, index, kind: initialKind }, i) => {
        const racer = {
          engine: null,
          video: null,
          entry,
          index,
          kind: initialKind,
          cleaned: false,
          failed: false,
          fragFail: 0,
          timers: [],
          cleanups: [],
          lastDecodedFrames: 0,
          abortController: null,
          effectiveTransport: initialKind && initialKind !== 'proxy_auto'
            ? { url: entry.url, sourceType: initialKind }
            : null,
        }
        racers.push(racer)

        void (async () => {
          let kind = initialKind
          let effectiveUrl = entry.url
          if (kind === 'proxy_auto') {
            const controller = new AbortController()
            racer.abortController = controller
            const transport = await preflightProxyPlaylistTransport(entry.url, true, controller.signal)
            racer.abortController = null
            if (settled || racer.cleaned || racer.failed || !isAttemptActive(attemptId)) return
            if (transport?.url && transport.sourceType) {
              effectiveUrl = transport.url
              kind = startupRaceCandidateKind(
                { ...entry, adapter_transport_pending: false, url: effectiveUrl },
                {
                  sourceType: transport.sourceType,
                  hlsSupported: canUseHls(),
                  mpegTsSupported: canUseMpegTs(),
                },
              )
              racer.effectiveTransport = { url: effectiveUrl, sourceType: kind }
            } else {
              kind = canUseHls() ? 'hls' : ''
              racer.effectiveTransport = kind ? { url: effectiveUrl, sourceType: kind } : null
            }
            racer.kind = kind
            if (!kind || kind === 'proxy_auto') {
              failRacer(racer)
              return
            }
          }

          const probeVideo = document.createElement('video')
          racer.video = probeVideo
          probeVideo.muted = true
          probeVideo.playsInline = true
          probeVideo.autoplay = true
          probeVideo.style.display = 'none'
          document.body.appendChild(probeVideo)

          if (kind === 'hls') {
            const hlsConfig = { enableWorker: false, maxBufferLength: 1, maxMaxBufferLength: 2 }
            if (entry.custom_ua) hlsConfig.xhrSetup = (xhr) => { xhr.setRequestHeader('User-Agent', entry.custom_ua) }
            const hls = new Hls(hlsConfig)
            racer.engine = hls
            trackHlsSource(hls)

            const label = `${phaseLabel}#${i} HLS`
            const onMediaAttached = () => {
              try { probeVideo.play()?.catch?.(() => {}) } catch {}
            }
            const onFragBuffered = () => {
              clearTimeout(racer.fragLoadedFallbackTimer)
              armHlsConfirmation(racer, label)
            }
            const onFragLoaded = () => {
              if (racer.confirming || racer.fragLoadedFallbackTimer) return
              racer.fragLoadedFallbackTimer = setTimeout(() => {
                racer.fragLoadedFallbackTimer = null
                armHlsConfirmation(racer, label)
              }, 250)
              racer.timers.push(racer.fragLoadedFallbackTimer)
            }
            const onError = (_, d) => {
              if (!isAttemptActive(attemptId)) { finish(null); return }
              if (settled || racer.cleaned || racer.failed) return
              if (!d.fatal && d.details === Hls.ErrorDetails.FRAG_LOAD_ERROR) racer.fragFail++
              if (d.fatal || d.type === Hls.ErrorTypes.NETWORK_ERROR || racer.fragFail >= 2) {
                failRacer(racer)
              }
            }
            hls.on(Hls.Events.MEDIA_ATTACHED, onMediaAttached)
            hls.on(Hls.Events.FRAG_BUFFERED, onFragBuffered)
            hls.on(Hls.Events.FRAG_LOADED, onFragLoaded)
            hls.on(Hls.Events.ERROR, onError)
            racer.cleanups.push(() => hls.off(Hls.Events.MEDIA_ATTACHED, onMediaAttached))
            racer.cleanups.push(() => hls.off(Hls.Events.FRAG_BUFFERED, onFragBuffered))
            racer.cleanups.push(() => hls.off(Hls.Events.FRAG_LOADED, onFragLoaded))
            racer.cleanups.push(() => hls.off(Hls.Events.ERROR, onError))
            hls.loadSource(effectiveUrl)
            hls.attachMedia(probeVideo)
            return
          }

          const player = mpegts.createPlayer({
            type: mpegtsPlayerType(kind, effectiveUrl),
            isLive: true,
            cors: true,
            url: effectiveUrl,
          }, {
            enableWorker: true,
            lazyLoad: false,
            liveBufferLatencyChasing: true,
            statisticsInfoReportInterval: 1000,
          })
          racer.engine = player
          const label = `${phaseLabel}#${i} ${kind === 'http_flv' ? 'HTTP-FLV' : 'MPEG-TS'}`
          const timeSnapshot = probeVideo.currentTime || 0
          const onMediaInfo = () => {
            try { probeVideo.play()?.catch?.(() => {}) } catch {}
          }
          const onStats = (stats) => {
            const decodedFrames = Number(stats?.decodedFrames || 0)
            if (decodedFrames > racer.lastDecodedFrames) {
              winRacer(racer, label)
              return
            }
            racer.lastDecodedFrames = Math.max(racer.lastDecodedFrames, decodedFrames)
          }
          const onError = () => failRacer(racer)
          const onVideoLoaded = () => {
            try { probeVideo.play()?.catch?.(() => {}) } catch {}
          }
          const onVideoError = () => failRacer(racer)
          player.on(mpegts.Events.MEDIA_INFO, onMediaInfo)
          player.on(mpegts.Events.STATISTICS_INFO, onStats)
          player.on(mpegts.Events.ERROR, onError)
          racer.cleanups.push(() => player.off(mpegts.Events.MEDIA_INFO, onMediaInfo))
          racer.cleanups.push(() => player.off(mpegts.Events.STATISTICS_INFO, onStats))
          racer.cleanups.push(() => player.off(mpegts.Events.ERROR, onError))
          probeVideo.addEventListener('loadedmetadata', onVideoLoaded)
          probeVideo.addEventListener('canplay', onVideoLoaded)
          probeVideo.addEventListener('error', onVideoError)
          racer.cleanups.push(() => probeVideo.removeEventListener('loadedmetadata', onVideoLoaded))
          racer.cleanups.push(() => probeVideo.removeEventListener('canplay', onVideoLoaded))
          racer.cleanups.push(() => probeVideo.removeEventListener('error', onVideoError))
          racer.interval = setInterval(() => {
            if (!settled && !racer.cleaned && !racer.failed && (probeVideo.currentTime || 0) - timeSnapshot > 0.1) {
              winRacer(racer, label)
            }
          }, 500)
          player.attachMediaElement(probeVideo)
          player.load()
          try { probeVideo.play()?.catch?.(() => {}) } catch {}
        })().catch(() => failRacer(racer))
      })
    }

    if (directRacers.length) {
      startRacers(directRacers, 'direct')
      if (stage2ProxyRacers.length) {
        proxyTimer = setTimeout(() => startRacers(stage2ProxyRacers, 'proxy'), HEDGED_PROXY_DELAY_MS)
      }
    } else {
      startRacers(stage2ProxyRacers, 'proxy')
    }
  })

  if (!isAttemptActive(attemptId)) return

  if (!result) {
    console.warn('[RACE:hedged] 本轮播放源探测全部失败')
    const lastRacedIndex = Math.max(...allCandidates.map(({ index }) => index))
    if (lastRacedIndex >= 0) await setIptvUrlIndexForAttempt(lastRacedIndex, attemptId)
    if (await fallbackToNextIptvUrl(attemptId)) {
      return await playCurrentIptvUrl(attemptId, playCurrentOptions)
    }
    markAllIptvSourcesUnavailable(attemptId)
    return
  }

  const { entry: winnerEntry, index: winnerIndex, effectiveTransport } = result
  if (!(await setIptvUrlIndexForAttempt(winnerIndex, attemptId))) return

  try {
    await tryPlayIptv(winnerEntry.url, isProxyLikeEntry(winnerEntry), winnerEntry.custom_ua || '', attemptId, winnerIndex, effectiveTransport?.sourceType || sourceType(winnerEntry), {
      startupTimeoutMs: playCurrentOptions.startupTimeoutMs,
      preserveFrame: playCurrentOptions.preserveFrame,
      effectiveTransport,
    })
    if (!isAttemptActive(attemptId)) return
    setSourceRuntimeStatus(winnerIndex, 'playing')
    clearRaceLoser(winnerEntry)
    playerStore.clearPlaybackError()
    playerStore.setLoading(false)
  } catch (e) {
    if (!isAttemptActive(attemptId)) return
    markRaceLoser(winnerEntry)
    setSourceRuntimeStatus(winnerIndex, 'failed')
    console.warn('[RACE:hedged] 胜出源正式起播失败:', e?.message)
    if (await fallbackToNextIptvUrl(attemptId)) {
      return await playCurrentIptvUrl(attemptId, playCurrentOptions)
    }
    markAllIptvSourcesUnavailable(attemptId)
  }
}

async function handleIptvError(e) {
  if (activeIptvEngine.value === 'youtube') return
  console.warn('[IPTV] video error:', e?.target?.error?.message || '')
  // hls.js / mpegts.js 接管中 → 由各自 ERROR 事件处理
  if (iptvHlsRef.value || iptvMpegtsRef.value) return
  // 起播阶段（Promise 还没 resolve）→ 由 Promise reject 处理
  if (playerStore.isLoading) return
  // 播放中途暴毙 -> 先恢复当前源，失败后再 fallback
  await recoverIptvPlayback('video-error', { force: true })
}

let _stallRecovering = false
let _stallRecoverySeq = 0
let _lastRecoveryTime = 0
let _lastVideoProgressAt = 0
let _lastVideoCurrentTime = 0
let _stallRecoveryTimer = null
let _lastVideoFrameAt = 0
let _lastPresentedFrames = 0
let _lastVideoFrameMediaTime = 0
let _videoFrameCallbackId = null
let _videoFrameWatchTimer = null
let _videoFrameWatchVideo = null
let _videoFrameWatchSeq = 0
let _playbackProgressWatchTimer = null
let _playbackProgressWatchVideo = null
let _playbackProgressWatchSeq = 0
let _lastAvSyncRecoveryTime = 0
let _avSyncFollowupTimer = null
let _lastReconnectTime = 0
let _lastBufferNudgeTime = 0

function stopVideoFrameWatch() {
  _videoFrameWatchSeq++
  if (_videoFrameCallbackId !== null && _videoFrameWatchVideo?.cancelVideoFrameCallback) {
    try {
      _videoFrameWatchVideo.cancelVideoFrameCallback(_videoFrameCallbackId)
    } catch {}
  }
  _videoFrameCallbackId = null
  _videoFrameWatchVideo = null
  if (_videoFrameWatchTimer) {
    clearInterval(_videoFrameWatchTimer)
    _videoFrameWatchTimer = null
  }
}

function stopPlaybackProgressWatch() {
  _playbackProgressWatchSeq++
  _playbackProgressWatchVideo = null
  if (_playbackProgressWatchTimer) {
    clearInterval(_playbackProgressWatchTimer)
    _playbackProgressWatchTimer = null
  }
}

function stopPlaybackWatchdogs() {
  _stallRecoverySeq++
  _stallRecovering = false
  clearStallRecoveryTimer()
  stopVideoFrameWatch()
  stopPlaybackProgressWatch()
  if (_avSyncFollowupTimer) {
    clearTimeout(_avSyncFollowupTimer)
    _avSyncFollowupTimer = null
  }
  _lastVideoProgressAt = 0
  _lastVideoCurrentTime = 0
  _lastVideoFrameAt = 0
  _lastPresentedFrames = 0
  _lastVideoFrameMediaTime = 0
}

function clearStallRecoveryTimer() {
  if (!_stallRecoveryTimer) return
  clearTimeout(_stallRecoveryTimer)
  _stallRecoveryTimer = null
}

function markVideoProgress(v) {
  if (!v) return
  const currentTime = v.currentTime || 0
  if (!_lastVideoProgressAt || Math.abs(currentTime - _lastVideoCurrentTime) > 0.05) {
    _lastVideoProgressAt = Date.now()
    _lastVideoCurrentTime = currentTime
    if (!_stallRecovering) clearStallRecoveryTimer()
  }
}

function seekNearLiveEdge(v) {
  const ranges = v?.seekable
  if (!ranges?.length) return false
  const last = ranges.length - 1
  const liveEdge = ranges.end(last)
  const rangeStart = ranges.start(last)
  if (!Number.isFinite(liveEdge)) return false
  if (liveEdge - v.currentTime < 8) return false

  // 跳的目标余量按 seekable window 自适应：长窗口（≥30s）保留 12s（约 1 段安全垫），
  // 短窗口（如 yxfy 15s）只能保留窗口的 ~1/3，避免直接跳到窗口起点又触发 stall。
  const windowSize = liveEdge - rangeStart
  const offsetFromEdge = Math.max(4, Math.min(12, windowSize / 3))
  v.currentTime = Math.max(rangeStart + 1, liveEdge - offsetFromEdge)
  return true
}

function getForwardBuffer(v) {
  if (!v?.buffered?.length || !Number.isFinite(v.currentTime)) return 0
  for (let i = 0; i < v.buffered.length; i += 1) {
    const start = v.buffered.start(i)
    const end = v.buffered.end(i)
    if (v.currentTime >= start && v.currentTime <= end) {
      return Math.max(0, end - v.currentTime)
    }
  }
  return 0
}

function getLiveLatency(v) {
  const ranges = v?.seekable
  if (!ranges?.length || !Number.isFinite(v.currentTime)) return 0
  const liveEdge = ranges.end(ranges.length - 1)
  return Number.isFinite(liveEdge) ? Math.max(0, liveEdge - v.currentTime) : 0
}

function seekToStableLivePoint(v, targetBehindEdge = 8) {
  const ranges = v?.seekable
  if (!ranges?.length || !Number.isFinite(v.currentTime)) return false
  const last = ranges.length - 1
  const liveEdge = ranges.end(last)
  const rangeStart = ranges.start(last)
  if (!Number.isFinite(liveEdge)) return false

  const target = Math.max(rangeStart, liveEdge - targetBehindEdge)
  if (target <= v.currentTime + 0.5) return false
  v.currentTime = target
  return true
}

async function doRecovery(v, reason = 'stalled') {
  if (!v || v.paused || _stallRecovering) return
  if (Date.now() - _lastRecoveryTime < 10_000) return

  const attemptId = _playAttemptId
  const seq = ++_stallRecoverySeq
  _stallRecovering = true
  _lastRecoveryTime = Date.now()
  console.warn(`[IPTV] ${reason} 持续无进展，尝试恢复`, {
    currentTime: v.currentTime,
    readyState: v.readyState,
    networkState: v.networkState,
  })

  try {
    try {
      iptvHlsRef.value?.startLoad?.(-1)
    } catch (e) {
      console.warn('[IPTV] stalled HLS startLoad failed:', e?.message || e)
    }
    await v.play()
    if (seq !== _stallRecoverySeq || !isAttemptActive(attemptId) || iptvVideoRef.value !== v) return
    if (Date.now() - _lastVideoProgressAt > 3000) {
      seekNearLiveEdge(v)
    }
  } catch (e) {
    console.warn('[IPTV] stalled 恢复 play() 失败:', e?.message || e)
  } finally {
    if (seq === _stallRecoverySeq) _stallRecovering = false
  }
}

async function reconnectCurrentIptvSource(reason = 'stalled') {
  if (!isIptvMode.value || !playerStore.currentIptvChannel) return
  if (Date.now() - _lastReconnectTime < 15_000) return
  _lastReconnectTime = Date.now()
  await recoverIptvPlayback(reason, { force: true })
}

function seekForwardTiny(v) {
  if (!v || !Number.isFinite(v.currentTime)) return false
  const ranges = v.seekable
  const nextTime = v.currentTime + 0.08
  if (ranges?.length) {
    for (let i = 0; i < ranges.length; i += 1) {
      if (nextTime >= ranges.start(i) && nextTime <= ranges.end(i)) {
        v.currentTime = nextTime
        return true
      }
    }
    return seekNearLiveEdge(v)
  }
  v.currentTime = nextTime
  return true
}

async function recoverAvSync(v, reason = 'video-frame-stall') {
  if (!v || v.paused || _stallRecovering) return
  if (Date.now() - _lastAvSyncRecoveryTime < 8000) return
  _lastAvSyncRecoveryTime = Date.now()

  console.warn(`[IPTV] ${reason}，尝试音画重同步`, {
    currentTime: v.currentTime,
    readyState: v.readyState,
    networkState: v.networkState,
    presentedFrames: _lastPresentedFrames,
    frameAgeMs: _lastVideoFrameAt ? Date.now() - _lastVideoFrameAt : null,
  })

  try {
    await v.play()
    if (!seekForwardTiny(v)) seekNearLiveEdge(v)
    if (_avSyncFollowupTimer) clearTimeout(_avSyncFollowupTimer)
    _avSyncFollowupTimer = setTimeout(() => {
      _avSyncFollowupTimer = null
      const current = iptvVideoRef.value
      if (!current || current.paused || !isIptvMode.value) return
      const noProgress = Date.now() - _lastVideoProgressAt > 5000
      const noFrame = _lastVideoFrameAt && Date.now() - _lastVideoFrameAt > 5000
      if (noProgress || noFrame) reconnectCurrentIptvSource(`${reason} 恢复后仍无进展`)
    }, 5500)
  } catch (e) {
    console.warn('[IPTV] 音画重同步失败:', e?.message || e)
  }
}

function scheduleStallRecovery(reason) {
  const v = iptvVideoRef.value
  if (!v || v.paused || !isIptvMode.value || !isPlaying.value || _stallRecovering) return
  if (!_lastVideoProgressAt) markVideoProgress(v)
  if (_stallRecoveryTimer) return

  const check = () => {
    _stallRecoveryTimer = null
    const current = iptvVideoRef.value
    if (!current || current.paused || !isIptvMode.value || !isPlaying.value) return
    const progressAge = Date.now() - _lastVideoProgressAt
    if (progressAge > RECOVERY_LOADING_DELAY_MS) {
      playerStore.setLoading(true)
    }
    if (progressAge > RECOVERY_SOFT_RECOVER_MS) {
      doRecovery(current, reason)
    }
    if (progressAge > RECOVERY_HARD_RELOAD_MS) {
      recoverIptvPlayback(reason).catch((e) => {
        console.warn('[IPTV] stalled recovery failed:', e?.message || e)
      })
      return
    }
    _stallRecoveryTimer = setTimeout(check, 500)
  }

  _stallRecoveryTimer = setTimeout(check, RECOVERY_LOADING_DELAY_MS)
}

function onVideoTimeUpdate() {
  markVideoProgress(iptvVideoRef.value)
}

function startPlaybackProgressWatch(v = iptvVideoRef.value) {
  if (!isIptvMode.value || !v) return
  if (_playbackProgressWatchVideo === v && _playbackProgressWatchTimer) return

  stopPlaybackProgressWatch()
  _playbackProgressWatchVideo = v
  const seq = ++_playbackProgressWatchSeq
  markVideoProgress(v)

  _playbackProgressWatchTimer = setInterval(() => {
    if (seq !== _playbackProgressWatchSeq || _playbackProgressWatchVideo !== v) return
    if (!isIptvMode.value || !isPlaying.value || v.paused || v.ended) return

    const now = Date.now()
    const progressAge = now - _lastVideoProgressAt
    const currentTime = v.currentTime || 0
    const bufferAhead = getForwardBuffer(v)
    const liveLatency = getLiveLatency(v)

    if (progressAge > RECOVERY_LOADING_DELAY_MS) {
      playerStore.setLoading(true)
    }

    // 禁用主动 seek-nudge：seekToStableLivePoint 会在 currentTime 附近留下空洞，
    // 让 hls.js 误判为缺段循环跳；交给 hls.js 自带的 nudgeOffset/nudgeMaxRetry 处理。
    // 仅在 latency 真的远超窗口（>20s）+ buffer 完全空（<0.2s）才介入。
    if (bufferAhead < 0.2 && liveLatency > 20 && now - _lastBufferNudgeTime > 15000) {
      _lastBufferNudgeTime = now
      if (seekToStableLivePoint(v, 8)) {
        console.warn('[IPTV] 严重落后 live edge，强制对齐', {
          bufferAhead,
          liveLatency,
          currentTime,
        })
        return
      }
    }

    if (progressAge > RECOVERY_SOFT_RECOVER_MS) {
      doRecovery(v, 'playback-progress-watchdog')
    }

    if (progressAge > RECOVERY_HARD_RELOAD_MS) {
      recoverIptvPlayback('播放进度长时间停滞').catch((e) => {
        console.warn('[IPTV] progress watchdog recovery failed:', e?.message || e)
      })
    }
  }, RECOVERY_PROGRESS_WATCH_INTERVAL_MS)
}

function startVideoFrameWatch(v = iptvVideoRef.value) {
  if (!isIOS || !isIptvMode.value || !v?.requestVideoFrameCallback) return
  if (_videoFrameWatchVideo === v && _videoFrameWatchTimer) return

  stopVideoFrameWatch()
  _videoFrameWatchVideo = v
  _lastVideoFrameAt = Date.now()
  _lastVideoFrameMediaTime = v.currentTime || 0
  const seq = ++_videoFrameWatchSeq

  const onFrame = (_now, metadata = {}) => {
    if (seq !== _videoFrameWatchSeq || _videoFrameWatchVideo !== v) return
    _lastVideoFrameAt = Date.now()
    _lastPresentedFrames = metadata.presentedFrames || _lastPresentedFrames
    _lastVideoFrameMediaTime = Number.isFinite(metadata.mediaTime)
      ? metadata.mediaTime
      : (v.currentTime || _lastVideoFrameMediaTime)
    _videoFrameCallbackId = v.requestVideoFrameCallback(onFrame)
  }

  _videoFrameCallbackId = v.requestVideoFrameCallback(onFrame)
  _videoFrameWatchTimer = setInterval(() => {
    if (seq !== _videoFrameWatchSeq || !isIptvMode.value || v.paused || v.ended) return
    const now = Date.now()
    const frameAge = now - _lastVideoFrameAt
    const progressAge = now - _lastVideoProgressAt
    const currentTime = v.currentTime || 0
    const mediaDrift = currentTime - _lastVideoFrameMediaTime
    const bufferAhead = getForwardBuffer(v)
    const liveLatency = getLiveLatency(v)

    // 禁用主动 seek-nudge：seekToStableLivePoint 会在 currentTime 附近留下空洞，
    // 让 hls.js 误判为缺段循环跳；交给 hls.js 自带的 nudgeOffset/nudgeMaxRetry 处理。
    // 仅在 latency 真的远超窗口（>20s）+ buffer 完全空（<0.2s）才介入。
    if (bufferAhead < 0.2 && liveLatency > 20 && now - _lastBufferNudgeTime > 15000) {
      _lastBufferNudgeTime = now
      if (seekToStableLivePoint(v, 8)) {
        console.warn('[IPTV] 严重落后 live edge，强制对齐', {
          bufferAhead,
          liveLatency,
          currentTime,
        })
        return
      }
    }

    if (frameAge > 2500 && progressAge < 2500 && mediaDrift > 0.35) {
      recoverAvSync(v, '视频帧停滞但播放时钟仍在前进')
      return
    }

    if (frameAge > 5000 && progressAge > 5000) {
      scheduleStallRecovery('video-frame-watchdog')
    }

    if (frameAge > 12_000 && progressAge > 12_000) {
      reconnectCurrentIptvSource('视频帧和播放进度长时间停滞')
    }
  }, 1200)
}

function onVideoStalled() {
  if (_cancelCurrentStartup) return
  const v = iptvVideoRef.value
  if (!v || v.paused) return
  console.warn('[IPTV] stalled observed', {
    currentTime: v.currentTime,
    readyState: v.readyState,
    networkState: v.networkState,
  })
  scheduleStallRecovery('stalled')
}

function onVideoEvent(evt) {
  if (_cancelCurrentStartup) return
  if (evt === 'playing') {
    clearPauseReleaseTimer()
    _softPausedAt = 0
    _softPauseReleased = false
    markVideoProgress(iptvVideoRef.value)
    startPlaybackProgressWatch(iptvVideoRef.value)
    startVideoFrameWatch(iptvVideoRef.value)
    clearStallRecoveryTimer()
    playerStore.setLoading(false)
    playerStore.togglePlay(true)
    syncIptvMediaSession('playing')
  }
  if (evt === 'pause') {
    clearStallRecoveryTimer()
    stopPlaybackProgressWatch()
    stopVideoFrameWatch()
    syncIptvMediaSession('paused')
  }
  if (evt === 'waiting') {
    playerStore.setLoading(true)
    scheduleStallRecovery('waiting')
  }
}

function disposeIptvPlayback() {
  _componentDisposed = true
  _playAttemptId++
  _recoverySeq++
  _recoveryInFlight = false
  _manualIptvStartPending = 0
  clearPauseReleaseTimer()
  _softPausedAt = 0
  _softPauseReleased = false
  stopPlaybackWatchdogs()
  cancelCurrentStartup()
  cancelActiveProxyRace()
  destroyIptvEngines()
  resetIptvVideo()
  clearIptvMediaSession()
  playerStore.iptvVideoEl = null
  iptvSourceRuntimeStatus.value = {}
  _racedLosers.clear()
  _mpegtsRecoveries.clear()
}

// A pending channel is a channel switch, not a pause.  Tear down every
// playback backend before waiting for the next channel's provider resolves so
// the previous audio/video cannot remain attached during the resolve window.
function beginIptvChannelSwitch() {
  _hardIptvSwitchTeardown = true
  _playAttemptId++
  _recoverySeq++
  _recoveryInFlight = false
  clearPauseReleaseTimer()
  _softPausedAt = 0
  _softPauseReleased = false
  stopPlaybackWatchdogs()
  cancelCurrentStartup()
  cancelActiveProxyRace()
  destroyIptvEngines()
  resetIptvVideo()
  clearIptvMediaSession()
  iptvSourceRuntimeStatus.value = {}
  _racedLosers.clear()
  _mpegtsRecoveries.clear()
  playerStore.isPlaying = false
  playerStore.isLoading = true
  playerStore.clearPlaybackError()
}

// 注册 video 元素到 store，同步 muted 状态
watch(iptvVideoRef, (el) => {
  playerStore.iptvVideoEl = el
  if (el) el.muted = iptvMuted.value
})

watch(() => playerStore.iptvSelectionToken, (selectionToken) => {
  if (selectionToken === _playSelectionToken) return
  _playAttemptId++
  _recoverySeq++
  _recoveryInFlight = false
  clearPauseReleaseTimer()
  stopPlaybackWatchdogs()
  cancelCurrentStartup()
  cancelActiveProxyRace()
})

watch(() => playerStore.pendingIptvChannel, (pending) => {
  if (!pending) return
  beginIptvChannelSwitch()
})

// 频道队列就绪后统一从这里启动正式播放管线。
watch(() => playerStore.currentIptvChannel, async (ch) => {
  const selectionToken = playerStore.iptvSelectionToken
  if (!ch) {
    resetIptvVideo()
    destroyIptvEngines()
    iptvSourceRuntimeStatus.value = {}
    return
  }
  if (ch) {
    const channelKey = (value) => String(
      value?.logical_channel_id || value?.canonical_key || value?.name || '',
    )
    if (playerStore.pendingIptvChannel && channelKey(playerStore.pendingIptvChannel) === channelKey(ch)) {
      playerStore.pendingIptvChannel = null
    }
    sourceMenuOpen.value = false
    iptvSourceRuntimeStatus.value = {}
    if (_manualIptvStartPending > 0) return
    await nextTick()
    if (
      selectionToken !== playerStore.iptvSelectionToken
      || playerStore.currentIptvChannel !== ch
    ) return
    if (iptvVideoRef.value) {
      _hardIptvSwitchTeardown = false
      resetRacedLosers()
      _playSelectionToken = selectionToken
      const attemptId = ++_playAttemptId
      await playCurrentIptvUrl(attemptId)
    }
  }
})

// Progressive adapter results can arrive after an earlier candidate has
// already exhausted. Resume the newly appended fallback only from the
// explicit all-sources-failed state; a user pause must never be mistaken for
// a request to auto-start again.
watch(() => playerStore.iptvUrls.length, async (length, previousLength) => {
  if (
    length <= previousLength
    || !isIptvMode.value
    || isPlaying.value
    || isLoading.value
    || playerStore.playbackError !== '所有播放源均不可用'
    || _hardIptvSwitchTeardown
  ) return
  const selectionToken = playerStore.iptvSelectionToken
  await nextTick()
  if (
    selectionToken !== playerStore.iptvSelectionToken
    || !isIptvMode.value
    || isPlaying.value
    || isLoading.value
    || playerStore.playbackError !== '所有播放源均不可用'
    || !iptvVideoRef.value
  ) return
  _playSelectionToken = selectionToken
  const attemptId = ++_playAttemptId
  await playCurrentIptvUrl(attemptId)
})

watch(sourceMenuOpen, async (open) => {
  if (!open) return
  await nextTick()
  updateSourceMenuPosition()
})

watch([overlayMustStayVisible, isDesktopLayout, isMobileLayout], () => {
  if (overlayMustStayVisible.value) {
    clearOverlayHideTimer()
    clearMobileOverlayTimer()
    desktopOverlayVisible.value = true
    if (isMobileLayout.value) mobileOverlayVisible.value = true
    return
  }
  if (isMobileLayout.value) {
    scheduleMobileOverlayHide()
    return
  }
  applyOverlayVisibility()
}, { immediate: true })

watch(activePlayerPanel, () => {
  nextTick(scheduleTabIndicatorUpdate)
})

watch(isPlayerExpanded, (expanded) => {
  if (!expanded) closeSourceMenu()
  if (expanded) {
    showMobileOverlayControls()
    if (isIptvMode.value) loadIptvChannels()
    nextTick(() => {
      if (isDesktopLayout.value) playerRootRef.value?.focus?.({ preventScroll: true })
      scheduleMediaFrameSizeUpdate()
      scheduleTabIndicatorUpdate()
    })
  } else {
    clearMobileOverlayTimer()
  }
  nextTick(() => setFullPlayerChromeOpen(expanded))
})

watch(isPlaying, (playing) => {
  if (typeof _hardIptvSwitchTeardown !== 'undefined' && _hardIptvSwitchTeardown) return
  if (!isIptvMode.value) return
  if (activeIptvEngine.value === 'youtube') {
    if (!_youtubePlayer) return
    try {
      if (playing) _youtubePlayer.playVideo?.()
      else _youtubePlayer.pauseVideo?.()
    } catch {}
    syncIptvMediaSession(playing ? 'playing' : 'paused')
    return
  }
  if (!iptvVideoRef.value) return
  // 用户手动暂停先保留当前管线；超过宽限期或软恢复失败后再重拉当前源。
  if (playing && iptvVideoRef.value.paused) {
    resumeSoftPausedIptv('resume-after-pause').then((resumed) => {
      if (resumed) return true
      return recoverIptvPlayback('resume-after-pause', {
        force: true,
        statusText: '正在恢复播放...',
      })
    }).then((recovered) => {
      if (!recovered) return
      syncIptvMediaSession('playing')
    }).catch((e) => {
      if (e?.message !== 'cancelled') console.warn('[IPTV] resume after pause failed:', e?.message || e)
    })
    return
  }
  if (!playing) {
    pauseIptvPlaybackPreservingFrame()
  }
})

watch(isLoading, (loading) => {
  if (!loading) isSourceSwitching.value = false
})

watch(volume, (v) => {
  if (Number(v) > 0) lastNonZeroVolume.value = Number(v)
  if (activeIptvEngine.value === 'youtube') {
    syncYoutubeAudioState()
    return
  }
  if (iptvVideoRef.value) iptvVideoRef.value.volume = v
})

watch(iptvMuted, (muted) => {
  if (activeIptvEngine.value === 'youtube') {
    syncYoutubeAudioState()
    return
  }
  if (iptvVideoRef.value) iptvVideoRef.value.muted = muted
})

watch(() => playerStore.iptvUrlIndex, () => {
  if (_suppressIptvUrlWatch) return
  if (isIptvMode.value && isPlaying.value) {
    const attemptId = ++_playAttemptId
    playCurrentIptvUrl(attemptId)
  }
})

// EPG 集成
const epgRequestDate = ref('')
const {
  current: _epgCurrent,
  next: _epgNext,
  schedule: _epgSchedule,
  selectedDate: _epgSelectedDate,
  availableDates: _epgAvailableDates,
  loading: _epgLoading,
  error: _epgError,
  fetchPrograms: _epgFetch,
  clearPrograms: _epgClear,
  invalidatePrograms: _epgInvalidate,
} = useEpg({
  getCurrentChannelKey: () => playerStore.currentIptvChannel?.canonical_key || '',
  getCurrentDate: () => epgRequestDate.value,
})

async function selectEpgDate(date) {
  const key = playerStore.currentIptvChannel?.canonical_key
  if (!key || !date || date === _epgSelectedDate.value) return
  epgRequestDate.value = date
  const result = await _epgFetch(key, { date })
  if (!result?.applied) return
  playerStore.currentEpgProgram = _epgCurrent.value
}

async function refreshCurrentEpg(options = {}) {
  const key = playerStore.currentIptvChannel?.canonical_key
  if (!key) return { applied: false, stale: true }
  epgRequestDate.value = String(options.date || '').trim()
  const result = await _epgFetch(key, options)
  if (!result?.applied) return result
  playerStore.currentEpgProgram = _epgCurrent.value
  return result
}

function clearEpgRefreshAfterEndTimer() {
  if (!epgRefreshAfterEndTimer) return
  clearTimeout(epgRefreshAfterEndTimer)
  epgRefreshAfterEndTimer = null
}

function scheduleEpgRefreshAfterProgramEnd(program = playerStore.currentEpgProgram) {
  clearEpgRefreshAfterEndTimer()
  if (!program?.stop) return
  const delay = new Date(program.stop).getTime() - Date.now() + 1200
  if (!Number.isFinite(delay)) return
  epgRefreshAfterEndTimer = setTimeout(() => {
    refreshCurrentEpg({ date: _epgSelectedDate.value }).catch((e) => {
      console.warn('[EPG] refresh after program end failed:', e?.message || e)
    })
  }, Math.max(1000, Math.min(delay, 2 * 60 * 60 * 1000)))
}

watch(() => playerStore.currentIptvChannel, (ch) => {
  if (ch?.canonical_key) {
    playerStore.currentEpgProgram = null
    epgRequestDate.value = ''
    _epgClear()
    clearEpgRefreshAfterEndTimer()
    refreshCurrentEpg().then((result) => {
      if (result?.applied) scheduleEpgRefreshAfterProgramEnd()
    })
  } else {
    playerStore.currentEpgProgram = null
    epgRequestDate.value = ''
    _epgClear()
    clearEpgRefreshAfterEndTimer()
  }
})

watch(() => playerStore.currentEpgProgram?.stop, () => {
  scheduleEpgRefreshAfterProgramEnd()
})

onMounted(() => {
  _componentDisposed = false
  epgTickTimer = setInterval(() => {
    epgNow.value = Date.now()
  }, 30_000)
  if (window.ResizeObserver) {
    mediaLayoutObserver = new ResizeObserver(scheduleMediaFrameSizeUpdate)
    if (playerLayoutRef.value) mediaLayoutObserver.observe(playerLayoutRef.value)
    if (playerMainRef.value) mediaLayoutObserver.observe(playerMainRef.value)
    if (nowPanelRef.value) mediaLayoutObserver.observe(nowPanelRef.value)
  }
  observePointerCapabilities()
  document.addEventListener('fullscreenchange', handleFullscreenChange)
  updateMediaFrameSize()
  syncFullPlayerTheme()
  setFullPlayerChromeOpen(isPlayerExpanded.value)
  if (isPlayerExpanded.value && isDesktopLayout.value) {
    nextTick(() => playerRootRef.value?.focus?.({ preventScroll: true }))
  }
  themeObserver = new MutationObserver(syncFullPlayerTheme)
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  themeObserver.observe(document.body, { attributes: true, attributeFilter: ['class'] })
  window.addEventListener('waveflow-theme-chrome-sync', handleThemeChromeSync)
  document.addEventListener('click', closeSourceMenu)
  window.addEventListener('resize', updateSourceMenuPosition)
  window.addEventListener('resize', scheduleMediaFrameSizeUpdate)
  window.addEventListener('orientationchange', updateSourceMenuPosition)
  window.addEventListener('orientationchange', scheduleMediaFrameSizeUpdate)
})

onBeforeUnmount(() => {
  setFullPlayerChromeOpen(false)
  if (mediaLayoutRaf) {
    window.cancelAnimationFrame(mediaLayoutRaf)
    mediaLayoutRaf = 0
  }
  if (tabIndicatorRaf) {
    window.cancelAnimationFrame(tabIndicatorRaf)
    tabIndicatorRaf = 0
  }
  mediaLayoutObserver?.disconnect()
  mediaLayoutObserver = null
  document.removeEventListener('fullscreenchange', handleFullscreenChange)
  clearOverlayHideTimer()
  stopObservingPointerCapabilities()
  pendingPointerFullscreenTrigger = null
  pendingPointerCommandControl = null
  clearPointerCommandFocusCleanup()
  themeObserver?.disconnect()
  themeObserver = null
  window.removeEventListener('waveflow-theme-chrome-sync', handleThemeChromeSync)
  document.removeEventListener('click', closeSourceMenu)
  window.removeEventListener('resize', updateSourceMenuPosition)
  window.removeEventListener('resize', scheduleMediaFrameSizeUpdate)
  window.removeEventListener('orientationchange', updateSourceMenuPosition)
  window.removeEventListener('orientationchange', scheduleMediaFrameSizeUpdate)
  disposeIptvPlayback()
  ++iptvListRequestSeq
  if (iptvListController) {
    iptvListController.abort()
    iptvListController = null
  }
  if (epgTickTimer) {
    clearInterval(epgTickTimer)
    epgTickTimer = null
  }
  _epgInvalidate()
  clearEpgRefreshAfterEndTimer()
  clearMobileOverlayTimer()
  clearOverlayLoadingTimer()
})
</script>

<style scoped>
.ios-sheet-enter-active {
  transition: transform 520ms cubic-bezier(0.22, 1, 0.36, 1), opacity 260ms ease;
  will-change: transform, opacity;
}
.ios-sheet-leave-active {
  transition: transform 420ms cubic-bezier(0.32, 0.72, 0, 1), opacity 200ms ease;
  will-change: transform, opacity;
}
.ios-sheet-enter-from,
.ios-sheet-leave-to {
  transform: translate3d(0, 18px, 0) scale(0.985);
  opacity: 0;
}
.ios-sheet-enter-to,
.ios-sheet-leave-from {
  transform: translate3d(0, 0, 0) scale(1);
  opacity: 1;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.16s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.panel-slide-enter-active {
  transition: opacity 280ms cubic-bezier(0.22, 1, 0.36, 1), transform 280ms cubic-bezier(0.22, 1, 0.36, 1);
  will-change: opacity, transform;
}
.panel-slide-leave-active {
  transition: opacity 180ms cubic-bezier(0.32, 0.72, 0, 1), transform 180ms cubic-bezier(0.32, 0.72, 0, 1);
  will-change: opacity, transform;
}
.panel-slide-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.panel-slide-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
.panel-slide-enter-to,
.panel-slide-leave-from {
  opacity: 1;
  transform: translateY(0);
}

.full-player {
  --page-bg: #f8f8f7;
  --surface-bg: #ffffff;
  --surface-soft: rgba(255, 255, 255, 0.72);
  --text-primary: #111827;
  --text-secondary: rgba(17, 24, 39, 0.56);
  --text-tertiary: rgba(17, 24, 39, 0.42);
  --text-quaternary: rgba(17, 24, 39, 0.28);
  --row-active-bg: rgba(15, 23, 42, 0.055);
  --row-separator: rgba(10, 10, 10, 0.045);
  --tag-bg: rgba(17, 24, 39, 0.08);
  --tag-text: rgba(17, 24, 39, 0.46);
  --logo-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
  --control-surface: rgba(255, 255, 255, 0.72);
  --control-shadow: 0 18px 42px rgba(0, 0, 0, 0.08), inset 0 0 0 1px rgba(255, 255, 255, 0.8);
  --media-placeholder-bg: #0b0d12;
  --progress-knob-bg: #fff;
  --accent: #35c87a;
  --black: #111827;
  --gold: #c79a2b;
  --muted: #8d9299;
  --line: rgba(17, 24, 39, 0.08);
  --layout-width: calc(100% - clamp(48px, 5vw, 64px));
  --layout-height: 100dvh;
  --layout-gap: clamp(20px, 1.7vw, 24px);
  --layout-padding: clamp(22px, 3.5vh, 42px) 0 clamp(12px, 1.8vh, 22px);
  --player-main-offset: 6px;
  --media-width: 100%;
  --media-height: auto;
  --media-aspect-ratio: 16 / 9;
  --media-radius: 8px;
  --media-shadow: 0 18px 42px rgba(15, 23, 42, 0.18);
  --panel-inline: 18px;
  --title-size: clamp(22px, 2vw, 30px);
  --title-weight: 750;
  --subtitle-size: 15px;
  --meta-size: 14px;
  --control-gap: clamp(30px, 4vw, 48px);
  --control-main-size: 56px;
  --control-main-icon: 24px;
  --control-side-size: 40px;
  --control-side-icon: 24px;
  --utility-gap: 24px;
  --utility-size: 28px;
  --utility-icon: 20px;
  --tab-gap: 22px;
  --tab-min-height: 36px;
  --tab-size: 15px;
  --tab-weight: 400;
  --tab-active-weight: 600;
  --tab-line-width: 46px;
  --channel-grid: 48px minmax(0, 1fr) 28px;
  --channel-gap: 12px;
  --channel-logo-size: 48px;
  --channel-min-height: 64px;
  --channel-margin: 4px;
  --channel-padding: 8px 10px 8px 6px;
  --channel-title-size: 14px;
  --channel-title-weight: 500;
  --channel-subtitle-size: 12px;
  --channel-subtitle-color: rgba(107, 114, 128, 0.68);
  --eq-width: 22px;
  --eq-height: 22px;
  --eq-opacity: 0.35;
  --timeline-grid: 66px 46px minmax(0, 1fr);
  --timeline-line-left: 89px;
  --timeline-row-height: 72px;
  --timeline-time-size: 15px;
  --timeline-title-size: 18px;
  --progress-track: rgba(17, 24, 39, 0.10);
  --progress-fill: rgba(17, 24, 39, 0.82);
  --progress-knob-bg: #ffffff;
  --progress-knob-border: rgba(17, 24, 39, 0.42);
  --progress-knob-ring: rgba(17, 24, 39, 0.12);
  overflow: hidden;
  min-height: 100dvh;
  isolation: isolate;
  background: var(--page-bg);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "PingFang SC", "Hiragino Sans", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

.full-player:focus {
  outline: none;
}

.full-player::after {
  content: "";
  position: fixed;
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 0;
  height: calc(env(safe-area-inset-bottom) + 120px);
  background: var(--page-bg);
  pointer-events: none;
}

:global(html.full-player-open),
:global(body.full-player-open),
:global(#app.full-player-open) {
  overflow: hidden !important;
}

.full-player.safari-chrome-refresh {
  display: none !important;
}

.full-player.theme-dark {
  --page-bg: #111113;
  --surface-bg: #18181b;
  --surface-soft: rgba(39, 39, 42, 0.72);
  --text-primary: rgba(250, 250, 250, 0.94);
  --text-secondary: rgba(250, 250, 250, 0.6);
  --text-tertiary: rgba(250, 250, 250, 0.42);
  --text-quaternary: rgba(250, 250, 250, 0.28);
  --row-active-bg: rgba(255, 255, 255, 0.055);
  --row-separator: rgba(255, 255, 255, 0.07);
  --tag-bg: rgba(255, 255, 255, 0.09);
  --tag-text: rgba(250, 250, 250, 0.48);
  --logo-shadow: 0 6px 16px rgba(0, 0, 0, 0.28);
  --control-surface: rgba(39, 39, 42, 0.72);
  --control-shadow: 0 18px 42px rgba(0, 0, 0, 0.22), inset 0 0 0 1px rgba(255, 255, 255, 0.08);
  --media-placeholder-bg: #050507;
  --progress-knob-bg: #f8f8f7;
  --gold: #d3aa43;
  --muted: rgba(250, 250, 250, 0.46);
  --line: rgba(255, 255, 255, 0.09);
  --channel-subtitle-color: rgba(250, 250, 250, 0.44);
  --progress-track: rgba(255, 255, 255, 0.18);
  --progress-fill: rgba(255, 255, 255, 0.92);
  --progress-knob-bg: #111113;
  --progress-knob-border: rgba(255, 255, 255, 0.52);
  --progress-knob-ring: rgba(255, 255, 255, 0.18);
}

.full-player,
.full-player *,
.full-player *::before,
.full-player *::after {
  box-sizing: border-box;
}

.player-layout {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: var(--media-frame-width, minmax(0, 1fr)) var(--side-panel-width, minmax(300px, 400px));
  gap: var(--layout-gap);
  width: var(--layout-width);
  max-width: calc(100% - clamp(48px, 5vw, 64px));
  height: var(--layout-height);
  margin: 0 auto;
  padding: var(--layout-padding);
  background: var(--page-bg);
  transition: grid-template-columns 320ms cubic-bezier(0.22, 1, 0.36, 1),
    column-gap 320ms cubic-bezier(0.22, 1, 0.36, 1);
  will-change: grid-template-columns, column-gap;
}

.full-player.full-player--theater .player-layout {
  grid-template-columns: minmax(0, var(--media-frame-width, 1fr)) 0fr;
  column-gap: 0;
}

.full-player.full-player--theater .side-panel {
  opacity: 0;
  transform: translate3d(12px, 0, 0);
  pointer-events: none;
  visibility: hidden;
  transition: opacity 180ms ease, transform 320ms cubic-bezier(0.22, 1, 0.36, 1),
    visibility 0s linear 320ms;
}

.desktop-collapse-btn {
  position: fixed;
  top: max(18px, env(safe-area-inset-top));
  left: max(18px, env(safe-area-inset-left));
  z-index: 20;
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--control-surface);
  color: var(--text-primary);
  cursor: pointer;
  box-shadow: var(--control-shadow);
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
  transition: opacity 0.18s ease, transform 0.16s ease, background 0.16s ease;
}

.desktop-collapse-btn:hover {
  transform: translateY(-1px);
}

.desktop-collapse-btn svg {
  width: 22px;
  height: 22px;
}

.player-main {
  display: flex;
  align-items: stretch;
  min-height: 0;
  min-width: 0;
  flex-direction: column;
  padding-top: var(--player-main-offset);
}

.media-card {
  position: relative;
  box-sizing: border-box;
  overflow: hidden;
  width: var(--media-frame-width, var(--media-width));
  height: var(--media-height);
  aspect-ratio: var(--stage-aspect-ratio, var(--media-aspect-ratio));
  border-radius: var(--media-radius);
  background: var(--media-placeholder-bg);
  box-shadow: var(--media-shadow);
}

.video-loading-enter-active,
.video-loading-leave-active {
  transition: opacity 180ms ease;
}

.video-loading-enter-from,
.video-loading-leave-to {
  opacity: 0;
}

.video-loading-indicator {
  position: absolute;
  inset: 0;
  z-index: 3;
  display: grid;
  place-items: center;
  pointer-events: none;
  color: rgba(255, 255, 255, 0.9);
}

.video-loading-spinner {
  width: 48px;
  height: 48px;
  border: 3px solid rgba(255, 255, 255, 0.26);
  border-top-color: currentColor;
  border-radius: 999px;
  animation: video-loading-spin 0.9s linear infinite;
}

.video-failure-enter-active,
.video-failure-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}

.video-failure-enter-from,
.video-failure-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

.video-playback-failure {
  position: absolute;
  inset: 0;
  z-index: 3;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  pointer-events: auto;
  color: rgba(255, 255, 255, 0.92);
  text-align: center;
}

.video-playback-failure-icon {
  width: 48px;
  height: 48px;
  color: rgba(255, 255, 255, 0.94);
}

.video-playback-failure-label {
  color: rgba(255, 255, 255, 0.88);
  font-size: 15px;
  font-weight: 600;
  line-height: 1.2;
}

.video-playback-failure-retry {
  margin-top: 4px;
}

.video-playback-failure-retry svg {
  width: 24px;
  height: 24px;
}

@keyframes video-loading-spin {
  to {
    transform: rotate(360deg);
  }
}

.video-overlay {
  position: absolute;
  inset: auto 0 0;
  z-index: 4;
  display: flex;
  min-height: 142px;
  flex-direction: column;
  justify-content: flex-end;
  padding: 48px 20px 16px;
  color: #fff;
  pointer-events: none;
  background: linear-gradient(to top, rgba(0, 0, 0, 0.78), rgba(0, 0, 0, 0.42) 48%, transparent);
  opacity: 1;
  transition: opacity 180ms ease;
}

.video-overlay.is-hidden {
  opacity: 0;
  pointer-events: none;
}

.mobile-video-overlay {
  display: none;
}

.mobile-video-overlay-button {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  padding: 0;
  border: 0;
  color: rgba(255, 255, 255, 0.9);
  background: transparent;
  cursor: pointer;
  transition: color 0.16s ease, background 0.16s ease, transform 0.16s ease;
}

.mobile-video-overlay-button:hover,
.mobile-video-overlay-button:focus-visible {
  color: #fff;
}

.mobile-video-overlay-button:focus-visible {
  outline: 2px solid rgba(255, 255, 255, 0.86);
  outline-offset: 2px;
}

.mobile-video-overlay-button--side {
  width: 44px;
  height: 44px;
}

.mobile-video-overlay-button--main {
  width: 56px;
  height: 56px;
  border-radius: 999px;
  color: #111827;
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.24);
}

.mobile-video-overlay-button--utility {
  width: 44px;
  height: 44px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.42);
}

.mobile-video-overlay-button--utility:hover,
.mobile-video-overlay-button--utility:focus-visible {
  background: rgba(15, 23, 42, 0.62);
}

.mobile-video-overlay-button--side svg {
  width: 22px;
  height: 22px;
}

.mobile-video-overlay-button--main svg {
  width: 23px;
  height: 23px;
}

.mobile-video-overlay-button--utility svg {
  width: 19px;
  height: 19px;
}

.full-player--mobile-layout .video-playback-failure-icon {
  width: 48px;
  height: 48px;
}

.full-player--mobile-layout .video-playback-failure-retry {
  width: 48px;
  height: 48px;
}

.full-player--mobile-layout .video-playback-failure-retry svg {
  width: 22px;
  height: 22px;
}

.media-surface--overlay-hidden {
  cursor: none;
}

.media-card:fullscreen,
.media-card:-webkit-full-screen {
  width: 100vw !important;
  height: 100vh !important;
  max-width: none;
  border-radius: 0;
}

.media-card:fullscreen .media-video,
.media-card:-webkit-full-screen .media-video,
.media-card:fullscreen .youtube-player-host,
.media-card:-webkit-full-screen .youtube-player-host,
.media-card:fullscreen .radio-art-stage,
.media-card:-webkit-full-screen .radio-art-stage {
  width: 100%;
  height: 100%;
}

.video-overlay-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  pointer-events: auto;
}

.video-overlay-transport,
.video-overlay-utility {
  display: flex;
  align-items: center;
  gap: 10px;
}

.video-overlay-transport {
  min-width: 0;
}

.video-overlay-status {
  display: block;
  min-width: 0;
  max-width: min(28vw, 220px);
  margin-left: 4px;
  overflow: hidden;
  color: rgba(255, 255, 255, 0.68);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.video-overlay-status.playing {
  color: rgba(142, 245, 185, 0.94);
}

.video-overlay-status.loading {
  color: rgba(255, 255, 255, 0.76);
}

.video-overlay-status.error {
  color: rgba(255, 184, 184, 0.94);
}

.video-overlay-button {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  padding: 0;
  border: 0;
  color: rgba(255, 255, 255, 0.92);
  background: transparent;
  cursor: pointer;
  transition: color 0.16s ease, background 0.16s ease, transform 0.16s ease;
}

.video-overlay-button:hover {
  color: #fff;
  transform: translateY(-1px);
}

.video-overlay-button:focus-visible {
  outline: 2px solid rgba(255, 255, 255, 0.86);
  outline-offset: 3px;
}

.video-overlay-button--side {
  width: 38px;
  height: 38px;
}

.video-overlay-button--side svg {
  width: 21px;
  height: 21px;
}

.video-overlay-button--main {
  width: 50px;
  height: 50px;
  border-radius: 999px;
  color: #111827;
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.24);
}

.video-overlay-button--main:hover {
  color: #111827;
  background: #fff;
}

.video-overlay-button--main svg {
  width: 22px;
  height: 22px;
}

.video-overlay-button.video-playback-failure-retry {
  width: 48px;
  height: 48px;
  color: rgba(255, 255, 255, 0.94);
  background: rgba(15, 23, 42, 0.62);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.24);
}

.video-overlay-button.video-playback-failure-retry:hover {
  color: #fff;
  background: rgba(15, 23, 42, 0.78);
}

.video-overlay-button--utility {
  width: 36px;
  height: 36px;
  border-radius: 9px;
}

.video-overlay-button--utility:hover {
  background: rgba(255, 255, 255, 0.14);
}

.video-overlay-button--utility.active {
  background: rgba(255, 255, 255, 0.18);
}

.video-overlay-button--utility:disabled,
.video-overlay-button--utility.disabled {
  cursor: not-allowed;
  opacity: 0.42;
}

.video-overlay-button--utility:disabled:hover,
.video-overlay-button--utility.disabled:hover {
  background: transparent;
  transform: none;
}

.video-overlay-button--utility svg {
  width: 19px;
  height: 19px;
}

.video-overlay-volume-panel {
  display: flex;
  align-items: center;
  gap: 3px;
  min-height: 36px;
  padding: 0 7px 0 2px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.16);
}

.video-overlay-volume-panel .video-overlay-button--utility:hover {
  background: rgba(255, 255, 255, 0.12);
}

.video-overlay-volume {
  display: flex;
  align-items: center;
  color: rgba(255, 255, 255, 0.82);
}

.video-overlay-volume input {
  width: 78px;
  accent-color: #fff;
}

.video-overlay-progress {
  width: 100%;
  margin: 0 0 10px;
  pointer-events: none;
}

.video-overlay-progress .progress-track {
  height: 3px;
  background: rgba(255, 255, 255, 0.34);
}

.video-overlay-progress .progress-fill {
  background: rgba(255, 255, 255, 0.92);
}

.video-overlay-progress .progress-times {
  margin-top: 5px;
  color: rgba(255, 255, 255, 0.68);
  font-size: 11px;
}

.video-overlay-progress .progress-knob {
  display: none;
}

.media-video {
  position: absolute;
  inset: 0;
  display: block;
  width: 100%;
  height: 100%;
  pointer-events: none;
  object-fit: contain;
  background: var(--media-placeholder-bg);
}

.youtube-player-host {
  position: absolute;
  inset: 0;
  display: none;
  background: #000;
}

.youtube-player-host.active {
  display: block;
}

.youtube-player-host iframe {
  width: 100%;
  height: 100%;
}

.radio-art-stage {
  display: grid;
  place-items: center;
  width: 100%;
  height: 100%;
  background:
    radial-gradient(circle at 30% 20%, rgba(255, 255, 255, 0.32), transparent 36%),
    linear-gradient(135deg, #dfe8f2, #f4f1eb 52%, #e7f0ed);
}

.radio-art {
  display: grid;
  place-items: center;
  width: min(28vw, 190px);
  aspect-ratio: 1;
  overflow: hidden;
  border-radius: 24px;
  background: var(--surface-soft);
  color: var(--text-primary);
  font-size: 44px;
  font-weight: 700;
  box-shadow: 0 18px 40px rgba(15, 23, 42, 0.16);
}

.radio-art img,
.pill-logo img,
.channel-logo img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.overlay-btn {
  position: absolute;
  z-index: 2;
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border: 0;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.62);
  color: #fff;
  cursor: pointer;
  backdrop-filter: none;
  transition: opacity 0.18s ease, transform 0.16s ease, background 0.16s ease;
}

.overlay-btn.hidden {
  opacity: 0;
  pointer-events: none;
}

.overlay-btn:hover {
  background: rgba(15, 23, 42, 0.76);
  transform: translateY(-1px);
}

.overlay-btn svg {
  width: 23px;
  height: 23px;
}

.overlay-back {
  top: 24px;
  left: 24px;
  display: none;
}

.overlay-info {
  top: 24px;
  right: 24px;
}

.mobile-live-pill {
  position: absolute;
  top: 16px;
  left: 50%;
  z-index: 3;
  display: none;
  align-items: center;
  justify-content: space-between;
  width: 180px;
  height: 48px;
  padding: 6px 14px 6px 8px;
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.72);
  transform: translateX(-50%);
  backdrop-filter: blur(18px);
}

.pill-logo {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  overflow: hidden;
  border-radius: 11px;
  background: var(--surface-bg);
  color: var(--text-primary);
  font-weight: 600;
}

.mini-eq {
  width: var(--eq-width);
  height: var(--eq-height);
  background: linear-gradient(90deg, currentColor 12%, transparent 12% 22%, currentColor 22% 34%, transparent 34% 45%, currentColor 45% 57%, transparent 57% 68%, currentColor 68% 80%, transparent 80% 90%, currentColor 90%);
  color: var(--text-quaternary);
  mask: linear-gradient(to top, transparent 10%, #000 10%);
  opacity: var(--eq-opacity);
}

.eq-icon {
  display: block;
  width: var(--eq-width);
  height: var(--eq-height);
  color: var(--accent);
  fill: currentColor;
  opacity: 0;
}

.mini-eq.active,
.eq-icon.active {
  color: var(--accent);
  opacity: 0.9;
}

.now-panel {
  width: var(--media-frame-width, var(--media-width));
  padding: 10px var(--panel-inline) 0;
  text-align: center;
}

.now-metadata,
.now-identity,
.now-programme {
  min-width: 0;
}

.now-panel .channel-subtitle {
  margin: 5px 0 0;
  color: var(--text-tertiary);
  font-size: var(--subtitle-size);
  font-weight: 500;
  line-height: 1.2;
}

.desktop-now-program-next {
  display: none;
}

@media (min-width: 981px) {
  .now-panel {
    padding-top: 12px;
    text-align: left;
  }

  .now-metadata {
    display: grid;
    grid-template-columns: minmax(180px, 0.85fr) minmax(240px, 1.55fr);
    gap: 18px 24px;
    align-items: baseline;
  }

  .now-metadata--without-programme {
    display: block;
  }

  .now-panel h1 {
    font-size: clamp(20px, 1.8vw, 27px);
  }

  .now-programme {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    column-gap: 18px;
    row-gap: 2px;
  }

  .now-panel .now-program-title {
    flex: 1 1 100%;
    max-width: 760px;
    margin-top: 0;
    font-size: clamp(14px, 1.05vw, 16px);
  }

  .now-panel .mobile-now-program-next {
    display: none;
  }

  .now-panel .desktop-now-program-next {
    display: flex;
    min-width: 0;
    max-width: 760px;
    gap: 10px;
    color: var(--text-tertiary);
    font-size: var(--meta-size);
    font-weight: 400;
    line-height: 1.3;
  }

  .desktop-now-program-next__label {
    flex: 0 0 auto;
    color: var(--text-secondary);
  }

  .desktop-now-program-next__title {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .now-panel .desktop-now-program-meta {
    flex: 0 0 auto;
    margin-top: 0;
    display: block;
  }

}

@media (max-width: 980px) {
  .now-panel .desktop-now-program-next {
    display: none;
  }

  .now-panel .desktop-now-program-meta {
    display: none;
  }

  .now-panel .mobile-now-program-remaining {
    display: block;
  }
}

.now-panel h1 {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: var(--title-size);
  line-height: 1.12;
  font-weight: 700;
  letter-spacing: 0;
}

.now-panel .now-program-title {
  display: -webkit-box;
  margin-top: 7px;
  overflow: hidden;
  color: var(--text-primary);
  font-size: clamp(14px, 1.2vw, 17px);
  font-weight: 600;
  line-height: 1.3;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.now-panel .now-program-title--loading,
.now-panel .now-program-title--empty {
  color: var(--text-tertiary);
  font-weight: 500;
}

.now-panel .now-program-title--error {
  color: var(--text-secondary);
  font-weight: 500;
}

.now-panel .now-program-next {
  margin-top: 4px;
  overflow: hidden;
  color: var(--text-tertiary);
  font-size: var(--meta-size);
  font-weight: 400;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.now-panel .now-program-remaining {
  margin-top: 5px;
  color: var(--text-tertiary);
  font-size: var(--meta-size);
  font-weight: 500;
}

.now-panel .mobile-now-program-remaining {
  display: none;
}

.program-progress {
  margin-top: 10px;
}

.progress-track {
  position: relative;
  height: 2px;
  border-radius: 999px;
  background: var(--progress-track);
}

.progress-fill {
  height: 100%;
  border-radius: inherit;
  background: var(--progress-fill);
}

.progress-knob {
  position: absolute;
  top: 50%;
  width: 12px;
  height: 12px;
  border: 2px solid var(--progress-knob-border);
  border-radius: 999px;
  background: var(--progress-knob-bg);
  transform: translate(-50%, -50%);
  box-shadow: 0 0 0 1px var(--progress-knob-ring);
}

.progress-times {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
  color: var(--text-primary);
  font-size: var(--meta-size);
  font-weight: 400;
}

.side-panel {
  min-width: 0;
  min-height: 0;
  padding-top: var(--player-main-offset);
  --channel-grid: 64px minmax(0, 1fr) 22px;
  --channel-gap: 10px;
  --channel-logo-size: 46px;
  --channel-logo-width: 64px;
  --channel-logo-height: 46px;
  --channel-min-height: 62px;
  --channel-margin: 2px;
  --channel-padding: 7px 8px;
  --channel-title-size: 14px;
  --channel-title-weight: 600;
  --channel-subtitle-size: 12px;
  --timeline-grid: 56px 30px minmax(0, 1fr);
  --timeline-line-left: 70px;
  --timeline-row-height: 60px;
  --timeline-time-size: 13px;
  --timeline-title-size: 15px;
  overflow: hidden;
  opacity: 1;
  transform: translate3d(0, 0, 0);
  pointer-events: auto;
  visibility: visible;
  transition: opacity 220ms ease, transform 320ms cubic-bezier(0.22, 1, 0.36, 1),
    visibility 0s linear 0s;
  will-change: opacity, transform;
}

.mobile-panel {
  display: none;
}

.panel-tabs {
  position: relative;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--tab-gap);
  border-bottom: 1px solid var(--line);
}

.panel-tabs button {
  position: relative;
  min-height: var(--tab-min-height);
  border: 0;
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--tab-size);
  font-weight: var(--tab-weight);
  letter-spacing: 0;
  text-align: center;
  cursor: pointer;
}

.panel-tabs button.active {
  color: var(--text-primary);
  font-weight: var(--tab-active-weight);
}

.tab-indicator {
  position: absolute;
  bottom: 0;
  left: 0;
  height: 3px;
  border-radius: 999px;
  background: var(--text-primary);
  transition: transform 320ms cubic-bezier(0.22, 1, 0.36, 1), width 320ms cubic-bezier(0.22, 1, 0.36, 1);
  will-change: transform, width;
}

.desktop-panel-scroll {
  max-height: calc(100dvh - 62px);
  overflow-y: auto;
  padding: 6px 6px 12px 0;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
  scrollbar-color: var(--text-quaternary) transparent;
}

.desktop-panel-scroll::-webkit-scrollbar {
  width: 6px;
}

.desktop-panel-scroll::-webkit-scrollbar-track {
  background: transparent;
}

.desktop-panel-scroll::-webkit-scrollbar-thumb {
  border-radius: 999px;
  background: var(--text-quaternary);
}

.channel-panel {
  padding-top: 14px;
}

.side-panel .panel-tabs {
  gap: 8px;
  padding: 0 6px;
}

.side-panel .panel-tabs button {
  min-height: 36px;
  padding: 0 6px;
  color: var(--text-tertiary);
  font-size: 14px;
  font-weight: 550;
}

.side-panel .panel-tabs button.active {
  color: var(--text-primary);
  font-weight: 650;
}

.side-panel .tab-indicator {
  height: 2px;
}

.side-panel .channel-panel {
  padding: 8px 2px 10px 0;
}

.side-panel .channel-sort-bar {
  min-height: 30px;
  align-items: center;
  padding: 0 4px 8px;
}

.side-panel .sort-btn {
  min-height: 30px;
  padding: 4px 8px;
  border-color: transparent;
  background: var(--surface-soft);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 550;
}

.side-panel .sort-btn:hover,
.side-panel .sort-btn:focus-visible {
  border-color: var(--line);
  background: var(--tag-bg);
  color: var(--text-primary);
}

.side-panel .sort-btn.active {
  border-color: rgba(53, 200, 122, 0.32);
  background: rgba(53, 200, 122, 0.08);
}

.side-panel .channel-row {
  border-radius: 9px;
}

.side-panel .channel-logo img {
  display: block;
  width: 44px;
  height: 44px;
  object-fit: contain;
  filter: drop-shadow(0 0 1px rgba(15, 23, 42, 0.52)) drop-shadow(0 1px 1px rgba(15, 23, 42, 0.18));
}

.side-panel .channel-logo--wide img {
  width: 60px;
  height: 44px;
}

.side-panel .channel-logo--cover img {
  width: 44px;
  height: 44px;
}

.side-panel .channel-logo {
  width: var(--channel-logo-width);
  height: var(--channel-logo-height);
  background: #f1f2f3;
  border: 1px solid rgba(15, 23, 42, 0.1);
}

.full-player.theme-dark .side-panel .channel-logo {
  background: var(--surface-bg);
  border-color: transparent;
}

.full-player.theme-dark .side-panel .channel-logo img {
  filter: none;
}

.side-panel .channel-copy {
  min-height: var(--channel-logo-height);
}

.side-panel .channel-row.active {
  box-shadow: inset 2px 0 0 var(--accent);
}

.side-panel .channel-copy {
  gap: 3px;
}

.side-panel .channel-title {
  overflow: hidden;
  gap: 6px;
  white-space: nowrap;
}

.side-panel .channel-title-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.side-panel .channel-subtitle {
  font-weight: 400;
}

.side-panel .channel-row.disabled {
  opacity: 0.42;
}

.side-panel .eq-icon.active {
  opacity: 0.86;
}

.side-panel .schedule-panel {
  padding: 12px 2px 12px 0;
}

.side-panel .schedule-date-list {
  gap: 6px;
  padding-bottom: 2px;
}

.side-panel .schedule-date-chip {
  min-width: 58px;
  padding: 6px 10px;
  font-size: 12px;
}

.side-panel .schedule-date-chip small {
  font-size: 10px;
}

.side-panel .timeline {
  margin-top: 16px;
}

.side-panel .timeline-row {
  min-height: var(--timeline-row-height);
}

.side-panel .timeline-title {
  gap: 8px;
  font-size: var(--timeline-title-size);
  font-weight: 600;
}

.side-panel .timeline-time {
  font-size: var(--timeline-time-size);
}

.channel-sort-bar {
  display: flex;
  justify-content: flex-end;
  padding: 4px 8px 8px;
}

.sort-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid var(--row-separator);
  border-radius: 999px;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.sort-btn:hover {
  border-color: var(--text-quaternary);
  color: var(--text-secondary);
}

.sort-btn.active {
  border-color: var(--accent);
  color: var(--accent);
  background: rgba(53, 200, 122, 0.08);
}

.channel-row {
  display: grid;
  grid-template-columns: var(--channel-grid);
  align-items: center;
  gap: var(--channel-gap);
  width: 100%;
  min-height: var(--channel-min-height);
  margin-bottom: var(--channel-margin);
  padding: var(--channel-padding);
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.channel-row.active {
  background: var(--row-active-bg);
}

.channel-row.disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.channel-row.disabled .eq-icon,
.channel-row.disabled .live-dot {
  display: none;
}

.channel-logo {
  display: grid;
  place-items: center;
  width: var(--channel-logo-size);
  height: var(--channel-logo-size);
  overflow: hidden;
  border-radius: 8px;
  background: var(--surface-bg);
  color: var(--text-primary);
  font-size: 14px;
  font-weight: 600;
  box-shadow: var(--logo-shadow);
}

.channel-copy {
  display: flex;
  min-height: var(--channel-logo-size);
  min-width: 0;
  flex-direction: column;
  justify-content: center;
  gap: 6px;
}

.channel-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  color: var(--text-primary);
  font-size: var(--channel-title-size);
  font-weight: var(--channel-title-weight);
  line-height: 1.25;
}

.channel-title > :first-child {
  min-width: 0;
}

.channel-subtitle {
  display: block;
  overflow: hidden;
  color: var(--channel-subtitle-color);
  font-size: var(--channel-subtitle-size);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tag {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--tag-bg);
  color: var(--text-tertiary);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}

.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--accent);
  opacity: 0.72;
}

.live-label {
  color: var(--accent);
  font-size: 15px;
  font-weight: 500;
  white-space: nowrap;
}

.schedule-panel {
  padding-top: 18px;
}

.schedule-date {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-primary);
  font-size: 24px;
  font-weight: 800;
  letter-spacing: 0;
}

.schedule-date svg {
  width: 21px;
  height: 21px;
}

.schedule-date-list {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 4px;
  scrollbar-width: none;
}

.schedule-date-list::-webkit-scrollbar {
  display: none;
}

.schedule-date-chip {
  display: inline-flex;
  min-width: 62px;
  flex: 0 0 auto;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
  color: var(--text-secondary);
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0;
  transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
}

.schedule-date-chip small {
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0;
}

.schedule-date-chip.active {
  border-color: transparent;
  background: var(--accent);
  color: #fff;
}

.schedule-date-chip.active small {
  color: rgba(255, 255, 255, 0.74);
}

.timeline {
  position: relative;
  margin-top: 22px;
}

.timeline::before {
  content: "";
  position: absolute;
  top: 13px;
  bottom: 24px;
  left: var(--timeline-line-left);
  width: 2px;
  background: var(--line);
}

.timeline-row {
  position: relative;
  display: grid;
  grid-template-columns: var(--timeline-grid);
  align-items: center;
  min-height: var(--timeline-row-height);
  color: var(--text-primary);
}

.timeline-time {
  color: var(--text-tertiary);
  font-size: var(--timeline-time-size);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

.timeline-dot {
  position: relative;
  z-index: 1;
  width: 15px;
  height: 15px;
  border: 2px solid var(--text-quaternary);
  border-radius: 999px;
  background: var(--page-bg);
  justify-self: center;
}

.timeline-row.current .timeline-dot {
  border-color: var(--accent);
  background: var(--accent);
  box-shadow: 0 0 22px rgba(47, 189, 115, 0.55);
}

.timeline-row.past {
  opacity: 0.4;
}

.timeline-title {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: var(--timeline-title-size);
  font-weight: 700;
  line-height: 1.3;
}

.live-tag {
  border: 1px solid rgba(47, 189, 115, 0.62);
  background: rgba(47, 189, 115, 0.08);
  color: #20a760;
}

:global(.source-menu-in-fullscreen) {
  position: absolute !important;
}

@media (max-width: 980px) {
  .full-player {
    --layout-width: 100%;
    --layout-height: auto;
    --layout-padding: 0 0 calc(env(safe-area-inset-bottom) + 96px);
    --player-main-offset: 0;
    --media-width: 100vw;
    --media-height: auto;
    --media-radius: 0;
    --media-shadow: none;
    --panel-inline: 32px;
    --title-size: 21px;
    --title-weight: 500;
    --subtitle-size: 13px;
    --meta-size: 12px;
    --control-gap: 28px;
    --control-main-size: 56px;
    --control-main-icon: 22px;
    --control-side-size: 42px;
    --control-side-icon: 22px;
    --utility-gap: 32px;
    --utility-size: 38px;
    --utility-icon: 18px;
    --tab-gap: 0;
    --tab-min-height: 32px;
    --tab-size: 14px;
    --tab-weight: 550;
    --tab-active-weight: 600;
    --tab-line-width: 32px;
    --channel-grid: 44px minmax(0, 1fr) 22px;
    --channel-gap: 14px;
    --channel-logo-size: 44px;
    --channel-min-height: 68px;
    --channel-margin: 0;
    --channel-padding: 6px 30px;
    --channel-title-size: 14px;
    --channel-title-weight: 500;
    --channel-subtitle-size: 11px;
    --channel-subtitle-color: rgba(10, 10, 10, 0.34);
    --eq-width: 20px;
    --eq-height: 22px;
    --eq-opacity: 0.16;
    --timeline-grid: 56px 30px minmax(0, 1fr);
    --timeline-line-left: 70px;
    --timeline-row-height: 58px;
    --timeline-time-size: 13px;
    --timeline-title-size: 15px;
    overflow-y: auto;
    background: var(--page-bg);
  }

  .player-layout {
    display: block;
    width: var(--layout-width);
    min-height: 100dvh;
    padding: var(--layout-padding);
    background: var(--page-bg);
  }

  .desktop-collapse-btn {
    display: none;
  }

  .media-card {
    width: var(--media-width);
    height: var(--media-height);
    margin-left: calc(50% - 50vw);
    aspect-ratio: var(--stage-aspect-ratio, var(--media-aspect-ratio));
    border-radius: var(--media-radius);
    box-shadow: var(--media-shadow);
  }

  .desktop-video-overlay {
    display: none;
  }

  .media-video {
    object-position: center center;
  }

  .radio-art-stage {
    position: absolute;
    inset: 0;
  }

  .mobile-live-pill {
    display: none;
  }

  .overlay-btn {
    top: 16px;
    display: grid;
    width: 44px;
    height: 44px;
    background: rgba(0, 0, 0, 0.36);
    color: #fff;
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }

  .overlay-btn:hover {
    background: rgba(0, 0, 0, 0.44);
    transform: none;
  }

  .overlay-btn svg {
    width: 23px;
    height: 23px;
  }

  .overlay-back {
    left: 24px;
    display: grid;
  }

  .overlay-info {
    right: 24px;
  }

  .now-panel {
    width: 100%;
    max-width: 720px;
    margin: 0 auto;
    padding: 16px clamp(16px, 4vw, 28px) 0;
    text-align: center;
  }

  .now-panel h1 {
    font-size: var(--title-size);
    line-height: 1.16;
    font-weight: 700;
    letter-spacing: -0.03em;
    font-synthesis: none;
  }

  .now-panel > p {
    margin: 6px 0 0;
    font-size: var(--subtitle-size);
    font-weight: 400;
    line-height: 1.2;
    color: var(--text-tertiary);
  }

  .program-progress {
    margin-top: 12px;
    padding-top: 0;
  }

  .progress-knob {
    width: 11px;
    height: 11px;
  }

  .progress-track {
    width: 100%;
    max-width: 480px;
    margin: 0 auto;
  }

  .progress-times {
    margin-top: 8px;
    font-size: var(--meta-size);
    font-weight: 400;
  }

























  .side-panel {
    display: none;
  }

  .mobile-panel {
    display: block;
    padding: 22px 0 0;
  }

  .panel-tabs {
    gap: var(--tab-gap);
    padding: 0 52px;
    border-bottom: 0;
  }

  .panel-tabs button {
    min-height: var(--tab-min-height);
    font-size: var(--tab-size);
    font-weight: var(--tab-weight);
    text-align: center;
  }

  .panel-tabs button.active {
    font-weight: var(--tab-active-weight);
  }

  .tab-indicator {
    bottom: -10px;
  }

  .channel-panel {
    padding-top: 14px;
  }

  .channel-sort-bar {
    padding: 4px 16px 6px;
  }

  .channel-row {
    grid-template-columns: var(--channel-grid);
    min-height: var(--channel-min-height);
    margin-bottom: var(--channel-margin);
    padding: var(--channel-padding);
    border-bottom: 1px solid var(--row-separator);
    border-radius: 0;
  }

  .channel-row:last-child {
    border-bottom: 0;
  }

  .channel-row.active {
    background: transparent;
  }

  .channel-logo {
    width: var(--channel-logo-size);
    height: var(--channel-logo-size);
    border-radius: 13px;
    font-weight: 500;
    box-shadow: var(--logo-shadow);
  }

  .channel-title {
    gap: 6px;
    font-size: var(--channel-title-size);
    font-weight: var(--channel-title-weight);
    line-height: 1.22;
    letter-spacing: -0.01em;
    font-synthesis: none;
  }

  .channel-subtitle {
    font-size: var(--channel-subtitle-size);
    font-weight: 400;
    color: var(--channel-subtitle-color);
  }

  .channel-row .eq-icon {
    align-self: center;
    justify-self: end;
  }

  .channel-row .eq-icon.active {
    opacity: 0.9;
  }

  .tag {
    min-height: 18px;
    padding: 1px 7px;
    font-size: 11px;
  }

  .schedule-panel {
    padding: 24px 30px 0;
  }

  .schedule-date {
    font-size: 20px;
    font-weight: 720;
  }

  .timeline {
    margin-top: 24px;
  }

  .timeline-row {
    grid-template-columns: var(--timeline-grid);
    min-height: var(--timeline-row-height);
  }

  .timeline::before {
    left: var(--timeline-line-left);
  }

  .timeline-title {
    font-size: var(--timeline-title-size);
    font-weight: 700;
  }

  .timeline-time {
    font-size: var(--timeline-time-size);
    font-weight: 500;
  }

  .timeline-dot {
    width: 11px;
    height: 11px;
  }

  .timeline-row.current .timeline-dot {
    width: 14px;
    height: 14px;
    box-shadow: 0 0 0 6px rgba(53, 200, 122, 0.14);
  }

  .live-tag {
    min-height: 26px;
    font-size: 13px;
  }
}

@media (max-width: 520px) {
  .full-player {
    --panel-inline: 30px;
    --media-height: auto;
    --title-size: 21px;
    --title-weight: 500;
    --subtitle-size: 13px;
    --meta-size: 12px;
    --control-gap: 24px;
    --control-main-size: 56px;
    --control-main-icon: 22px;
    --control-side-size: 42px;
    --control-side-icon: 22px;
    --utility-gap: 32px;
    --utility-size: 38px;
    --utility-icon: 18px;
    --channel-padding: 6px 30px;
    --channel-grid: 44px minmax(0, 1fr) 22px;
    --channel-logo-size: 44px;
    --channel-min-height: 68px;
    --channel-title-size: 14px;
    --channel-title-weight: 500;
    --channel-subtitle-size: 11px;
    --channel-subtitle-color: rgba(10, 10, 10, 0.34);
    --eq-width: 20px;
    --eq-height: 22px;
    --eq-opacity: 0.16;
    --timeline-grid: 56px 30px minmax(0, 1fr);
    --timeline-line-left: 70px;
  }
}

/* Reuse the existing mobile presentation when video-first sizing selects mobile mode. */

  .full-player.full-player--mobile-layout {
    --layout-width: 100%;
    --layout-height: auto;
    --layout-padding: 0 0 calc(env(safe-area-inset-bottom) + 96px);
    --player-main-offset: 0;
    --media-width: 100vw;
    --media-height: auto;
    --media-radius: 0;
    --media-shadow: none;
    --panel-inline: 32px;
    --title-size: 21px;
    --title-weight: 500;
    --subtitle-size: 13px;
    --meta-size: 12px;
    --control-gap: 28px;
    --control-main-size: 56px;
    --control-main-icon: 22px;
    --control-side-size: 42px;
    --control-side-icon: 22px;
    --utility-gap: 32px;
    --utility-size: 38px;
    --utility-icon: 18px;
    --tab-gap: 0;
    --tab-min-height: 32px;
    --tab-size: 14px;
    --tab-weight: 550;
    --tab-active-weight: 600;
    --tab-line-width: 32px;
    --channel-grid: 44px minmax(0, 1fr) 22px;
    --channel-gap: 14px;
    --channel-logo-size: 44px;
    --channel-min-height: 68px;
    --channel-margin: 0;
    --channel-padding: 6px 30px;
    --channel-title-size: 14px;
    --channel-title-weight: 500;
    --channel-subtitle-size: 11px;
    --channel-subtitle-color: rgba(10, 10, 10, 0.34);
    --eq-width: 20px;
    --eq-height: 22px;
    --eq-opacity: 0.16;
    --timeline-grid: 56px 30px minmax(0, 1fr);
    --timeline-line-left: 70px;
    --timeline-row-height: 58px;
    --timeline-time-size: 13px;
    --timeline-title-size: 15px;
    overflow-y: auto;
    background: var(--page-bg);
  }

  .full-player--mobile-layout .player-layout {
    display: block;
    width: var(--layout-width);
    min-height: 100dvh;
    padding: var(--layout-padding);
    background: var(--page-bg);
  }

  .full-player--mobile-layout .desktop-collapse-btn {
    display: none;
  }

  .full-player--mobile-layout .media-card {
    width: var(--media-width);
    height: var(--media-height);
    margin-left: calc(50% - 50vw);
    aspect-ratio: var(--stage-aspect-ratio, var(--media-aspect-ratio));
    border-radius: var(--media-radius);
    box-shadow: var(--media-shadow);
  }

  .full-player--mobile-layout .desktop-video-overlay {
    display: none;
  }

  .full-player--mobile-layout .mobile-video-overlay {
    inset: 0;
    display: block;
    min-height: 0;
    padding: 0 16px 10px;
    background: linear-gradient(to top, rgba(0, 0, 0, 0.82), rgba(0, 0, 0, 0.46) 58%, transparent);
  }

  .full-player--mobile-layout .mobile-video-overlay-progress {
    position: absolute;
    right: 16px;
    bottom: 62px;
    left: 16px;
    margin: 0;
  }

  .full-player--mobile-layout .mobile-video-overlay-progress .progress-track {
    height: 2px;
    background: rgba(255, 255, 255, 0.34);
  }

  .full-player--mobile-layout .mobile-video-overlay-progress .progress-fill {
    background: rgba(255, 255, 255, 0.92);
  }

  .full-player--mobile-layout .mobile-video-overlay-progress .progress-times {
    margin-top: 5px;
    color: rgba(255, 255, 255, 0.7);
    font-size: 11px;
  }

  .full-player--mobile-layout .mobile-video-overlay-transport {
    display: flex;
    position: absolute;
    top: 50%;
    left: 50%;
    align-items: center;
    justify-content: center;
    gap: 28px;
    min-height: 56px;
    transform: translate(-50%, -50%);
    pointer-events: auto;
  }

  .full-player--mobile-layout .mobile-video-overlay-bottom {
    display: flex;
    position: absolute;
    right: 16px;
    bottom: 10px;
    left: 16px;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
    margin: 0;
    pointer-events: auto;
  }

  .full-player--mobile-layout .mobile-video-overlay-status {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    color: rgba(255, 255, 255, 0.72);
    font-size: 12px;
    font-weight: 500;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .full-player--mobile-layout .mobile-video-overlay-status.playing {
    color: rgba(142, 245, 185, 0.94);
  }

  .full-player--mobile-layout .mobile-video-overlay-status.loading {
    color: rgba(255, 255, 255, 0.82);
  }

  .full-player--mobile-layout .mobile-video-overlay-status.error {
    color: rgba(255, 184, 184, 0.94);
  }

  .full-player--mobile-layout .mobile-video-overlay-utility {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 4px;
  }

  .full-player--mobile-layout .mobile-video-overlay-button--utility {
    background: transparent;
    box-shadow: none;
  }

  .full-player--mobile-layout .mobile-video-overlay-button--utility:hover,
  .full-player--mobile-layout .mobile-video-overlay-button--utility:focus-visible {
    background: transparent;
  }

  .full-player--mobile-layout .mobile-video-overlay.is-loading .mobile-video-overlay-transport,
  .full-player--mobile-layout .mobile-video-overlay.is-switching .mobile-video-overlay-transport {
    opacity: 0;
    pointer-events: none;
  }

  .full-player--mobile-layout .media-video {
    object-position: center center;
  }

  .full-player--mobile-layout .radio-art-stage {
    position: absolute;
    inset: 0;
  }

  .full-player--mobile-layout .mobile-live-pill {
    display: none;
  }

  .full-player--mobile-layout .overlay-btn {
    top: 16px;
    display: grid;
    width: 44px;
    height: 44px;
    background: rgba(0, 0, 0, 0.36);
    color: #fff;
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }

  .full-player--mobile-layout .overlay-btn:hover {
    background: rgba(0, 0, 0, 0.44);
    transform: none;
  }

  .full-player--mobile-layout .overlay-btn svg {
    width: 23px;
    height: 23px;
  }

  .full-player--mobile-layout .overlay-back {
    left: 24px;
    display: grid;
  }

  .full-player--mobile-layout .overlay-info {
    right: 24px;
  }

  .full-player--mobile-layout .now-panel {
    width: 100%;
    max-width: 720px;
    margin: 0 auto;
    padding: 16px clamp(16px, 4vw, 28px) 0;
    text-align: center;
  }

  .full-player--mobile-layout .now-panel h1 {
    font-size: var(--title-size);
    line-height: 1.16;
    font-weight: 700;
    letter-spacing: -0.03em;
    font-synthesis: none;
  }

  .full-player--mobile-layout .now-panel > p {
    margin: 6px 0 0;
    font-size: var(--subtitle-size);
    font-weight: 400;
    line-height: 1.2;
    color: var(--text-tertiary);
  }

  .full-player--mobile-layout .program-progress {
    margin-top: 12px;
    padding-top: 0;
  }

  .full-player--mobile-layout .progress-knob {
    width: 11px;
    height: 11px;
  }

  .full-player--mobile-layout .progress-track {
    width: 100%;
    max-width: 480px;
    margin: 0 auto;
  }

  .full-player--mobile-layout .progress-times {
    margin-top: 8px;
    font-size: var(--meta-size);
    font-weight: 400;
  }

























  .full-player--mobile-layout .side-panel {
    display: none;
  }

  .full-player--mobile-layout .mobile-panel {
    display: block;
    padding: 22px 0 0;
  }

  .full-player--mobile-layout .panel-tabs {
    gap: var(--tab-gap);
    padding: 0 52px;
    border-bottom: 0;
  }

  .full-player--mobile-layout .panel-tabs button {
    min-height: var(--tab-min-height);
    font-size: var(--tab-size);
    font-weight: var(--tab-weight);
    text-align: center;
  }

  .full-player--mobile-layout .panel-tabs button.active {
    font-weight: var(--tab-active-weight);
  }

  .full-player--mobile-layout .tab-indicator {
    bottom: -10px;
  }

  .full-player--mobile-layout .channel-panel {
    padding-top: 14px;
  }

  .full-player--mobile-layout .channel-sort-bar {
    padding: 4px 16px 6px;
  }

  .full-player--mobile-layout .channel-row {
    grid-template-columns: var(--channel-grid);
    min-height: var(--channel-min-height);
    margin-bottom: var(--channel-margin);
    padding: var(--channel-padding);
    border-bottom: 1px solid var(--row-separator);
    border-radius: 0;
  }

  .full-player--mobile-layout .channel-row:last-child {
    border-bottom: 0;
  }

  .full-player--mobile-layout .channel-row.active {
    background: transparent;
  }

  .full-player--mobile-layout .channel-logo {
    width: var(--channel-logo-size);
    height: var(--channel-logo-size);
    border-radius: 13px;
    font-weight: 500;
    box-shadow: var(--logo-shadow);
  }

  .full-player--mobile-layout .channel-title {
    gap: 6px;
    font-size: var(--channel-title-size);
    font-weight: var(--channel-title-weight);
    line-height: 1.22;
    letter-spacing: -0.01em;
    font-synthesis: none;
  }

  .full-player--mobile-layout .channel-subtitle {
    font-size: var(--channel-subtitle-size);
    font-weight: 400;
    color: var(--channel-subtitle-color);
  }

  .full-player--mobile-layout .channel-row .eq-icon {
    align-self: center;
    justify-self: end;
  }

  .full-player--mobile-layout .channel-row .eq-icon.active {
    opacity: 0.9;
  }

  .full-player--mobile-layout .tag {
    min-height: 18px;
    padding: 1px 7px;
    font-size: 11px;
  }

  .full-player--mobile-layout .schedule-panel {
    padding: 24px 30px 0;
  }

  .full-player--mobile-layout .schedule-date {
    font-size: 20px;
    font-weight: 720;
  }

  .full-player--mobile-layout .timeline {
    margin-top: 24px;
  }

  .full-player--mobile-layout .timeline-row {
    grid-template-columns: var(--timeline-grid);
    min-height: var(--timeline-row-height);
  }

  .full-player--mobile-layout .timeline::before {
    left: var(--timeline-line-left);
  }

  .full-player--mobile-layout .timeline-title {
    font-size: var(--timeline-title-size);
    font-weight: 700;
  }

  .full-player--mobile-layout .timeline-time {
    font-size: var(--timeline-time-size);
    font-weight: 500;
  }

  .full-player--mobile-layout .timeline-dot {
    width: 11px;
    height: 11px;
  }

  .full-player--mobile-layout .timeline-row.current .timeline-dot {
    width: 14px;
    height: 14px;
    box-shadow: 0 0 0 6px rgba(53, 200, 122, 0.14);
  }

  .full-player--mobile-layout .live-tag {
    min-height: 26px;
    font-size: 13px;
  }

  /* IPTV Mobile keeps the video surface, metadata, and list header as separate layers. */
  .full-player--mobile-iptv .now-panel {
    width: 100%;
    max-width: none;
    margin: 0;
    padding: 12px 16px 14px;
    text-align: left;
  }

  .full-player--mobile-iptv .mobile-now-playing {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 12px;
  }

  .full-player--mobile-iptv .mobile-now-playing__logo {
    display: grid;
    flex: 0 0 44px;
    place-items: center;
    width: 44px;
    height: 44px;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 9px;
    background: var(--surface-bg);
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 650;
  }

  .full-player--mobile-iptv .mobile-now-playing__logo img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  .full-player--mobile-iptv .mobile-now-playing__copy {
    min-width: 0;
    flex: 1 1 auto;
  }

  .full-player--mobile-iptv .mobile-now-playing__identity {
    display: flex;
    min-width: 0;
    align-items: baseline;
    gap: 8px;
  }

  .full-player--mobile-iptv .mobile-now-playing__identity h1 {
    min-width: 0;
    overflow: hidden;
    color: var(--text-primary);
    font-size: 17px;
    font-weight: 650;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .full-player--mobile-iptv .mobile-now-playing__state {
    flex: 0 0 auto;
    overflow: hidden;
    color: var(--text-tertiary);
    font-size: 11px;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .full-player--mobile-iptv .mobile-now-playing__programme {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 3px 8px;
    margin: 4px 0 0;
    color: var(--text-secondary);
    font-size: 12px;
    line-height: 1.25;
  }

  .full-player--mobile-iptv .mobile-now-playing__current {
    grid-column: 1 / -1;
    overflow: hidden;
    color: var(--text-primary);
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .full-player--mobile-iptv .mobile-now-playing__next {
    min-width: 0;
    overflow: hidden;
    color: var(--text-tertiary);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .full-player--mobile-iptv .mobile-now-playing__remaining {
    color: var(--text-tertiary);
    white-space: nowrap;
  }

  .full-player--mobile-iptv .mobile-panel {
    display: block;
    padding: 0;
  }

  .full-player--mobile-iptv .mobile-panel-header {
    position: sticky;
    top: env(safe-area-inset-top, 0px);
    z-index: 8;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    min-height: 52px;
    padding: 0 16px;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    background: var(--page-bg);
  }

  .full-player--mobile-iptv .mobile-panel-header .panel-tabs {
    min-width: 0;
    gap: 0;
    padding: 0;
    border-bottom: 0;
  }

  .full-player--mobile-iptv .mobile-panel-header .panel-tabs button {
    min-width: 0;
    min-height: 44px;
    padding: 0 8px;
    color: var(--text-tertiary);
    font-size: 13px;
    font-weight: 550;
    text-align: center;
  }

  .full-player--mobile-iptv .mobile-panel-header .panel-tabs button.active {
    color: var(--text-primary);
    font-weight: 650;
  }

  .full-player--mobile-iptv .mobile-panel-header .panel-tabs button:focus-visible,
  .full-player--mobile-iptv .mobile-panel-header .sort-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }

  .full-player--mobile-iptv .mobile-panel-header .tab-indicator {
    bottom: 0;
    height: 2px;
  }

  .full-player--mobile-iptv .mobile-panel-sort {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .full-player--mobile-iptv .mobile-panel-sort .sort-btn {
    min-height: 44px;
    max-width: 100px;
    padding: 0 4px 0 8px;
    border: 0;
    border-radius: 8px;
    color: var(--text-tertiary);
    font-size: 11px;
    font-weight: 550;
    white-space: nowrap;
  }

  .full-player--mobile-iptv .mobile-panel-sort .sort-btn:hover,
  .full-player--mobile-iptv .mobile-panel-sort .sort-btn.active {
    border-color: transparent;
    background: transparent;
    color: var(--text-primary);
  }

  .full-player--mobile-iptv .mobile-panel-sort .sort-btn.active {
    color: var(--accent);
  }

  .full-player--mobile-iptv .channel-panel {
    padding: 6px 0 10px;
  }

  .full-player--mobile-iptv .channel-row {
    min-height: 68px;
    border-bottom: 1px solid var(--row-separator);
    border-radius: 0;
  }

  .full-player--mobile-iptv .channel-row.active {
    background: var(--row-active-bg);
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .full-player--mobile-iptv .channel-logo {
    width: 44px;
    height: 44px;
    border-radius: 9px;
    box-shadow: 0 3px 10px rgba(15, 23, 42, 0.08);
  }

  .full-player--mobile-iptv .schedule-panel {
    padding: 16px 16px 0;
  }

  .full-player--mobile-iptv .schedule-panel--empty {
    min-height: 0;
    padding: 0;
  }

  /* Mobile IPTV overlay controls share the existing MiniPlayer glass language. */
  .full-player--mobile-iptv {
    --mobile-overlay-glass-source: rgba(15, 23, 42, 0.42);
    --mobile-overlay-glass-surface: color-mix(in srgb, var(--mobile-overlay-glass-source) 72%, transparent);
    --mobile-overlay-glass-surface-soft: color-mix(in srgb, var(--mobile-overlay-glass-source) 56%, transparent);
    --mobile-overlay-glass-border: rgba(255, 255, 255, 0.16);
    --mobile-overlay-glass-highlight: rgba(255, 255, 255, 0.12);
    --mobile-overlay-glass-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  }

  .full-player--mobile-iptv .overlay-btn,
  .full-player--mobile-iptv .mobile-video-overlay-button--main,
  .full-player--mobile-iptv .mobile-video-overlay-button--utility {
    border: 1px solid var(--mobile-overlay-glass-border);
    background: var(--mobile-overlay-glass-surface);
    box-shadow: var(--mobile-overlay-glass-shadow), inset 0 0 0 1px var(--mobile-overlay-glass-highlight);
    backdrop-filter: blur(12px) saturate(110%);
    -webkit-backdrop-filter: blur(12px) saturate(110%);
  }

  .full-player--mobile-iptv .overlay-btn,
  .full-player--mobile-iptv .mobile-video-overlay-button--main {
    color: rgba(255, 255, 255, 0.94);
  }

  .full-player--mobile-iptv .overlay-btn:hover {
    background: var(--mobile-overlay-glass-surface);
    border-color: var(--mobile-overlay-glass-border);
    box-shadow: var(--mobile-overlay-glass-shadow), inset 0 0 0 1px var(--mobile-overlay-glass-highlight);
    transform: none;
  }

  .full-player--mobile-iptv .mobile-video-overlay-button--main {
    background: var(--mobile-overlay-glass-source);
    color: rgba(255, 255, 255, 0.94);
  }

  .full-player--mobile-iptv .mobile-video-overlay-button--main:hover,
  .full-player--mobile-iptv .mobile-video-overlay-button--main:focus-visible {
    background: var(--mobile-overlay-glass-source);
    color: #fff;
  }

  .full-player--mobile-iptv .mobile-video-overlay-button--utility {
    background: var(--mobile-overlay-glass-surface-soft);
    color: rgba(255, 255, 255, 0.9);
  }

  .full-player--mobile-iptv .mobile-video-overlay-button--utility:hover,
  .full-player--mobile-iptv .mobile-video-overlay-button--utility:focus-visible {
    background: var(--mobile-overlay-glass-surface);
    border-color: var(--mobile-overlay-glass-border);
    box-shadow: var(--mobile-overlay-glass-shadow), inset 0 0 0 1px var(--mobile-overlay-glass-highlight);
  }

  .full-player--mobile-iptv .mobile-video-overlay-button--side {
    border-color: transparent;
    background: transparent;
    box-shadow: none;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
    filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.38));
  }

  .full-player--mobile-iptv .mobile-video-overlay-button--side:hover,
  .full-player--mobile-iptv .mobile-video-overlay-button--side:focus-visible {
    border-color: transparent;
    background: transparent;
    box-shadow: none;
  }

  .full-player--mobile-iptv .mobile-video-overlay-button:active,
  .full-player--mobile-iptv .overlay-btn:active {
    transform: scale(0.96);
  }

  .full-player--mobile-iptv .overlay-btn:focus-visible,
  .full-player--mobile-iptv .mobile-video-overlay-button:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.86);
    outline-offset: 2px;
  }

</style>
