import { setUnauthorizedHandler } from '../api/client.js'
import { useAuthStore } from '../stores/auth.js'

export function installSessionExpiryHandling(router) {
  setUnauthorizedHandler(() => {
    const auth = useAuthStore()
    if (!auth.expireSession()) return

    const current = router.currentRoute.value
    if (current.meta.authPage) return
    if (current.meta.requiresAdmin || !auth.setup.anonymousBrowse) {
      void router.replace({
        path: '/login',
        query: { redirect: current.fullPath },
      })
    }
  })
}
