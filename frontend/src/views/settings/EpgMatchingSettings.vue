<template>
  <section class="epg-matching-page" aria-label="频道匹配">
    <section class="epg-match-summary" aria-label="节目单匹配概况">
      <div class="epg-match-summary-item">
        <span>已匹配</span>
        <strong>{{ compactCount(overview?.logical_channels?.bound) }}</strong>
      </div>
      <span class="epg-match-summary-separator" aria-hidden="true"></span>
      <div class="epg-match-summary-item">
        <span>未匹配</span>
        <strong>{{ compactCount(overview?.logical_channels?.unbound) }}</strong>
      </div>
      <span class="epg-match-summary-separator" aria-hidden="true"></span>
      <div class="epg-match-summary-item epg-match-summary-item--warning">
        <span>需处理</span>
        <strong>{{ compactCount(overview?.logical_channels?.needs_attention) }}</strong>
      </div>
      <span class="ml-auto text-xs text-[var(--text-tertiary)]">共 {{ compactCount(overview?.logical_channels?.total) }} 个当前频道</span>
    </section>

    <section class="epg-match-controls" aria-label="频道匹配筛选">
      <form class="epg-match-search" @submit.prevent="applySearch">
        <button type="submit" class="epg-match-search-submit" aria-label="搜索频道">
          <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="6.5" stroke="currentColor" stroke-width="1.7"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
        </button>
        <input v-model="searchDraft" type="search" autocomplete="off" placeholder="搜索频道或节目单" aria-label="搜索频道或节目单" />
        <button v-if="searchDraft || searchQuery" type="button" class="epg-match-clear" aria-label="清除搜索" @click="clearSearch">清除</button>
      </form>

      <nav class="epg-match-filters" aria-label="匹配状态">
        <button
          v-for="option in scopeOptions"
          :key="option.key"
          type="button"
          class="epg-match-filter"
          :class="{ 'is-active': scope === option.key }"
          :aria-pressed="scope === option.key"
          @click="setScope(option.key)"
        >
          {{ option.label }}
        </button>
      </nav>
    </section>

    <section v-if="listLoading" class="grid gap-2.5" aria-label="正在加载频道匹配" aria-busy="true">
      <div v-for="index in 6" :key="index" class="h-[88px] animate-pulse rounded-2xl border border-[var(--border)] bg-[var(--surface)]"></div>
    </section>

    <section v-else-if="listError" class="epg-match-state-panel" role="alert">
      <h3>频道匹配加载失败</h3>
      <p>{{ listError }}</p>
      <button type="button" class="epg-match-button epg-match-button--secondary" @click="loadList">重试</button>
    </section>

    <template v-else-if="items.length">
      <div class="grid gap-2.5" role="list" aria-label="频道匹配列表">
        <button
          v-for="item in items"
          :key="item.channel.logical_channel_id"
          type="button"
          class="epg-match-row"
          role="listitem"
          @click="openDetail(item)"
        >
          <span class="flex min-w-0 items-center gap-3">
            <span class="epg-match-channel-icon">
              <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="6" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M9 3.5 12 6l3-2.5M8 11h8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
            </span>
            <span class="min-w-0 text-left">
              <span class="block truncate text-sm font-semibold text-[var(--text-primary)]">{{ item.channel.display_name }}</span>
              <span class="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] text-[var(--text-tertiary)]">
                <span>{{ compactCount(item.channel.member_count) }} 个直播源</span>
                <template v-if="originLabel(item.binding.origin)">
                  <span aria-hidden="true">·</span>
                  <span>{{ originLabel(item.binding.origin) }}</span>
                </template>
                <template v-if="item.binding.locked">
                  <span aria-hidden="true">·</span>
                  <span>已锁定</span>
                </template>
              </span>
            </span>
          </span>

          <span class="epg-match-target">
            <span class="block truncate text-[13px] font-medium text-[var(--text-primary)]">
              {{ targetLabel(item) }}
            </span>
            <span class="mt-1 block truncate text-[11px] text-[var(--text-tertiary)]">{{ matchingState(item).detail }}</span>
          </span>

          <span class="epg-match-status" :class="`epg-match-status--${matchingState(item).tone}`">
            {{ matchingState(item).label }}
          </span>
        </button>
      </div>

      <footer class="epg-match-pagination" aria-label="分页">
        <span>第 {{ page }} / {{ pageCount }} 页</span>
        <div class="flex items-center gap-1.5">
          <button type="button" class="epg-match-page-button" :disabled="page <= 1 || listLoading" @click="changePage(page - 1)">上一页</button>
          <button type="button" class="epg-match-page-button" :disabled="page >= pageCount || listLoading" @click="changePage(page + 1)">下一页</button>
        </div>
      </footer>
    </template>

    <section v-else class="epg-match-state-panel">
      <span class="epg-match-empty-icon">
        <svg class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="6" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M8 11h8M8 14h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </span>
      <h3>{{ searchQuery ? '没有找到相关频道' : scope === 'all' ? '还没有可管理的频道' : '当前筛选下没有频道' }}</h3>
      <p>{{ searchQuery ? '换个频道名称或节目单名称试试。' : '频道同步完成后会显示在这里。' }}</p>
      <button v-if="searchQuery || scope !== 'all'" type="button" class="epg-match-button epg-match-button--secondary" @click="resetFilters">清除筛选</button>
    </section>
  </section>

  <Teleport to="body">
    <Transition name="epg-match-drawer">
      <div v-if="drawerOpen" class="epg-match-drawer-layer">
        <div class="epg-match-drawer-backdrop" @click="closeDrawer"></div>
        <aside
          ref="drawerRef"
          class="epg-match-drawer-pane"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="catalogMode ? 'epg-catalog-title' : 'epg-match-detail-title'"
          tabindex="-1"
          @keydown.esc.stop.prevent="handleDrawerEscape"
        >
          <template v-if="catalogMode">
            <header class="epg-match-drawer-header">
              <button type="button" class="epg-match-icon-button" aria-label="返回频道详情" @click="closeCatalog">
                <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m15 5-7 7 7 7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
              <div class="min-w-0 flex-1">
                <h2 id="epg-catalog-title" class="truncate text-base font-semibold text-[var(--text-primary)]">选择节目单</h2>
                <p class="mt-0.5 truncate text-xs text-[var(--text-secondary)]">{{ selectedChannelName }}</p>
              </div>
              <button type="button" class="epg-match-icon-button" aria-label="关闭" @click="closeDrawer">
                <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
              </button>
            </header>

            <div class="epg-match-drawer-body">
              <form class="space-y-2.5" @submit.prevent="applyCatalogSearch">
                <div class="epg-match-search w-full">
                  <svg class="size-4 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="6.5" stroke="currentColor" stroke-width="1.7"/><path d="m16 16 4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
                  <input v-model="catalogQueryDraft" type="search" autocomplete="off" placeholder="搜索 EPG 频道" aria-label="搜索 EPG 频道" />
                  <button type="submit" class="epg-match-clear">搜索</button>
                </div>
                <select v-model="catalogSourceId" class="epg-match-select" aria-label="筛选节目单来源" @change="changeCatalogSource">
                  <option value="">全部节目单来源</option>
                  <option v-for="source in sourceOptions" :key="source.id" :value="String(source.id)">{{ sourceDisplayName(source.id, source.name) }}</option>
                </select>
              </form>

              <div v-if="catalogLoading" class="grid gap-2" aria-label="正在搜索 EPG 频道" aria-busy="true">
                <div v-for="index in 6" :key="index" class="h-[70px] animate-pulse rounded-2xl bg-[var(--surface)]"></div>
              </div>
              <section v-else-if="catalogError" class="epg-match-inline-state" role="alert">
                <p>{{ catalogError }}</p>
                <button type="button" class="epg-match-link-button" @click="loadCatalog">重试</button>
              </section>
              <div v-else-if="catalogItems.length" class="grid gap-2" role="listbox" aria-label="EPG 频道搜索结果">
                <button
                  v-for="item in catalogItems"
                  :key="`${item.epg_source_id}:${item.epg_channel_id}`"
                  type="button"
                  class="epg-catalog-row"
                  :class="{ 'is-selected': catalogTargetSelected(item) }"
                  :aria-selected="catalogTargetSelected(item)"
                  role="option"
                  @click="selectedCatalogTarget = item"
                >
                  <span class="min-w-0 text-left">
                    <span class="block truncate text-sm font-medium text-[var(--text-primary)]">{{ catalogDisplayName(item) }}</span>
                    <span class="mt-1 block truncate text-xs text-[var(--text-secondary)]">{{ sourceDisplayName(item.epg_source_id, item.epg_source_name) }}</span>
                    <span v-if="item.epg_channel_display_name && item.epg_channel_id !== item.epg_channel_display_name" class="mt-0.5 block truncate text-[11px] text-[var(--text-tertiary)]">频道标识 {{ item.epg_channel_id }}</span>
                  </span>
                  <span class="flex shrink-0 items-center gap-2">
                    <span v-if="sourceHealthLabel(item.source_health)" class="text-[11px] text-[var(--text-tertiary)]">{{ sourceHealthLabel(item.source_health) }}</span>
                    <span class="epg-catalog-check" aria-hidden="true">✓</span>
                  </span>
                </button>
              </div>
              <section v-else class="epg-match-inline-state">
                <p>没有找到相关 EPG 频道</p>
                <span>可以更换关键词或节目单来源。</span>
              </section>

              <footer v-if="catalogTotal > catalogPageSize" class="epg-match-catalog-pagination">
                <span>第 {{ catalogPage }} / {{ catalogPageCount }} 页</span>
                <div class="flex gap-1.5">
                  <button type="button" class="epg-match-page-button" :disabled="catalogPage <= 1" @click="changeCatalogPage(catalogPage - 1)">上一页</button>
                  <button type="button" class="epg-match-page-button" :disabled="catalogPage >= catalogPageCount" @click="changeCatalogPage(catalogPage + 1)">下一页</button>
                </div>
              </footer>
            </div>

            <footer class="epg-match-drawer-footer">
              <div class="min-w-0 flex-1">
                <p class="truncate text-xs font-medium text-[var(--text-primary)]">{{ selectedCatalogTarget ? catalogDisplayName(selectedCatalogTarget) : '请选择一个 EPG 频道' }}</p>
                <p v-if="selectedCatalogTarget" class="mt-0.5 truncate text-[11px] text-[var(--text-tertiary)]">{{ sourceDisplayName(selectedCatalogTarget.epg_source_id, selectedCatalogTarget.epg_source_name) }}</p>
              </div>
              <button type="button" class="epg-match-button epg-match-button--primary" :disabled="!selectedCatalogTarget || actionPending" @click="bindSelectedCatalogTarget">
                {{ actionBusy === 'binding' ? '保存中…' : '绑定节目单' }}
              </button>
            </footer>
          </template>

          <template v-else>
            <header class="epg-match-drawer-header">
              <span class="epg-match-channel-icon">
                <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="6" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M8 11h8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
              </span>
              <div class="min-w-0 flex-1">
                <h2 id="epg-match-detail-title" class="truncate text-base font-semibold text-[var(--text-primary)]">{{ selectedChannelName }}</h2>
                <p class="mt-0.5 text-xs text-[var(--text-secondary)]">频道节目单设置</p>
              </div>
              <span v-if="selectedItem" class="epg-match-status" :class="`epg-match-status--${selectedState.tone}`">{{ selectedState.label }}</span>
              <button type="button" class="epg-match-icon-button" aria-label="关闭" @click="closeDrawer">
                <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
              </button>
            </header>

            <div class="epg-match-drawer-body">
              <div v-if="detailLoading" class="grid gap-3" aria-label="正在加载频道详情" aria-busy="true">
                <div v-for="index in 4" :key="index" class="h-24 animate-pulse rounded-2xl bg-[var(--surface)]"></div>
              </div>
              <section v-else-if="detailError" class="epg-match-inline-state" role="alert">
                <p>{{ detailError }}</p>
                <button type="button" class="epg-match-link-button" @click="loadDetail(selectedLogicalId)">重试</button>
              </section>
              <template v-else-if="detail">
                <section class="epg-match-detail-section">
                  <h3>当前状态</h3>
                  <div class="flex items-start justify-between gap-4">
                    <div class="min-w-0">
                      <p class="text-sm font-medium text-[var(--text-primary)]">{{ selectedState.label }}</p>
                      <p class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">{{ selectedState.detail }}</p>
                    </div>
                    <span class="shrink-0 text-xs text-[var(--text-tertiary)]">{{ logicalState(detail).label }}</span>
                  </div>
                </section>

                <section class="epg-match-detail-section">
                  <h3>当前节目单</h3>
                  <template v-if="detail.binding?.epg_source_id != null">
                    <p class="truncate text-sm font-medium text-[var(--text-primary)]">{{ bindingSourceName }}</p>
                    <p class="mt-1 truncate text-xs text-[var(--text-secondary)]">{{ detail.binding.epg_channel_display_name || detail.binding.epg_channel_id || '原 EPG 频道信息不可用' }}</p>
                    <p class="mt-2 text-[11px] text-[var(--text-tertiary)]">
                      {{ originLabel(detail.binding.origin) || '已有绑定' }}<template v-if="detail.binding.locked"> · 已锁定</template><template v-else> · 未锁定，当前绑定仍保留</template>
                    </p>
                  </template>
                  <template v-else>
                    <p class="text-sm font-medium text-[var(--text-primary)]">{{ detail.policy?.mode === 'no_epg' ? '不使用节目单' : '暂无节目单' }}</p>
                    <p class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">{{ detail.policy?.mode === 'no_epg' ? '这是明确设置，不会作为未匹配问题提示。' : '没有节目单不影响频道播放。' }}</p>
                    <p v-if="detail.production_read?.status === 'legacy_fallback'" class="mt-2 text-xs leading-5 text-amber-700 dark:text-amber-300">
                      当前节目仍通过旧版兼容映射读取；这不计入正式绑定，可手动选择节目单建立正式绑定。
                    </p>
                  </template>
                </section>

                <section class="epg-match-detail-section">
                  <h3>来源偏好</h3>
                  <p class="text-sm font-medium" :class="preferenceSummary(detail.preference).tone === 'warning' ? 'text-amber-600 dark:text-amber-300' : 'text-[var(--text-primary)]'">{{ preferenceSummary(detail.preference).label }}</p>
                  <p v-if="preferenceSummary(detail.preference).detail" class="mt-1 text-xs text-[var(--text-secondary)]">{{ preferenceSummary(detail.preference).detail }}</p>
                </section>

                <section v-if="detail.candidates?.length" class="epg-match-detail-section">
                  <h3>可能的节目单</h3>
                  <div class="grid gap-1.5">
                    <button
                      v-for="candidate in detail.candidates"
                      :key="`${candidate.epg_source_id}:${candidate.epg_channel_id}`"
                      type="button"
                      class="epg-match-candidate"
                      :disabled="!selectedManageable"
                      @click="openCatalog(candidate)"
                    >
                      <span class="min-w-0 text-left">
                        <span class="block truncate text-[13px] font-medium text-[var(--text-primary)]">{{ catalogDisplayName(candidate) }}</span>
                        <span class="mt-0.5 block truncate text-[11px] text-[var(--text-tertiary)]">{{ sourceDisplayName(candidate.epg_source_id, candidate.epg_source_name) }}</span>
                      </span>
                      <span v-if="selectedManageable" class="shrink-0 text-xs text-[var(--text-secondary)]">选择</span>
                    </button>
                  </div>
                </section>

                <section class="epg-match-detail-section">
                  <h3>管理</h3>
                  <template v-if="selectedManageable">
                    <div class="flex flex-wrap gap-2">
                      <button type="button" class="epg-match-button epg-match-button--primary" :disabled="actionPending" @click="openCatalog()">
                        {{ detail.binding?.epg_source_id != null ? '更换节目单' : '选择节目单' }}
                      </button>
                      <button
                        v-if="detail.binding?.epg_source_id != null && detail.binding.locked"
                        type="button"
                        class="epg-match-button epg-match-button--secondary"
                        :disabled="actionPending"
                        @click="setBindingLock(false)"
                      >{{ actionBusy === 'lock' ? '处理中…' : '解锁绑定' }}</button>
                      <button
                        v-else-if="detail.binding?.epg_source_id != null && detail.diagnostic?.status !== 'missing_target'"
                        type="button"
                        class="epg-match-button epg-match-button--secondary"
                        :disabled="actionPending"
                        @click="setBindingLock(true)"
                      >{{ actionBusy === 'lock' ? '处理中…' : '锁定绑定' }}</button>
                      <button v-if="showRestoreAutomatic" type="button" class="epg-match-button epg-match-button--secondary" :disabled="actionPending" @click="confirmation = 'restore'">恢复自动匹配</button>
                      <button v-if="detail.policy?.mode !== 'no_epg'" type="button" class="epg-match-button epg-match-button--danger" :disabled="actionPending" @click="confirmation = 'no_epg'">不使用节目单</button>
                    </div>

                    <div v-if="confirmation === 'restore'" class="epg-match-confirmation">
                      <p class="font-medium text-[var(--text-primary)]">恢复自动匹配？</p>
                      <p>当前手动绑定会移除，WaveFlow 会重新尝试匹配；也可能暂时保持未匹配。</p>
                      <div class="mt-3 flex justify-end gap-2">
                        <button type="button" class="epg-match-button epg-match-button--secondary" @click="confirmation = ''">取消</button>
                        <button type="button" class="epg-match-button epg-match-button--primary" :disabled="actionPending" @click="restoreAutomatic">确认恢复</button>
                      </div>
                    </div>

                    <div v-else-if="confirmation === 'no_epg'" class="epg-match-confirmation epg-match-confirmation--danger">
                      <p class="font-medium text-[var(--text-primary)]">不使用节目单？</p>
                      <p>当前绑定会移除，这个频道以后不会再提示为普通未匹配。</p>
                      <div class="mt-3 flex justify-end gap-2">
                        <button type="button" class="epg-match-button epg-match-button--secondary" @click="confirmation = ''">取消</button>
                        <button type="button" class="epg-match-button epg-match-button--danger-solid" :disabled="actionPending" @click="disableEpg">确认不使用</button>
                      </div>
                    </div>

                    <p v-if="detail.binding?.epg_source_id != null && !detail.binding.locked" class="mt-3 text-[11px] leading-5 text-[var(--text-tertiary)]">解锁只取消保护，当前节目单仍会保留；如需重新匹配，请使用“恢复自动匹配”。</p>
                  </template>
                  <p v-else class="text-xs leading-5 text-[var(--text-secondary)]">这个频道当前存在直播源冲突或已经失效，请先在直播源中处理频道状态。</p>
                </section>

                <details class="epg-match-advanced">
                  <summary>高级信息</summary>
                  <dl>
                    <dt>逻辑频道</dt>
                    <dd>{{ detail.channel.logical_channel_id }}</dd>
                    <template v-if="detail.binding?.epg_source_id != null">
                      <dt>EPG 目标</dt>
                      <dd>{{ detail.binding.epg_source_id }} / {{ detail.binding.epg_channel_id }}</dd>
                    </template>
                    <dt>频道成员</dt>
                    <dd>{{ compactCount(detail.channel.member_count) }}</dd>
                  </dl>
                </details>
              </template>
            </div>
          </template>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  disableLogicalChannelEpg,
  epgMatchingErrorMessage,
  fetchEpgMatchingChannels,
  fetchEpgMatchingDetail,
  fetchEpgMatchingOverview,
  lockEpgBinding,
  restoreAutomaticEpgBinding,
  searchEpgChannelCatalog,
  setManualEpgBinding,
  unlockEpgBinding,
} from '../../api/epgMatchingManagement'
import { fetchEpgSources } from '../../api/epgManagement'
import { useToastStore } from '../../stores/toast'
import { epgSourceDisplayName, formatCompactCount } from './epgSourceUi'
import {
  MATCHING_SCOPE_OPTIONS,
  epgBindingOriginLabel,
  epgCatalogDisplayName,
  epgCatalogTarget,
  epgLogicalState,
  epgMatchingState,
  epgMatchingTarget,
  epgPreferenceSummary,
  epgSourceHealthLabel,
} from './epgMatchingUi'

