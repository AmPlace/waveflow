import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { afterEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'

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
} from '../../src/api/epgMatchingManagement.js'
import {
  MATCHING_SCOPE_OPTIONS,
  epgBindingOriginLabel,
  epgCatalogTarget,
  epgLogicalState,
  epgMatchingState,
  epgMatchingTarget,
  epgPreferenceSummary,
  epgScopeCount,
} from '../../src/views/settings/epgMatchingUi.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const originalFetch = globalThis.fetch

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

function installFetch(payload = {}) {
  const calls = []
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options })
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return calls
}

afterEach(() => {
  globalThis.fetch = originalFetch
})

test('matching read client 使用 5A overview、分页列表、lazy detail 和 Catalog contract', async () => {
  const calls = installFetch({ items: [], page: 1, page_size: 30, total: 0 })
  await fetchEpgMatchingOverview()
  await fetchEpgMatchingChannels({ page: 2, pageSize: 30, scope: 'needs_attention', query: 'CCTV 1' })
  await fetchEpgMatchingDetail('logical/a', { candidateLimit: 8 })
  await searchEpgChannelCatalog({ page: 3, pageSize: 20, query: 'NHK 総合', sourceId: 7, availability: 'all' })

  assert.equal(calls[0].url, '/api/admin/epg/overview')
  assert.match(calls[1].url, /^\/api\/admin\/epg\/matching\?/)
  assert.match(calls[1].url, /page=2/)
  assert.match(calls[1].url, /page_size=30/)
  assert.match(calls[1].url, /logical_scope=active/)
  assert.match(calls[1].url, /scope=needs_attention/)
  assert.match(calls[1].url, /q=CCTV\+1/)
  assert.equal(calls[2].url, '/api/admin/epg/matching/logical%2Fa?candidate_limit=8')
  assert.match(calls[3].url, /^\/api\/admin\/epg\/catalog\?/)
  assert.match(calls[3].url, /source_id=7/)
  assert.match(calls[3].url, /q=NHK\+%E7%B7%8F%E5%90%88/)
})

test('manual bind 原样提交 source-aware composite identity', async () => {
  const calls = installFetch({ operation: 'manual_bind' })
  const identity = { epg_source_id: 12, epg_channel_id: 'shared/id' }
  await setManualEpgBinding('logical one', identity)
  assert.equal(calls[0].url, '/api/admin/epg/logical-channels/logical%20one/binding')
  assert.equal(calls[0].options.method, 'PUT')
  assert.deepEqual(JSON.parse(calls[0].options.body), identity)
})

test('lock、unlock、restore automatic 和 no-EPG 使用独立 5B-b endpoint', async () => {
  const calls = installFetch({})
  await lockEpgBinding('lc')
  await unlockEpgBinding('lc')
  await restoreAutomaticEpgBinding('lc')
  await disableLogicalChannelEpg('lc')
  assert.deepEqual(calls.map(({ url, options }) => [url, options.method]), [
    ['/api/admin/epg/logical-channels/lc/binding/lock', 'PUT'],
    ['/api/admin/epg/logical-channels/lc/binding/lock', 'DELETE'],
    ['/api/admin/epg/logical-channels/lc/restore-automatic', 'POST'],
    ['/api/admin/epg/logical-channels/lc/no-epg', 'PUT'],
  ])
})

test('状态文案将 unmatched 保持中性，只把真实冲突标记为需处理', () => {
  assert.deepEqual(MATCHING_SCOPE_OPTIONS.map((item) => item.label), ['全部', '已匹配', '未匹配', '需处理'])
  assert.deepEqual(epgMatchingState({ diagnostic: { status: 'healthy_bound' } }), {
    key: 'matched', label: '已匹配', tone: 'success', detail: '频道已有可用节目单',
  })
  assert.equal(epgMatchingState({ diagnostic: { status: 'unmatched' } }).tone, 'neutral')
  assert.equal(epgMatchingState({ diagnostic: { status: 'ambiguous' } }).label, '多个候选')
  assert.equal(epgMatchingState({ diagnostic: { status: 'split_conflict' } }).label, '需要处理')
  assert.equal(epgMatchingState({ diagnostic: { status: 'missing_target' } }).label, '原节目单不可用')
  assert.equal(epgMatchingState({ diagnostic: { status: 'not_applicable' } }).label, '不使用节目单')
})

