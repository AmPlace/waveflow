import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  EPG_BATCH_REFRESH_FALLBACK_MS,
  EPG_BATCH_REFRESH_MAX_MS,
  channelProgrammeSubtitle,
  epgBatchRefreshDelay,
  formatEpgClock,
} from '../../src/utils/epgViewing.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

test('频道节目文案只在 stop 可靠时显示 HH:mm 结束', () => {
  const stop = new Date(2026, 7, 9, 12, 30).toISOString()
  assert.equal(formatEpgClock(stop), '12:30')
  assert.equal(
    channelProgrammeSubtitle({ title: '午间新闻', stop }, '新闻'),
    '午间新闻 · 12:30 结束',
  )
  assert.equal(channelProgrammeSubtitle({ title: '未知结束时间' }, '新闻'), '未知结束时间')
  assert.equal(channelProgrammeSubtitle({ title: '空结束时间', stop: null }, '新闻'), '空结束时间')
  assert.equal(formatEpgClock(null), '')
  assert.equal(channelProgrammeSubtitle(null, '新闻'), '新闻')
})

test('batch refresh 选择最近节目结束边界，无边界时使用低频 fallback', () => {
  const now = Date.parse('2026-08-09T10:00:00+08:00')
  const delay = epgBatchRefreshDelay({
    later: { current: { stop: '2026-08-09T11:00:00+08:00' } },
    nearest: { current: { stop: '2026-08-09T10:05:00+08:00' } },
  }, now)
  assert.equal(delay, 5 * 60_000 + 1_200)
  assert.equal(epgBatchRefreshDelay({}, now), EPG_BATCH_REFRESH_FALLBACK_MS)
  assert.equal(
    epgBatchRefreshDelay({ long: { current: { stop: '2026-08-10T10:00:00+08:00' } } }, now),
    EPG_BATCH_REFRESH_MAX_MS,
  )
})

test('观看侧继续复用 programme 与 batch-current，未引入 N+1 或管理 UI', () => {
  const fullPlayer = source('src/components/FullPlayer.vue')
  const home = source('src/views/IptvHome.vue')
  const useEpg = source('src/composables/useEpg.js')
  const bottomPlayer = source('src/components/BottomPlayer.vue')

  assert.match(fullPlayer, /current: _epgCurrent[\s\S]*next: _epgNext[\s\S]*loading: _epgLoading[\s\S]*error: _epgError/)
  assert.match(fullPlayer, /scheduleEpgRefreshAfterProgramEnd/)
  assert.match(fullPlayer, /selectEpgDate/)
  assert.match(home, /batchCurrent\(keys, \{ signal: ctrl\.signal \}\)/)
  assert.match(home, /_invalidateListRequest\(\)[\s\S]*_invalidateEpgBatchRefresh\(\)/)
  assert.match(home, /onUnmounted\(\(\) => \{[\s\S]*_invalidateListRequest\(\)/)
  assert.doesNotMatch(home, /\/api\/iptv\/epg\/programs/)
  assert.match(useEpg, /\/api\/iptv\/epg\/batch-current/)
  assert.match(bottomPlayer, /currentEpgProgram[\s\S]*prog\?\.title/)
  assert.doesNotMatch(`${fullPlayer}\n${home}`, /settings\/epg|channel_epg_map|match-status/)
})