const PAGE_SIZE = 30
const CATALOG_PAGE_SIZE = 20

const toastStore = useToastStore()
const scopeOptions = MATCHING_SCOPE_OPTIONS
const compactCount = formatCompactCount
const matchingState = epgMatchingState
const originLabel = epgBindingOriginLabel
const logicalState = epgLogicalState
const preferenceSummary = epgPreferenceSummary
const catalogDisplayName = epgCatalogDisplayName
const sourceHealthLabel = epgSourceHealthLabel

const overview = ref(null)
const sourceOptions = ref([])
let overviewController = null
let sourceOptionsController = null
const items = ref([])
const page = ref(1)
const total = ref(0)
const scope = ref('all')
const searchDraft = ref('')
const searchQuery = ref('')
const listLoading = ref(true)
const listError = ref('')
let listController = null

const drawerOpen = ref(false)
const drawerRef = ref(null)
const selectedSeed = ref(null)
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
let detailController = null

const catalogMode = ref(false)
const catalogQueryDraft = ref('')
const catalogQuery = ref('')
const catalogSourceId = ref('')
const catalogItems = ref([])
const catalogPage = ref(1)
const catalogTotal = ref(0)
const catalogLoading = ref(false)
const catalogError = ref('')
const selectedCatalogTarget = ref(null)
let catalogController = null

