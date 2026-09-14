<template>
  <section aria-labelledby="live-sources-title">
    <header class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h2 id="live-sources-title" class="text-xl font-semibold tracking-[-0.015em] text-[var(--text-primary)]">直播源</h2>
        <p class="mt-1.5 text-sm leading-6 text-[var(--text-secondary)]">添加和维护 M3U/M3U8 订阅，管理测速与导出</p>
      </div>
      <div class="scrollbar-hide flex max-w-full gap-2 overflow-x-auto pb-1 sm:justify-end">
        <button
          type="button"
          class="flex min-h-10 shrink-0 items-center rounded-xl border border-[var(--border)] bg-[var(--surface)] px-3.5 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)] disabled:opacity-50"
          :disabled="refreshRunning"
          @click="handleRefreshAll"
        >
          {{ refreshRunning ? '刷新中…' : '全部刷新' }}
        </button>
        <button
          type="button"
          class="flex min-h-10 shrink-0 items-center rounded-xl border border-[var(--border)] bg-[var(--surface)] px-3.5 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)] disabled:opacity-50"
          :disabled="testRunning"
          @click="handleTestAll"
        >
          {{ testRunning ? `测速中 ${testProgress.tested}/${testProgress.total}` : '全部测速' }}
        </button>
        <button
          type="button"
          class="flex min-h-10 shrink-0 items-center rounded-xl border border-[var(--border)] bg-[var(--surface)] px-3.5 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)]"
          @click="openExportDialog"
        >
          导出 M3U8
        </button>
      </div>
    </header>

    <section v-if="testRunning" class="mb-5 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4" aria-live="polite">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3 text-xs text-[var(--text-secondary)]">
        <span>正在测速 · {{ testProgress.tested }}/{{ testProgress.total }}</span>
        <button
          type="button"
          class="min-h-9 rounded-xl px-3 text-xs font-medium text-red-500 transition-colors hover:bg-red-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400"
          @click="handleCancelTest"
        >
          取消测速
        </button>
      </div>
      <div class="h-1.5 overflow-hidden rounded-full bg-[var(--surface-strong)]">
        <div class="h-full rounded-full bg-emerald-500 transition-all duration-300" :style="{ width: `${testPercent}%` }"></div>
      </div>
      <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--text-tertiary)]">
        <span>可用 {{ testProgress.working }}</span>
        <span>不可用 {{ testProgress.failed }}</span>
        <span v-if="testProgress.not_live">未开播 {{ testProgress.not_live }}</span>
        <span v-if="testProgress.untested">未测试 {{ testProgress.untested }}</span>
        <span v-if="testProgress.current" class="max-w-full truncate">当前 {{ testProgress.current }}</span>
      </div>
    </section>

    <section class="mb-6 rounded-3xl border border-[var(--border)] bg-[var(--card-bg)] p-4 sm:p-5" aria-labelledby="add-source-title">
      <div class="mb-4">
        <h3 id="add-source-title" class="text-sm font-semibold text-[var(--text-primary)]">添加直播源</h3>
        <p class="mt-1 text-xs leading-5 text-[var(--text-secondary)]">支持标准 M3U/M3U8 订阅地址</p>
      </div>
      <div class="flex flex-col gap-3 sm:flex-row">
        <label class="min-w-0 flex-1">
          <span class="sr-only">M3U/M3U8 订阅链接</span>
          <input
            v-model="addUrl"
            type="url"
            placeholder="输入 M3U/M3U8 订阅链接"
            class="min-h-11 w-full rounded-xl border border-[var(--border)] bg-[var(--bg-soft)] px-3.5 text-sm text-[var(--text-primary)] outline-none transition-colors placeholder:text-[var(--text-tertiary)] focus:border-[var(--border-strong)] focus:ring-2 focus:ring-[var(--surface-strong)]"
            @keydown.enter="handleAdd"
          />
        </label>
        <button
          type="button"
          class="min-h-11 shrink-0 rounded-xl bg-[var(--text-primary)] px-5 text-sm font-semibold text-[var(--bg)] transition-transform hover:-translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)] active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="!addUrl.trim() || addLoading"
          @click="handleAdd"
        >
          {{ addLoading ? '解析中…' : '添加' }}
        </button>
      </div>
      <div class="mt-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
        <label>
          <span class="sr-only">自定义 User-Agent</span>
          <input
            v-model="addUa"
            type="text"
            placeholder="自定义 User-Agent（可选）"
            class="min-h-10 w-full rounded-xl border border-[var(--border)] bg-[var(--bg-soft)] px-3.5 text-xs text-[var(--text-primary)] outline-none transition-colors placeholder:text-[var(--text-tertiary)] focus:border-[var(--border-strong)] focus:ring-2 focus:ring-[var(--surface-strong)]"
          />
        </label>
        <label class="flex min-h-10 items-center gap-2 rounded-xl border border-[var(--border)] px-3 text-xs text-[var(--text-secondary)]">
          <input v-model="addForceProxy" type="checkbox" class="size-4 rounded accent-neutral-950 dark:accent-white" />
          强制中转
        </label>
      </div>
      <p v-if="addError" class="mt-3 text-xs leading-5 text-red-500" role="alert">{{ addError }}</p>
    </section>

    <div v-if="loadError" class="rounded-2xl border border-red-500/20 bg-red-500/5 px-5 py-6 text-center" role="alert">
      <p class="text-sm font-medium text-[var(--text-primary)]">直播源加载失败</p>
      <p class="mt-1 text-xs text-[var(--text-secondary)]">{{ loadError }}</p>
      <button type="button" class="mt-4 min-h-10 rounded-xl border border-[var(--border)] px-4 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--surface-hover)]" @click="loadSubscriptions">重试</button>
    </div>

    <div v-else-if="loading" class="grid gap-3" aria-label="正在加载直播源" aria-busy="true">
      <div v-for="index in 3" :key="index" class="h-28 animate-pulse rounded-3xl border border-[var(--border)] bg-[var(--surface)]"></div>
    </div>

    <div v-else-if="subscriptions.length" class="grid gap-3">
      <article
        v-for="sub in subscriptions"
        :key="sub.id"
        class="rounded-3xl border border-[var(--border)] bg-[var(--card-bg)] p-4 sm:p-5"
      >
        <div class="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div class="min-w-0 flex-1">
            <div class="flex min-w-0 items-center gap-2">
              <span class="size-2 shrink-0 rounded-full" :class="sub.valid ? 'bg-emerald-500' : 'bg-red-400'"></span>
              <h3 class="truncate text-sm font-semibold text-[var(--text-primary)]" :title="sub.title">{{ sub.title }}</h3>
            </div>
            <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--text-tertiary)]">
              <span>{{ sub.channel_count }} 个频道</span>
              <span v-if="sub.last_tested">测速于 {{ formatTime(sub.last_tested) }}</span>
              <span v-else-if="sub.last_updated">更新于 {{ formatTime(sub.last_updated) }}</span>
              <span v-if="sub.custom_ua">自定义 UA</span>
              <span v-if="sub.force_proxy">强制中转</span>
            </div>
          </div>
          <div class="scrollbar-hide flex max-w-full gap-1 overflow-x-auto sm:justify-end">
            <button type="button" class="min-h-10 shrink-0 rounded-xl px-3 text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)]" @click="handleRefresh(sub)">刷新</button>
            <button type="button" class="min-h-10 shrink-0 rounded-xl px-3 text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)]" @click="handleTestSub(sub)">测速</button>
            <button type="button" class="min-h-10 shrink-0 rounded-xl px-3 text-xs font-medium text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--border-strong)]" @click="openExportDialog">导出</button>
            <button type="button" class="min-h-10 shrink-0 rounded-xl px-3 text-xs font-medium text-red-500 hover:bg-red-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400" @click="handleDelete(sub)">删除</button>
          </div>
        </div>
      </article>
    </div>

    <section v-else class="rounded-3xl border border-dashed border-[var(--border-strong)] bg-[var(--surface)] px-6 py-12 text-center">
      <span class="mx-auto flex size-11 items-center justify-center rounded-2xl border border-[var(--border)] bg-[var(--bg-soft)] text-[var(--text-secondary)]">
        <svg class="size-5" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
      </span>
      <h3 class="mt-4 text-sm font-semibold text-[var(--text-primary)]">还没有直播源</h3>
      <p class="mt-2 text-sm leading-6 text-[var(--text-secondary)]">在上方粘贴 M3U/M3U8 地址即可开始。</p>
    </section>
  </section>

  <Teleport to="body">
    <div v-if="exportDialogOpen"
      class="fixed inset-0 z-[80] flex items-center justify-center bg-neutral-950/35 px-4 py-8 backdrop-blur-md"
      @click.self="closeExportDialog">
      <section
        ref="dialogRef"
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-dialog-title"
        tabindex="-1"
        class="max-h-full w-full max-w-xl overflow-y-auto rounded-3xl border border-white/60 bg-white/95 p-6 shadow-2xl shadow-neutral-950/20 dark:border-white/10 dark:bg-neutral-900/95"
        @keydown.esc.prevent="closeExportDialog">
        <div class="mb-5 relative flex items-center justify-center">

          <div class="text-center">
            <h2 id="export-dialog-title" class="text-lg font-semibold text-neutral-900 dark:text-neutral-50">订阅导出</h2>
          </div>

          <button type="button"
            class="absolute right-0 flex size-8 shrink-0 items-center justify-center rounded-full text-neutral-500 transition-colors hover:bg-neutral-100 dark:hover:bg-neutral-800"
            aria-label="关闭" @click="closeExportDialog">
            <svg class="size-4" viewBox="0 0 24 24" fill="none">
              <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
            </svg>
          </button>

        </div>

        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <button v-for="mode in exportModes" :key="mode.id" type="button"
            class="group relative rounded-2xl border border-neutral-200 bg-white p-4 text-center transition-all hover:-translate-y-0.5 hover:border-neutral-400 hover:shadow-lg active:translate-y-0 dark:border-neutral-700 dark:bg-neutral-900 dark:hover:border-neutral-500"
            @click="copySubscriptionUrl(mode.id)">
            <span v-if="mode.badge"
              class="absolute right-3 top-3 rounded-full border border-neutral-200 px-2 py-0.5 text-[0.65rem] font-medium text-neutral-500 dark:border-neutral-700 dark:text-neutral-400">
              {{ mode.badge }}
            </span>
            <span
              class="mx-auto mb-3 flex size-9 items-center justify-center rounded-full bg-neutral-100 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100">
              <svg v-if="mode.id === 'hybrid'" class="size-5" viewBox="0 0 24 24" fill="none">
                <path
                  d="M10 13a5 5 0 0 0 7.1 0l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1M14 11a5 5 0 0 0-7.1 0l-2 2A5 5 0 0 0 12 20.1l1.1-1.1"
                  stroke="currentColor" stroke-width="2" stroke-linecap="round" />
              </svg>
              <svg v-else-if="mode.id === 'direct'" class="size-5" viewBox="0 0 24 24" fill="none">
                <path d="m13 2-8 12h6l-1 8 8-12h-6l1-8Z" stroke="currentColor" stroke-width="2"
                  stroke-linejoin="round" />
              </svg>
              <svg v-else-if="mode.id === 'proxy'" class="size-5" viewBox="0 0 24 24" fill="none">
                <path
                  d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM3.6 9h16.8M3.6 15h16.8M12 3c2.2 2.4 3.3 5.4 3.3 9S14.2 18.6 12 21M12 3c-2.2 2.4-3.3 5.4-3.3 9s1.1 6.6 3.3 9"
                  stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
              </svg>
              <svg v-else class="size-5" viewBox="0 0 24 24" fill="none">
                <path
                  d="M12 3l1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3ZM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z"
                  stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
              </svg>
            </span>
            <h3 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">{{ mode.title }}</h3>
            <p class="mt-1 text-xs text-neutral-500 dark:text-neutral-400">{{ mode.subtitle }}</p>
            <!-- <p class="mt-3 text-[0.7rem] text-neutral-400 dark:text-neutral-500">{{ mode.detail }}</p> -->
            <p v-if="copiedMode === mode.id" class="mt-3 text-xs font-medium text-emerald-500">已复制</p>
          </button>
        </div>

        <div
          class="mt-5 rounded-2xl border border-neutral-200 bg-neutral-50/80 p-3 dark:border-neutral-800 dark:bg-neutral-950/40">
          <button type="button"
            class="flex w-full items-center justify-between text-left text-xs font-medium text-neutral-700 dark:text-neutral-300"
            @click="advancedOpen = !advancedOpen">
            高级选项
            <svg class="size-4 transition-transform" :class="{ 'rotate-180': advancedOpen }" viewBox="0 0 24 24"
              fill="none">
              <path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round"
                stroke-linejoin="round" />
            </svg>
          </button>
          <div v-if="advancedOpen" class="mt-3 space-y-3">
            <div class="grid grid-cols-1 gap-2 text-xs text-neutral-600 dark:text-neutral-300 sm:grid-cols-2">
              <label class="flex items-center gap-2"><input v-model="exportOptions.healthyOnly" type="checkbox"
                  class="size-3.5 rounded accent-neutral-950 dark:accent-white" />只导出可用源</label>
              <label class="flex items-center gap-2"><input v-model="exportOptions.includeEpg" type="checkbox"
                  class="size-3.5 rounded accent-neutral-950 dark:accent-white" />附带 EPG ID</label>
              <label class="flex items-center gap-2"><input v-model="exportOptions.includeLogo" type="checkbox"
                  class="size-3.5 rounded accent-neutral-950 dark:accent-white" />附带台标</label>
            </div>
            <input v-model="exportOptions.groups" type="text" placeholder="分组过滤，用逗号分隔，例如：央视,卫视"
              class="w-full rounded-xl border border-neutral-200 bg-white px-3 py-2 text-xs text-neutral-700 outline-none focus:border-neutral-400 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200" />
          </div>
        </div>
        <div class="my-6 flex items-center justify-center">
          <div class="h-px flex-grow bg-neutral-200 dark:bg-neutral-800"></div>
          <span class="px-3 text-sm font-medium text-neutral-400 font-semibold">支持客户端</span>
          <div class="h-px flex-grow bg-neutral-200 dark:bg-neutral-800"></div>
        </div>

        <div class="mb-4 flex flex-wrap items-center justify-center gap-3">
          <div
            class="flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-xs text-neutral-600 shadow-sm dark:border-neutral-800 dark:bg-neutral-900/50 dark:text-neutral-300">
            <svg class="size-5 text-[#FD335B]" fill="currentColor" role="img" viewBox="0 0 160 160"
              xmlns="http://www.w3.org/2000/svg">
              <title>Icon</title>
              <g transform="translate(0, 160) scale(0.1, -0.1)" stroke="none">
                <path fill-rule="evenodd"
                  d="M976 1280 c211 -17 275 -47 323 -149 25 -55 26 -61 26 -256 -1 -224 -12 -292 -67 -377 -64 -102 -230 -166 -475 -184 -104 -7 -257 8 -327 33 -140 51 -194 201 -183 507 6 169 18 231 60 293 39 57 126 108 219 128 77 16 252 19 424 5z M553 1205 c-79 -18 -114 -36 -147 -76 -67 -79 -78 -139 -66 -353 6 -97 15 -180 24 -199 16 -40 77 -89 129 -106 47 -16 363 -15 421 0 81 21 152 116 182 246 24 102 24 283 0 357 -22 66 -68 119 -112 130 -62 15 -367 16 -431 1z" />
                <path
                  d="M761 979 c109 -71 149 -106 149 -130 0 -16 -148 -164 -197 -196 -28 -19 -69 -10 -78 16 -10 34 -25 184 -25 263 0 68 3 79 22 92 12 9 25 16 29 16 4 0 49 -27 100 -61z" />
                <path
                  d="M1180 1065 c-13 -35 -13 -231 0 -256 14 -25 46 -24 60 1 6 12 9 71 8 147 l-3 128 -27 3 c-22 3 -30 -2 -38 -23z" />
                <path d="M1185 720 c-35 -39 6 -95 43 -58 17 17 15 65 -4 72 -21 8 -18 9 -39 -14z" />
              </g>
            </svg>
            <span class="font-bold text-black-600">APTV</span>
          </div>
          <div
            class="flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-xs text-neutral-600 shadow-sm dark:border-neutral-800 dark:bg-neutral-900/50 dark:text-neutral-300">
            <svg class="size-5 text-blue-500" fill="currentColor" role="img" viewBox="0 0 256 256"
              xmlns="http://www.w3.org/2000/svg">
              <title>TiviMate</title>
              <g transform="translate(0, 256) scale(0.1, -0.1)" stroke="none">
                <path
                  d="M550 1765 l0 -155 -50 0 -50 0 0 -100 0 -100 49 0 49 0 4 -262 c3 -245 5 -266 25 -310 12 -26 33 -63 48 -82 57 -74 212 -125 342 -112 64 7 168 39 178 56 3 5 -11 51 -31 104 l-36 94 -34 -14 c-43 -18 -117 -18 -152 0 -53 27 -57 48 -57 295 l0 226 118 3 117 3 0 99 0 100 -120 0 -120 0 0 155 0 155 -140 0 -140 0 0 -155z" />
                <path
                  d="M1883 1478 c-23 -51 -76 -173 -118 -270 -43 -98 -80 -178 -84 -178 -3 0 -80 63 -171 141 -91 77 -197 167 -237 200 l-72 60 19 -43 c10 -24 91 -200 178 -393 l160 -350 140 -3 140 -3 20 58 c11 32 59 164 107 293 47 129 107 291 132 360 25 69 54 146 64 173 l19 47 -129 0 -128 0 -40 -92z" />
              </g>
            </svg>
            <span class="font-bold text-black-500">TiviMate</span>
          </div>
          <div
            class="flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-xs text-neutral-600 shadow-sm dark:border-neutral-800 dark:bg-neutral-900/50 dark:text-neutral-300">
            <svg class="size-4 text-orange-500" fill="currentColor" role="img" viewBox="0 0 24 24"
              xmlns="http://www.w3.org/2000/svg">
              <title>VLC media player</title>
              <path
                d="M12.0319 0c-.8823 0-1.0545.136-1.0545.136-.1738.056-.3556.255-.4105.43L9.683 3.3808c.4729.1729 1.3222.4266 2.2337.4266 1.0987 0 2.017-.3494 2.3763-.5075L13.4352.566c-.055-.1755-.237-.3707-.4067-.4374 0 0-.1142-.1286-.9966-.1286zm3.5645 7.455c-.3601.34-1.3276.9373-3.6797.9373-2.2929 0-3.189-.5678-3.5213-.9113l-1.3887 4.4227c.2272.3614 1.2539 1.5594 4.8847 1.5594 3.7569 0 4.8539-1.3467 5.0649-1.6737zm-8.5897 4.4487l-1.0025 3.1922H4.3428c-.2486 0-.5097.1932-.5826.4315l-2.334 7.6317a.3962.3962 0 0 0-.0169.1537c-.0008.0053-.002.0099-.002.016 0 .0839.0233.226.0233.226.0322.2456.2612.4452.5098.4452h20.1192c.2487 0 .4768-.1994.5098-.4453 0 0 .0234-.142.0234-.226a.0245.0245 0 0 0-.0025-.01.3201.3201 0 0 0 .0024-.0313.4096.4096 0 0 0-.019-.1282l-2.3339-7.6318c-.0729-.2383-.334-.4314-.5826-.4314h-1.6636l.2005.6391c-.2407.4854-1.4886 2.38-6.3027 2.38-4.6003 0-5.8288-1.73-6.1107-2.3072z" />
            </svg>
            <span class="font-bold text-black-500">VLC</span>
          </div>
          <div
            class="flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-xs text-neutral-600 shadow-sm dark:border-neutral-800 dark:bg-neutral-900/50 dark:text-neutral-300">
            <svg class="size-4" viewBox="0 0 205 256" xmlns="http://www.w3.org/2000/svg">
              <title>PotPlayer</title>
              <path fill="#fff"
                d="m27.76,245.86c-9.66,0-17.52-7.86-17.52-17.51V27.71c0-9.66,7.86-17.51,17.52-17.51,3.47,0,6.84,1.04,9.75,3l149.04,100.31c4.84,3.26,7.73,8.68,7.73,14.52s-2.89,11.26-7.73,14.52L37.51,242.86c-2.91,1.96-6.28,3-9.75,3h0Z" />
              <path fill="#2ea4ff"
                d="m27.76,19.7c1.5,0,3.04.43,4.44,1.38l149.04,100.31c4.71,3.17,4.71,10.1,0,13.27L32.2,234.98c-1.41.95-2.94,1.38-4.44,1.38-4.16,0-8.02-3.31-8.02-8.01V27.71c0-4.71,3.86-8.01,8.02-8.01m0-19C12.86.7.74,12.82.74,27.71v200.63c0,14.9,12.12,27.01,27.02,27.01,5.36,0,10.57-1.6,15.05-4.61l149.04-100.31c7.47-5.03,11.92-13.4,11.92-22.4,0-9-4.46-17.37-11.92-22.4L42.81,5.32c-4.49-3.02-9.69-4.61-15.05-4.61h0Z" />
            </svg>
            <span class="font-bold text-black-500">PotPlayer</span>
          </div>
          <div
            class="flex items-center gap-1.5 rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-xs text-neutral-600 shadow-sm dark:border-neutral-800 dark:bg-neutral-900/50 dark:text-neutral-300">
            <svg class="size-4 text-emerald-500" fill="currentColor" role="img" viewBox="0 0 24 24"
              xmlns="http://www.w3.org/2000/svg">
              <title>Emby</title>
              <path
                d="M11.041 0c-.007 0-1.456 1.43-3.219 3.176L4.615 6.352l.512.513.512.512-2.819 2.791L0 12.961l1.83 1.848c1.006 1.016 2.438 2.46 3.182 3.209l1.351 1.359.508-.496c.28-.273.515-.498.524-.498.008 0 1.266 1.264 2.794 2.808L12.97 24l.187-.182c.23-.225 5.007-4.95 5.717-5.656l.52-.516-.502-.513c-.276-.282-.5-.52-.496-.53.003-.009 1.264-1.26 2.802-2.783 1.538-1.522 2.8-2.776 2.803-2.785.005-.012-3.617-3.684-6.107-6.193L17.65 4.6l-.505.505c-.279.278-.517.501-.53.497-.013-.005-1.27-1.267-2.793-2.805A449.655 449.655 0 0011.041 0zM9.223 7.367c.091.038 7.951 4.608 7.957 4.627.003.013-1.781 1.056-3.965 2.32a999.898 999.898 0 01-3.996 2.307c-.019.006-.026-1.266-.026-4.629 0-3.7.007-4.634.03-4.625Z" />
            </svg>
            <span class="font-bold text-black-500">Emby</span>
          </div>
        </div>
        <p class="mt-4 text-center text-xs text-neutral-400 dark:text-neutral-500">
          不知道选什么？<a href="https://bgm.gs/" target="_blank" class="text-blue-500 hover:underline">点我</a>
        </p>
        <p v-if="copyError" class="mt-2 text-center text-xs text-red-500">{{ copyError }}</p>
      </section>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  addSubscription,
  cancelTest,
  deleteSubscription,
  fetchGlobalTestStatus,
  fetchSubscriptions,
  refreshAllSubscriptions,
  refreshSubscription,
  testAllChannels,
  testAllGlobal,
} from '../../api/iptv'
import { API_BASE } from '../../apiBase'
import { useToastStore } from '../../stores/toast'

