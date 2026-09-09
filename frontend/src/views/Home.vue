<template>
  <main class="radio-main page-shell min-h-screen w-full page-with-mini-player">
    <header class="mb-7 space-y-7">
      <div class="space-y-3 lg:pr-[300px]">
        <TagFilterRow
          :items="regionItems"
          :is-active="(it) => it.value === selectedRegion"
          @select="(it) => selectedRegion = it.value === selectedRegion ? '' : it.value"
        />
        <TagFilterRow
          :items="typeItems"
          :is-active="(it) => it.value === selectedType"
          @select="(it) => selectedType = it.value === selectedType ? '' : it.value"
        />
      </div>

      <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div class="flex min-w-0 items-center gap-3">
          <span class="flex size-8 shrink-0 items-center justify-center rounded-full bg-[var(--surface)] text-[var(--text-primary)]">
            <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 10.5a7 7 0 0 1 14 0M8 10.5a4 4 0 0 1 8 0M12 11.5v6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="8.5" r="1.2" fill="currentColor"/></svg>
          </span>
          <h1 class="truncate text-lg font-semibold leading-none text-[var(--text-primary)]">电台直播</h1>
          <span class="shrink-0 text-sm text-[var(--text-secondary)]">共 {{ filteredStations.length }} 个电台</span>
          <span v-if="radioLoading" class="hidden text-sm text-[var(--text-tertiary)] sm:inline">正在加载...</span>
        </div>

        <button
          type="button"
          class="inline-flex h-[var(--control-height)] shrink-0 self-start items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] sm:self-auto"
          @click="nextSortMode"
        >
          <svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="16" y2="12"/><line x1="4" y1="18" x2="12" y2="18"/>
          </svg>
          {{ currentSortLabel }}
        </button>
      </div>
    </header>

    <div
      v-if="radioCatalogNotice"
      class="mb-5 flex min-h-10 items-center justify-between gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm text-[var(--text-secondary)]"
      role="status"
      aria-live="polite"
    >
      <span class="min-w-0">{{ radioCatalogNotice }}</span>
      <button
        v-if="!radioLoading"
        type="button"
        class="shrink-0 rounded-lg px-2.5 py-1 text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-hover)]"
        @click="loadRadioCatalog"
      >
        重试
      </button>
    </div>

    <section
      ref="gridRef"
      class="relative min-h-[240px] w-full"
      :style="{ height: `${totalHeight}px` }"
      :aria-busy="radioLoading ? 'true' : 'false'"
    >
      <div v-if="filteredStations.length === 0" class="flex min-h-[240px] items-center justify-center px-4 py-12">
        <div class="flex max-w-md flex-col items-center gap-3 text-center" role="status" aria-live="polite">
          <span class="flex size-11 items-center justify-center rounded-xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)]">
            <svg v-if="radioLoading && allStations.length === 0" class="size-5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8" stroke-opacity=".25"/><path d="M20 12a8 8 0 0 0-8-8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            <svg v-else-if="radioCatalogState === 'error' && allStations.length === 0" class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 4 21 20H3L12 4Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 9v5M12 17h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            <svg v-else class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.7"/><path d="M8.5 12h7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
          </span>
          <p v-if="radioLoading && allStations.length === 0" class="text-sm text-[var(--text-secondary)]">正在加载电台目录...</p>
          <template v-else-if="radioCatalogState === 'error' && allStations.length === 0">
            <p class="text-sm text-[var(--text-secondary)]">电台目录暂时无法加载</p>
            <button
              type="button"
              class="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-hover)]"
              @click="loadRadioCatalog"
            >
              重试加载
            </button>
          </template>
          <p v-else-if="radioCatalogState === 'empty' && allStations.length === 0" class="text-sm text-[var(--text-secondary)]">暂无可用电台</p>
          <template v-else>
            <p class="text-sm text-[var(--text-secondary)]">没有匹配的电台</p>
            <button
              type="button"
              class="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-hover)]"
              @click="clearRadioFilters"
            >
              清除筛选
            </button>
          </template>
        </div>
      </div>

      <template v-else>
      <div
        v-for="row in virtualRows"
        :key="row.startIndex"
        class="absolute left-0 top-0 w-full"
        :style="{ transform: `translateY(${row.startIndex * (rowHeight + gap)}px)` }"
      >
        <div
          class="grid"
          :style="{ gap: 'var(--card-gap)', gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }"
        >
          <button
            v-for="item in row.items"
            :key="item.station.id"
            type="button"
            :aria-label="`切换到 ${item.station.name}`"
            :aria-current="isCurrentStationSelected(item.station.id) ? 'true' : undefined"
            :style="{ height: `${cardHeight}px` }"
            class="channel-card group relative overflow-hidden rounded-[var(--card-radius)] border border-[var(--border)] bg-[var(--card-bg)] text-left outline-none transition duration-200 ease-out hover:-translate-y-0.5 hover:border-[var(--border-strong)]"
            :class="[
              'channel-card--logo-card',
              {
                'channel-card-selected': isCurrentStationSelected(item.station.id),
                'channel-card-current': isCurrentStationPlaying(item.station.id),
              },
            ]"
            @click="playerStore.switchStation(item.station.id)"
          >
            <span class="channel-card__logo-card-visual" aria-hidden="true">
              <span
                class="channel-card__logo-stage"
                :class="stationLogoStageClass(item.station)"
              >
                <img
                  v-if="shouldShowStationLogo(item.station)"
                  class="channel-card__center-logo"
                  :class="stationLogoImageClass(item.station)"
                  :src="stationLogoUrl(item.station)"
                  alt=""
                  loading="lazy"
                  decoding="async"
                  @load="classifyStationLogo(item.station, $event)"
                  @error="markStationLogoFailed(item.station)"
                />
                <span
                  v-else
                  class="channel-card__text-logo"
                  :class="stationTextLogoSizeClass(item.station)"
                  :title="stationDisplayName(item.station)"
                >
                  {{ stationDisplayName(item.station) }}
                </span>
              </span>
              <span class="channel-card__logo-card-shade"></span>
            </span>
            <span class="card-info">
              <span class="card-channel">
                <span
                  class="channel-play-state-dot"
                  :class="stationStatusDotClass(item.station)"
                  :title="stationStatusLabel(item.station)"
                  :aria-label="stationStatusLabel(item.station)"
                  role="img"
                />
                <span class="card-channel-name">{{ item.station.name }}</span>
              </span>
              <span class="card-program-name">{{ stationSubtitle(item.station) }}</span>
            </span>
          </button>
        </div>
      </div>
      </template>
    </section>
  </main>