const actionBusy = ref('')
const confirmation = ref('')
let actionRequestId = 0
let componentDisposed = false

const actionPending = computed(() => Boolean(actionBusy.value))
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const catalogPageSize = CATALOG_PAGE_SIZE
const catalogPageCount = computed(() => Math.max(1, Math.ceil(catalogTotal.value / CATALOG_PAGE_SIZE)))
const selectedItem = computed(() => detail.value || selectedSeed.value)
const selectedState = computed(() => epgMatchingState(selectedItem.value))
const selectedLogicalId = computed(() => String(selectedSeed.value?.channel?.logical_channel_id || detail.value?.channel?.logical_channel_id || ''))
const selectedChannelName = computed(() => String(detail.value?.channel?.display_name || selectedSeed.value?.channel?.display_name || '频道'))
const selectedManageable = computed(() => Boolean(detail.value && epgLogicalState(detail.value).manageable))
const sourceById = computed(() => new Map(sourceOptions.value.map((source) => [Number(source.id), source])))
const bindingSourceName = computed(() => {
  const binding = detail.value?.binding || {}
  return sourceDisplayName(binding.epg_source_id, binding.epg_source_name || '原来源已不存在')
})
const showRestoreAutomatic = computed(() => {
  if (!detail.value) return false
  return detail.value.policy?.mode === 'no_epg'
    || detail.value.binding?.origin === 'manual'
    || Boolean(detail.value.binding?.locked)
    || detail.value.diagnostic?.status === 'missing_target'
})

