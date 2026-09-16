import assert from 'node:assert/strict'
import { test } from 'node:test'
import { contractLabel, featureLabels, permissionLabel, permissionDescription, runtimeLabel, trustLabel, pluginStatusLabel } from '../../src/views/pluginPresentation.js'
import { pluginPublisher, pluginRuntimeLabel, pluginDependencies, requestedPermissions, providerContractLabels } from '../../src/views/marketPackageUi.js'

test('known contract labels translate explicit values; unknown and absent fields are not inferred', () => {
  assert.equal(contractLabel('radio_provider'), '电台来源')
  assert.equal(contractLabel('channel_catalog'), '频道目录')
  assert.equal(contractLabel('vendor.custom'), 'vendor.custom')
  assert.deepEqual(providerContractLabels({ name: '电视直播电台目录', tags: ['tv_provider'] }), [])
  assert.deepEqual(featureLabels([{contract:'tv_provider',features:['resolve_stream']}]), ['电视播放地址解析'])
  assert.deepEqual(featureLabels([{contract:'radio_provider',features:['catalog','custom']}]), ['电台目录','radio_provider.custom'])
  assert.deepEqual(providerContractLabels({ plugin: { provider_contracts: [{contract:'tv_provider'}, {contract:'radio_provider'}, {contract:'vendor.custom'}] } }), ['电视来源','电台来源','vendor.custom'])
})
test('publisher display never infers official status or maintainer from names', () => {
  assert.equal(pluginPublisher({publisher:{id:'org.other',name:'其他发布者'},plugin:{publisher_id:'org.signed'}}), '其他发布者')
  assert.equal(pluginPublisher({plugin:{publisher_id:'org.signed'}}), 'org.signed')
  assert.equal(pluginPublisher({name:'WaveFlow 官方 Provider'}), '')
  assert.equal(trustLabel('third_party'), '已信任的第三方发布者')
  assert.equal(trustLabel('custom'), 'custom')
})
test('permission projection and raw manifest booleans agree without collapsing network access', () => {
  const names=['network.managed','network.direct','network.managed_http']
  assert.deepEqual(requestedPermissions({plugin_manifest:{permissions:{network:{managed:true,direct:true,allow_http:true}}}}),names)
  assert.deepEqual(requestedPermissions({plugin:{permissions:[]},plugin_manifest:{permissions:{network:{direct:true}}}}),[])
  assert.deepEqual(requestedPermissions({plugin_manifest:{permissions:{network:{direct:false}}}}),[])
  assert.equal(permissionLabel('unknown.permission'),'unknown.permission')
  assert.match(permissionDescription('network.direct'),/不构成安全沙箱/)
  assert.match(permissionDescription('network.managed_http'),/明文 HTTP/)
})
test('runtime does not invent subprocess when absent and keeps Python range', () => {
  assert.equal(pluginRuntimeLabel({}), '未提供运行环境信息')
  assert.equal(pluginRuntimeLabel({plugin_manifest:{runtime:{type:'python',python_version_range:'>=3.11.0 <3.15.0'}}}), 'Python >=3.11.0 <3.15.0')
  assert.equal(runtimeLabel({type:'subprocess'}),'独立进程（二进制）')
  assert.equal(runtimeLabel({type:'vendor.runtime'}),'vendor.runtime')
})
test('dependencies work with card projection or manifest and are not capped at eight', () => {
  const deps=Array.from({length:12},(_,i)=>({name:`lib${i}`,version:'1.0'}))
  assert.equal(pluginDependencies({plugin:{dependencies:deps}}).length,12)
  assert.equal(pluginDependencies({plugin_manifest:{runtime:{dependency_lock:{artifacts:[...deps,deps[0]]}}}}).length,12)
})
test('status uses backend structured state, not error-text inference', () => {
  assert.equal(pluginStatusLabel({enabled:true,runtime_available:true}),'运行中')
  assert.equal(pluginStatusLabel({enabled:false,runtime_available:true}),'已停用')
  assert.equal(pluginStatusLabel({enabled:true,quarantined:true}),'已隔离')
  assert.equal(pluginStatusLabel({enabled:true,runtime_available:false,last_error:'PLUGIN_CRASHED'}),'不可用')
})
