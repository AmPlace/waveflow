<template>
  <main class="settings-page page-shell min-h-screen w-full page-with-mini-player">
    <nav
      ref="tabsRef"
      class="settings-primary-tabs scrollbar-hide mb-[var(--card-gap)] flex max-w-full items-center gap-2 overflow-x-auto pb-[3px]"
      aria-label="设置分类"
    >
      <RouterLink
        v-for="tab in SETTINGS_TABS"
        :key="tab.key"
        :to="tab.to"
        class="settings-primary-tab flex h-10 shrink-0 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 text-sm font-medium text-[var(--text-secondary)] outline-none transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:ring-2 focus-visible:ring-[var(--border-strong)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)]"
        :class="{ 'is-active': activeTab === tab.key }"
        :aria-current="activeTab === tab.key ? 'page' : undefined"
      >
        {{ tab.label }}
      </RouterLink>
    </nav>

    <RouterView />
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { SETTINGS_TABS, activeSettingsTab } from './settingsNavigation'

const route = useRoute()
const tabsRef = ref(null)
const activeTab = computed(() => activeSettingsTab(route.path))

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
.settings-primary-tabs {
  -webkit-overflow-scrolling: touch;
  scroll-padding-inline: 1rem;
  scrollbar-width: none;
}

.settings-primary-tabs::-webkit-scrollbar {
  display: none;
}

.settings-primary-tab.is-active {
  border-color: var(--text-primary);
  background: var(--text-primary);
  color: var(--bg);
}
</style>