function sourceDisplayName(sourceId, fallback = '') {
  const source = sourceById.value.get(Number(sourceId))
  return source ? epgSourceDisplayName(source) : String(fallback || '未知节目单')
}

function targetLabel(item) {
  return epgMatchingTarget(
    item,
    sourceDisplayName(item?.binding?.epg_source_id, item?.binding?.epg_source_name),
  )
}

async function loadOverview() {
  overviewController?.abort()
  const controller = new AbortController()
  overviewController = controller
  try {
    const result = await fetchEpgMatchingOverview({ signal: controller.signal })
    if (!controller.signal.aborted && !componentDisposed) overview.value = result
  } catch {
    if (!controller.signal.aborted && !componentDisposed) overview.value = null
  }
}

async function loadSourceOptions() {
  sourceOptionsController?.abort()
  const controller = new AbortController()
  sourceOptionsController = controller
  try {
    const rows = await fetchEpgSources({ signal: controller.signal })
    if (!controller.signal.aborted && !componentDisposed) sourceOptions.value = Array.isArray(rows) ? rows : []
  } catch {
    if (!controller.signal.aborted && !componentDisposed) sourceOptions.value = []
  }
}

async function loadList() {
  listController?.abort()
  const controller = new AbortController()
  listController = controller
  listLoading.value = true
  listError.value = ''
  try {
    const result = await fetchEpgMatchingChannels({
      page: page.value,
      pageSize: PAGE_SIZE,
      scope: scope.value,
      logicalScope: 'active',
      query: searchQuery.value,
      signal: controller.signal,
    })
    if (controller.signal.aborted || componentDisposed) return
    items.value = Array.isArray(result?.items) ? result.items : []
    total.value = Number(result?.total || 0)
    const maxPage = Math.max(1, Math.ceil(total.value / PAGE_SIZE))
    if (page.value > maxPage) {
      page.value = maxPage
      await loadList()
    }
  } catch (error) {
    if (!controller.signal.aborted && !componentDisposed) {
      listError.value = epgMatchingErrorMessage(error, '无法加载频道匹配，请稍后重试')
      items.value = []
      total.value = 0
    }
  } finally {
    if (listController === controller && !componentDisposed) listLoading.value = false
  }
}

