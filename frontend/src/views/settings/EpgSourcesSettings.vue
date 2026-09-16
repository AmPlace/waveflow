<template>
  <section aria-label="节目单来源">
    <Teleport defer to="#epg-settings-actions">
      <button type="button" class="epg-primary-button" @click="openCreateDialog">添加来源</button>
    </Teleport>

    <section v-if="loading" class="grid gap-3" aria-label="正在加载节目单来源" aria-busy="true">
      <div v-for="index in 2" :key="index" class="h-36 animate-pulse rounded-3xl border border-[var(--border)] bg-[var(--surface)]"></div>
    </section>

    <section v-else-if="loadError" class="rounded-3xl border border-red-500/20 bg-red-500/5 px-6 py-10 text-center" role="alert">
      <h3 class="text-sm font-semibold text-[var(--text-primary)]">节目单来源加载失败</h3>
      <p class="mt-2 text-sm text-[var(--text-secondary)]">{{ loadError }}</p>
      <button type="button" class="epg-secondary-button mt-4" @click="loadSources">重试</button>
    </section>

    <template v-else-if="sources.length">
      <div class="grid gap-3">
        <article v-for="source in sources" :key="source.id" class="min-w-0 rounded-3xl border border-[var(--border)] bg-[var(--card-bg)] p-4 sm:p-5">
          <div class="flex min-w-0 flex-col gap-4 lg:flex-row lg:items-center lg:justify-between lg:gap-6">
            <div class="flex min-w-0 flex-1 items-start gap-3">
              <span class="flex size-9 shrink-0 items-center justify-center rounded-xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)]">
                <svg class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 3.75h7.5L18.5 8v12.25H7V3.75Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M14.5 3.75V8h4M9.75 12h6M9.75 15.5h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
              </span>
              <div class="min-w-0">
                <div class="flex min-w-0 flex-wrap items-center gap-1.5">
                  <h3 class="max-w-full truncate text-[15px] font-semibold text-[var(--text-primary)]" :title="sourceName(source)">{{ sourceName(source) }}</h3>
                  <span class="epg-identity-pill">{{ sourceKind(source) }}</span>
                  <span class="epg-status-pill" :class="`epg-status--${sourceState(source).key}`" :title="sourceState(source).detail">{{ sourceState(source).label }}</span>
                </div>
                <p v-if="source.source_origin === 'custom' && displayUrl(source)" class="mt-1 max-w-full truncate text-xs text-[var(--text-tertiary)]" :title="displayUrl(source)">{{ displayUrl(source) }}</p>
                <p v-else-if="source.source_origin === 'builtin'" class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">覆盖常用中国电视频道，由 WaveFlow 维护来源地址</p>

                <div class="mt-2 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[var(--text-secondary)]" aria-label="来源数据统计">
                  <span>{{ compactCount(source.data?.channel_count) }} 频道</span>
                  <span aria-hidden="true">·</span>
                  <span>{{ compactCount(source.data?.programme_count) }} 节目</span>
                  <template v-if="coverage(source)">
                    <span aria-hidden="true">·</span>
                    <span>数据 {{ coverage(source) }}</span>
                  </template>
                </div>

                <div class="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-[var(--text-tertiary)]">
                  <span>最近成功 {{ formatTime(source.refresh?.last_success) }}</span>
                  <span class="hidden sm:inline" aria-hidden="true">·</span>
                  <span>下次更新 {{ source.automation?.scheduled ? formatTime(source.automation?.next_run) : '未安排' }}</span>
                  <template v-if="sourceState(source).key !== 'healthy' && source.refresh?.last_attempt && source.refresh.last_attempt !== source.refresh?.last_success">
                    <span class="hidden sm:inline" aria-hidden="true">·</span>
                    <span>最近尝试 {{ formatTime(source.refresh.last_attempt) }}</span>
                  </template>
                  <span v-if="source.automation?.running" class="text-[var(--text-primary)]">正在更新</span>
                </div>
                <p v-if="source.refresh?.failure" class="mt-2 text-xs leading-5 text-red-600 dark:text-red-300">{{ safeAdminDiagnostic(source.refresh.failure) }}</p>
                <p v-if="source.automation?.last_run_status === 'partial'" class="mt-2 text-xs leading-5 text-amber-700 dark:text-amber-300">节目单数据可用，后续绑定维护未完全完成</p>
              </div>
            </div>

            <div class="flex max-w-full shrink-0 flex-wrap items-center gap-1 border-t border-[var(--border)] pt-3 lg:max-w-[22rem] lg:justify-end lg:border-t-0 lg:pt-0">
              <label v-if="source.capabilities?.can_enable_disable" class="flex min-h-10 shrink-0 items-center gap-2 px-2 text-xs font-medium text-[var(--text-secondary)]">
                <span>{{ source.enabled ? '已启用' : '已停用' }}</span>
                <input
                  type="checkbox"
                  class="size-4 rounded accent-neutral-950 dark:accent-white"
                  :checked="source.enabled"
                  :disabled="isUpdating(source.id)"
                  :aria-label="`${sourceName(source)}启用状态`"
                  @change="toggleSource(source, $event.target.checked)"
                />
              </label>
              <button
                v-if="source.capabilities?.can_refresh"
                type="button"
                class="epg-action-button"
                :disabled="!source.enabled || source.automation?.running || isRefreshing(source.id)"
                @click="runRefresh(source)"
              >
                {{ isRefreshing(source.id) || source.automation?.running ? '刷新中…' : '立即刷新' }}
              </button>
              <button
                v-if="source.capabilities?.can_edit_name || source.capabilities?.can_edit_url"
                type="button"
                class="epg-action-button"
                @click="openEditDialog(source)"
              >编辑</button>
              <button
                v-if="source.capabilities?.can_delete"
                type="button"
                class="epg-action-button epg-action-button--danger"
                @click="openDeleteDialog(source)"
              >删除</button>
            </div>
          </div>
        </article>
      </div>

      <section v-if="!customSources.length" class="mt-4 rounded-3xl border border-dashed border-[var(--border)] bg-[var(--surface)]/60 px-6 py-6 text-center">
        <h3 class="text-sm font-semibold text-[var(--text-primary)]">添加自己的 XMLTV 节目单</h3>
        <p class="mx-auto mt-2 max-w-lg text-sm leading-6 text-[var(--text-secondary)]">用于其他地区、自建服务或当前内置来源未覆盖的频道。</p>
        <button type="button" class="epg-secondary-button mt-4" @click="openCreateDialog">添加来源</button>
      </section>
    </template>

    <section v-else class="rounded-3xl border border-dashed border-[var(--border-strong)] bg-[var(--surface)] px-6 py-12 text-center">
      <span class="mx-auto flex size-11 items-center justify-center rounded-2xl border border-[var(--border)] bg-[var(--bg-soft)] text-[var(--text-secondary)]">
        <svg class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 5h12v14H6zM9 9h6M9 13h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
      <h3 class="mt-4 text-sm font-semibold text-[var(--text-primary)]">还没有节目单来源</h3>
      <p class="mx-auto mt-2 max-w-md text-sm leading-6 text-[var(--text-secondary)]">添加标准 XMLTV 地址，为频道提供当前节目和节目单信息。</p>
      <button type="button" class="epg-primary-button mt-5" @click="openCreateDialog">添加来源</button>
    </section>
  </section>

  <Teleport to="body">
    <div v-if="editorOpen" class="epg-dialog-layer" @click.self="closeEditor">
      <section class="epg-dialog-panel" role="dialog" aria-modal="true" :aria-labelledby="editorTitleId" @keydown.esc.prevent="closeEditor">
        <header class="flex items-start justify-between gap-4">
          <div>
            <h2 :id="editorTitleId" class="text-lg font-semibold text-[var(--text-primary)]">{{ editorMode === 'create' ? '添加节目单来源' : '编辑节目单来源' }}</h2>
            <p class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">支持任意标准 XMLTV 地址，不区分地区。</p>
          </div>
          <button type="button" class="epg-icon-button" aria-label="关闭" @click="closeEditor">
            <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </button>
        </header>

        <form class="mt-6 space-y-4" @submit.prevent="saveEditor">
          <label class="block">
            <span class="epg-field-label">名称</span>
            <input ref="editorNameInput" v-model="editorDraft.name" type="text" maxlength="120" autocomplete="off" class="epg-input" placeholder="例如 Japan XMLTV" required />
          </label>

          <label v-if="editorMode === 'create'" class="block">
            <span class="epg-field-label">XMLTV URL</span>
            <input v-model="editorDraft.url" type="url" autocomplete="off" spellcheck="false" class="epg-input" placeholder="https://example.com/epg.xml" required />
            <span class="mt-2 block text-xs leading-5 text-[var(--text-tertiary)]">地址可能包含访问参数，保存后不会在页面中显示完整参数。</span>
          </label>

          <template v-else-if="editingSource?.capabilities?.can_edit_url">
            <div class="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-3.5">
              <span class="epg-field-label">当前地址</span>
              <p class="mt-1.5 truncate text-xs text-[var(--text-secondary)]" :title="displayUrl(editingSource)">{{ displayUrl(editingSource) || '未提供' }}</p>
              <label class="mt-3 flex items-center gap-2 text-xs font-medium text-[var(--text-primary)]">
                <input v-model="editorDraft.replaceUrl" type="checkbox" class="size-4 rounded accent-neutral-950 dark:accent-white" />
                更换 XMLTV 地址
              </label>
            </div>
            <label v-if="editorDraft.replaceUrl" class="block">
              <span class="epg-field-label">新的 XMLTV URL</span>
              <input v-model="editorDraft.url" type="url" autocomplete="off" spellcheck="false" class="epg-input" placeholder="重新输入完整地址" required />
              <span class="mt-2 block text-xs leading-5 text-[var(--text-tertiary)]">不会使用上方净化后的展示地址覆盖原始 URL。</span>
            </label>
          </template>

          <div v-else class="rounded-2xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 text-xs leading-5 text-[var(--text-secondary)]">内置来源地址由 WaveFlow 维护。</div>

          <label class="flex min-h-11 items-center justify-between gap-4 rounded-2xl border border-[var(--border)] px-4 py-3">
            <span>
              <span class="block text-sm font-medium text-[var(--text-primary)]">启用来源</span>
              <span class="mt-0.5 block text-xs text-[var(--text-secondary)]">启用后参与定时更新</span>
            </span>
            <input v-model="editorDraft.enabled" type="checkbox" class="size-4 rounded accent-neutral-950 dark:accent-white" />
          </label>

          <p v-if="editorError" class="rounded-2xl border border-red-500/20 bg-red-500/5 px-4 py-3 text-xs leading-5 text-red-500" role="alert">{{ editorError }}</p>

          <footer class="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
            <button type="button" class="epg-secondary-button" :disabled="editorSaving" @click="closeEditor">取消</button>
            <button type="submit" class="epg-primary-button" :disabled="!editorCanSave || editorSaving">{{ editorSaving ? '保存中…' : editorMode === 'create' ? '添加来源' : '保存修改' }}</button>
          </footer>
        </form>
      </section>
    </div>

    <div v-if="deleteDialogOpen" class="epg-dialog-layer" @click.self="closeDeleteDialog">
      <section class="epg-dialog-panel max-w-lg" role="dialog" aria-modal="true" aria-labelledby="delete-source-title" @keydown.esc.prevent="closeDeleteDialog">
        <header class="flex items-start justify-between gap-4">
          <div>
            <h2 id="delete-source-title" class="text-lg font-semibold text-[var(--text-primary)]">删除“{{ deleteSource ? sourceName(deleteSource) : '' }}”？</h2>
            <p class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">删除后会移除该来源下载的节目数据。</p>
          </div>
          <button type="button" class="epg-icon-button" aria-label="关闭" :disabled="deleteSaving" @click="closeDeleteDialog">
            <svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </button>
        </header>

        <div v-if="deleteImpactLoading" class="mt-6 space-y-3" aria-label="正在检查删除影响" aria-busy="true">
          <div class="h-20 animate-pulse rounded-2xl bg-[var(--surface)]"></div>
          <div class="h-12 animate-pulse rounded-2xl bg-[var(--surface)]"></div>
        </div>

        <template v-else-if="deleteImpact">
          <div class="mt-6 grid grid-cols-2 gap-2">
            <div class="rounded-2xl bg-[var(--surface)] p-3.5">
              <p class="text-[11px] text-[var(--text-tertiary)]">正在使用的频道</p>
              <p class="mt-1 text-lg font-semibold tabular-nums text-[var(--text-primary)]">{{ compactCount(deleteImpact.affected_active_logical_channel_count) }}</p>
            </div>
            <div class="rounded-2xl bg-[var(--surface)] p-3.5">
              <p class="text-[11px] text-[var(--text-tertiary)]">来源偏好引用</p>
              <p class="mt-1 text-lg font-semibold tabular-nums text-[var(--text-primary)]">{{ compactCount(deleteImpact.preference_references) }}</p>
            </div>
          </div>
          <p class="mt-4 text-sm leading-6 text-[var(--text-secondary)]">
            <template v-if="deleteImpact.affected_active_logical_channel_count">{{ compactCount(deleteImpact.affected_active_logical_channel_count) }} 个频道正在使用这个节目单，</template>
            <template v-else>当前没有活跃频道使用这个节目单，</template>
            其中 {{ compactCount(deleteImpact.manual_bindings_count) }} 个为手动绑定，{{ compactCount(deleteImpact.locked_bindings_count) }} 个已锁定。删除后受影响频道可能暂时没有节目单。
          </p>

          <label v-if="deleteRequiresAck" class="mt-4 flex items-start gap-3 rounded-2xl border border-amber-500/25 bg-amber-500/5 p-4 text-xs leading-5 text-[var(--text-secondary)]">
            <input v-model="deleteAcknowledged" type="checkbox" class="mt-0.5 size-4 shrink-0 rounded accent-neutral-950 dark:accent-white" />
            <span>我了解手动或锁定绑定会保留为待修复状态，不会自动改到其他来源。</span>
          </label>

          <footer class="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button type="button" class="epg-secondary-button" :disabled="deleteSaving" @click="closeDeleteDialog">取消</button>
            <button type="button" class="epg-danger-button" :disabled="deleteSaving || (deleteRequiresAck && !deleteAcknowledged)" @click="confirmDelete">{{ deleteSaving ? '删除中…' : '删除来源' }}</button>
          </footer>
        </template>
      </section>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  createEpgSource,
  deleteEpgSource,
  epgSourceErrorMessage,
  fetchEpgSourceDeleteImpact,
  fetchEpgSources,
  refreshEpgSource,
  updateEpgSource,
} from '../../api/epgManagement'
import { useToastStore } from '../../stores/toast'
import { safeAdminDiagnostic } from '../../api/adminUi.js'
import {
  buildEpgSourceCreatePayload,
  buildEpgSourceUpdatePayload,
  deleteNeedsAcknowledgement,
  epgSourceDisplayName,
  epgSourceKindLabel,
  epgSourceStatus,
  formatCompactCount,
  formatCoverage,
  formatEpgTime,
  safeDisplayUrl,
} from './epgSourceUi'

