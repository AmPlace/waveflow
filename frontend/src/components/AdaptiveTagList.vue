<template>
  <div
    ref="rootRef"
    class="adaptive-tag-list"
    :class="{ 'is-measured': measured }"
    :style="{ '--adaptive-tag-gap': `${gap}px` }"
  >
    <!-- 真实可见的子项。组件外部传入插槽 #tag 渲染单个 chip。 -->
    <span
      v-for="(item, idx) in visibleItems"
      :key="`v-${item.key ?? item.label ?? idx}`"
      class="adaptive-tag-list__cell"
    >
      <slot name="tag" :item="item" :index="idx">
        <span class="market-tag" :class="item.accentClass">{{ item.label }}</span>
      </slot>
    </span>
    <span v-if="overflowCount > 0" class="adaptive-tag-list__cell">
      <slot name="more" :count="overflowCount">
        <span class="market-tag market-tag-rest">+{{ overflowCount }}</span>
      </slot>
    </span>

    <!-- 隐藏的测量层：宽度 = 容器宽度，离屏渲染所有项与 +N 占位，用于读取每项 offsetWidth。
         visibility:hidden 而不是 display:none，否则 layout 不会发生，测不到尺寸。 -->
    <div ref="measureRef" class="adaptive-tag-list__measure" aria-hidden="true">
      <span
        v-for="(item, idx) in items"
        :key="`m-${item.key ?? item.label ?? idx}`"
        class="adaptive-tag-list__cell"
      >
        <slot name="tag" :item="item" :index="idx">
          <span class="market-tag" :class="item.accentClass">{{ item.label }}</span>
        </slot>
      </span>
      <span ref="moreSampleRef" class="adaptive-tag-list__cell">
        <slot name="more" :count="overflowSampleCount">
          <span class="market-tag market-tag-rest">+{{ overflowSampleCount }}</span>
        </slot>
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  items: { type: Array, required: true },
  // Market cards use one row; the component keeps this configurable for other callers.
  maxRows: { type: Number, default: 1 },
  // 行内项之间的间距（与 CSS gap 相同），用来计算累计宽度。
  gap: { type: Number, default: 6 },
})

const rootRef = ref(null)
const measureRef = ref(null)
const moreSampleRef = ref(null)

// 计算后真正渲染的子项数量。初始 0，等到第一次有效测量后才显示。
const visibleCount = ref(0)
// 是否已经完成第一次有效测量。容器宽度为 0（如隐藏页面）时保持 false。
const measured = ref(false)

// +N 永远基于完整 items 数量计算，不受测量进度影响——保证 +N 始终准确。
const visibleItems = computed(() => props.items.slice(0, visibleCount.value))
const overflowCount = computed(() => Math.max(0, props.items.length - visibleCount.value))
const overflowSampleCount = computed(() => {
  return Math.max(props.items.length, 1)
})

// 同宽度 + 同 items 签名的连续两次回调短路掉，避免 ResizeObserver 反复触发布局抖动。
let lastMeasuredWidth = -1
let lastLayoutSignature = ''

function itemsSignature() {
  // 只包含影响测量结果的字段：label + accentClass。order 也算入是因为 slot 渲染顺序变化会改变测量结果。
  return props.items.map((it, i) => `${i}:${it?.label ?? ''}|${it?.accentClass ?? ''}`).join('§')
}

function readWidths() {
  const root = measureRef.value
  if (!root) return { itemWidths: [], moreWidth: 0 }
  const cells = root.querySelectorAll(':scope > .adaptive-tag-list__cell')
  const all = Array.from(cells)
  const sampleEl = moreSampleRef.value
  const moreWidth = sampleEl ? sampleEl.getBoundingClientRect().width : 0
  const itemWidths = all
    .filter(el => el !== sampleEl)
    .map(el => el.getBoundingClientRect().width)
  return { itemWidths, moreWidth }
}

