import assert from 'node:assert/strict'
import test from 'node:test'
import { appRoutes } from '../../src/router/routes.js'

test('内容入口使用规范化的 /tv 与 /radio 路由', () => {
  const radio = appRoutes.find((route) => route.name === 'radio')
  const tv = appRoutes.find((route) => route.name === 'tv')

  assert.equal(radio?.path, '/radio')
  assert.equal(tv?.path, '/tv')
  assert.equal(appRoutes.find((route) => route.path === '/')?.redirect, '/radio')
  assert.equal(appRoutes.find((route) => route.path === '/iptv')?.redirect, '/tv')
})