</template>

<script setup>
import { storeToRefs } from 'pinia'
import { usePlayerStore } from '../stores/player'
import {
  fetchRadioCatalog,
  fetchRadioProgramme,
  summarizeRadioCatalogState,
} from '../api/radioStations'
import { computed, inject, onBeforeUnmount, onMounted, ref, watch, watchEffect } from 'vue'
import { useScroll, useThrottleFn } from '@vueuse/core'
import { useLogoVisual } from '../composables/useLogoVisual'
import TagFilterRow from '../components/TagFilterRow.vue'

const playerStore = usePlayerStore()
const { currentStation, isPlaying, isLoading, playbackError, stationList } = storeToRefs(playerStore)

const radioLoading = ref(false)
const radioCatalogState = ref('loading')
let radioCatalogRequestSeq = 0
let radioCatalogController = null
let disposed = false
const {
  displayName: stationDisplayName,
  shouldShowLogo: shouldShowStationLogo,
  logoStageClass: stationLogoStageClass,
  logoImageClass: stationLogoImageClass,
  classifyLogo: classifyStationLogo,
  markLogoFailed: markStationLogoFailed,
  textLogoSizeClass: stationTextLogoSizeClass,
} = useLogoVisual({
  getLogoUrl: stationLogoUrl,
  getDisplayName: stationDisplayNameValue,
  getIdentityKey: stationLogoIdentityKey,
  fallbackName: '未知电台',
})

const allStations = computed(() => {
  // RadioStation/RadioStationSource identities are explicit.  Never merge
  // providers by display name, frequency, or an upstream URL.
  return stationList.value.filter((station) => (
    !station?.catalogRemoved || station.id === currentStation.value
  ))
})

const regionLabels = {
  TW: '台湾', CN: '中国大陆', JP: '日本', US: '美国', KR: '韩国', GB: '英国', DE: '德国', FR: '法国',
  HK: '香港', SG: '新加坡',
  安徽: '安徽', 北京: '北京', 重庆: '重庆', 福建: '福建', 甘肃: '甘肃', 广东: '广东', 广西: '广西',
  贵州: '贵州', 海南: '海南', 河北: '河北', 河南: '河南', 黑龙江: '黑龙江', 湖北: '湖北', 湖南: '湖南',
  吉林: '吉林', 江苏: '江苏', 江西: '江西', 辽宁: '辽宁', 内蒙古: '内蒙古', 宁夏: '宁夏', 青海: '青海',
  山东: '山东', 山西: '山西', 陕西: '陕西', 上海: '上海', 四川: '四川', 西藏: '西藏', 新疆: '新疆',
  新疆兵团: '新疆兵团', 云南: '云南', 浙江: '浙江',
}

const typeLabels = { music: '音乐', news: '新闻', talk: '谈话', sports: '体育', religious: '宗教', other: '其他' }

const selectedRegion = ref('')
const selectedType = ref('')
const searchQuery = inject('searchQuery')
const stationSortMode = ref('original')

