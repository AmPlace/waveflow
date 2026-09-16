import { defineStore } from 'pinia'



let _idSeq = 0
const nextId = () => ++_idSeq

const DEFAULT_DURATION = 3000
const ERROR_DURATION = 4000

export const useToastStore = defineStore('toast', {
  state: () => ({
    // 每项：{ id, type: 'info'|'success'|'warning'|'error', message, duration }
    toasts: [],
    // null 或 { id, title?, message, confirmText, cancelText, danger, resolve }
    confirm: null,
  }),

  actions: {
    // ── toast ────────────────────────────────────────────────────
    push({ type = 'info', message, duration }) {
      if (!message) return null
      const id = nextId()
      const ttl = duration ?? (type === 'error' ? ERROR_DURATION : DEFAULT_DURATION)
      this.toasts.push({ id, type, message: String(message), duration: ttl })
      if (ttl > 0) {
        setTimeout(() => this.dismiss(id), ttl)
      }
      return id
    },

    info(message, duration) {
      return this.push({ type: 'info', message, duration })
    },
    success(message, duration) {
      return this.push({ type: 'success', message, duration })
    },
    warning(message, duration) {
      return this.push({ type: 'warning', message, duration })
    },
    error(message, duration) {
      return this.push({ type: 'error', message, duration })
    },

    dismiss(id) {
      const idx = this.toasts.findIndex((t) => t.id === id)
      if (idx >= 0) this.toasts.splice(idx, 1)
    },

    clearToasts() {
      this.toasts.splice(0, this.toasts.length)
    },

    askConfirm({ message, title = '', confirmText = '确定', cancelText = '取消', danger = false } = {}) {
      if (this.confirm) {
        // 已有确认框在展示：直接把旧的 resolve 成 false，再开新的。
        try { this.confirm.resolve(false) } catch { /* ignore */ }
        this.confirm = null
      }
      return new Promise((resolve) => {
        this.confirm = {
          id: nextId(),
          title: title ? String(title) : '',
          message: String(message || ''),
          confirmText: String(confirmText),
          cancelText: String(cancelText),
          danger: Boolean(danger),
          resolve,
        }
      })
    },

    resolveConfirm(value) {
      const c = this.confirm
      if (!c) return
      this.confirm = null
      try { c.resolve(Boolean(value)) } catch { /* ignore */ }
    },

    // 点遮罩 / 按 ESC：等价于取消
    dismissConfirm() {
      this.resolveConfirm(false)
    },
  },
})