// 模拟 flex-wrap：按顺序逐行塞 chip，超出当前行就换行；最多 maxRows 行。
// 若放不下全部，在最后一行尾部留出 +N 宽度，必要时回退最后一行的项直到能容下。
function recompute({ force = false } = {}) {
  const root = rootRef.value
  if (!root) return
  const containerWidth = root.clientWidth

  // 容器宽度为 0（页面隐藏 / 还未布局完成 / display:none 父级）→ 维持未测量状态，不显示任何项。
  if (containerWidth <= 0) {
    measured.value = false
    visibleCount.value = 0
    return
  }

  const sig = itemsSignature()
  if (!force && containerWidth === lastMeasuredWidth && sig === lastLayoutSignature && measured.value) {
    return
  }

  const { itemWidths, moreWidth } = readWidths()
  if (!itemWidths.length) {
    visibleCount.value = 0
    measured.value = true
    lastMeasuredWidth = containerWidth
    lastLayoutSignature = sig
    return
  }

  const gap = props.gap
  const maxRows = Math.max(1, props.maxRows | 0)
  const total = itemWidths.length

  // 第一遍：尝试在 maxRows 行内塞下尽量多的项，不预留 +N。
  const rowOf = []
  let row = 0
  let used = 0
  let placed = 0
  for (let i = 0; i < total; i += 1) {
    const w = itemWidths[i]
    const candidate = used === 0 ? w : used + gap + w
    if (candidate <= containerWidth) {
      used = candidate
      rowOf.push(row)
      placed += 1
      continue
    }
    if (row + 1 >= maxRows) break
    row += 1
    used = w
    rowOf.push(row)
    placed += 1
  }

  if (placed >= total) {
    visibleCount.value = total
  } else {
    // 需要 +N：从最后一行回退，直到 +N 能放进当前最后一行尾部。
    function lastRowUsed() {
      if (!rowOf.length) return 0
      const lastRow = rowOf[rowOf.length - 1]
      let acc = 0
      for (let i = rowOf.length - 1; i >= 0; i -= 1) {
        if (rowOf[i] !== lastRow) break
        acc = acc === 0 ? itemWidths[i] : acc + gap + itemWidths[i]
      }
      return acc
    }

    while (rowOf.length > 0 && lastRowUsed() + gap + moreWidth > containerWidth) {
      rowOf.pop()
    }
    visibleCount.value = rowOf.length
  }

  measured.value = true
  lastMeasuredWidth = containerWidth
  lastLayoutSignature = sig
}

let observer = null
let rafId = 0

function schedule() {
  // 合并相同 tick 的多次触发；nextTick 保证测量层 DOM 已更新到最新 props。
  if (rafId) return
  rafId = 1
  nextTick(() => {
    rafId = 0
    recompute()
  })
}

onMounted(() => {
  schedule()
  if (typeof window !== 'undefined' && 'ResizeObserver' in window) {
    observer = new ResizeObserver(() => schedule())
    if (rootRef.value) observer.observe(rootRef.value)
  } else if (typeof window !== 'undefined') {
    window.addEventListener('resize', schedule)
  }
})

onBeforeUnmount(() => {
  if (observer) {
    observer.disconnect()
    observer = null
  } else if (typeof window !== 'undefined') {
    window.removeEventListener('resize', schedule)
  }
})

// items / maxRows / gap 变化都强制重新测量（签名变化也会让 recompute 接受新结果）。
watch(() => props.items, () => {
  lastLayoutSignature = ''
  schedule()
}, { deep: false })
watch(() => props.maxRows, () => {
  lastLayoutSignature = ''
  schedule()
})
watch(() => props.gap, () => {
  lastLayoutSignature = ''
  schedule()
})
</script>

<style scoped>
.adaptive-tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--adaptive-tag-gap, 6px);
  position: relative;
  /* 首次有效测量前隐藏真实子项，避免"全部 Tag 一闪后变少"。
     visibility 而不是 display 或 v-if：测量层依赖容器仍然占位才能拿到 clientWidth。
     卡片侧 .market-card-tags 自带 min-height 撑住高度，未测量也不会让卡片塌陷。 */
  visibility: hidden;
}

.adaptive-tag-list.is-measured {
  visibility: visible;
}

.adaptive-tag-list__cell {
  display: inline-flex;
}

/* 测量层：和外层等宽，但不可见也不参与布局影响（visibility:hidden + position:absolute
   让真实子项不被推开）。仍走正常 flex-wrap，因此每个 chip 的 offsetWidth 反映真实渲染宽度。
   注意：测量层一直保持 visibility:hidden，与外层 .adaptive-tag-list.is-measured 的状态无关——
   外层切到 visible 时，子级 .adaptive-tag-list__measure 仍然显式 hidden，不会闪现。 */
.adaptive-tag-list__measure {
  position: absolute;
  inset: 0;
  display: flex;
  flex-wrap: wrap;
  gap: var(--adaptive-tag-gap, 6px);
  visibility: hidden;
  pointer-events: none;
  /* 不能被外部限高遮挡，否则项会被裁掉测不准；让它纵向自然延伸但仍隐藏。 */
  height: auto;
  overflow: visible;
}
</style>
