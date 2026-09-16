// Ratios in this module are always expressed as width / height.
// Keeping the convention here avoids using a visual ratio as a height multiplier.
export const IPTV_CARD_RATIOS = Object.freeze({
  STANDARD_VISUAL: 16 / 9,
  COMPACT: 1.85,
})

export function heightForWidth(width, widthHeightRatio, footerHeight = 0) {
  const cardWidth = Number(width)
  const ratio = Number(widthHeightRatio)
  const footer = Number(footerHeight) || 0
  if (!Number.isFinite(cardWidth) || cardWidth <= 0) return footer
  if (!Number.isFinite(ratio) || ratio <= 0) return footer
  return cardWidth / ratio + footer
}
