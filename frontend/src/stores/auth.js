import { defineStore } from 'pinia'

import { fetchMe, fetchSetupStatus, initializeAdmin, login, logout } from '../api/auth.js'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    booted: false,
    status: 'booting',
    user: null,
    mode: 'nas',
    setup: {
      initialized: false,
      anonymousBrowse: true,
      anonymousPlayback: true,
    },
  }),
  getters: {
    isAuthenticated: (state) => Boolean(state.user),
    requiresSetup: (state) => state.status === 'setup-required',
  },
  actions: {
    async boot() {
      const setup = await fetchSetupStatus()
      this.mode = setup.mode || 'nas'
      this.setup = {
        initialized: Boolean(setup.initialized),
        anonymousBrowse: Boolean(setup.anonymous_browse),
        anonymousPlayback: Boolean(setup.anonymous_playback),
      }
      if (!this.setup.initialized && this.mode !== 'desktop') {
        this.status = 'setup-required'
        this.booted = true
        return
      }
      try {
        const me = await fetchMe()
        this.user = me.user || null
        this.status = this.mode === 'desktop' ? 'desktop' : 'authenticated'
      } catch {
        this.user = null
        this.status = this.setup.anonymousBrowse ? 'anonymous' : 'login-required'
      } finally {
        this.booted = true
      }
    },
    async initialize(payload) {
      const result = await initializeAdmin(payload)
      this.user = result.user || null
      this.setup.initialized = true
      this.status = 'authenticated'
      return result
    },
    async login(payload) {
      const result = await login(payload)
      this.user = result.user || null
      this.status = 'authenticated'
      return result
    },
    expireSession() {
      if (!this.user) return false
      this.user = null
      this.status = this.setup.anonymousBrowse ? 'anonymous' : 'login-required'
      return true
    },
    async logout() {
      await logout()
      this.user = null
      this.status = this.setup.anonymousBrowse ? 'anonymous' : 'login-required'
    },
  },
})
