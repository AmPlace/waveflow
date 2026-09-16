import { createRouter, createWebHashHistory } from 'vue-router'

import { installSessionExpiryHandling } from '../auth/sessionExpiry.js'
import { useAuthStore } from '../stores/auth'
import { appRoutes } from './routes'

const router = createRouter({
  history: createWebHashHistory(),
  routes: appRoutes,
})

installSessionExpiryHandling(router)

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.booted) {
    try {
      await auth.boot()
    } catch {
      auth.booted = true
      auth.status = 'offline'
    }
  }

  if (auth.requiresSetup && to.path !== '/setup') {
    return { path: '/setup' }
  }

  if (!auth.requiresSetup && to.path === '/setup') {
    return { path: '/radio' }
  }

  if (!to.meta.authPage && !auth.setup.anonymousBrowse && !auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  if (to.meta.requiresAdmin && !auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  if (to.path === '/login' && auth.requiresSetup) {
    return { path: '/setup' }
  }

  if (to.path === '/login' && auth.isAuthenticated) {
    return { path: String(to.query.redirect || '/radio') }
  }

  return true
})

export default router
