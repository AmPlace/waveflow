<template>
  <div ref="rootRef" class="tag-filter-row relative">
    <!-- 原地多行 wrap：只动画容器高度，标签内容不再做额外的进出场动画 -->
    <div
      class="flex flex-wrap items-start gap-2 overflow-hidden"
      :style="{ height: expanded ? `${Math.max(expandedHeight, 44)}px` : '44px', transition: 'height 300ms cubic-bezier(0.4, 0, 0.2, 1)', willChange: 'height' }"
      @transitionend="onTransitionEnd"
    >
      <button
        v-for="(item, index) in displayItems"
        :key="`t-${itemKey(item, index)}`"
        type="button"
        class="tag-filter-row__item h-[var(--chip-hit-height)] shrink-0 rounded-full border px-5 text-sm font-medium transition-colors"
        :class="[pillClass(isActive(item)), { 'tag-filter-row__item--selected': isActive(item) }]"
        @click="$emit('select', item)"
      >
        {{ itemLabel(item) }}
      </button>

      <button
        v-if="hasOverflow"
        type="button"
        class="tag-filter-row__more inline-flex h-[var(--chip-hit-height)] shrink-0 items-center gap-1 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)]"
        :aria-expanded="expanded || collapsing"
        :aria-label="expanded || collapsing ? '收起标签' : '展开全部标签'"
        @click="toggle"
      >
        {{ expanded || collapsing ? '收起' : '更多' }}
        <svg
          class="size-4 transition-transform duration-200"
          :class="{ 'rotate-180': expanded || collapsing }"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
          <path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  items: { type: Array, required: true },
  modelValueKey: { type: [String, Number, null], default: null },
  labelOf: { type: Function, default: (item) => (typeof item === 'object' ? item.label ?? item.name ?? String(item) : String(item)) },
  keyOf: { type: Function, default: (item) => (typeof item === 'object' ? item.key ?? item.value ?? item.id ?? item.name ?? String(item) : String(item)) },
  isActive: { type: Function, required: true },
})

const emit = defineEmits(['select'])

const rootRef = ref(null)
const expanded = ref(false)
const collapsing = ref(false)
const visibleCount = ref(props.items.length)
const expandedHeight = ref(0)

function itemLabel(item) { return props.labelOf(item) }
function itemKey(item, index) {
  try { return props.keyOf(item) ?? index } catch { return index }
}

function pillClass(active) {
  return active
    ? 'border-[var(--text-primary)] bg-[var(--text-primary)] text-[var(--bg)]'
    : 'border-[var(--border)] bg-[var(--surface)] text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)]'
}

const visibleItems = computed(() => props.items.slice(0, visibleCount.value))
const hasOverflow = computed(() => visibleCount.value < props.items.length)
const hiddenSelectedItem = computed(() => {
  if (!hasOverflow.value) return null
  const hidden = props.items.slice(visibleCount.value)
  return hidden.find((it) => props.isActive(it)) || null
})
// 折叠态把隐藏的当前选中项作为稳定的末尾 chip 保留，避免 Transition
// 在收起完成后再插入一个 chip，造成闪回或额外的布局跳动。
const collapsedItems = computed(() => (
  hiddenSelectedItem.value
    ? [...visibleItems.value, hiddenSelectedItem.value]
    : visibleItems.value
))
const displayItems = computed(() => (expanded.value ? props.items : collapsedItems.value))

/* 测量：在不可见的克隆容器里逐个累加按钮宽度，得出一行能放下多少个，
   再为「更多」按钮（必要时还有隐藏选中 chip）预留宽度。
   只算 visibleCount，不在折叠态测 expanded 高度——后者会因 DOM 仍渲染截断项而误测，
   交给 watch(expanded) 在展开后再测。 */
function measure() {
  const root = rootRef.value
  if (!root) return
  const totalWidth = root.clientWidth
  if (!totalWidth) return

  const probe = document.createElement('div')
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;left:-9999px;top:0;display:flex;gap:8px;'
  document.body.appendChild(probe)

  // 复刻按钮样式以测准宽度
  const sample = (text) => {
    const b = document.createElement('button')
    b.className = 'h-[var(--chip-hit-height)] shrink-0 rounded-full border px-5 text-sm font-medium'
    b.textContent = text
    return b
  }

  // 1) 先量出每一项的宽度
  const widths = props.items.map((it) => {
    const node = sample(itemLabel(it))
    probe.appendChild(node)
    return node.getBoundingClientRect().width
  })

  document.body.removeChild(probe)

  const gap = 8
  const moreBtn = 96 // 「更多 ▾」按钮的预估宽度

  // 2) 找出当前选中项的索引（用于判断折叠后是否需要额外预留 chip 空间）
  const selectedIndex = props.items.findIndex((it) => props.isActive(it))

  // 3) 逐个累加可见项宽度。已知"超出范围 + 选中项被截"时还要再放一个 chip，
  //    所以候选 visibleCount 还得满足 selected chip 也能塞进剩余宽度。
  let used = 0
  let count = 0
  for (let i = 0; i < props.items.length; i += 1) {
    const w = widths[i]
    const next = used + (count > 0 ? gap : 0) + w
    const remaining = props.items.length - i - 1
    // 是否需要为「更多」按钮预留位置（还有未显示项时需要）
    const needMore = remaining > 0
    // 折叠后，selected 是否落在隐藏区——若是，要再为选中 chip 留位置
    const needSelChip = needMore && selectedIndex > i
    let reserve = 0
    if (needMore) reserve += moreBtn + gap
    if (needSelChip) reserve += widths[selectedIndex] + gap
    const ceiling = totalWidth - reserve
    if (next > ceiling) break
    used = next
    count = i + 1
  }

  visibleCount.value = Math.max(1, count || props.items.length)

  // 同时用离屏 flex-wrap 容器量出展开后的真实高度
  const wrapProbe = document.createElement('div')
  wrapProbe.style.cssText = `position:absolute;visibility:hidden;pointer-events:none;left:-9999px;top:0;display:flex;flex-wrap:wrap;gap:8px;width:${totalWidth}px;`
  const sampleBtn = (text) => {
    const b = document.createElement('button')
    b.className = 'h-[var(--chip-hit-height)] shrink-0 rounded-full border px-5 text-sm font-medium'
    b.textContent = text
    return b
  }
  for (const it of props.items) {
    wrapProbe.appendChild(sampleBtn(itemLabel(it)))
  }
  document.body.appendChild(wrapProbe)
  expandedHeight.value = wrapProbe.getBoundingClientRect().height
  document.body.removeChild(wrapProbe)
}

let resizeObserver = null
onMounted(() => {
  nextTick(measure)
  if (typeof ResizeObserver !== 'undefined' && rootRef.value) {
    resizeObserver = new ResizeObserver(() => measure())
    resizeObserver.observe(rootRef.value)
  }
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
})

watch(() => props.items, () => nextTick(measure), { deep: false })

function toggle() {
  if (expanded.value) {
    // 收起时立即切换到稳定的折叠内容，更多按钮保持在同一 flex gap 中。
    collapsing.value = true
    expanded.value = false
  } else {
    expanded.value = true
  }
}

function onTransitionEnd(e) {
  if (e.propertyName === 'height' && collapsing.value) {
    collapsing.value = false
  }
}
</script>
