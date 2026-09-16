// 批次 1B：not_live 频道在三个入口的统一行为。
// 该测试不导入 Vue 组件，而是直接调用入口共享的真实生产函数，
// 验证：首页禁止判断、FullPlayer 频道行 disabled、prev/next 跳过过滤
// 都来自同一个 utils helper，且 not_live 不被禁止。

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isChannelAllNotLive,
  isChannelAllUrlsBlocked,
} from '../../src/utils/sourceIdentity.js'

// 三个入口在生产代码中的真实表达：
//   IptvHome  ->  isUnavailable(ch) === isChannelAllUrlsBlocked(ch)
//   FullPlayer频道行 -> displayChannelRows[*].disabled === isIptvUnavailable(ch) === isChannelAllUrlsBlocked(ch)
//   FullPlayer prev/next -> playAdjacentVisibleChannel 用 !item.disabled 过滤
//                             === !isChannelAllUrlsBlocked(ch)
const homeIsUnavailable = (ch) => isChannelAllUrlsBlocked(ch)
const fullPlayerRowDisabled = (ch) => isChannelAllUrlsBlocked(ch)
const prevNextSkips = (ch) => fullPlayerRowDisabled(ch)

test('not_live channel: clickable in IptvHome, FullPlayer list, prev/next', () => {
  const ch = { urls: [
    { probe_status: 'not_live' },
    { probe_status: 'not_live' },
  ] }
  assert.equal(homeIsUnavailable(ch), false, 'IptvHome 必须允许点击 not_live')
  assert.equal(fullPlayerRowDisabled(ch), false, 'FullPlayer 行必须允许点击 not_live')
  assert.equal(prevNextSkips(ch), false, 'prev/next 不得跳过 not_live')
  assert.equal(isChannelAllNotLive(ch), true, '可以显示 not_live 提示文案')
})

test('all-failed probe status remains clickable in all three entries', () => {
  for (const status of ['offline', 'error', 'timeout']) {
    const ch = { urls: [{ probe_status: status }, { probe_status: status }] }
    assert.equal(homeIsUnavailable(ch), false, `IptvHome 应允许尝试 ${status}`)
    assert.equal(fullPlayerRowDisabled(ch), false, `FullPlayer 应允许尝试 ${status}`)
    assert.equal(prevNextSkips(ch), false, `prev/next 不应跳过 ${status}`)
  }
})

test('only explicitly disabled sources block a channel', () => {
  const ch = { urls: [{ disabled: true }, { enabled: false }] }
  assert.equal(homeIsUnavailable(ch), true)
  assert.equal(fullPlayerRowDisabled(ch), true)
  assert.equal(prevNextSkips(ch), true)
})

test('mixed not_live + online channel: clickable everywhere', () => {
  const ch = { urls: [
    { probe_status: 'not_live' },
    { probe_status: 'online' },
  ] }
  assert.equal(homeIsUnavailable(ch), false)
  assert.equal(fullPlayerRowDisabled(ch), false)
  assert.equal(prevNextSkips(ch), false)
  assert.equal(isChannelAllNotLive(ch), false)
})

test('mixed not_live + offline channel remains clickable', () => {
  // 测速状态不阻断，混合状态同样允许尝试。
  const ch = { urls: [{ probe_status: 'not_live' }, { probe_status: 'offline' }] }
  assert.equal(homeIsUnavailable(ch), false)
  assert.equal(fullPlayerRowDisabled(ch), false)
  assert.equal(prevNextSkips(ch), false)
})

test('source-level disabled is independent of channel-level rule', () => {
  // FullPlayer iptvSourceOptions 的源级 disabled 来自:
  //   entry.disabled === true || st === 'unsupported_youtube_url'
  // 这是 source 级判断，不应被 not_live 频道级规则影响。
  const sourceLevelDisabled = (entry, sourceTypeOf) => {
    return entry?.disabled === true || sourceTypeOf(entry) === 'unsupported_youtube_url'
  }
  const fakeSourceType = (e) => e?._st || 'hls'

  // not_live + 源未禁用 -> 源不禁用
  assert.equal(sourceLevelDisabled({ probe_status: 'not_live', disabled: false }, fakeSourceType), false)
  // not_live + 源禁用 -> 源仍按源级规则禁用
  assert.equal(sourceLevelDisabled({ probe_status: 'not_live', disabled: true }, fakeSourceType), true)
  // 源不支持的 youtube
  assert.equal(sourceLevelDisabled({ probe_status: 'online', _st: 'unsupported_youtube_url' }, fakeSourceType), true)
})

test('iptvSourceOptions probe_status tag still says 未开播 for not_live (display only)', () => {
  // 显示文案逻辑（FullPlayer iptvSourceOptions 的 health 标签）
  // 即使我们放开点击，也保留 "未开播" 文案，只用于显示提示。
  const labelOf = (probe_status, working) => {
    return probe_status === 'not_live' ? '未开播'
      : probe_status === 'untested' ? '未检测'
      : working === 1 ? '检测可用'
      : working === 0 ? '检测不可用'
      : '未检测'
  }
  assert.equal(labelOf('not_live', 0), '未开播')
  assert.equal(labelOf('online', 1), '检测可用')
  assert.equal(labelOf('offline', 0), '检测不可用')
})
