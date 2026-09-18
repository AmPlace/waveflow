// The source lamp answers exactly one question -- "is this source known to be
// playable?" -- for both IPTV and Radio.  "Which source is selected / playing"
// is the active row's job and must never be folded into the lamp.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  SOURCE_LAMP, iptvSourceStatus, lampClass, radioSourceLamp, radioSourceText,
} from '../../src/utils/sourceStatus.js'

test('IPTV：本次会话的状态优先于探测结论', () => {
  assert.equal(iptvSourceStatus('trying', '', undefined).lamp, SOURCE_LAMP.TRYING)
  assert.equal(iptvSourceStatus('playing', '', undefined).lamp, SOURCE_LAMP.GOOD)
  assert.equal(iptvSourceStatus('failed', '', undefined).lamp, SOURCE_LAMP.BAD)
  // 探测说可用，但本次会话刚失败 -> 仍然报失败
  assert.equal(iptvSourceStatus('failed', '', 1).lamp, SOURCE_LAMP.BAD)
  // 探测说不可用，但本次会话正在播 -> 以正在播为准
  assert.equal(iptvSourceStatus('playing', '', 0).lamp, SOURCE_LAMP.GOOD)
})

test('IPTV：没有会话状态时回落到探测结论', () => {
  assert.equal(iptvSourceStatus('idle', '', 1).lamp, SOURCE_LAMP.GOOD)
  assert.equal(iptvSourceStatus('idle', '', 0).lamp, SOURCE_LAMP.BAD)
  assert.equal(iptvSourceStatus('idle', 'not_live', undefined).lamp, SOURCE_LAMP.BAD)
  assert.equal(iptvSourceStatus('idle', 'untested', undefined).lamp, SOURCE_LAMP.UNKNOWN)
  assert.equal(iptvSourceStatus('idle', '', undefined).lamp, SOURCE_LAMP.UNKNOWN)
})

test('IPTV：切走的源("stopped")不再被当成未知', () => {
  // 旧实现里 stopped 和 idle 都是灰灯，于是"刚播过的源"看起来和"从没测过"一样。
  assert.equal(iptvSourceStatus('stopped', '', 1).lamp, SOURCE_LAMP.GOOD)
  assert.equal(iptvSourceStatus('stopped', '', 0).lamp, SOURCE_LAMP.BAD)
  assert.equal(iptvSourceStatus('stopped', '', undefined).lamp, SOURCE_LAMP.UNKNOWN)
})

test('Radio：使用与 IPTV 相同的语义，不再默认绿灯', () => {
  assert.equal(radioSourceLamp('healthy'), SOURCE_LAMP.GOOD)
  assert.equal(radioSourceLamp('failed'), SOURCE_LAMP.BAD)
  assert.equal(radioSourceLamp('unhealthy'), SOURCE_LAMP.BAD)
  assert.equal(radioSourceLamp('unknown'), SOURCE_LAMP.UNKNOWN)
  assert.equal(radioSourceLamp(''), SOURCE_LAMP.UNKNOWN)
  assert.equal(radioSourceLamp(undefined), SOURCE_LAMP.UNKNOWN)
})

test('文案区分两种测速：本次播放尝试与原测速（频道存储）', () => {
  // 本次会话得出的结论
  assert.equal(iptvSourceStatus('playing', '', undefined).text, '本次可播')
  assert.equal(iptvSourceStatus('trying', '', undefined).text, '正在尝试')
  assert.equal(iptvSourceStatus('failed', '', undefined).text, '本次失败')
  // 频道自带（存储）的测速结论
  assert.equal(iptvSourceStatus('idle', 'online', 1).text, '原测速可用')
  assert.equal(iptvSourceStatus('idle', 'offline', 0).text, '原测速失败')
  assert.equal(iptvSourceStatus('idle', 'not_live', 0).text, '原测速未开播')
  assert.equal(iptvSourceStatus('idle', 'untested', undefined).text, '未测速')
  assert.equal(iptvSourceStatus('stopped', '', 1).text, '原测速可用')
  // Radio 用同一套词汇
  assert.equal(radioSourceText('healthy'), '原测速可用')
  assert.equal(radioSourceText('unknown'), '未测速')
})

test('灯的颜色与文案映射稳定', () => {
  assert.equal(lampClass(SOURCE_LAMP.GOOD), 'bg-emerald-500')
  assert.equal(lampClass(SOURCE_LAMP.TRYING), 'bg-amber-400')
  assert.equal(lampClass(SOURCE_LAMP.BAD), 'bg-red-500')
  assert.equal(lampClass(SOURCE_LAMP.UNKNOWN), 'bg-neutral-300 dark:bg-neutral-600')
  assert.equal(lampClass('nonsense'), lampClass(SOURCE_LAMP.UNKNOWN))
})

test('Radio 源菜单不再硬编码绿灯，IPTV 与 Radio 共用 statusClass', () => {
  const source = readFileSync(
    fileURLToPath(new URL('../../src/components/FullPlayer.vue', import.meta.url)), 'utf8',
  )
  // Radio 行的灯必须来自 statusClass，而不是写死的 bg-emerald-500。
  assert.match(source, /:class="source\.statusClass"/)
  assert.doesNotMatch(source, /rounded-full bg-emerald-500"><\/span>/)
  // 两个菜单的 active 行都保留灰色选中表达，且不再与灯混用。
  const activeRows = source.match(/ring-1 ring-inset ring-neutral-300/g) || []
  assert.equal(activeRows.length, 2, 'IPTV 与 Radio 的 active 行都应加强为可见的灰色选中')
})