function setScope(nextScope) {
  if (scope.value === nextScope) return
  scope.value = nextScope
  page.value = 1
  loadList()
}

function applySearch() {
  const next = searchDraft.value.trim()
  if (next === searchQuery.value) return
  searchQuery.value = next
  page.value = 1
  loadList()
}

function clearSearch() {
  searchDraft.value = ''
  searchQuery.value = ''
  page.value = 1
  loadList()
}

function resetFilters() {
  scope.value = 'all'
  searchDraft.value = ''
  searchQuery.value = ''
  page.value = 1
  loadList()
}

function changePage(nextPage) {
  if (nextPage < 1 || nextPage > pageCount.value || nextPage === page.value) return
  page.value = nextPage
  loadList()
  window.scrollTo?.({ top: 0, behavior: 'smooth' })
}

async function openDetail(item) {
  selectedSeed.value = item
  detail.value = null
  detailError.value = ''
  catalogMode.value = false
  confirmation.value = ''
  drawerOpen.value = true
  await nextTick()
  drawerRef.value?.focus()
  await loadDetail(item.channel.logical_channel_id)
}

async function loadDetail(logicalChannelId) {
  if (!logicalChannelId) return
  detailController?.abort()
  const controller = new AbortController()
  detailController = controller
  detailLoading.value = true
  detailError.value = ''
  try {
    const result = await fetchEpgMatchingDetail(logicalChannelId, {
      candidateLimit: 10,
      signal: controller.signal,
    })
    if (!controller.signal.aborted && !componentDisposed) detail.value = result
  } catch (error) {
    if (!controller.signal.aborted && !componentDisposed) detailError.value = epgMatchingErrorMessage(error, '无法加载频道详情，请稍后重试')
  } finally {
    if (detailController === controller && !componentDisposed) detailLoading.value = false
  }
}

function closeDrawer() {
  if (actionBusy.value) return
  detailController?.abort()
  catalogController?.abort()
  drawerOpen.value = false
  catalogMode.value = false
  selectedSeed.value = null
  detail.value = null
  confirmation.value = ''
}

function handleDrawerEscape() {
  if (confirmation.value) confirmation.value = ''
  else if (catalogMode.value) closeCatalog()
  else closeDrawer()
}

function openCatalog(preselected = null) {
  if (!selectedManageable.value) return
  selectedCatalogTarget.value = preselected || null
  catalogQueryDraft.value = detail.value?.channel?.display_name || ''
  catalogQuery.value = catalogQueryDraft.value
  catalogSourceId.value = preselected?.epg_source_id ? String(preselected.epg_source_id) : ''
  catalogPage.value = 1
  catalogMode.value = true
  confirmation.value = ''
  loadCatalog()
}

function closeCatalog() {
  catalogController?.abort()
  catalogMode.value = false
  catalogItems.value = []
  catalogError.value = ''
  selectedCatalogTarget.value = null
}