const SORT_MODES = [
  { key: 'original', label: '默认排序' },
  { key: 'natural', label: 'A-Z排序' },
]
function naturalSort(a, b) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
}
function nextSortMode() {
  const idx = SORT_MODES.findIndex(m => m.key === stationSortMode.value)
  stationSortMode.value = SORT_MODES[(idx + 1) % SORT_MODES.length].key
}
const currentSortLabel = computed(() => SORT_MODES.find(m => m.key === stationSortMode.value)?.label || '默认排序')

const regions = computed(() => {
  const set = new Set()
  for (const s of allStations.value) {
    for (const t of stationRegions(s)) {
      if (t) set.add(t)
    }
  }
  return [...set]
})

const types = computed(() => {
  const set = new Set()
  for (const s of allStations.value) {
    for (const t of stationTypes(s)) {
      if (t) set.add(t)
    }
  }
  return [...set]
})

function stationRegions(station) {
  const explicitRegions = (station.tags || []).filter(tag => Object.hasOwn(regionLabels, tag))
  return [...new Set([station.radioRegion, ...explicitRegions].filter(Boolean))]
}
function stationTypes(station) {
  return station.radioDomain ? [station.radioType].filter(Boolean) : (station.tags || []).filter(tag => typeLabels[tag])
}

const regionItems = computed(() => [
  { value: '', label: '全部地区' },
  ...regions.value.map((r) => ({ value: r, label: regionLabels[r] || r })),
])
const typeItems = computed(() => [
  { value: '', label: '全部类型' },
  ...types.value.map((t) => ({ value: t, label: typeLabels[t] || t })),
])

watch(regions, (availableRegions) => {
  if (selectedRegion.value && !availableRegions.includes(selectedRegion.value)) {
    selectedRegion.value = ''
  }
})

watch(types, (availableTypes) => {
  if (selectedType.value && !availableTypes.includes(selectedType.value)) {
    selectedType.value = ''
  }
})

const filteredStations = ref([])

const radioCatalogNotice = computed(() => {
  if (radioLoading.value && allStations.value.length > 0) return '正在更新电台目录，当前频道仍可使用'
  if (radioCatalogState.value === 'stale') return '电台目录更新失败，已保留上次频道'
  if (radioCatalogState.value === 'degraded') return '部分电台目录暂时不可用，已保留可用频道'
  if (radioCatalogState.value === 'error' && allStations.value.length > 0) return '电台目录暂时无法更新，已保留现有频道'
  return ''
})

watchEffect(() => {
  const region = selectedRegion.value
  const type = selectedType.value
  const query = searchQuery.value.trim().toLowerCase()
  const result = allStations.value.filter((s) => {
    if (region && !stationRegions(s).includes(region)) return false
    if (type && !stationTypes(s).includes(type)) return false
    if (query && !(s.name || '').toLowerCase().includes(query)) return false
    return true
  })
  if (stationSortMode.value === 'natural') {
    result.sort((a, b) => naturalSort(a.name || '', b.name || ''))
  }
  filteredStations.value = result
})

function isCurrentStationPlaying(stationId) {
  return currentStation.value === stationId
    && isPlaying.value
    && !isLoading.value
    && !playbackError.value
}

function isCurrentStationSelected(stationId) {
  return currentStation.value === stationId
}

function isCurrentStationLoading(stationId) {
  return currentStation.value === stationId && isLoading.value
}

function stationSubtitle(station) {
  return station.subtitle || ''
}

function stationLogoUrl(station) {
  return station.logoUrl || ''
}

function stationDisplayNameValue(station) {
  return String(station?.name || '未知电台').trim() || '未知电台'
}

function stationLogoIdentityKey(station) {
  return station?.id || station?.name || ''
}

function stationStatusDotClass(station) {
  if (currentStation.value === station.id && playbackError.value) return 'channel-play-state-dot--danger'
  if (isCurrentStationLoading(station.id)) return 'channel-play-state-dot--warn'
  if (isCurrentStationPlaying(station.id)) return 'channel-play-state-dot--live'
  return 'channel-play-state-dot--neutral'
}

function stationStatusLabel(station) {
  if (isCurrentStationLoading(station.id)) return '加载中'
  if (currentStation.value === station.id && playbackError.value) return '播放失败'
  if (isCurrentStationPlaying(station.id)) return '播放中'
  if (station.catalogRemoved) return '目录已更新'
  if (currentStation.value === station.id) return '已暂停'
  return '待播放'
}

function clearRadioFilters() {
  selectedRegion.value = ''
  selectedType.value = ''
  if (searchQuery?.value !== undefined) searchQuery.value = ''
}

