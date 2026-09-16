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
