import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

function source(relativePath) {
  return fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')
}

test('侧边栏 GitHub 入口打开 WaveFlow 仓库', () => {
  const app = source('src/App.vue')

  assert.match(app, /label: 'GitHub', icon: 'github', external: 'https:\/\/github\.com\/amplace\/waveflow'/)
  assert.doesNotMatch(app, /label: '关于'/)
  assert.match(app, /if \(item\.external\) \{[\s\S]*window\.open\(item\.external, '_blank', 'noopener,noreferrer'\)/)
  assert.match(app, /item\.icon === 'github'/)
})

test('搜索与日夜图标使用统一的 19px、1.7px 视觉规格', () => {
  const app = source('src/App.vue')

  assert.equal((app.match(/class="[^"]*size-\[19px\][^"]*"/g) || []).length, 6)
  assert.equal((app.match(/stroke-width="1\.7"/g) || []).length, 11)
  assert.equal((app.match(/class="size-\[19px\] toolbar-search-icon"/g) || []).length, 2)
  assert.match(source('src/style.css'), /\.toolbar-search-icon\s*\{\s*transform: translate\(-0\.5px, -0\.5px\);/)
  assert.doesNotMatch(app, /<svg class="size-5"[^>]*>.*?(cx="11"|M20\.5)/)
})

test('collapsed navigation clips and fades labels without removing their layout', () => {
  const app = source('src/App.vue')
  const css = source('src/style.css')
  assert.equal((app.match(/:aria-label="item.label"/g) || []).length, 2)
  assert.doesNotMatch(css, /\.sidebar-collapsed \.nav-label\s*\{[^}]*display: none;/)
  assert.match(css, /\.nav-label\s*\{\s*flex: 0 0 auto;\s*transition: opacity 260ms cubic-bezier\(0\.4, 0, 0\.2, 1\);/)
  assert.match(css, /\.app-sidebar \.nav-item\s*\{[^}]*overflow: hidden;/)
  assert.doesNotMatch(css, /\.sidebar-collapsed \.nav-item\s*\{[^}]*justify-content: center;/)
  assert.doesNotMatch(app, /class="nav-item[^"\n]*(?:h-14|px-3)/)
})

test('navigation preserves the original height and radius with a fixed icon rail', () => {
  const css = source('src/style.css')
  assert.match(css, /\.app-sidebar \.nav-item\s*\{[^}]*height: 56px;[^}]*justify-content: flex-start;[^}]*padding-inline: 11px;[^}]*border-radius: var\(--card-radius\);/)
  assert.match(css, /\.sidebar-brand\s*\{[^}]*padding-inline: 11px;[^}]*border: 1px solid transparent;/)
  assert.match(css, /\.sidebar-collapsed \.sidebar-toggle\s*\{\s*right: calc\(\(var\(--sidebar-w-collapsed\) - 40px\) \/ 2\);/)
  assert.match(css, /box-shadow: inset -1px 0 var\(--border\);/)
})