const toastStore = useToastStore()
const sources = ref([])
const loading = ref(true)
const loadError = ref('')
const refreshingIds = ref(new Set())
const updatingIds = ref(new Set())

const editorOpen = ref(false)
const editorMode = ref('create')
const editingSource = ref(null)
const editorSaving = ref(false)
const editorError = ref('')
const editorNameInput = ref(null)
const editorDraft = reactive({ name: '', url: '', enabled: true, replaceUrl: false })

const deleteDialogOpen = ref(false)
const deleteSource = ref(null)
const deleteImpact = ref(null)
const deleteImpactLoading = ref(false)
const deleteAcknowledged = ref(false)
const deleteSaving = ref(false)
let loadController = null
let deleteImpactController = null
let componentDisposed = false

const customSources = computed(() => sources.value.filter((source) => source.source_origin === 'custom'))
const editorTitleId = computed(() => editorMode.value === 'create' ? 'create-epg-source-title' : 'edit-epg-source-title')
const editorCanSave = computed(() => {
  if (!editorDraft.name.trim()) return false
  if (editorMode.value === 'create') return Boolean(editorDraft.url.trim())
  if (editorDraft.replaceUrl) return Boolean(editorDraft.url.trim())
  return true
})
const deleteRequiresAck = computed(() => deleteNeedsAcknowledgement(deleteImpact.value))

