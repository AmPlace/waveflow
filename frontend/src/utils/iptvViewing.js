/**
 * Format the compact programme label used by IPTV Home cards.
 *
 * This intentionally differs from the shared viewing helper: the Home card
 * has limited horizontal space and only presents the current title. FullPlayer
 * and Radio keep their existing time-aware presentation.
 */
export function iptvCardProgrammeTitle(program, fallback = '') {
  const title = String(program?.title || '').trim()
  return title || String(fallback || '')
}
