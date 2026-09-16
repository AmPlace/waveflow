// Labels translate explicit contract values only; unknown values stay visible.
export function contractLabel(value) {
  return ({ tv_provider: '电视来源', radio_provider: '电台来源', channel_catalog: '频道目录', tv_visual_provider: '电视图像信息' })[value] || value
}
export function featureLabels(contracts = []) {
  const labels = { tv_provider: { resolve_stream: '电视播放地址解析', catalog: '频道目录' }, radio_provider: { catalog: '电台目录', resolve_stream: '电台播放地址解析', programme: '电台节目信息' }, channel_catalog: { discover: '频道发现' }, tv_visual_provider: { metadata: '电视图像元数据' } }
  return contracts.flatMap(item => (item?.features || []).map(feature => labels[item.contract]?.[feature] || `${item.contract}.${feature}`))
}
export function permissionLabel(value) {
  return ({ 'network.managed': '受控网络访问', 'network.direct': '直接网络访问', 'network.managed_http': '明文 HTTP 访问' })[value] || value
}
export function permissionDescription(value) {
  return ({
    'network.managed': '通过 WaveFlow 访问网络，受目标地址与 SSRF 检查约束。',
    'network.direct': '高风险：插件直接访问网络，不经过 WaveFlow 受控 HTTP；不构成安全沙箱。',
    'network.managed_http': '高风险：允许受控网络访问明文 HTTP，地址与 SSRF 检查仍然有效。',
  })[value] || '具体权限含义由插件契约定义。'
}
export function runtimeLabel(runtime) {
  if (runtime?.type === 'python') return runtime.python_version_range ? `Python ${runtime.python_version_range}` : 'Python'
  if (runtime?.type === 'subprocess') return '独立进程（二进制）'
  return runtime?.type || '未提供运行环境信息'
}
export function trustLabel(value) {
  return ({ official: '官方签名发布者', third_party: '已信任的第三方发布者', developer_local: '本地开发插件（未验证官方签名）' })[value] || value || '未提供信任状态'
}
export function environmentLabel(value) {
  return ({ ready: '就绪', unavailable: '不可用', invalid: '无效', not_applicable: '无需 Python 环境' })[value] || value || '未提供环境状态'
}
export function lifecycleLabel(value) {
  return ({ active: '运行中', disabled: '已停用', unavailable: '不可用', quarantined: '已隔离', installed: '已安装', staged: '待激活' })[value] || value || '未知'
}
export function pluginStatusLabel(plugin) {
  if (plugin.quarantined) return '已隔离'
  if (!plugin.enabled) return '已停用'
  return plugin.runtime_available ? '运行中' : '不可用'
}