async function loadCatalog() {
  catalogController?.abort()
  const controller = new AbortController()
  catalogController = controller
  catalogLoading.value = true
  catalogError.value = ''
  try {
    const result = await searchEpgChannelCatalog({
      page: catalogPage.value,
      pageSize: CATALOG_PAGE_SIZE,
      query: catalogQuery.value,
      sourceId: catalogSourceId.value ? Number(catalogSourceId.value) : null,
      availability: 'all',
      signal: controller.signal,
    })
    if (controller.signal.aborted || componentDisposed) return
    catalogItems.value = Array.isArray(result?.items) ? result.items : []
    catalogTotal.value = Number(result?.total || 0)
  } catch (error) {
    if (!controller.signal.aborted && !componentDisposed) {
      catalogError.value = epgMatchingErrorMessage(error, '无法搜索节目单频道，请稍后重试')
      catalogItems.value = []
      catalogTotal.value = 0
    }
  } finally {
    if (catalogController === controller && !componentDisposed) catalogLoading.value = false
  }
}

function applyCatalogSearch() {
  catalogQuery.value = catalogQueryDraft.value.trim()
  catalogPage.value = 1
  selectedCatalogTarget.value = null
  loadCatalog()
}

function changeCatalogSource() {
  catalogPage.value = 1
  selectedCatalogTarget.value = null
  loadCatalog()
}

function changeCatalogPage(nextPage) {
  if (nextPage < 1 || nextPage > catalogPageCount.value || nextPage === catalogPage.value) return
  catalogPage.value = nextPage
  selectedCatalogTarget.value = null
  loadCatalog()
}

function catalogTargetSelected(item) {
  if (!selectedCatalogTarget.value) return false
  const selected = epgCatalogTarget(selectedCatalogTarget.value)
  const candidate = epgCatalogTarget(item)
  return selected.epg_source_id === candidate.epg_source_id
    && selected.epg_channel_id === candidate.epg_channel_id
}

async function refreshCurrentState() {
  await Promise.all([loadOverview(), loadList()])
  if (selectedLogicalId.value) await loadDetail(selectedLogicalId.value)
}

async function bindSelectedCatalogTarget() {
  if (!selectedCatalogTarget.value || !selectedLogicalId.value || actionBusy.value) return
  const identity = epgCatalogTarget(selectedCatalogTarget.value)
  if (!Number.isInteger(identity.epg_source_id) || identity.epg_source_id <= 0 || !identity.epg_channel_id) return
  const requestId = ++actionRequestId
  actionBusy.value = 'binding'
  try {
    await setManualEpgBinding(selectedLogicalId.value, identity)
    if (requestId !== actionRequestId || componentDisposed) return
    toastStore.success('节目单已绑定并锁定')
    closeCatalog()
    await refreshCurrentState()
  } catch (error) {
    if (requestId === actionRequestId && !componentDisposed) toastStore.error(epgMatchingErrorMessage(error, '节目单绑定失败，请稍后重试'))
  } finally {
    if (requestId === actionRequestId && !componentDisposed) actionBusy.value = ''
  }
}

async function setBindingLock(locked) {
  if (!selectedLogicalId.value || actionBusy.value) return
  const requestId = ++actionRequestId
  actionBusy.value = 'lock'
  try {
    if (locked) await lockEpgBinding(selectedLogicalId.value)
    else await unlockEpgBinding(selectedLogicalId.value)
    if (requestId !== actionRequestId || componentDisposed) return
    toastStore.success(locked ? '节目单绑定已锁定' : '已解锁，当前节目单仍保留')
    await refreshCurrentState()
  } catch (error) {
    if (requestId === actionRequestId && !componentDisposed) toastStore.error(epgMatchingErrorMessage(error, locked ? '锁定失败，请稍后重试' : '解锁失败，请稍后重试'))
  } finally {
    if (requestId === actionRequestId && !componentDisposed) actionBusy.value = ''
  }
}

async function restoreAutomatic() {
  if (!selectedLogicalId.value || actionBusy.value) return
  const requestId = ++actionRequestId
  actionBusy.value = 'restore'
  try {
    const result = await restoreAutomaticEpgBinding(selectedLogicalId.value)
    if (requestId !== actionRequestId || componentDisposed) return
    confirmation.value = ''
    if (result?.warning?.code === 'maintenance_degraded') {
      toastStore.warning('已恢复自动匹配，自动维护暂时未完成，稍后会重试')
    } else {
      toastStore.success('已恢复自动匹配')
    }
    await refreshCurrentState()
  } catch (error) {
    if (requestId === actionRequestId && !componentDisposed) toastStore.error(epgMatchingErrorMessage(error, '恢复自动匹配失败，请稍后重试'))
  } finally {
    if (requestId === actionRequestId && !componentDisposed) actionBusy.value = ''
  }
}

async function disableEpg() {
  if (!selectedLogicalId.value || actionBusy.value) return
  const requestId = ++actionRequestId
  actionBusy.value = 'no_epg'
  try {
    await disableLogicalChannelEpg(selectedLogicalId.value)
    if (requestId !== actionRequestId || componentDisposed) return
    confirmation.value = ''
    toastStore.success('这个频道已设置为不使用节目单')
    await refreshCurrentState()
  } catch (error) {
    if (requestId === actionRequestId && !componentDisposed) toastStore.error(epgMatchingErrorMessage(error, '节目单设置失败，请稍后重试'))
  } finally {
    if (requestId === actionRequestId && !componentDisposed) actionBusy.value = ''
  }
}

onMounted(() => {
  loadOverview()
  loadSourceOptions()
  loadList()
})

onBeforeUnmount(() => {
  componentDisposed = true
  actionRequestId += 1
  overviewController?.abort()
  sourceOptionsController?.abort()
  listController?.abort()
  detailController?.abort()
  catalogController?.abort()
})
</script>

