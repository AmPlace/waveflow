import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  adminRequestErrorMessage,
  safeAdminDiagnostic,
  safeAdminUrl,
} from '../../src/api/adminUi.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const source = relativePath => fs.readFileSync(path.join(frontendRoot, relativePath), 'utf8')

test('Admin API errors distinguish auth, denial, validation, conflict, timeout, and server failure', () => {
  assert.equal(adminRequestErrorMessage({ status: 400 }), '提交内容不符合要求，请检查后重试')
  assert.equal(adminRequestErrorMessage({ status: 401 }), '登录已失效，请重新登录')
  assert.equal(adminRequestErrorMessage({ status: 403 }), '当前账号无权执行此操作')
  assert.equal(adminRequestErrorMessage({ status: 422 }), '提交内容不符合要求，请检查后重试')
  assert.equal(adminRequestErrorMessage({ status: 400, message: 'probe_concurrency 必须在 1 至 64 之间' }), '提交内容不符合要求：probe_concurrency 必须在 1 至 64 之间')
  assert.equal(adminRequestErrorMessage({ status: 409 }), '状态已发生变化，请刷新后重试')
  assert.equal(adminRequestErrorMessage({ status: 503, message: '/private/path token=secret' }), '服务暂时不可用，请稍后重试')
  assert.equal(adminRequestErrorMessage({ status: 0, message: '请求超时' }), '请求超时，请稍后重试')
})

test('Admin diagnostics and URLs redact credentials, query secrets, paths, and tracebacks', () => {
  const url = safeAdminUrl('https://user:pass@example.test/market.json?token=secret&signature=sig#fragment')
  assert.equal(url, 'https://example.test/market.json')

  const diagnostic = safeAdminDiagnostic(
    'Authorization: Bearer secret Cookie: session=secret https://example.test/a?token=secret api_key: topsecret /Users/me/private.py Traceback (most recent call last)',
  )
  assert.doesNotMatch(diagnostic, /Bearer|session=|token=secret|topsecret|\/Users\/me|Traceback/i)
  assert.match(diagnostic, /\[redacted\]|\[local-path\]|\[diagnostic-hidden\]/)
})

test('Settings exposes verified consumer fields and separates effective, stored, forced, and restart state', () => {
  const view = source('src/views/settings/SecuritySettings.vue')
  const presentation = source('src/views/settings/securitySettingsUi.js')
  for (const key of [
    'anonymous_browse',
    'anonymous_playback',
    'allow_private',
    'allow_loopback',
    'enable_rtsp_proxy',
    'rtsp_max_sessions',
    'media_credential_default_ttl_days',
    'session_max_age_days',
  ]) assert.match(presentation, new RegExp(`\\b${key}\\b`), key)
  assert.match(view, /data\?\.runtime/)
  assert.match(view, /data\?\.schema/)
  assert.match(view, /restart_required/)
  assert.match(view, /v-if="isForced\(item\.key\)"/)
  assert.match(view, /当前生效/)
  assert.match(view, /已保存/)
  assert.match(view, /:disabled="fieldDisabled\(item\)"/)
})

test('Admin views own latest reads and invalidate pending work on unmount', () => {
  const settings = source('src/views/settings/SecuritySettings.vue')
  const epgSources = source('src/views/settings/EpgSourcesSettings.vue')
  const epgMatching = source('src/views/settings/EpgMatchingSettings.vue')
  const plugins = source('src/views/settings/PluginsSettings.vue')

  assert.match(settings, /onBeforeUnmount/)
  assert.match(settings, /loadController/)
  assert.match(settings, /saveRequestId/)

  assert.match(epgSources, /onBeforeUnmount/)
  assert.match(epgSources, /loadController/)
  assert.match(epgSources, /deleteImpactController/)

  assert.match(epgMatching, /overviewController/)
  assert.match(epgMatching, /sourceOptionsController/)

  assert.match(plugins, /onBeforeUnmount/)
  assert.match(plugins, /listController/)
  assert.match(plugins, /detailController/)
})

test('Market separates failed initial load from valid empty and sanitizes admin diagnostics', () => {
  const market = source('src/views/MarketView.vue')
  assert.match(market, /v-else-if="error && !packages\.length"/)
  assert.match(market, /safeAdminDiagnostic\(source\.last_error/)
  assert.match(market, /safeAdminUrl\(source\.url/)
  assert.match(market, /if \(importLoading\.value\) return/)
  assert.match(market, /packageOperationCurrent\(operationId\)[\s\S]*for \(const pkg of updatableInstalledPackages\.value\)/)
  assert.match(market, /restoreMarketSourceDraft\(source\)/)
  assert.match(market, /refreshing\.value \|\| importLoading\.value \|\| updating\.value/)
  assert.match(market, /componentDisposed/)
})

test('Plugin runtime diagnostics are presentation-safe and detail requests are identity-owned', () => {
  const plugins = source('src/views/settings/PluginsSettings.vue')
  assert.match(plugins, /safeAdminDiagnostic\(selected\.last_error/)
  assert.match(plugins, /selected\.value\?\.plugin !== identity/)
  assert.match(plugins, /detailController\?\.abort\(\)/)
})