const toastStore = useToastStore()
const subscriptions = ref([])
const loading = ref(false)
const loadError = ref('')
const addUrl = ref('')
const addUa = ref('')
const addForceProxy = ref(false)
const addError = ref('')
const addLoading = ref(false)
const refreshRunning = ref(false)
const testRunning = ref(false)
const emptyTestProgress = (total = 0) => ({ total, tested: 0, working: 0, failed: 0, not_live: 0, untested: 0, phase: '', current: '', cancelled: false })
const testProgress = ref(emptyTestProgress())
const testPercent = computed(() => testProgress.value.total ? Math.min(100, Math.round((testProgress.value.tested / testProgress.value.total) * 100)) : 0)
const exportDialogOpen = ref(false)
const dialogRef = ref(null)
const advancedOpen = ref(false)
const copiedMode = ref('')
const copyError = ref('')
const exportOptions = ref({ healthyOnly: true, includeEpg: true, includeLogo: true, groups: '' })
const exportModes = [
  { id: 'smart', title: 'Smart', subtitle: '每频道一条智能链接，自动选择直连或代理', badge: '推荐' },
  { id: 'hybrid', title: '混合', subtitle: '每个源只输出直连或代理之一', badge: '兼容' },
  { id: 'proxy', title: '代理', subtitle: '所有频道使用代理', badge: '' },
  { id: 'direct', title: '直链', subtitle: '只输出真正可直连的源', badge: '' },
]
let testTimer = null