const scrollRef = inject('scrollRef')
const gridRef = ref(null)
const containerWidth = ref(1024)
const viewportWidth = ref(typeof window === 'undefined' ? 1280 : window.innerWidth)
let resizeObserver = null
let programmeRequestSeq = 0
let programmeTimer = null

async function refreshCurrentRadioProgramme(
  stationId = currentStation.value,
  sourceId = playerStore.stationMap[stationId]?.radioSourceId,
) {
  const station = playerStore.stationMap[stationId]
  if (!station?.radioStationId || !sourceId) return
  const requestSeq = ++programmeRequestSeq
  const requestStation = { ...station, radioSourceId: sourceId }
  const result = await fetchRadioProgramme(requestStation)
  if (
    disposed
    ||
    requestSeq !== programmeRequestSeq
    || currentStation.value !== stationId
    || playerStore.stationMap[stationId]?.radioSourceId !== sourceId
    || !result
  ) return
  playerStore.updateRadioProgramme(stationId, result.programmes || [])
}

const currentRadioSelection = computed(() => {
  const stationId = currentStation.value
  return stationId ? `${stationId}:${playerStore.stationMap[stationId]?.radioSourceId || ''}` : ''
})

watch(currentRadioSelection, (selection) => {
  programmeRequestSeq += 1
  if (selection) {
    const [stationId, ...sourceParts] = selection.split(':')
    refreshCurrentRadioProgramme(stationId, sourceParts.join(':'))
  }
})

const { y: scrollY } = useScroll(scrollRef)
const throttledScrollY = useThrottleFn((val) => { scrollPosition.value = val }, 16)
const scrollPosition = ref(0)
watchEffect(() => { throttledScrollY(scrollY.value) })

const columns = computed(() => {
  const w = viewportWidth.value
  if (w >= 1280) return 5
  if (w >= 1024) return 4
  return 2
})

const gap = computed(() => 20)

const rowHeight = computed(() => {
  const cols = columns.value
  const cardWidth = (containerWidth.value - gap.value * (cols - 1)) / cols
  // Match TV's media-plus-footer sizing; the footer is not part of the 16:9 image.
  return cardWidth * 9 / 16 + 44
})

const cardHeight = computed(() => rowHeight.value)

const rows = computed(() => {
  const cols = columns.value
  const stations = filteredStations.value
  const result = []
  for (let i = 0; i < stations.length; i += cols) {
    result.push(stations.slice(i, i + cols).map((station, offset) => ({
      station,
      index: i + offset,
    })))
  }
  return result
})

const virtualRows = computed(() => {
  const totalRows = rows.value.length
  if (totalRows === 0) return []
  const rh = rowHeight.value + gap.value
  const startRow = Math.max(0, Math.floor(scrollPosition.value / rh) - 3)
  const viewportH = scrollRef.value?.clientHeight || window.innerHeight
  const endRow = Math.min(totalRows, Math.ceil((scrollPosition.value + viewportH) / rh) + 3)
  return rows.value.slice(startRow, endRow).map((items, i) => ({
    items,
    startIndex: startRow + i,
  }))
})

const totalHeight = computed(() => rows.value.length * (rowHeight.value + gap.value))

async function loadRadioCatalog() {
  const requestSeq = ++radioCatalogRequestSeq
  radioCatalogController?.abort()
  const controller = new AbortController()
  radioCatalogController = controller
  radioLoading.value = true

  const ownsRequest = () => !disposed && requestSeq === radioCatalogRequestSeq && !controller.signal.aborted
  const result = await fetchRadioCatalog({ signal: controller.signal })
  if (ownsRequest() && result.status === 'success') playerStore.addRadioStations(result.stations)
  if (disposed || requestSeq !== radioCatalogRequestSeq || result.status === 'cancelled') return
  if (radioCatalogController === controller) radioCatalogController = null

  radioLoading.value = false
  const states = result.status === 'success' ? [...result.catalogStates] : [{ status: 'failed' }]
  radioCatalogState.value = summarizeRadioCatalogState(allStations.value, states)
  if (currentStation.value) refreshCurrentRadioProgramme(currentStation.value)
}

onMounted(() => {
  disposed = false
  resizeObserver = new ResizeObserver((entries) => {
    for (const entry of entries) {
      containerWidth.value = entry.contentRect.width
      viewportWidth.value = window.innerWidth
    }
  })
  if (gridRef.value) resizeObserver.observe(gridRef.value)

  void loadRadioCatalog()

  programmeTimer = setInterval(() => {
    if (currentStation.value) refreshCurrentRadioProgramme(currentStation.value)
  }, 180_000)
})

onBeforeUnmount(() => {
  disposed = true
  ++radioCatalogRequestSeq
  ++programmeRequestSeq
  radioCatalogController?.abort()
  radioCatalogController = null
  if (resizeObserver) resizeObserver.disconnect()
  if (programmeTimer) clearInterval(programmeTimer)
})
</script>