const sourceName = epgSourceDisplayName
const sourceKind = epgSourceKindLabel
const sourceState = epgSourceStatus
const displayUrl = (source) => safeDisplayUrl(source?.display_url)
const compactCount = formatCompactCount
const formatTime = formatEpgTime
const coverage = formatCoverage

function setBusy(collection, sourceId, busy) {
  const next = new Set(collection.value)
  if (busy) next.add(sourceId)
  else next.delete(sourceId)
  collection.value = next
}

function isRefreshing(sourceId) {
  return refreshingIds.value.has(sourceId)
}

function isUpdating(sourceId) {
  return updatingIds.value.has(sourceId)
}

async function loadSources(options = {}) {
  if (componentDisposed) return
  loadController?.abort()
  const controller = new AbortController()
  loadController = controller
  const background = options?.background === true
  if (!background) {
    loading.value = true
    loadError.value = ''
  }
  try {
    const result = await fetchEpgSources({ signal: controller.signal })
    if (controller.signal.aborted || componentDisposed) return
    sources.value = Array.isArray(result) ? result : []
  } catch (error) {
    if (controller.signal.aborted || componentDisposed) return
    const message = epgSourceErrorMessage(error, '无法加载节目单来源，请稍后重试')
    if (background) toastStore.warning('操作已完成，但来源状态暂时无法重新加载')
    else loadError.value = message
  } finally {
    if (loadController === controller && !componentDisposed && !background) loading.value = false
  }
}

