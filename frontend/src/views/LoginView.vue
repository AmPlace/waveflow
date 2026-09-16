<template>
  <main class="auth-screen min-h-screen px-5 py-12 sm:px-8">
    <section class="auth-panel mx-auto flex min-h-[calc(100vh-6rem)] w-full max-w-md flex-col justify-center">
      <div class="mb-8">
        <div class="mb-5 flex size-12 items-center justify-center rounded-2xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-primary)]">
          <svg class="size-7" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3.5a5 5 0 0 0-5 5v2H6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2h-1v-2a5 5 0 0 0-5-5Zm-3 7v-2a3 3 0 1 1 6 0v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <h1 class="text-2xl font-semibold text-[var(--text-primary)]">登录 WaveFlow</h1>
        <p class="mt-2 text-sm leading-6 text-[var(--text-secondary)]">登录后可以管理订阅、Market、节目单和外部播放器授权。</p>
      </div>

      <form class="space-y-4" @submit.prevent="submit">
        <label class="block">
          <span class="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">用户名</span>
          <input v-model.trim="username" autocomplete="username" class="auth-input" type="text" />
        </label>
        <label class="block">
          <span class="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">密码</span>
          <input v-model="password" autocomplete="current-password" class="auth-input" type="password" />
        </label>

        <p v-if="error" class="text-sm text-red-500">{{ error }}</p>

        <button class="auth-button" type="submit" :disabled="loading">
          {{ loading ? '正在登录' : '登录' }}
        </button>
      </form>
    </section>
  </main>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const username = ref('admin')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  loading.value = true
  try {
    await auth.login({ username: username.value, password: password.value })
    router.replace(String(route.query.redirect || '/radio'))
  } catch (err) {
    error.value = err?.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>
