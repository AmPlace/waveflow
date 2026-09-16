<script setup>
import { onMounted, onBeforeUnmount } from 'vue'
import { storeToRefs } from 'pinia'
import { useToastStore } from '../stores/toast'



const store = useToastStore()
const { toasts, confirm } = storeToRefs(store)

const ICON_PATH = {
  success: 'M5 13l4 4L19 7',
  error:   'M6 6l12 12M18 6 6 18',
  warning: 'M12 8v4M12 16h.01',
  info:    'M12 8v4M12 16h.01',
}
function iconPath(t) {
  return ICON_PATH[t.type] || ICON_PATH.info
}

function onKeydown(e) {
  if (e.key === 'Escape' && confirm.value) {
    e.preventDefault()
    store.dismissConfirm()
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <!-- toast 容器：顶部居中 -->
  <Teleport to="body">
    <div
      class="pointer-events-none fixed left-1/2 top-5 z-[150] flex max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-col items-center gap-2.5"
      aria-live="polite"
    >
      <TransitionGroup name="wf-toast">
        <div
          v-for="t in toasts"
          :key="t.id"
          class="pointer-events-auto flex w-fit max-w-full items-center gap-3 rounded-3xl border border-white/60 bg-white/95 px-5 py-3.5 text-base font-medium text-neutral-800 shadow-2xl shadow-neutral-950/20 dark:border-white/10 dark:bg-neutral-900/95 dark:text-neutral-200"
          role="status"
        >
          <svg
            class="size-5 shrink-0 text-neutral-800 dark:text-neutral-100"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
          >
            <path
              :d="iconPath(t)"
              stroke="currentColor"
              stroke-width="2.6"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <span class="break-words leading-snug">{{ t.message }}</span>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>

  <!-- 确认对话框 -->
  <Teleport to="body">
    <Transition name="wf-confirm">
      <div
        v-if="confirm"
        class="fixed inset-0 z-[160] flex items-center justify-center bg-neutral-950/35 px-4 py-8 backdrop-blur-md"
        @click.self="store.dismissConfirm()"
      >
        <section
          class="w-fit max-w-sm min-w-[15rem] rounded-3xl border border-white/60 bg-white/95 p-6 text-center shadow-2xl shadow-neutral-950/20 dark:border-white/10 dark:bg-neutral-900/95"
          role="dialog"
          aria-modal="true"
        >
          <h3
            v-if="confirm.title"
            class="mb-2 text-lg font-semibold text-neutral-900 dark:text-neutral-50"
          >
            {{ confirm.title }}
          </h3>
          <p class="text-[0.95rem] leading-relaxed text-neutral-600 dark:text-neutral-300">
            {{ confirm.message }}
          </p>
          <div class="mt-6 flex justify-center gap-3">
            <button
              type="button"
              class="rounded-xl border border-neutral-200 bg-neutral-100 px-5 py-2.5 text-sm font-medium text-neutral-700 transition-colors hover:bg-neutral-200 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-200 dark:hover:bg-neutral-700"
              @click="store.resolveConfirm(false)"
            >
              {{ confirm.cancelText }}
            </button>
            <button
              type="button"
              class="rounded-xl bg-neutral-900 px-5 py-2.5 text-sm font-semibold text-white transition-transform active:scale-95 dark:bg-neutral-100 dark:text-neutral-900"
              @click="store.resolveConfirm(true)"
            >
              {{ confirm.confirmText }}
            </button>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.wf-toast-enter-active,
.wf-toast-leave-active {
  transition:
    opacity 0.22s ease,
    transform 0.22s ease;
}
.wf-toast-enter-from {
  opacity: 0;
  transform: translateY(-10px);
}
.wf-toast-leave-to {
  opacity: 0;
  transform: translateY(-6px) scale(0.97);
}
.wf-toast-move {
  transition: transform 0.22s ease;
}

.wf-confirm-enter-active,
.wf-confirm-leave-active {
  transition: opacity 0.18s ease;
}
.wf-confirm-enter-from,
.wf-confirm-leave-to {
  opacity: 0;
}
</style>
