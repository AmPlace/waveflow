<template>
  <section
    class="rounded-3xl border border-[var(--border)] bg-[var(--card-bg)] px-6 py-10 text-center sm:px-10 sm:py-14"
    :data-epg-placeholder="section"
  >
    <span class="mx-auto flex size-11 items-center justify-center rounded-2xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)]">
      <svg v-if="section === 'sources'" class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M5 7.5h14M5 12h14M5 16.5h9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
      <svg v-else class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M5 7h9M5 12h14M5 17h7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
        <circle cx="17" cy="7" r="2" stroke="currentColor" stroke-width="1.6"/>
      </svg>
    </span>
    <h3 class="mt-4 text-base font-semibold text-[var(--text-primary)]">{{ title }}</h3>
    <p class="mx-auto mt-2 max-w-md text-sm leading-6 text-[var(--text-secondary)]">{{ description }}</p>
    <p class="mt-4 text-xs text-[var(--text-tertiary)]">将在后续 EPG 管理阶段提供</p>
  </section>
</template>

<script setup>
defineProps({
  section: { type: String, required: true },
  title: { type: String, required: true },
  description: { type: String, required: true },
})
</script>