test('绑定目标展示 source 和 channel，跨来源同 channel id 不会丢失 source identity', () => {
  const first = {
    diagnostic: { status: 'healthy_bound' },
    binding: { status: 'bound', epg_source_id: 1, epg_source_name: 'First', epg_channel_id: 'shared', epg_channel_display_name: 'First Shared' },
  }
  const second = {
    diagnostic: { status: 'healthy_bound' },
    binding: { status: 'bound', epg_source_id: 2, epg_source_name: 'Second', epg_channel_id: 'shared', epg_channel_display_name: 'Second Shared' },
  }
  assert.equal(epgMatchingTarget(first), 'First · First Shared')
  assert.equal(epgMatchingTarget(second), 'Second · Second Shared')
  assert.notEqual(epgMatchingTarget(first), epgMatchingTarget(second))
  assert.deepEqual(epgCatalogTarget({ identity: { epg_source_id: 2, epg_channel_id: 'shared' } }), {
    epg_source_id: 2, epg_channel_id: 'shared',
  })
})

test('no_epg、orphan target、origin、logical state 和 preference 使用产品化语义', () => {
  assert.equal(epgMatchingTarget({ diagnostic: { status: 'not_applicable' }, binding: {} }), '不使用节目单')
  assert.equal(epgMatchingTarget({ diagnostic: { status: 'missing_target' }, binding: {} }), '原节目单来源已不可用')
  assert.equal(epgBindingOriginLabel('manual'), '手动绑定')
  assert.equal(epgBindingOriginLabel('automatic'), '自动匹配')
  assert.equal(epgLogicalState({ channel: { state: 'active' } }).manageable, true)
  assert.equal(epgLogicalState({ channel: { state: 'merge_conflict' } }).manageable, false)
  assert.equal(epgPreferenceSummary({ status: 'missing_source' }).label, '偏好来源已不存在')
})

test('overview scope count 不把 no_epg 当普通 unmatched 或需处理', () => {
  const overview = { logical_channels: { total: 356, bound: 61, unbound: 294, not_applicable: 1, needs_attention: 0 } }
  assert.equal(epgScopeCount(overview, 'all'), 356)
  assert.equal(epgScopeCount(overview, 'bound'), 61)
  assert.equal(epgScopeCount(overview, 'unbound'), 294)
  assert.equal(epgScopeCount(overview, 'needs_attention'), 0)
})

test('页面使用分页轻列表，详情和候选仅在打开 drawer 后读取', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  const listTemplate = view.split('<Teleport to="body">')[0]
  assert.match(view, /fetchEpgMatchingChannels/)
  assert.match(view, /async function openDetail\(item\)[\s\S]*loadDetail\(item\.channel\.logical_channel_id\)/)
  assert.match(view, /fetchEpgMatchingDetail\(logicalChannelId/)
  assert.match(view, /candidateLimit: 10/)
  assert.match(view, /PAGE_SIZE = 30/)
  assert.match(view, /logicalScope: 'active'/)
  assert.doesNotMatch(view, /leftRank|rightRank/)
  assert.doesNotMatch(listTemplate, /canonical_key|shadow_run_id|confidence|evidence_json/)
  assert.doesNotMatch(listTemplate, /<table|overflow-x-scroll/)
})

test('legacy fallback 只作为 production read 解释，不作为正式绑定展示', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  assert.match(view, /detail\.production_read\?\.status === 'legacy_fallback'/)
  assert.match(view, /当前节目仍通过旧版兼容映射读取/)
  assert.doesNotMatch(view, /legacy_mapping|channel_epg_map/)
})

test('页面 summary、筛选、loading、错误、空状态和分页均使用产品化结构', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  const listTemplate = view.split('<Teleport to="body">')[0]
  assert.match(listTemplate, /已匹配[\s\S]*未匹配[\s\S]*需处理/)
  assert.match(listTemplate, /个当前频道/)
  assert.match(listTemplate, /v-for="item in items"/)
  assert.doesNotMatch(listTemplate, /scopeCount\(overview, option\.key\)/)
  assert.match(listTemplate, /搜索频道或节目单/)
  assert.match(listTemplate, /type="submit" class="epg-match-search-submit" aria-label="搜索频道"/)
  assert.match(listTemplate, /aria-busy="true"/)
  assert.match(listTemplate, /频道匹配加载失败/)
  assert.match(listTemplate, /没有找到相关频道/)
  assert.match(listTemplate, /上一页[\s\S]*下一页/)
  assert.doesNotMatch(listTemplate, /ambiguous[\s\S]*split_conflict[\s\S]*merge_conflict/)
})

