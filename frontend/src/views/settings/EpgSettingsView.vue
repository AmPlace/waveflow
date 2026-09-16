<template>
  <section class="mx-auto w-full max-w-7xl" aria-labelledby="epg-settings-title">
    <header class="mb-5">
      <h2 id="epg-settings-title" class="text-xl font-semibold tracking-[-0.015em] text-[var(--text-primary)]">EPG</h2>
      <p class="mt-1.5 text-sm leading-6 text-[var(--text-secondary)]">节目单来源与频道匹配</p>
    </header>

    <div class="settings-secondary-header mb-4 flex min-w-0 items-end justify-between gap-4 border-b border-[var(--border)]">
      <nav
        ref="tabsRef"
        class="settings-secondary-tabs scrollbar-hide flex min-w-0 max-w-full flex-1 gap-6 overflow-x-auto"
        aria-label="EPG 设置分类"
      >
        <RouterLink
          v-for="tab in EPG_SETTINGS_TABS"
          :key="tab.key"
          :to="tab.to"
          class="settings-secondary-tab relative flex min-h-11 shrink-0 items-center px-0.5 text-[13px] font-medium text-[var(--text-secondary)] outline-none transition-colors hover:text-[var(--text-primary)] focus-visible:ring-2 focus-visible:ring-[var(--border-strong)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)]"
          :class="{ 'is-active': activeTab === tab.key }"
          :aria-current="activeTab === tab.key ? 'page' : undefined"
        >
          {{ tab.label }}
        </RouterLink>
      </nav>
      <div id="epg-settings-actions" class="flex min-h-11 shrink-0 items-center pb-1"></div>
    </div>

    <RouterView />
  </section>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { EPG_SETTINGS_TABS, activeEpgSettingsTab } from './settingsNavigation'

const route = useRoute()
const tabsRef = ref(null)
const activeTab = computed(() => activeEpgSettingsTab(route.path))

function revealActiveTab() {
  nextTick(() => {
    const active = tabsRef.value?.querySelector('[aria-current="page"]')
    active?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  })
}

watch(() => route.path, revealActiveTab)
onMounted(revealActiveTab)
</script>

<style scoped>
.settings-secondary-tabs {
  -webkit-overflow-scrolling: touch;
  scroll-padding-inline: 0.125rem;
}

.settings-secondary-tab::after {
  position: absolute;
  right: 0;
  bottom: -1px;
  left: 0;
  height: 2px;
  border-radius: 999px;
  background: transparent;
  content: '';
}

.settings-secondary-tab.is-active {
  color: var(--text-primary);
}

.settings-secondary-tab.is-active::after {
  background: var(--text-primary);
}
</style>