async function loadSubscriptions() {
  loading.value = true
  loadError.value = ''
  try {
    subscriptions.value = await fetchSubscriptions()
  } catch (error) {
    loadError.value = error?.message || '暂时无法读取直播源'
  } finally {
    loading.value = false
  }
}

async function handleAdd() {
  const url = addUrl.value.trim()
  if (!url || addLoading.value) return
  addLoading.value = true
  addError.value = ''
  try {
    await addSubscription(url, '', addUa.value.trim(), addForceProxy.value)
    addUrl.value = ''
    addUa.value = ''
    addForceProxy.value = false
    await loadSubscriptions()
    toastStore.success('直播源已添加')
  } catch (error) {
    addError.value = error?.message || '添加直播源失败'
  } finally {
    addLoading.value = false
  }
}

async function handleDelete(sub) {
  const confirmed = await toastStore.askConfirm({ message: `确定删除「${sub.title}」？`, confirmText: '删除', danger: true })
  if (!confirmed) return
  try {
    await deleteSubscription(sub.id)
    await loadSubscriptions()
    toastStore.success('已删除')
  } catch (error) {
    toastStore.error(`删除失败: ${error.message}`)
  }
}

async function handleRefresh(sub) {
  try {
    await refreshSubscription(sub.id)
    await loadSubscriptions()
    toastStore.success('直播源已刷新')
  } catch (error) {
    toastStore.error(`刷新失败: ${error.message}`)
  }
}

