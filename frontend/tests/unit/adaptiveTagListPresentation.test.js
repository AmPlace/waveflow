import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'

test('AdaptiveTagList measures the same overflow slot that is rendered visibly', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'waveflow-adaptive-tag-'))
  try {
    await build({
      configFile: false,
      logLevel: 'silent',
      plugins: [vue()],
      build: {
        outDir: dir,
        lib: { entry: new URL('../../src/components/AdaptiveTagList.vue', import.meta.url).pathname, formats: ['es'], fileName: () => 'component.mjs' },
        rollupOptions: { external: ['vue'], output: { paths: { vue: new URL('../../node_modules/vue/index.js', import.meta.url).href } } },
      },
    })
    const { default: AdaptiveTagList } = await import(pathToFileURL(path.join(dir, 'component.mjs')))
    const items = ['Custom first', '央视', 'Custom first'].map((label, i) => ({ key: i, label }))
    const html = await renderToString(createSSRApp({ render: () => h(AdaptiveTagList, { items }, {
      tag: ({ item }) => h('span', { class: 'authored-tag' }, item.label),
      more: ({ count }) => h('span', { class: 'authored-overflow' }, `+${count}`),
    }) }))
    // SSR has not measured yet: one visible +N and its hidden measurement twin.
    assert.equal((html.match(/class="authored-overflow"/g) || []).length, 2)
    assert.equal((html.match(/\+3/g) || []).length, 2)
    assert.ok(html.indexOf('Custom first') < html.indexOf('央视'))
    assert.equal((html.match(/Custom first/g) || []).length, 2)
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})