test('drawer 产品化展示详情、候选、来源偏好和受限高级信息', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  assert.match(view, /epg-match-drawer-pane/)
  assert.match(view, /role="dialog"/)
  assert.match(view, /当前状态/)
  assert.match(view, /当前节目单/)
  assert.match(view, /来源偏好/)
  assert.match(view, /可能的节目单/)
  assert.match(view, /<details class="epg-match-advanced">/)
  assert.match(view, /原来源已不存在/)
  assert.doesNotMatch(view, /hint_conflicts|reasons_json|evidence_json|shadow_run_id/)
})

test('manual bind、replace、lock、unlock、restore、degraded 和 no_epg 都会刷新 detail/list', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  assert.match(view, /const actionPending = computed\(\(\) => Boolean\(actionBusy\.value\)\)/)
  assert.doesNotMatch(view, /:disabled="[^"]*actionBusy/)
  assert.match(view, /setManualEpgBinding\(selectedLogicalId\.value, identity\)/)
  assert.match(view, /更换节目单/)
  assert.match(view, /lockEpgBinding\(selectedLogicalId\.value\)/)
  assert.match(view, /unlockEpgBinding\(selectedLogicalId\.value\)/)
  assert.match(view, /已解锁，当前节目单仍保留/)
  assert.match(view, /restoreAutomaticEpgBinding\(selectedLogicalId\.value\)/)
  assert.match(view, /maintenance_degraded/)
  assert.match(view, /disableLogicalChannelEpg\(selectedLogicalId\.value\)/)
  assert.match(view, /不使用节目单/)
  assert.match(view, /await refreshCurrentState\(\)/)
})

test('catalog search 显示 source 和 channel，并将用户所选 identity 作为唯一写入目标', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  assert.match(view, /searchEpgChannelCatalog/)
  assert.match(view, /筛选节目单来源/)
  assert.match(view, /epg_channel_display_name/)
  assert.match(view, /频道标识/)
  assert.match(view, /selectedCatalogTarget = item/)
  assert.match(view, /const identity = epgCatalogTarget\(selectedCatalogTarget\.value\)/)
  assert.doesNotMatch(view, /infer.*target|find\(.*epg_channel_id/i)
})

test('真正 conflict/orphan logical 在 UI 中不可写，避免点击后才返回领域错误', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  assert.match(view, /selectedManageable/)
  assert.match(view, /频道当前存在直播源冲突或已经失效/)
  assert.match(view, /if \(!selectedManageable\.value\) return/)
})

test('移动端采用堆叠 rows 和全宽 drawer，不产生 debug 宽表', () => {
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  assert.match(view, /@media \(max-width: 767px\)/)
  assert.match(view, /grid-template-columns: minmax\(0, 1fr\) auto/)
  assert.match(view, /width: 100vw/)
  assert.match(view, /overflow-x: auto/)
  assert.doesNotMatch(view, /min-width:\s*(?:8|9)\d\dpx|<table/)
})

test('EPG-6B 不调用 legacy matcher、mapping bind/unbind、programme 或 backend enum 写接口', () => {
  const api = source('src/api/epgMatchingManagement.js')
  const view = source('src/views/settings/EpgMatchingSettings.vue')
  const combined = `${api}\n${view}`
  assert.doesNotMatch(combined, /\/api\/[^'"`]*match-status|\/api\/admin\/epg\/bind\/|channel_epg_map|run_epg_matching|\/api\/iptv\/epg\/programs|batch-current/)
})

test('write errors 使用稳定错误码映射，不回显服务端任意 message', () => {
  assert.equal(epgMatchingErrorMessage({ detail: { detail: { code: 'orphan_target', message: 'secret detail' } } }), '原节目单已不可用，请更换节目单或恢复自动匹配')
  assert.equal(epgMatchingErrorMessage({ detail: { detail: { code: 'logical_channel_conflict' } } }), '频道来源仍有冲突，请先处理直播源')
  const api = source('src/api/epgMatchingManagement.js')
  assert.doesNotMatch(api, /payload\.message|error\?\.detail\?\.message/)
})