async function handleRefreshAll() {
  if (refreshRunning.value) return
  refreshRunning.value = true
  try {
    const result = await refreshAllSubscriptions()
    await loadSubscriptions()
    if (result.failed) toastStore.warning(`已刷新 ${result.updated || 0} 个订阅，${result.failed} 个失败`)
    else toastStore.success(`已刷新 ${result.updated || 0} 个订阅`)
  } catch (error) {
    toastStore.error(`全部刷新失败: ${error.message}`)
  } finally {
    refreshRunning.value = false
  }
}

async function handleTestSub(sub) {
  if (testRunning.value) return toastStore.warning('已有测速任务正在进行中')
  try {
    const result = await testAllChannels(sub.id)
    startTestPolling(result.total || 0)
  } catch (error) {
    toastStore.error(`测速失败: ${error.message}`)
  }
}

async function handleTestAll() {
  if (testRunning.value) return
  try {
    const result = await testAllGlobal()
    startTestPolling(result.total || 0)
  } catch (error) {
    toastStore.error(`启动测速失败: ${error.message}`)
  }
}

function startTestPolling(total) {
  testRunning.value = true
  testProgress.value = emptyTestProgress(total)
  if (testTimer) clearInterval(testTimer)
  testTimer = setInterval(pollTestStatus, 1000)
}

