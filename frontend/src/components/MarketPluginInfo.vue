<template>
  <div class="market-plugin-info">
    <section>
      <h3>提供的能力</h3>
      <p>{{ providerContractLabels(pkg).join(' · ') || '未声明提供方契约' }}</p>
      <p v-if="featureLabels(contracts).length" class="secondary">{{ featureLabels(contracts).join(' · ') }}</p>
      <dl>
        <dt>发布者</dt><dd>{{ pluginPublisher(pkg) || '未提供' }}</dd>
        <dt>解析协议</dt><dd>{{ pluginSchemeLabels(pkg).map(value => value + '://').join(' · ') || '未声明' }}</dd>
      </dl>
    </section>
    <section>
      <h3>运行要求与权限</h3>
      <p>{{ pluginRuntimeLabel(pkg) }}</p>
      <p class="secondary">运行环境由 WaveFlow 所在设备提供。可用性以当前设备的安装检查为准。</p>
      <ul v-if="requestedPermissions(pkg).length" class="permissions">
        <li v-for="name in requestedPermissions(pkg)" :key="name">
          <strong>{{ permissionLabel(name) }}</strong>
          <p class="secondary">{{ permissionDescription(name) }}</p>
        </li>
      </ul>
      <p v-else class="secondary">未申请额外权限；这不代表插件运行于安全沙箱。</p>
      <p class="secondary">{{ pkg.installed_trust_state ? trustLabel(pkg.installed_trust_state) : '安装时验证发布者与签名。' }} 签名不等于安全沙箱。</p>
    </section>
    <section>
      <h3>安装后管理</h3>
      <p class="secondary">在「设置 → Plugins」管理启用状态、权限与解析接管。当前不提供通用的插件参数编辑器。</p>
      <button v-if="pkg.installed" type="button" @click="$emit('manage')">管理已安装插件</button>
    </section>
    <details>
      <summary>技术详情</summary>
      <dl>
        <dt>签名发布者 ID</dt><dd>{{ pkg.plugin?.publisher_id || pkg.plugin_manifest?.publisher_id || '未提供' }}</dd>
        <dt>插件标识</dt><dd>{{ pluginIdentity(pkg) || '未提供' }}</dd>
        <dt>版本</dt><dd>{{ pkg.version || '未提供' }}</dd>
        <template v-if="pkg.installed_version"><dt>已安装版本</dt><dd>{{ pkg.installed_version }}</dd></template>
        <dt>契约声明</dt><dd><ul><li v-for="(contract, index) in contracts" :key="index">{{ typeof contract === 'string' ? contract : [contract.contract, contract.contract_version, ...(contract.features || [])].filter(Boolean).join(' · ') }}</li></ul></dd>
        <dt>数据来源</dt><dd>{{ pkg.market_source?.name || pkg.source_origin || '未提供' }}</dd>
        <dt>最近更新</dt><dd>{{ pkg.updated_at || pkg.published_at || '未提供' }}</dd>
        <dt>风险声明</dt><dd>{{ pkg.risk_level || '未提供' }}</dd>
        <dt>包清单</dt><dd>{{ safeAdminUrl(pkg.manifest_url) || '内联配置' }}</dd>
      </dl>
      <h4>运行依赖</h4>
      <ul v-if="pluginDependencies(pkg).length" class="dependencies">
        <li v-for="item in pluginDependencies(pkg)" :key="`${item.name}@${item.version}`"><span>{{ item.name }}</span><span>{{ item.version }}</span></li>
      </ul>
      <p v-else class="secondary">未声明额外 Python 依赖。</p>
    </details>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { pluginDependencies, pluginIdentity, pluginPublisher, pluginRuntimeLabel, pluginSchemeLabels, providerContractLabels, requestedPermissions } from '../views/marketPackageUi.js'
import { featureLabels, permissionLabel, permissionDescription, trustLabel } from '../views/pluginPresentation.js'
import { safeAdminUrl } from '../api/adminUi.js'
const props = defineProps({ pkg: { type: Object, required: true } })
defineEmits(['manage'])
const contracts = computed(() => props.pkg.plugin?.provider_contracts || props.pkg.plugin_manifest?.provider_contracts || [])
</script>

<style scoped>
.market-plugin-info { font-size: 13px; line-height: 20px; color: var(--text-primary); overflow-wrap: anywhere; }
section, details { padding-block: 16px; border-top: 1px solid var(--border); }
h3, summary { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
summary { cursor: pointer; }
h4 { font-weight: 500; margin-top: 16px; }
dl { display: grid; grid-template-columns: 96px minmax(0,1fr); gap: 8px 12px; margin-top: 12px; }
dt, .secondary { color: var(--text-secondary); }
.secondary { font-size: 12px; margin-top: 4px; }
.permissions { margin-block: 12px; display: grid; gap: 12px; }
strong { font-weight: 500; }
.dependencies { max-height: 220px; overflow-y: auto; }
.dependencies li { display: flex; justify-content: space-between; gap: 12px; padding-block: 4px; }
button { margin-top: 12px; min-height: var(--control-height); padding-inline: 12px; border: 1px solid var(--border); border-radius: var(--control-radius); }
button:hover { background: var(--surface-hover); }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--text-secondary); outline-offset: 3px; }
</style>