async function toggleSource(source, enabled) {
  if (!source?.capabilities?.can_enable_disable || isUpdating(source.id)) return
  setBusy(updatingIds, source.id, true)
  try {
    await updateEpgSource(source.id, { enabled })
    if (componentDisposed) return
    await loadSources({ background: true })
    if (componentDisposed) return
    toastStore.success(enabled ? '节目单来源已启用' : '节目单来源已停用')
  } catch (error) {
    if (componentDisposed) return
    toastStore.error(epgSourceErrorMessage(error, '启用状态更新失败'))
    await loadSources({ background: true })
  } finally {
    setBusy(updatingIds, source.id, false)
  }
}

async function runRefresh(source) {
  if (!source?.enabled || isRefreshing(source.id) || source.automation?.running) return
  setBusy(refreshingIds, source.id, true)
  try {
    const result = await refreshEpgSource(source.id)
    if (componentDisposed) return
    if (result?.status === 'success') toastStore.success('节目单已更新')
    else if (result?.status === 'partial') toastStore.warning(result?.message || '节目单已更新，但部分维护任务未完成')
    else toastStore.error(result?.message || '节目单更新失败，当前继续使用已有数据')
    await loadSources({ background: true })
  } catch (error) {
    if (componentDisposed) return
    toastStore.error(epgSourceErrorMessage(error, '节目单刷新失败，请稍后重试'))
  } finally {
    setBusy(refreshingIds, source.id, false)
  }
}

