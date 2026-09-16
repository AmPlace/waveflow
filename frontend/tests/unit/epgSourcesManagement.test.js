import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  buildEpgSourceCreatePayload,
  buildEpgSourceUpdatePayload,
  deleteNeedsAcknowledgement,
  epgSourceDisplayName,
  epgSourceKindLabel,
  epgSourceStatus,
  safeDisplayUrl,
} from '../../src/views/settings/epgSourceUi.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

test('来源读取和写入使用 EPG-5B-a 稳定 endpoint', () => {
  const api = source('src/api/epgManagement.js')
  assert.match(api, /apiRequest\('\/api\/admin\/epg\/sources', \{ signal \}\)/)
  assert.match(api, /apiRequest\('\/api\/admin\/epg\/sources', \{[\s\S]*method: 'POST'/)
  assert.match(api, /sources\/\$\{encodeURIComponent\(sourceId\)\}`,[\s\S]*method: 'PATCH'/)
  assert.match(api, /sources\/\$\{encodeURIComponent\(sourceId\)\}\/refresh`/)
  assert.match(api, /sources\/\$\{encodeURIComponent\(sourceId\)\}\/delete-impact`/)
  assert.match(api, /confirm \? '\?confirm=true' : ''/)
})

test('builtin 使用产品名称并由 capability 限制 URL 与删除操作', () => {
  const builtin = {
    name: '51zmt',
    source_origin: 'builtin',
    builtin_key: 'china_51zmt',
    capabilities: { can_edit_name: true, can_edit_url: false, can_delete: false },
  }
  assert.equal(epgSourceDisplayName(builtin), '中国节目单')
  assert.equal(epgSourceKindLabel(builtin), 'WaveFlow 内置')
  assert.equal(epgSourceKindLabel({ source_origin: 'custom' }), '自定义')
  assert.equal(epgSourceDisplayName({ ...builtin, name: '家庭节目单' }), '家庭节目单')

  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /source\.capabilities\?\.can_delete/)
  assert.match(view, /editingSource\?\.capabilities\?\.can_edit_url/)
  assert.match(view, /内置来源地址由 WaveFlow 维护/)
  assert.doesNotMatch(view, />51zmt<|>china_51zmt<|epg\.51zmt\.top/)
})

test('custom 创建 payload 支持任意 XMLTV 且不包含国家模式', () => {
  assert.deepEqual(buildEpgSourceCreatePayload({
    name: '  Korea Guide  ',
    url: '  https://guide.example.kr/xmltv?region=kr  ',
    enabled: false,
  }), {
    name: 'Korea Guide',
    url: 'https://guide.example.kr/xmltv?region=kr',
    enabled: false,
  })
  assert.doesNotMatch(source('src/views/settings/EpgSourcesSettings.vue'), /country|国家选择|海外 EPG/i)
})

test('custom 编辑默认不回传 URL，只有明确更换才提交 replacement', () => {
  assert.deepEqual(buildEpgSourceUpdatePayload({
    name: 'Renamed',
    enabled: true,
    replaceUrl: false,
    url: 'https://display.example/should-not-save',
  }), { name: 'Renamed', enabled: true })

  assert.deepEqual(buildEpgSourceUpdatePayload({
    name: 'Renamed',
    enabled: true,
    replaceUrl: true,
    url: '  https://secret.example/xmltv?token=new-secret  ',
  }), {
    name: 'Renamed',
    enabled: true,
    url: 'https://secret.example/xmltv?token=new-secret',
  })

  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /resetEditorDraft\(\)[\s\S]*editorDraft\.url = ''/)
  assert.doesNotMatch(view, /editorDraft\.url\s*=\s*(?:source|editingSource).*display_url/)
  assert.match(view, /不会使用上方净化后的展示地址覆盖原始 URL/)
})

test('净化展示地址不会显示 token、key 或 signature query', () => {
  const secret = 'visible-secret-value'
  const displayed = safeDisplayUrl(`https://user:pass@guide.example/xmltv/path?token=${secret}&signature=sig#part`)
  assert.equal(displayed, 'https://guide.example/xmltv/path')
  assert.doesNotMatch(displayed, /visible-secret-value|signature|user:pass/)
})

test('healthy、stale、failed、disabled 使用独立产品状态', () => {
  assert.deepEqual(epgSourceStatus({ enabled: true, refresh: { status: 'healthy' } }), {
    key: 'healthy', label: '正常', detail: '节目单数据可正常使用',
  })
  assert.equal(epgSourceStatus({ enabled: true, refresh: { status: 'stale' } }).label, '使用旧数据')
  assert.equal(epgSourceStatus({ enabled: true, refresh: { status: 'failed' } }).label, '更新失败')
  assert.equal(epgSourceStatus({ enabled: false, refresh: { status: 'healthy' } }).label, '已停用')
  assert.match(epgSourceStatus({ enabled: true, refresh: { status: 'stale' } }).detail, /继续使用已有节目单/)
})

test('刷新按 source 隔离 busy 状态并使用产品化结果', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /refreshingIds = ref\(new Set\(\)\)/)
  assert.match(view, /isRefreshing\(source\.id\)/)
  assert.match(view, /source\.automation\?\.running/)
  assert.match(view, /result\?\.status === 'success'/)
  assert.match(view, /result\?\.status === 'partial'/)
  assert.doesNotMatch(view, /traceback|automation[_ ]payload|last_run_status\s*\}\}/i)
})