<style scoped>
.epg-matching-page {
  min-width: 0;
}

.epg-match-summary {
  display: flex;
  min-height: 44px;
  align-items: center;
  gap: 14px;
  border-bottom: 1px solid var(--border);
  padding: 0 2px 12px;
}

.epg-match-summary-item {
  display: inline-flex;
  align-items: baseline;
  gap: 7px;
  color: var(--text-secondary);
  font-size: 12px;
}

.epg-match-summary-item strong {
  color: var(--text-primary);
  font-size: 15px;
  font-weight: 650;
}

.epg-match-summary-item--warning strong { color: rgb(217 119 6); }
.epg-match-summary-separator { width: 1px; height: 16px; background: var(--border); }

.epg-match-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 0;
}

.epg-match-search {
  display: flex;
  width: min(360px, 100%);
  min-height: var(--control-height);
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  padding: 0 12px;
  color: var(--text-tertiary);
}

.epg-match-search:focus-within { border-color: var(--border-strong); box-shadow: 0 0 0 2px var(--surface-strong); }
.epg-match-search-submit { display: inline-flex; flex-shrink: 0; align-items: center; justify-content: center; color: inherit; }
.epg-match-search-submit:hover { color: var(--text-secondary); }
.epg-match-search input { min-width: 0; flex: 1; background: transparent; font-size: 13px; color: var(--text-primary); outline: none; }
.epg-match-search input::placeholder { color: var(--text-tertiary); }
.epg-match-clear { flex-shrink: 0; font-size: 11px; font-weight: 600; color: var(--text-secondary); }

.epg-match-filters { display: flex; min-width: 0; align-items: center; gap: 3px; overflow-x: auto; }
.epg-match-filter { display: inline-flex; min-height: var(--control-height-small); flex-shrink: 0; align-items: center; gap: 5px; border-radius: 10px; padding: 0 10px; font-size: 12px; font-weight: 500; color: var(--text-secondary); }
.epg-match-filter:hover { background: var(--surface); color: var(--text-primary); }
.epg-match-filter.is-active { background: var(--surface-strong); color: var(--text-primary); }
.epg-match-filter span { color: var(--text-tertiary); font-size: 10px; }

.epg-match-row {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(220px, 1fr) minmax(260px, 0.8fr) auto;
  align-items: center;
  gap: 18px;
  border: 1px solid var(--border);
  border-radius: var(--card-radius);
  background: var(--card-bg);
  padding: 15px 16px;
  text-align: left;
  transition: border-color 150ms ease, background-color 150ms ease, transform 150ms ease;
}

.epg-match-row:hover { border-color: var(--border-strong); background: var(--surface-hover); transform: translateY(-1px); }
.epg-match-row:focus-visible { outline: 2px solid var(--border-strong); outline-offset: 2px; }
.epg-match-channel-icon { display: inline-flex; width: var(--control-height-small); height: var(--control-height-small); flex-shrink: 0; align-items: center; justify-content: center; border: 1px solid var(--border); border-radius: 11px; background: var(--surface); color: var(--text-secondary); }
.epg-match-target { min-width: 0; text-align: left; }

.epg-match-status {
  display: inline-flex;
  min-height: 22px;
  flex-shrink: 0;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 10px;
  font-weight: 650;
  white-space: nowrap;
}

.epg-match-status--success { border-color: rgb(16 185 129 / 0.22); background: rgb(16 185 129 / 0.08); color: rgb(5 150 105); }
.epg-match-status--neutral { background: var(--surface); color: var(--text-secondary); }
.epg-match-status--warning { border-color: rgb(245 158 11 / 0.25); background: rgb(245 158 11 / 0.08); color: rgb(217 119 6); }
.epg-match-status--muted { background: var(--surface); color: var(--text-tertiary); }

.epg-match-pagination,
.epg-match-catalog-pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 2px 0; font-size: 11px; color: var(--text-tertiary); }
.epg-match-page-button { min-height: var(--control-height-small); border: 1px solid var(--border); border-radius: 10px; padding: 0 11px; font-size: 11px; color: var(--text-secondary); }
.epg-match-page-button:hover:not(:disabled) { border-color: var(--border-strong); background: var(--surface); color: var(--text-primary); }
.epg-match-page-button:disabled { cursor: not-allowed; opacity: 0.45; }

.epg-match-state-panel { display: flex; min-height: 220px; flex-direction: column; align-items: center; justify-content: center; border: 1px dashed var(--border); border-radius: var(--panel-radius); padding: 28px; text-align: center; }
.epg-match-state-panel h3 { margin-top: 12px; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.epg-match-state-panel p { margin-top: 6px; max-width: 380px; font-size: 12px; line-height: 1.7; color: var(--text-secondary); }
.epg-match-state-panel .epg-match-button { margin-top: 14px; }
.epg-match-empty-icon { display: inline-flex; width: 42px; height: 42px; align-items: center; justify-content: center; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); color: var(--text-secondary); }

.epg-match-drawer-layer { position: fixed; inset: 0; z-index: 120; }
.epg-match-drawer-backdrop { position: absolute; inset: 0; background: rgb(0 0 0 / 0.18); backdrop-filter: blur(2px); }
.epg-match-drawer-pane { position: absolute; top: 0; right: 0; bottom: 0; display: flex; width: min(500px, 100vw); flex-direction: column; overflow: hidden; border-left: 1px solid var(--border); background: var(--bg-soft); box-shadow: -18px 0 48px rgb(15 23 42 / 0.16); outline: none; }
:global(.dark) .epg-match-drawer-pane { box-shadow: -18px 0 48px rgb(0 0 0 / 0.48); }
.epg-match-drawer-header { display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border); padding: 15px 16px; }
.epg-match-drawer-body { display: flex; min-height: 0; flex: 1; flex-direction: column; gap: 0; overflow-y: auto; padding: 4px 16px 22px; }
.epg-match-drawer-footer { display: flex; align-items: center; gap: 12px; border-top: 1px solid var(--border); background: var(--bg-soft); padding: 12px 16px calc(env(safe-area-inset-bottom) + 12px); }

