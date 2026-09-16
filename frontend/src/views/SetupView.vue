<template>
  <main class="auth-screen min-h-screen px-5 py-12 sm:px-8">
    <section class="auth-panel mx-auto flex min-h-[calc(100vh-6rem)] w-full max-w-md flex-col justify-center">
      <div class="mb-8">
        <div class="mb-5 flex size-12 items-center justify-center rounded-2xl border border-[var(--border)] bg-[var(--surface)] text-[var(--text-primary)]">
          <svg class="size-7" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m12 3 8 4.5-8 4.5-8-4.5L12 3Zm8 9-8 4.5L4 12m16 4.5L12 21l-8-4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <h1 class="text-2xl font-semibold text-[var(--text-primary)]">初始化 WaveFlow</h1>
        <p class="mt-2 text-sm leading-6 text-[var(--text-secondary)]">创建管理员账号后即可管理订阅、节目单、Market 和外部播放器授权。</p>
      </div>

      <form class="space-y-4" @submit.prevent="submit">
        <label class="block">
          <span class="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">管理员用户名</span>
          <input v-model.trim="username" autocomplete="username" class="auth-input" type="text" />
        </label>
        <label class="block">
          <span class="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">密码</span>
          <input v-model="password" autocomplete="new-password" class="auth-input" type="password" />
        </label>
        <label class="block">
          <span class="mb-1.5 block text-xs font-medium text-[var(--text-secondary)]">确认密码</span>
          <input v-model="confirmPassword" autocomplete="new-password" class="auth-input" type="password" />
        </label>

        <p v-if="error" class="text-sm text-red-500">{{ error }}</p>

        <button class="auth-button" type="submit" :disabled="loading">
          {{ loading ? '正在创建' : '创建管理员账号' }}
        </button>
      </form>
    </section>
  </main>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const username = ref('admin')
const password = ref('')
const confirmPassword = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  if (!username.value || username.value.length < 3) {
    error.value = '用户名至少需要 3 个字符'
    return
  }
  if (!password.value || password.value.length < 8) {
    error.value = '密码至少需要 8 个字符'
    return
  }
  if (password.value !== confirmPassword.value) {
    error.value = '两次输入的密码不一致'
    return
  }
  loading.value = true
  try {
    await auth.initialize({ username: username.value, password: password.value })
    router.replace('/radio')
  } catch (err) {
    error.value = err?.message || '初始化失败'
  } finally {
    loading.value = false
  }
}
</script>