test('初次加载有 skeleton，启停与刷新只做后台状态重载', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /v-if="loading"[\s\S]*aria-busy="true"/)
  assert.match(view, /updateEpgSource\(source\.id, \{ enabled \}\)/)
  assert.match(view, /loadSources\(\{ background: true \}\)/)
  assert.match(view, /loadController === controller[\s\S]*!background[\s\S]*loading\.value = false/)
})

test('来源状态保留 last-known-good，并单独展示失败诊断和部分维护状态', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /source\.refresh\?\.failure[\s\S]*safeAdminDiagnostic\(source\.refresh\.failure\)/)
  assert.match(view, /source\.automation\?\.last_run_status === 'partial'/)
  assert.match(view, /节目单数据可用，后续绑定维护未完全完成/)
})

test('删除先读取影响，manual/locked binding 需要明确确认', () => {
  assert.equal(deleteNeedsAcknowledgement({ requires_confirmation: false }), false)
  assert.equal(deleteNeedsAcknowledgement({ manual_bindings_count: 1 }), true)
  assert.equal(deleteNeedsAcknowledgement({ locked_bindings_count: 1 }), true)

  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /fetchEpgSourceDeleteImpact\(source\.id, \{ signal: controller\.signal \}\)/)
  assert.match(view, /deleteAcknowledged/)
  assert.match(view, /手动或锁定绑定会保留为待修复状态/)
  assert.match(view, /deleteEpgSource\(deleteSource\.value\.id, \{ confirm: deleteRequiresAck\.value \}\)/)
  assert.doesNotMatch(view, /automation_task|task_id|logical_channel_id/)
})

test('错误码映射读取 FastAPI detail 且不回显服务端任意内容', () => {
  const api = source('src/api/epgManagement.js')
  assert.match(api, /error\?\.detail\?\.detail \|\| error\?\.detail/)
  assert.match(api, /invalid_url: '请输入有效的 HTTP 或 HTTPS XMLTV 地址'/)
  assert.doesNotMatch(api, /payload\.message|error\?\.detail\?\.message/)
})

test('空状态区分 builtin-only 与整个列表为空', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /!customSources\.length/)
  assert.match(view, /添加自己的 XMLTV 节目单/)
  assert.match(view, /还没有节目单来源/)
})

test('来源页面不重复标题，统计和时间使用轻量 typography 而非 dashboard 或 pill', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  const sourceList = view.split('<Teleport to="body">')[0]
  assert.match(sourceList, /<Teleport defer to="#epg-settings-actions">/)
  assert.doesNotMatch(sourceList, /<h2[^>]*>节目单来源<\/h2>/)
  assert.doesNotMatch(sourceList, /管理 WaveFlow 使用的 XMLTV 节目单/)
  assert.doesNotMatch(sourceList, /<dl|<dt|<dd|grid-cols-4/)
  assert.match(sourceList, /epg-identity-pill/)
  assert.match(sourceList, /epg-status-pill/)
  assert.doesNotMatch(sourceList, /epg-stat-chip/)
  assert.match(sourceList, /<span>\{\{ compactCount\(source\.data\?\.channel_count\) \}\} 频道<\/span>/)
  assert.match(sourceList, /<span>\{\{ compactCount\(source\.data\?\.programme_count\) \}\} 节目<\/span>/)
  assert.match(sourceList, /数据 \{\{ coverage\(source\) \}\}/)
  assert.match(sourceList, /最近成功 \{\{ formatTime/)
  assert.match(sourceList, /下次更新/)
  assert.match(sourceList, /sourceState\(source\)\.key !== 'healthy'/)
  assert.match(sourceList, /添加自己的 XMLTV 节目单/)
})

test('来源卡片明确分为 content 和 action cluster，mobile 可换行且不引入横向表格', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  assert.match(view, /flex min-w-0 flex-col gap-4 lg:flex-row/)
  assert.match(view, /flex min-w-0 flex-1 items-start gap-3/)
  assert.match(view, /shrink-0 flex-wrap items-center gap-1 border-t/)
  assert.match(view, /\{\{ source\.enabled \? '已启用' : '已停用' \}\}/)
  assert.match(view, /flex min-w-0 flex-wrap items-center gap-x-2/)
  assert.match(view, /max-w-full truncate/)
  assert.match(view, /align-items: flex-end/)
  assert.match(view, /@media \(min-width: 640px\)/)
  assert.doesNotMatch(view, /<table|overflow-x-scroll/)
})

test('来源页面本身不接入 matching、Matcher 或 binding，旧 EPG 路由跳转到新管理页', () => {
  const view = source('src/views/settings/EpgSourcesSettings.vue')
  const routes = source('src/router/routes.js')
  assert.doesNotMatch(view, /manual bind|lock\/unlock|no_epg|match_logical_channel|channel_epg_map/i)
  assert.match(routes, /settings-epg-matching[\s\S]*EpgMatchingSettings\.vue/)
  assert.match(routes, /path: '\/admin\/epg'[\s\S]*redirect: '\/settings\/epg\/matching'/)
  assert.doesNotMatch(routes, /EpgDebug|epg-debug/)
})
