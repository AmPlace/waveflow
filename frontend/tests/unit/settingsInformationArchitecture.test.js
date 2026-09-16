import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { afterEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createMemoryHistory, createRouter } from 'vue-router'

import { appRoutes } from '../../src/router/routes.js'
import {
  EPG_SETTINGS_TABS,
  SETTINGS_TABS,
  activeEpgSettingsTab,
  activeSettingsTab,
} from '../../src/views/settings/settingsNavigation.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const routers = []
const routeTestComponent = { template: '<div />' }

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

function sourceTree(directory) {
  return fs.readdirSync(directory, { withFileTypes: true })
    .map((entry) => {
      const target = path.join(directory, entry.name)
      return entry.isDirectory() ? sourceTree(target) : fs.readFileSync(target, 'utf8')
    })
    .join('\n')
}

function routeRecordsForNode(routes) {
  return routes.map((route) => ({
    ...route,
    ...(route.component ? { component: routeTestComponent } : {}),
    ...(route.children ? { children: routeRecordsForNode(route.children) } : {}),
  }))
}

function memoryRouter() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: routeRecordsForNode(appRoutes),
  })
  routers.push(router)
  return router
}

async function settleNavigation() {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

afterEach(() => {
  routers.length = 0
})

test('Settings 路由默认进入直播源并支持所有正式 deep link', async () => {
  const router = memoryRouter()
  await router.push('/settings')
  assert.equal(router.currentRoute.value.fullPath, '/settings/sources')

  await router.push('/settings/security')
  assert.equal(router.currentRoute.value.name, 'settings-security')

  await router.push('/settings/plugins')
  assert.equal(router.currentRoute.value.name, 'settings-plugins')

  await router.push('/settings/epg')
  assert.equal(router.currentRoute.value.fullPath, '/settings/epg/sources')
  assert.equal(router.currentRoute.value.name, 'settings-epg-sources')

  await router.push('/settings/epg/matching')
  assert.equal(router.currentRoute.value.name, 'settings-epg-matching')
})

test('Settings active tab 完全由当前 route 推导', () => {
  assert.deepEqual(SETTINGS_TABS.map((item) => item.label), ['直播源', 'EPG', 'Plugins', '安全与访问'])
  assert.deepEqual(EPG_SETTINGS_TABS.map((item) => item.label), ['来源', '频道匹配'])
  assert.equal(activeSettingsTab('/settings/sources'), 'sources')
  assert.equal(activeSettingsTab('/settings/epg/matching'), 'epg')
  assert.equal(activeSettingsTab('/settings/security'), 'security')
  assert.equal(activeSettingsTab('/settings/plugins'), 'plugins')
  assert.equal(activeEpgSettingsTab('/settings/epg/sources'), 'sources')
  assert.equal(activeEpgSettingsTab('/settings/epg/matching'), 'matching')
})

test('Settings route 支持 browser back 和 forward', async () => {
  const router = memoryRouter()
  await router.push('/settings/sources')
  await router.push('/settings/epg/matching')
  await router.push('/settings/security')

  router.back()
  await settleNavigation()
  assert.equal(router.currentRoute.value.fullPath, '/settings/epg/matching')

  router.back()
  await settleNavigation()
  assert.equal(router.currentRoute.value.fullPath, '/settings/sources')

  router.forward()
  await settleNavigation()
  assert.equal(router.currentRoute.value.fullPath, '/settings/epg/matching')
})

test('旧 /admin 兼容跳转，/admin/epg 转到新频道匹配页，Market 保持可达', async () => {
  const router = memoryRouter()
  await router.push('/admin')
  assert.equal(router.currentRoute.value.fullPath, '/settings/sources')

  await router.push('/admin/epg')
  assert.equal(router.currentRoute.value.fullPath, '/settings/epg/matching')
  assert.equal(router.currentRoute.value.name, 'settings-epg-matching')

  await router.push('/market')
  assert.equal(router.currentRoute.value.name, 'market')
})

test('未知 Settings child 有确定 fallback', async () => {
  const router = memoryRouter()
  await router.push('/settings/unknown/child')
  assert.equal(router.currentRoute.value.fullPath, '/settings/sources')

  await router.push('/settings/epg/unknown')
  assert.equal(router.currentRoute.value.fullPath, '/settings/epg/sources')
})

test('Desktop 和 Mobile 共用可滚动横向 tabs，不引入 Settings sidebar', () => {
  const settings = source('src/views/settings/SettingsView.vue')
  const epg = source('src/views/settings/EpgSettingsView.vue')
  const market = source('src/views/MarketView.vue')
  assert.match(market, /market-main page-shell min-h-screen w-full page-with-mini-player/)
  assert.match(settings, /settings-page page-shell min-h-screen w-full page-with-mini-player/)
  assert.match(settings, /settings-primary-tabs[\s\S]*overflow-x-auto/)
  assert.match(settings, /settings-primary-tab[\s\S]*rounded-full[\s\S]*border-\[var\(--border\)\][\s\S]*bg-\[var\(--surface\)\]/)
  assert.doesNotMatch(settings, />WaveFlow<|>设置<|管理直播来源、节目单与访问方式/)
  assert.match(settings, /aria-label="设置分类"/)
  assert.match(settings, /aria-current/)
  assert.match(settings, /scrollIntoView/)
  assert.match(settings, /focus-visible:ring-2/)
  assert.doesNotMatch(settings, /<aside|sidebar/i)
  assert.match(epg, /settings-secondary-tabs[\s\S]*overflow-x-auto/)
  assert.match(epg, /mx-auto w-full max-w-7xl/)
  assert.match(epg, /settings-secondary-header[\s\S]*epg-settings-actions/)
  assert.match(epg, /aria-label="EPG 设置分类"/)
  assert.match(epg, /settings-secondary-tab::after/)
  assert.doesNotMatch(epg, /<aside|sidebar/i)
})

test('直播源迁移保留主要管理能力、确认模式和状态页面', () => {
  const live = source('src/views/settings/LiveSourcesSettings.vue')
  for (const symbol of [
    'fetchSubscriptions',
    'addSubscription',
    'deleteSubscription',
    'refreshSubscription',
    'refreshAllSubscriptions',
    'testAllChannels',
    'testAllGlobal',
    'cancelTest',
  ]) {
    assert.match(live, new RegExp(`\\b${symbol}\\b`))
  }
  for (const label of ['添加', '全部刷新', '全部测速', '刷新', '测速', '删除', '导出 M3U8']) {
    assert.match(live, new RegExp(label))
  }
  assert.match(live, /askConfirm/)
  assert.match(live, /loadError/)
  assert.match(live, /aria-busy="true"/)
  assert.match(live, /还没有直播源/)
  assert.match(live, /role="dialog"/)
  assert.match(live, /aria-modal="true"/)
  assert.match(live, /bg-white\/95/)
  assert.match(live, /group relative rounded-2xl[\s\S]*text-center/)
  assert.match(live, /支持客户端/)
  assert.match(live, /APTV[\s\S]*TiviMate[\s\S]*VLC[\s\S]*PotPlayer[\s\S]*Emby/)
  assert.match(live, /不知道选什么？[\s\S]*https:\/\/bgm\.gs\//)
})

test('安全设置只复用既有 API 并保留 forced 字段语义', () => {
  const security = source('src/views/settings/SecuritySettings.vue')
  const fields = source('src/views/settings/securitySettingsUi.js')
  assert.match(security, /fetchSecuritySettings/)
  assert.match(security, /updateSecuritySettings/)
  assert.match(security, /anonymous_browse/)
  assert.match(security, /anonymous_playback/)
  assert.match(fields, /allow_private/)
  assert.match(fields, /allow_loopback/)
  assert.match(fields, /session_max_age_days/)
  assert.match(fields, /media_credential_default_ttl_days/)
  assert.match(security, /环境变量锁定/)
  assert.match(security, /安全设置加载失败/)
  assert.match(security, />重试</)
})

test('EPG 来源和频道匹配分别接入正式 Settings 子页面', () => {
  const sources = source('src/views/settings/EpgSourcesSettings.vue')
  const matching = source('src/views/settings/EpgMatchingSettings.vue')
  const routes = source('src/router/routes.js')
  const navigation = source('src/views/settings/settingsNavigation.js')
  assert.match(routes, /settings-epg-sources[\s\S]*EpgSourcesSettings\.vue/)
  assert.match(navigation, /频道匹配/)
  assert.match(routes, /settings-epg-matching[\s\S]*EpgMatchingSettings\.vue/)
  assert.match(sources, /fetchEpgSources/)
  assert.match(matching, /fetchEpgMatchingChannels/)
  assert.match(sources, /添加节目单来源/)
})

test('App 移除旧节目单一级入口，电视、电台、Market 和设置导航保持', () => {
  const app = source('src/App.vue')
  const primaryNavigation = app
    .split('const primaryNavItems')[1]
    .split('const secondaryNavItems')[0]
  assert.match(primaryNavigation, /label: '电视'[\s\S]*route: '\/tv'/)
  assert.match(primaryNavigation, /label: '电台'[\s\S]*route: '\/radio'/)
  assert.doesNotMatch(primaryNavigation, /label: '节目单'|route: '\/admin\/epg'/)
  assert.match(app, /label: '设置'[\s\S]*route: '\/settings\/sources'/)
  assert.match(app, /label: 'Market'[\s\S]*route: '\/market'/)
  assert.match(app, /route\.path\.startsWith\('\/settings'\)/)
})

test('旧 EpgDebug production code 已退役，match-status 不再有 frontend caller', () => {
  const routes = source('src/router/routes.js')
  const sourceRoot = path.join(frontendRoot, 'src')
  const productionSource = sourceTree(sourceRoot)

  assert.equal(fs.existsSync(path.join(frontendRoot, 'src/views/EpgDebug.vue')), false)
  assert.doesNotMatch(routes, /EpgDebug|epg-debug/)
  assert.match(routes, /path: '\/admin\/epg'[\s\S]*redirect: '\/settings\/epg\/matching'/)
  assert.doesNotMatch(productionSource, /\/api\/iptv\/epg\/match-status/)
})
