// Shared "click outside" handling for expanding search / popover surfaces.
//
// TV and Radio search used to have no outside handling at all: once expanded
// they stayed open until the trigger was clicked again.  This keeps one
// implementation for both so the two pages cannot drift apart.
import { onBeforeUnmount, watch } from 'vue'

/**
 * @param {import('vue').Ref<HTMLElement|null>} targetRef element that counts as "inside"
 * @param {() => void} onOutside called when a pointerdown lands outside the target
 * @param {object} [options]
 * @param {import('vue').Ref<boolean>} [options.enabled] only listen while true
 * @param {() => boolean} [options.shouldClose] extra guard; return false to keep it open
 */
export function useClickOutside(targetRef, onOutside, options = {}) {
  const { enabled = null, shouldClose = null } = options
  let listening = false

  function isInside(node) {
    const target = targetRef?.value
    if (!target || !node) return false
    return target === node || target.contains(node)
  }

  function handler(event) {
    if (isInside(event.target)) return
    if (shouldClose && !shouldClose()) return
    onOutside()
  }

  function start() {
    if (listening) return
    // pointerdown fires before focus changes, so clicking the trigger itself
    // never double-toggles.
    document.addEventListener('pointerdown', handler, true)
    listening = true
  }

  function stop() {
    if (!listening) return
    document.removeEventListener('pointerdown', handler, true)
    listening = false
  }

  if (enabled) {
    watch(enabled, (value) => (value ? start() : stop()), { immediate: true })
  } else {
    start()
  }

  onBeforeUnmount(stop)

  return { start, stop }
}
