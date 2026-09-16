import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import postcss from 'postcss'

const source = readFileSync(new URL('../../src/views/MarketView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const css = postcss.parse(descriptor.styles[0].content)

function declarations(selector, media = null) {
  const values = {}
  css.walkRules(selector, rule => {
    const condition = rule.parent.type === 'atrule' ? rule.parent.params : null
    if (condition !== media) return
    rule.walkDecls(decl => { values[decl.prop] = decl.value })
  })
  return values
}

test('Market reuses TV material with wider package-specific desktop columns', () => {
  const card = declarations('.market-card')
  assert.equal(card.padding, 'var(--card-padding)')
  assert.equal(card['border-radius'], 'var(--card-radius)')
  assert.equal(card['box-shadow'], 'var(--iptv-card-shadow)')
  assert.equal(declarations('.market-grid').gap, 'var(--card-gap)')
  assert.equal(declarations('.market-grid', '(min-width: 1024px)')['grid-template-columns'], 'repeat(3, minmax(0, 1fr))')
  assert.equal(declarations('.market-grid', '(min-width: 1280px)')['grid-template-columns'], 'repeat(4, minmax(0, 1fr))')
  assert.equal(declarations('.market-grid', '(min-width: 1600px)')['grid-template-columns'], 'repeat(5, minmax(0, 1fr))')
  assert.equal(declarations('.market-grid', '(max-width: 640px)')['grid-template-columns'], 'minmax(0, 1fr)')
  assert.equal(declarations('.market-card', '(max-width: 640px)')['border-radius'], '14px')
})

test('desktop cards share a stable height and bottom-aligned full-width actions', () => {
  assert.equal(declarations('.market-grid', '(min-width: 641px)')['grid-auto-rows'], '1fr')
  assert.equal(declarations('.market-card', '(min-width: 641px)')['min-height'], '176px')
  for (const selector of ['.market-card', '.market-card-body']) {
    const style = declarations(selector)
    assert.equal(style['min-height'], undefined)
    assert.equal(style.height, undefined)
    assert.equal(style.flex, undefined)
  }
  const footer = declarations('.market-card-foot')
  assert.equal(footer['border-top'], undefined)
  assert.equal(footer['margin-top'], 'auto')
  assert.equal(footer['justify-content'], 'space-between')
  assert.equal(footer.width, '100%')
  assert.equal(footer['flex-shrink'], '0')
  assert.equal(footer['padding-top'], undefined)
})

test('neutral tags and overflow share a light borderless presentation', () => {
  const tag = declarations('.market-tag')
  assert.equal(tag.border, '0')
  assert.equal(tag.height, '22px')
  assert.equal(tag['font-weight'], '400')
  assert.equal(tag.background, 'color-mix(in srgb, var(--surface-active) 75%, transparent)')
  assert.equal(tag.color, 'color-mix(in srgb, var(--text-secondary) 80%, var(--text-primary))')
  assert.equal(declarations('.market-tag-rest').color, undefined)
  assert.equal(declarations('.market-tag-neutral').color, undefined)
  assert.equal(declarations('.market-tag-rest').background, undefined)
  assert.equal(declarations('.market-tag-rest').border, undefined)
  assert.equal(declarations('.market-tag-neutral').background, undefined)
  assert.match(descriptor.template.content, /class="market-tag market-tag-rest"/)
})

test('only normal install action is demoted; update keeps its primary treatment', () => {
  assert.match(descriptor.template.content, /class="market-btn-ghost market-card-install"[\s\S]*?@click\.stop="handleImport\(pkg\)"/)
  assert.match(descriptor.template.content, /class="market-btn-primary"[\s\S]*?@click\.stop="handleReinstall\(pkg\)"/)
  assert.equal(declarations('.market-filter-scroll').flex, '0 1 auto')
  assert.equal(declarations('.market-more-filter-anchor').position, 'relative')
})

test('final polish preserves text geometry and aligns toolbar controls', () => {
  const title = declarations('.market-card-title')
  assert.equal(title['font-size'], '15px')
  assert.equal(title['font-weight'], '600')
  assert.equal(title['line-height'], '21px')
  const metric = declarations('.market-card-scale')
  assert.equal(metric.color, 'var(--iptv-card-label-primary)')
  assert.equal(metric['font-weight'], '400')
  assert.equal(metric['margin-top'], '4px')
  assert.equal(declarations('.market-card-body').gap, '10px')
  assert.equal(declarations('.market-filter-scroll')['padding-bottom'], undefined)
  assert.equal(declarations('.market-filter-bar').gap, '8px')
  assert.equal(declarations('.market-list-actions').gap, '8px')
})

test('card actions have a consistent disabled treatment and visible keyboard focus', () => {
  assert.equal(declarations('.market-card-foot > button:disabled').opacity, '0.5')
  assert.equal(declarations('.market-card-foot .market-btn-status:hover:not(:disabled)').background, 'var(--surface-hover)')
  const focus = declarations(':is(.market-card-foot > button, .market-more-btn, .market-package-type-button, .market-filter-pill, .market-more-filter-trigger, .market-action-pill, .market-action-icon):focus-visible')
  assert.equal(focus.outline, '2px solid var(--text-secondary)')
  assert.equal(focus['outline-offset'], '3px')
})
