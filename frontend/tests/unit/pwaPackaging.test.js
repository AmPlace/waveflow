import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const viteConfig = fs.readFileSync(path.resolve(here, '../../vite.config.js'), 'utf8')
const nginxConfig = fs.readFileSync(path.resolve(here, '../../nginx.conf'), 'utf8')

test('PWA keeps dynamic APIs out of service-worker Cache Storage', () => {
  assert.match(viteConfig, /registerType: 'prompt'/)
  assert.match(viteConfig, /urlPattern: \/\^\\\/api\(\?:\\\/\|\$\)\//)
  assert.match(viteConfig, /handler: 'NetworkOnly'/)
  assert.doesNotMatch(viteConfig, /StaleWhileRevalidate/)
  assert.doesNotMatch(viteConfig, /cacheName: 'api-config'/)
})

test('PWA logo cache is bounded, revalidating, and success-only', () => {
  assert.match(viteConfig, /handler: 'NetworkFirst'/)
  assert.match(viteConfig, /cacheName: 'waveflow-logo-assets-v1'/)
  assert.match(viteConfig, /networkTimeoutSeconds: 3/)
  assert.match(viteConfig, /cacheableResponse: \{ statuses: \[200\] \}/)
  assert.match(viteConfig, /maxEntries: 100/)
  assert.match(viteConfig, /maxAgeSeconds: 30 \* 24 \* 3600/)
})

test('static serving separates mutable shell and API responses from hashed assets', () => {
  assert.match(nginxConfig, /location = \/api \{[\s\S]*?return 404;/)
  assert.match(nginxConfig, /location \/api\/[\s\S]*?add_header Cache-Control "no-store" always;/)
  assert.match(nginxConfig, /location = \/sw\.js \{[\s\S]*?no-cache, no-store, must-revalidate/)
  assert.match(nginxConfig, /location = \/index\.html \{[\s\S]*?no-cache, no-store, must-revalidate/)
  assert.match(nginxConfig, /location ~\* \^\/assets\/.*\\\.\(\?:js\|css\|woff2\)\$[\s\S]*?immutable/)
  assert.match(nginxConfig, /location ~\* \^\/\(\?:icons\|logos\)\/[\s\S]*?no-cache, must-revalidate/)
})