function resetEditorDraft() {
  editorDraft.name = ''
  editorDraft.url = ''
  editorDraft.enabled = true
  editorDraft.replaceUrl = false
  editorError.value = ''
}

function openCreateDialog() {
  resetEditorDraft()
  editorMode.value = 'create'
  editingSource.value = null
  editorOpen.value = true
  nextTick(() => editorNameInput.value?.focus())
}

function openEditDialog(source) {
  resetEditorDraft()
  editorMode.value = 'edit'
  editingSource.value = source
  editorDraft.name = sourceName(source)
  editorDraft.enabled = source?.enabled !== false
  editorOpen.value = true
  nextTick(() => editorNameInput.value?.focus())
}

function closeEditor() {
  if (editorSaving.value) return
  editorOpen.value = false
  editingSource.value = null
  resetEditorDraft()
}

async function saveEditor() {
  if (!editorCanSave.value || editorSaving.value) return
  editorSaving.value = true
  editorError.value = ''
  try {
    if (editorMode.value === 'create') {
      await createEpgSource(buildEpgSourceCreatePayload(editorDraft))
      if (componentDisposed) return
      toastStore.success('节目单来源已添加')
    } else {
      await updateEpgSource(editingSource.value.id, buildEpgSourceUpdatePayload(editorDraft))
      if (componentDisposed) return
      toastStore.success('节目单来源已保存')
    }
    editorOpen.value = false
    editingSource.value = null
    resetEditorDraft()
    await loadSources({ background: true })
  } catch (error) {
    if (componentDisposed) return
    editorError.value = epgSourceErrorMessage(error, editorMode.value === 'create' ? '节目单来源添加失败' : '节目单来源保存失败')
  } finally {
    editorSaving.value = false
  }
}