async function handleCancelTest() {
  try {
    await cancelTest()
    testProgress.value = { ...testProgress.value, cancelled: true, phase: 'cancelled' }
  } catch (error) {
    toastStore.error(`取消测速失败: ${error.message}`)
    return
  }
  stopTestPolling()
}

async function pollTestStatus() {
  try {
    const status = await fetchGlobalTestStatus()
    testProgress.value = { ...emptyTestProgress(), ...status }
    if (status.cancelled || (status.tested >= status.total && status.total > 0)) stopTestPolling()
  } catch {}
}

function stopTestPolling() {
  testRunning.value = false
  if (testTimer) clearInterval(testTimer)
  testTimer = null
}

function formatTime(value) {
  if (!value) return ''
  try { return new Date(value).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }) } catch { return value }
}

function openExportDialog() {
  copyError.value = ''
  copiedMode.value = ''
  exportDialogOpen.value = true
  nextTick(() => dialogRef.value?.focus())
}

function closeExportDialog() {
  exportDialogOpen.value = false
}

function buildSubscriptionUrl(mode) {
  const params = new URLSearchParams()
  params.set('mode', mode)
  params.set('healthy_only', exportOptions.value.healthyOnly ? '1' : '0')
  params.set('include_epg', exportOptions.value.includeEpg ? '1' : '0')
  params.set('include_logo', exportOptions.value.includeLogo ? '1' : '0')
  const groups = exportOptions.value.groups.trim()
  if (groups) params.set('groups', groups)
  return `${API_BASE}/api/iptv/subscription.m3u?${params.toString()}`
}

function fallbackCopyText(text) {
  const element = document.createElement('textarea')
  element.value = text
  element.setAttribute('readonly', '')
  element.style.position = 'fixed'
  element.style.opacity = '0'
  document.body.appendChild(element)
  element.select()
  const copied = document.execCommand('copy')
  element.remove()
  if (!copied) throw new Error('复制失败')
}

async function copySubscriptionUrl(mode) {
  const url = buildSubscriptionUrl(mode)
  copyError.value = ''
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(url)
    else fallbackCopyText(url)
    copiedMode.value = mode
    window.setTimeout(() => { if (copiedMode.value === mode) copiedMode.value = '' }, 1800)
  } catch (error) {
    try {
      fallbackCopyText(url)
      copiedMode.value = mode
    } catch {
      copyError.value = error?.message || '复制失败，请手动复制链接'
    }
  }
}

onMounted(loadSubscriptions)
onBeforeUnmount(() => { if (testTimer) clearInterval(testTimer) })
</script>
