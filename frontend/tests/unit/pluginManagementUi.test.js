import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { ApiError } from '../../src/api/client.js'
import { pluginErrorCode, pluginErrorMessage } from '../../src/api/plugins.js'
import { isLogoPackage, isPluginPackage, packageActionLabel, packageInstallable, pluginIdentity, pluginSchemeLabels, providerContractLabels } from '../../src/views/marketPackageUi.js'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const source = relative => fs.readFileSync(path.join(frontendRoot, relative), 'utf8')

const plugin = {
  package_type: 'plugin_package', plugin_installable: true,
  plugin: { publisher_id: 'org.waveflow', plugin_id: 'ptbtv', provider_contracts: [{ contract: 'tv_provider' }] },
}

test('Market presenter 区分 Content 与 Plugin Package 语义', () => {
  assert.equal(isPluginPackage(plugin), true)
  assert.equal(packageInstallable(plugin), true)
  assert.equal(pluginIdentity(plugin), 'org.waveflow/ptbtv')
  assert.deepEqual(providerContractLabels(plugin), ['电视来源'])
  assert.deepEqual(pluginSchemeLabels({ plugin: { owned_schemes: [{ scheme: 'ptbtv', contract: 'tv_provider' }] } }), ['ptbtv'])
  assert.equal(packageActionLabel(plugin, 'install'), '安装 Plugin')
  assert.equal(packageActionLabel({ package_type: 'content_package' }, 'install'), '导入')
})

test('Logo Package 只描述台标改善，不伪装成频道包', () => {
  const logoPack = { package_type: 'content_package', kind: 'logo_pack', content_capabilities: ['logos'], supported_in_v1: true, importable: true }
  assert.equal(isLogoPackage(logoPack), true)
  assert.equal(packageInstallable(logoPack), true)
  assert.equal(packageActionLabel(logoPack, 'install'), '安装 Logo')
  assert.equal(isLogoPackage({ package_type: 'content_package', kind: 'playlist' }), false)
})

test('Plugin stable errors 使用 code 映射而非回显任意服务端消息', () => {
  const error = new ApiError('raw sensitive backend message', {
    status: 409,
    detail: { detail: { code: 'SCHEME_CONFLICT', message: 'raw sensitive backend message' } },
  })
  assert.equal(pluginErrorCode(error), 'SCHEME_CONFLICT')
  assert.match(pluginErrorMessage(error), /取消插件接管/)
  assert.doesNotMatch(pluginErrorMessage(error), /sensitive/)
})

test('Market 保留 Content presenter 并增加独立 package type 和 Plugin detail', () => {
  const market = source('src/views/MarketView.vue')
  // requires_resolver 的文案必须写明「解析由插件提供」，不能再承诺 Core 内置解析。
  assert.match(market, /不再提供内置解析/)
  assert.match(market, /market-package-type-switch/)
  assert.match(market, /内容[\s\S]*Plugins/)
  assert.match(market, /packageType === 'plugins'/)
  assert.match(market, /packageTagItems/)
  assert.doesNotMatch(market, /function pluginTagItems/)
  assert.match(market, /安装 Plugin/)
  assert.match(market, /MarketPluginInfo/)
  assert.match(market, /network\.direct/)
  assert.match(market, /允许并继续/)
  assert.match(market, /handleImport[\s\S]*已导入 \$\{result\.channel_count/)
  assert.match(market, /v-if="drawerTags\.length"/)
  assert.match(market, /<h3 class="market-section-title">频道<\/h3>/)
})

test('Settings Plugins 只做 runtime management 并把 update/uninstall 留给 Market', () => {
  const view = source('src/views/settings/PluginsSettings.vue')
  for (const symbol of ['fetchPlugins', 'fetchPlugin', 'enablePlugin', 'disablePlugin', 'recoverPlugin', 'approvePluginPermission', 'revokePluginPermission', 'setPluginOwnership']) {
    assert.match(view, new RegExp(`\\b${symbol}\\b`))
  }
  for (const label of ['运行状态', '权限', '运行依赖', '交由插件解析', '取消插件接管', '在 Market 中查看']) {
    assert.match(view, new RegExp(label))
  }
  assert.doesNotMatch(view, /updatePlugin|uninstallPlugin|安装 Plugin/)
  // Provider resolution 为 Plugin-only，设置页不得再宣传 Core 内置解析器。
  assert.doesNotMatch(view, /内置解析器|切回内置解析|内置兼容解析/)
  assert.match(view, /暂无已安装的 Plugins/)
  assert.match(view, /detailError/)
  assert.match(view, /role="dialog"/)
})

test('Plugins API client 集中封装 lifecycle、permission 与 ownership endpoint', () => {
  const api = source('src/api/plugins.js')
  assert.match(api, /\/api\/admin\/plugins/)
  assert.match(api, /permissions\/approve/)
  assert.match(api, /permissions\/revoke/)
  assert.match(api, /ownership\/\$\{encodeURIComponent\(scheme\)\}/)
  assert.match(api, /\/enable/)
  assert.match(api, /\/disable/)
  assert.match(api, /\/recover/)
})

// Provider resolution 已收敛为 Plugin-only：Core 不再持有 provider-specific 解析器，
// 也不存在「切回内置解析」的回退路径。以下三个短语是旧语义的载体，一旦回流即视为回归。
const RETIRED_PROVIDER_COPY = ['内置解析器', '切回内置解析', '内置兼容解析']

const frontendSourceFiles = () => {
  const srcRoot = path.join(frontendRoot, 'src')
  return fs
    .readdirSync(srcRoot, { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile() && /\.(vue|js|ts)$/.test(entry.name))
    .map(entry => path.join(entry.parentPath ?? entry.path, entry.name))
}

test('前端不再宣传 Core 内置解析器（Provider resolution 为 Plugin-only）', () => {
  const files = frontendSourceFiles()
  // 扫描面必须非空：否则「零命中」只说明没扫到文件，而非文案已清理。
  assert.ok(files.length > 30, `扫描到的前端源文件过少（${files.length}），护栏失效`)

  const offenders = []
  for (const file of files) {
    const text = fs.readFileSync(file, 'utf8')
    for (const retired of RETIRED_PROVIDER_COPY) {
      if (text.includes(retired)) {
        offenders.push(`${path.relative(frontendRoot, file)}: ${retired}`)
      }
    }
  }
  assert.deepEqual(offenders, [], `Core 内置解析器旧文案回流：\n${offenders.join('\n')}`)
})

test('护栏自身有效：能识别被植入的旧文案', () => {
  // 防止 RETIRED_PROVIDER_COPY 被改空/改错导致上面的扫描恒真。
  const probe = '本频道由内置解析器处理，可在设置中切回内置解析。'
  const hit = RETIRED_PROVIDER_COPY.filter(retired => probe.includes(retired))
  assert.deepEqual(hit, ['内置解析器', '切回内置解析'])
  assert.ok(frontendSourceFiles().length > 30)
})
