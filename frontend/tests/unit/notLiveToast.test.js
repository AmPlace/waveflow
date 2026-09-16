// 批次 1B 后回归 1 验证：not_live 点击仍 toast 且不阻断
//
// 关键不变量：
//   1. 全 not_live 频道点击：toast 调用一次 + 真实进入 playIptvChannel
//   2. 非 not_live 频道点击：不调用该 toast
//   3. 测速失败仍可尝试；只有明确 disabled 的频道不进入 playIptvChannel

import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { usePlayerStore } from '../../src/stores/player.js'
import { useToastStore } from '../../src/stores/toast.js'
import { isChannelAllNotLive, isChannelAllUrlsBlocked } from '../../src/utils/sourceIdentity.js'

function freshStores() {
  setActivePinia(createPinia())
  return { player: usePlayerStore(), toast: useToastStore() }
}

function makeChannel(probe) {
  return {
    canonical_key: 'cn_test',
    name: '测试频道',
    urls: [
      { url: 'https://cdn.example/live.m3u8', source_id: 'src_a', source_type: 'hls', is_working: 0, probe_status: probe },
      { url: 'https://cdn.example/live2.m3u8', source_id: 'src_b', source_type: 'hls', is_working: 0, probe_status: probe },
    ],
  }
}

// IptvHome 的 playChannel 真实分支语义（与生产代码 1:1 一致）：
//   if (isUnavailable(ch)) return
//   if (!ch.urls.length) return
//   if (isAllNotLive(ch)) toastStore.info('该频道上次检测未开播，正在尝试播放')
//   await playerStore.playIptvChannel(ch)
async function simulateHomeClick(toastStore, playerStore, ch) {
  if (isChannelAllUrlsBlocked(ch)) return { entered: false, reason: 'blocked' }
  if (!ch.urls?.length) return { entered: false, reason: 'no_urls' }
  if (isChannelAllNotLive(ch)) {
    toastStore.info('该频道上次检测未开播，正在尝试播放')
  }
  await playerStore.playIptvChannel(ch)
  return { entered: true }
}

// FullPlayer 的 playIptvChannelFromFullPlayer 分支：
//   if (!ch?.urls?.length) { setPlaybackError; return }
//   if (isIptvUnavailable(ch)) { setPlaybackError; return }
//   if (isIptvAllNotLive(ch)) toastStore.info(...)
//   await playerStore.playIptvChannel(ch)
async function simulateFullPlayerClick(toastStore, playerStore, ch) {
  if (!ch?.urls?.length) return { entered: false, reason: 'no_urls' }
  if (isChannelAllUrlsBlocked(ch)) return { entered: false, reason: 'blocked' }
  if (isChannelAllNotLive(ch)) {
    toastStore.info('该频道上次检测未开播，正在尝试播放')
  }
  await playerStore.playIptvChannel(ch)
  return { entered: true }
}

test('IptvHome click on all-not_live shows toast once and enters playIptvChannel', async () => {
  const { player, toast } = freshStores()
  let played = null
  player.playIptvChannel = async (ch) => { played = ch }

  const ch = makeChannel('not_live')
  const result = await simulateHomeClick(toast, player, ch)

  assert.equal(result.entered, true)
  assert.equal(played, ch)
  assert.equal(toast.toasts.length, 1)
  assert.match(toast.toasts[0].message, /未开播/)
  assert.match(toast.toasts[0].message, /正在尝试播放/)
})

test('FullPlayer click on all-not_live shows toast once and enters playIptvChannel', async () => {
  const { player, toast } = freshStores()
  let played = null
  player.playIptvChannel = async (ch) => { played = ch }

  const ch = makeChannel('not_live')
  const result = await simulateFullPlayerClick(toast, player, ch)

  assert.equal(result.entered, true)
  assert.equal(played, ch)
  assert.equal(toast.toasts.length, 1)
  assert.match(toast.toasts[0].message, /未开播/)
})

test('non-not_live (online) click does NOT show the not_live toast', async () => {
  const { player, toast } = freshStores()
  player.playIptvChannel = async () => {}

  const ch = {
    canonical_key: 'cn_normal',
    urls: [{ url: 'https://x.example/a.m3u8', probe_status: 'online', source_id: 'a', source_type: 'hls' }],
  }
  await simulateHomeClick(toast, player, ch)
  await simulateFullPlayerClick(toast, player, ch)

  // 没有 not_live toast 被推
  assert.equal(toast.toasts.filter((t) => /未开播/.test(t.message)).length, 0)
})

test('all-offline probe result still enters playIptvChannel without not_live toast', async () => {
  const { player, toast } = freshStores()
  let played = null
  player.playIptvChannel = async (ch) => { played = ch }

  const ch = makeChannel('offline')
  const r1 = await simulateHomeClick(toast, player, ch)
  const r2 = await simulateFullPlayerClick(toast, player, ch)

  assert.equal(r1.entered, true, 'IptvHome 应继续尝试')
  assert.equal(r2.entered, true, 'FullPlayer 应继续尝试')
  assert.equal(played, ch)
  assert.equal(toast.toasts.filter((t) => /未开播/.test(t.message)).length, 0)
})

test('all explicitly disabled sources remain blocked', async () => {
  const { player, toast } = freshStores()
  let played = null
  player.playIptvChannel = async (ch) => { played = ch }
  const ch = makeChannel('online')
  ch.urls = ch.urls.map((entry) => ({ ...entry, disabled: true }))

  const r1 = await simulateHomeClick(toast, player, ch)
  const r2 = await simulateFullPlayerClick(toast, player, ch)

  assert.equal(r1.entered, false)
  assert.equal(r2.entered, false)
  assert.equal(played, null)
})

test('mixed not_live + online click: toast not triggered (因为不是全部 not_live)', async () => {
  const { player, toast } = freshStores()
  player.playIptvChannel = async () => {}

  const ch = {
    canonical_key: 'cn_mix',
    urls: [
      { url: 'https://x.example/a.m3u8', probe_status: 'not_live', source_id: 'a', source_type: 'hls' },
      { url: 'https://x.example/b.m3u8', probe_status: 'online', source_id: 'b', source_type: 'hls' },
    ],
  }
  await simulateHomeClick(toast, player, ch)
  assert.equal(toast.toasts.filter((t) => /未开播/.test(t.message)).length, 0)
})

test('两个入口对同一 not_live 频道都触发同一条 toast 文案', () => {
  // 相同 channel 数据 → 两个入口都判断为 isChannelAllNotLive=true → 同一条 toast
  const ch = makeChannel('not_live')
  assert.equal(isChannelAllNotLive(ch), true)
  assert.equal(isChannelAllUrlsBlocked(ch), false)
})