async function openDeleteDialog(source) {
  if (!source?.capabilities?.can_delete) return
  deleteImpactController?.abort()
  const controller = new AbortController()
  deleteImpactController = controller
  deleteSource.value = source
  deleteImpact.value = null
  deleteAcknowledged.value = false
  deleteDialogOpen.value = true
  deleteImpactLoading.value = true
  try {
    const impact = await fetchEpgSourceDeleteImpact(source.id, { signal: controller.signal })
    if (controller.signal.aborted || componentDisposed || deleteSource.value?.id !== source.id) return
    deleteImpact.value = impact
  } catch (error) {
    if (controller.signal.aborted || componentDisposed) return
    closeDeleteDialog()
    toastStore.error(epgSourceErrorMessage(error, '无法检查删除影响，请稍后重试'))
  } finally {
    if (deleteImpactController === controller && !componentDisposed) deleteImpactLoading.value = false
  }
}

function closeDeleteDialog() {
  if (deleteSaving.value) return
  deleteImpactController?.abort()
  deleteDialogOpen.value = false
  deleteSource.value = null
  deleteImpact.value = null
  deleteAcknowledged.value = false
}

async function confirmDelete() {
  if (!deleteSource.value || !deleteImpact.value || deleteSaving.value) return
  if (deleteRequiresAck.value && !deleteAcknowledged.value) return
  deleteSaving.value = true
  try {
    await deleteEpgSource(deleteSource.value.id, { confirm: deleteRequiresAck.value })
    if (componentDisposed) return
    const deletedName = sourceName(deleteSource.value)
    deleteDialogOpen.value = false
    deleteSource.value = null
    deleteImpact.value = null
    deleteAcknowledged.value = false
    await loadSources({ background: true })
    toastStore.success(`已删除“${deletedName}”`)
  } catch (error) {
    if (componentDisposed) return
    toastStore.error(epgSourceErrorMessage(error, '节目单来源删除失败'))
  } finally {
    deleteSaving.value = false
  }
}

onMounted(loadSources)
onBeforeUnmount(() => {
  componentDisposed = true
  loadController?.abort()
  deleteImpactController?.abort()
})
</script>

<style scoped>
.epg-primary-button,
.epg-secondary-button,
.epg-danger-button {
  min-height: var(--control-height);
  border-radius: var(--control-radius);
  padding: 0.625rem 1rem;
  font-size: 0.875rem;
  font-weight: 600;
  transition: transform 150ms ease, opacity 150ms ease, background-color 150ms ease, border-color 150ms ease;
}

.epg-primary-button {
  background: var(--text-primary);
  color: var(--bg);
}

.epg-secondary-button {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-primary);
}