.epg-match-detail-section { border-bottom: 1px solid var(--border); padding: 16px 2px; }
.epg-match-detail-section h3 { margin-bottom: 9px; font-size: 11px; font-weight: 650; letter-spacing: 0.04em; color: var(--text-tertiary); }
.epg-match-candidate { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 12px; border-radius: 12px; background: var(--surface); padding: 10px 12px; }
.epg-match-candidate:hover:not(:disabled) { background: var(--surface-hover); }
.epg-match-candidate:disabled { cursor: default; }

.epg-match-button { display: inline-flex; min-height: var(--control-height); align-items: center; justify-content: center; border-radius: 11px; padding: 0 13px; font-size: 12px; font-weight: 600; transition: opacity 150ms ease, background-color 150ms ease, border-color 150ms ease; }
.epg-match-button:disabled { cursor: not-allowed; opacity: 0.5; }
.epg-match-button--primary { background: var(--text-primary); color: var(--bg); }
.epg-match-button--secondary { border: 1px solid var(--border); background: var(--surface); color: var(--text-primary); }
.epg-match-button--secondary:hover:not(:disabled) { border-color: var(--border-strong); background: var(--surface-hover); }
.epg-match-button--danger { color: rgb(220 38 38); }
.epg-match-button--danger:hover:not(:disabled) { background: rgb(239 68 68 / 0.08); }
.epg-match-button--danger-solid { background: rgb(239 68 68); color: white; }
.epg-match-icon-button { display: inline-flex; width: var(--icon-button-size); height: var(--icon-button-size); flex-shrink: 0; align-items: center; justify-content: center; border-radius: 11px; color: var(--text-secondary); }
.epg-match-icon-button:hover { background: var(--surface-hover); color: var(--text-primary); }
.epg-match-link-button { margin-top: 6px; font-size: 12px; font-weight: 600; color: var(--text-primary); }

.epg-match-confirmation { margin-top: 12px; border: 1px solid rgb(245 158 11 / 0.22); border-radius: 14px; background: rgb(245 158 11 / 0.06); padding: 12px; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
.epg-match-confirmation--danger { border-color: rgb(239 68 68 / 0.2); background: rgb(239 68 68 / 0.05); }
.epg-match-advanced { margin-top: 14px; border: 1px solid var(--border); border-radius: 14px; padding: 11px 12px; font-size: 11px; color: var(--text-tertiary); }
.epg-match-advanced summary { cursor: pointer; font-weight: 600; color: var(--text-secondary); }
.epg-match-advanced dl { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 7px 10px; margin-top: 10px; }
.epg-match-advanced dd { min-width: 0; overflow-wrap: anywhere; color: var(--text-secondary); }

.epg-match-select { width: 100%; min-height: var(--control-height); border: 1px solid var(--border); border-radius: var(--control-radius); background: var(--surface); padding: 0 11px; font-size: 12px; color: var(--text-primary); outline: none; }
.epg-match-inline-state { display: flex; min-height: 150px; flex-direction: column; align-items: center; justify-content: center; padding: 20px; text-align: center; font-size: 12px; color: var(--text-secondary); }
.epg-match-inline-state span { margin-top: 4px; color: var(--text-tertiary); }
.epg-catalog-row { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 12px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); padding: 11px 12px; }
.epg-catalog-row:hover { border-color: var(--border-strong); background: var(--surface-hover); }
.epg-catalog-row.is-selected { border-color: var(--text-secondary); background: var(--surface-strong); }
.epg-catalog-check { visibility: hidden; color: rgb(5 150 105); }
.epg-catalog-row.is-selected .epg-catalog-check { visibility: visible; }

.epg-match-drawer-enter-active,
.epg-match-drawer-leave-active { transition: opacity 180ms ease; }
.epg-match-drawer-enter-active .epg-match-drawer-pane,
.epg-match-drawer-leave-active .epg-match-drawer-pane { transition: transform 240ms cubic-bezier(0.32, 0.72, 0, 1); }
.epg-match-drawer-enter-from,
.epg-match-drawer-leave-to { opacity: 0; }
.epg-match-drawer-enter-from .epg-match-drawer-pane,
.epg-match-drawer-leave-to .epg-match-drawer-pane { transform: translateX(100%); }

@media (max-width: 767px) {
  .epg-match-summary { flex-wrap: wrap; gap: 8px 12px; }
  .epg-match-summary .ml-auto { width: 100%; margin-left: 0; }
  .epg-match-controls { align-items: stretch; flex-direction: column; }
  .epg-match-search { width: 100%; }
  .epg-match-filters { margin: 0 -2px; padding-bottom: 2px; }
  .epg-match-row { grid-template-columns: minmax(0, 1fr) auto; gap: 10px 12px; padding: 14px; }
  .epg-match-target { grid-column: 1 / -1; padding-left: 46px; }
  .epg-match-status { grid-column: 2; grid-row: 1; }
  .epg-match-drawer-pane { width: 100vw; border-left: 0; }
  .epg-match-drawer-header { padding-top: calc(env(safe-area-inset-top) + 12px); }
  .epg-match-drawer-footer { align-items: stretch; flex-direction: column; }
  .epg-match-drawer-footer .epg-match-button { width: 100%; }
}
</style>
