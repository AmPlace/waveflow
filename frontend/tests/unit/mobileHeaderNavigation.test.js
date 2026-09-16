import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

function mobileHeader(sourceText) {
  const start = sourceText.indexOf('class="fixed inset-x-0 top-0')
  const end = sourceText.indexOf('class="desktop-top-actions fixed top-6')
  assert.notEqual(start, -1, 'mobile app shell is present')
  assert.notEqual(end, -1, 'desktop app shell is present')
  return sourceText.slice(start, end)
}

test('Mobile header只保留内容切换、搜索、主题和More', () => {
  const app = source('src/App.vue')
  const header = mobileHeader(app)

  assert.match(header, /mobile-mode-switch/)
  assert.match(header, />\s*Radio\s*</)
  assert.match(header, />\s*TV\s*</)
  assert.match(header, /aria-label="搜索"/)
  assert.match(header, /aria-label="更多"/)
  assert.doesNotMatch(header, /aria-label="订阅源"/)
  assert.doesNotMatch(header, /aria-label="设置"/)
  assert.doesNotMatch(header, /v-show="activeMode === 'iptv'"/)
})

test('More菜单只保留低频入口，不伪造About或重复内容域切换', () => {
  const app = source('src/App.vue')
  const header = mobileHeader(app)
  const itemsStart = app.indexOf('const mobileNavigationItems')
  const itemsEnd = app.indexOf('\n])', itemsStart)
  const items = app.slice(itemsStart, itemsEnd)

  assert.match(header, /role="menu" aria-label="更多导航"/)
  assert.match(app, /label: 'Market', route: '\/market'/)
  assert.match(app, /label: '设置', route: '\/settings\/sources'/)
  assert.doesNotMatch(items, /label: 'Radio'|label: 'TV'/)
  assert.doesNotMatch(items, /label: '关于'|disabled: true/)
  assert.match(items, /label: '返回内容'/)
  assert.match(app, /function navigateMobile\(item\)/)
  assert.match(app, /if \(item\.disabled \|\| !item\.route\) return/)
})

test('More入口状态由当前route推导，且路由变化会关闭菜单', () => {
  const app = source('src/App.vue')

  assert.match(app, /active: route\.path === '\/market'/)
  assert.match(app, /active: route\.path\.startsWith\('\/settings'\)/)
  assert.match(app, /!\['\/radio', '\/tv'\]\.includes\(route\.path\)/)
  assert.match(app, /watch\(\(\) => route\.path, \(path\) => \{\s*mobileMenuOpen\.value = false/)
})

test('Mobile header controls retain 44px hit targets and fit expanded search within the viewport', () => {
  const css = source('src/style.css')

  assert.match(css, /\.mobile-action-btn[\s\S]*?width: 2\.75rem;[\s\S]*?height: 2\.75rem;/)
  assert.match(css, /\.mobile-search-trigger[\s\S]*?width: 2\.75rem;[\s\S]*?height: 2\.75rem;/)
  assert.match(css, /\.mobile-search-expanded \{\s*width: clamp\(9rem, calc\(100vw - 15rem\), 14rem\);/)
  assert.match(css, /\.mobile-nav-popover__item \{[\s\S]*?min-height: 2\.75rem;/)
})

test('Desktop sidebar与Desktop header仍保留原有入口，不被More替代', () => {
  const app = source('src/App.vue')

  assert.match(app, /class="app-sidebar[\s\S]*hidden[\s\S]*lg:flex/)
  assert.doesNotMatch(app, /label: '收藏'/)
  assert.doesNotMatch(app, /label: '回看'/)
  assert.match(app, /label: '电视', icon: 'tv', route: '\/tv'/)
  assert.match(app, /label: '电台', icon: 'radio', route: '\/radio'/)
  assert.match(app, /label: 'Market', icon: 'layers', route: '\/market'/)
  assert.match(app, /label: '设置', icon: 'settings', route: '\/settings\/sources'/)
  assert.match(app, /class="desktop-top-actions fixed top-6 z-30 hidden items-center gap-3 lg:flex"/)
  assert.match(source('src/style.css'), /\.desktop-top-actions \{\s*right: 56px;/)
})

test('Desktop header移除未使用的最近播放时钟和通知按钮', () => {
  const app = source('src/App.vue')
  const start = app.indexOf('class="desktop-top-actions fixed top-6')
  const end = app.indexOf('\n    </div>', start)
  const desktopHeader = app.slice(start, end)

  assert.doesNotMatch(desktopHeader, /aria-label="最近播放"/)
  assert.doesNotMatch(desktopHeader, /aria-label="通知"/)
  assert.doesNotMatch(desktopHeader, /aria-label="用户"/)
  assert.match(desktopHeader, /aria-label="搜索"/)
  assert.match(desktopHeader, /切换到浅色模式|切换到深色模式/)
  assert.match(source('src/style.css'), /\.desktop-action-btn[\s\S]*?background: var\(--surface\);/)
  assert.doesNotMatch(source('src/style.css'), /\.desktop-action-btn[\s\S]*?box-shadow: 0 8px 20px/)
})

test('Search只把焦点交给当前viewport对应的输入框', () => {
  const app = source('src/App.vue')

  assert.match(app, /const isMobileViewport = window\.matchMedia\('\(max-width: 1023px\)'\)\.matches/)
  assert.match(app, /\(isMobileViewport \? searchInputRef : desktopSearchInputRef\)\.value\?\.focus\(\)/)
})