.epg-danger-button {
  background: rgb(239 68 68);
  color: white;
}

.epg-primary-button:hover:not(:disabled),
.epg-danger-button:hover:not(:disabled) {
  transform: translateY(-1px);
}

.epg-secondary-button:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}

.epg-primary-button:focus-visible,
.epg-secondary-button:focus-visible,
.epg-danger-button:focus-visible,
.epg-action-button:focus-visible,
.epg-icon-button:focus-visible {
  outline: 2px solid var(--border-strong);
  outline-offset: 2px;
}

.epg-primary-button:disabled,
.epg-secondary-button:disabled,
.epg-danger-button:disabled,
.epg-action-button:disabled,
.epg-icon-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.epg-identity-pill,
.epg-status-pill {
  display: inline-flex;
  align-items: center;
  min-height: 1.375rem;
  border: 1px solid transparent;
  border-radius: 9999px;
  padding: 0.125rem 0.5rem;
  font-size: 0.625rem;
  font-weight: 600;
  white-space: nowrap;
}

.epg-identity-pill {
  border-color: var(--border);
  background: var(--surface);
  color: var(--text-tertiary);
}

.epg-status--healthy { border-color: rgb(16 185 129 / 0.22); background: rgb(16 185 129 / 0.08); color: rgb(5 150 105); }
.epg-status--stale { border-color: rgb(245 158 11 / 0.25); background: rgb(245 158 11 / 0.08); color: rgb(217 119 6); }
.epg-status--failed { border-color: rgb(239 68 68 / 0.22); background: rgb(239 68 68 / 0.08); color: rgb(220 38 38); }
.epg-status--disabled { border-color: var(--border); background: var(--surface); color: var(--text-tertiary); }

.epg-action-button {
  min-height: var(--control-height);
  flex-shrink: 0;
  border-radius: var(--control-radius);
  padding: 0.5rem 0.75rem;
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--text-secondary);
  transition: background-color 150ms ease, color 150ms ease;
}

.epg-action-button:hover:not(:disabled) { background: var(--surface-hover); color: var(--text-primary); }
.epg-action-button--danger { color: rgb(239 68 68); }
.epg-action-button--danger:hover:not(:disabled) { background: rgb(239 68 68 / 0.08); color: rgb(220 38 38); }

.epg-dialog-layer {
  position: fixed;
  inset: 0;
  z-index: 95;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  overflow-y: auto;
  background: rgb(10 10 10 / 0.35);
  padding: 1rem 0 0;
  backdrop-filter: blur(8px);
}

.epg-dialog-panel {
  width: 100%;
  max-height: calc(100dvh - 1rem);
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--panel-radius) var(--panel-radius) 0 0;
  background: var(--bg-soft);
  padding: 1.25rem;
  box-shadow: 0 24px 64px rgb(0 0 0 / 0.22);
}

.epg-icon-button {
  display: inline-flex;
  width: var(--icon-button-size);
  height: var(--icon-button-size);
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  border-radius: var(--control-radius);
  color: var(--text-secondary);
}

.epg-icon-button:hover:not(:disabled) { background: var(--surface-hover); color: var(--text-primary); }

.epg-field-label {
  display: block;
  margin-bottom: 0.5rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-primary);
}

.epg-input {
  width: 100%;
  min-height: var(--chip-hit-height);
  border: 1px solid var(--border);
  border-radius: var(--control-radius);
  background: var(--bg-soft);
  padding: 0.625rem 0.875rem;
  font-size: 0.875rem;
  color: var(--text-primary);
  outline: none;
}

.epg-input::placeholder { color: var(--text-tertiary); }
.epg-input:focus { border-color: var(--border-strong); box-shadow: 0 0 0 2px var(--surface-strong); }

@media (min-width: 640px) {
  .epg-dialog-layer { align-items: center; padding: 2rem 1rem; }
  .epg-dialog-panel { max-width: 36rem; max-height: calc(100dvh - 4rem); border-radius: var(--panel-radius); padding: 1.5rem; }
}
</style>
