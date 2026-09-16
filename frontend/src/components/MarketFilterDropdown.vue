<template>
  <div ref="rootRef" class="market-dropdown" :class="{ 'is-open': open }">
    <button
      ref="triggerRef"
      type="button"
      class="market-dropdown-trigger"
      :class="{ 'has-selection': highlightSelection && selectedCount > 0 }"
      :aria-expanded="open"
      @click.stop="onTriggerClick"
    >
      <span>{{ triggerLabel }}</span>
      <span v-if="multi && showCountBadge && selectedCount > 0" class="market-dropdown-count">{{ selectedCount }}</span>
      <svg class="size-3.5 transition-transform" :class="{ 'rotate-180': open }" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>

    <Teleport to="body">
      <transition name="market-dropdown-pop">
        <div
          v-if="open"
          ref="menuRef"
          class="market-dropdown-menu market-dropdown-teleported"
          :class="`market-dropdown-menu--${menuWidthMode}`"
          :style="menuStyle"
          @click.stop
        >
          <ul class="market-dropdown-list">
            <li v-for="opt in options" :key="opt.key">
              <button
                type="button"
                class="market-dropdown-item"
                :class="{ 'is-selected': isSelected(opt.key) }"
                @click="onPick(opt.key)"
              >
                <span class="truncate">{{ opt.label }}</span>
                <svg v-if="isSelected(opt.key)" class="size-3.5 shrink-0" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>
            </li>
            <li v-if="!options.length" class="px-3 py-2 text-[12px] text-[var(--text-tertiary)]">无可用选项</li>
          </ul>
          <div v-if="multi && selectedCount > 0" class="market-dropdown-foot">
            <button type="button" class="text-[12px] text-[var(--text-secondary)] hover:text-[var(--text-primary)]" @click="clearAll">清除选择</button>
          </div>
        </div>
      </transition>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  options: { type: Array, default: () => [] },
  selected: { type: Array, default: () => [] },
  multi: { type: Boolean, default: true },
  showCountBadge: { type: Boolean, default: true },
  // 单选时若希望按钮显示当前所选项的 label 而不是 group label
  displayAsCurrent: { type: Boolean, default: false },
  // 是否在有选中值时显示黑色激活态。排序之类的中性控件应当传 false。
  highlightSelection: { type: Boolean, default: true },
  // 受控开关：父级管理 open 状态。未传则使用内部状态。
  open: { type: Boolean, default: null },
  // 父级用于区分多个 Dropdown 的 key。会在 toggle/close 事件中回传。
  dropdownKey: { type: [String, Number], default: '' },
  // 菜单尺寸模式：
  //   normal  —— 常规筛选（地区、运营商、标签等），最小 200px、最大 280px；
  //   compact —— 排序之类的短选项，宽度按内容自适应，最小至少与 trigger 同宽。
  menuWidthMode: {
    type: String,
    default: 'normal',
    validator: (v) => ['normal', 'compact'].includes(v),
  },
})

const emit = defineEmits(['change', 'toggle', 'close'])

const rootRef = ref(null)
const triggerRef = ref(null)
const menuRef = ref(null)
const internalOpen = ref(false)
const menuStyle = ref({
  position: 'fixed',
  top: '0px',
  left: '0px',
  minWidth: '200px',
})

const isControlled = computed(() => props.open !== null)
const open = computed(() => (isControlled.value ? props.open : internalOpen.value))

const selectedCount = computed(() => (props.selected || []).length)

const triggerLabel = computed(() => {
  if (props.displayAsCurrent && !props.multi && props.selected?.length) {
    const cur = props.options.find(o => o.key === props.selected[0])
    if (cur) return cur.label
  }
  return props.label
})

function isSelected(key) {
  return (props.selected || []).includes(key)
}

function onTriggerClick() {
  if (isControlled.value) {
    emit('toggle', props.dropdownKey)
  } else {
    internalOpen.value = !internalOpen.value
  }
}

function close() {
  if (isControlled.value) {
    emit('close', props.dropdownKey)
  } else {
    internalOpen.value = false
  }
}

function onPick(key) {
  if (props.multi) {
    const cur = new Set(props.selected || [])
    if (cur.has(key)) cur.delete(key)
    else cur.add(key)
    emit('change', Array.from(cur))
  } else {
    emit('change', [key])
    close()
  }
}

function clearAll() {
  emit('change', [])
  close()
}

function onDocClick(e) {
  if (!open.value) return
  const root = rootRef.value
  const menu = menuRef.value
  if (root && root.contains(e.target)) return
  if (menu && menu.contains(e.target)) return
  close()
}

function onKey(e) {
  if (e.key === 'Escape' && open.value) close()
}

// 计算菜单 fixed 定位，根据触发按钮 rect。
function updateMenuPosition() {
  if (!open.value) return
  const trigger = triggerRef.value
  if (!trigger) return
  const rect = trigger.getBoundingClientRect()
  const vpW = window.innerWidth
  const vpH = window.innerHeight
  const gap = 6
  // 估算菜单尺寸：实际菜单尚未挂或刚挂。优先用 menuRef 的 offsetWidth/Height；否则用回退值。
  const menu = menuRef.value
  // compact：宽度由内容决定，最小至少等于 trigger，初次估算时只取 trigger 宽度。
  // normal：保留原先 200px 最小估算，避免初次定位偏窄。
  const isCompact = props.menuWidthMode === 'compact'
  const fallbackW = isCompact ? rect.width : Math.max(rect.width, 200)
  const menuW = menu?.offsetWidth || fallbackW
  const menuH = menu?.offsetHeight || 200
  const minW = Math.max(rect.width, isCompact ? 0 : 200)

  // 默认按钮下方左对齐
  let left = rect.left
  let top = rect.bottom + gap

  // 右侧溢出 → 改右对齐
  if (left + menuW > vpW - 8) {
    left = Math.max(8, rect.right - menuW)
  }
  // 左侧溢出
  if (left < 8) left = 8

  // 下方空间不足且上方更宽裕 → 显示在按钮上方
  const spaceBelow = vpH - rect.bottom - gap
  const spaceAbove = rect.top - gap
  if (spaceBelow < menuH && spaceAbove > spaceBelow) {
    top = Math.max(8, rect.top - gap - menuH)
  }

  menuStyle.value = {
    position: 'fixed',
    top: `${Math.round(top)}px`,
    left: `${Math.round(left)}px`,
    // compact：用 trigger 宽度作为 min-width，菜单本身宽度由 max-content 接管；
    // normal：min-width 至少 200px（最大值仍由 CSS 限制 280px）。
    minWidth: `${Math.round(minW || rect.width)}px`,
  }
}

function onScrollOrResize() {
  if (!open.value) return
  updateMenuPosition()
}

watch(open, (val) => {
  if (val) {
    nextTick(() => {
      updateMenuPosition()
      // 第二次：菜单 DOM 此时已渲染，用真实尺寸再修一次。
      requestAnimationFrame(updateMenuPosition)
    })
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
  } else {
    window.removeEventListener('scroll', onScrollOrResize, true)
    window.removeEventListener('resize', onScrollOrResize)
  }
})

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('scroll', onScrollOrResize, true)
  window.removeEventListener('resize', onScrollOrResize)
})
</script>

<style scoped>
.market-dropdown {
  position: relative;
  display: inline-block;
}

.market-dropdown-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  height: var(--chip-height);
  padding: 0 20px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  transition: background-color 160ms ease, border-color 160ms ease;
}

.market-dropdown-trigger:hover {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}

.market-dropdown.is-open .market-dropdown-trigger {
  border-color: var(--border-strong);
  background: var(--surface-strong);
}

.market-dropdown-trigger.has-selection {
  border-color: var(--text-primary);
  background: var(--text-primary);
  color: var(--bg);
}

.market-dropdown-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--text-primary);
  color: var(--bg);
  font-size: 10.5px;
  font-weight: 600;
}

.market-dropdown-trigger.has-selection .market-dropdown-count {
  background: var(--bg);
  color: var(--text-primary);
}

</style>

<style>
/* 全局样式（非 scoped），因为菜单 Teleport 到 body，scoped 选择器无法命中。 */
.market-dropdown-menu.market-dropdown-teleported {
  position: fixed;
  z-index: 110;
  padding: 6px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--bg-soft);
  box-shadow: 0 14px 32px rgba(15, 23, 42, 0.10);
}

/* normal：常规筛选菜单，固定较宽。 */
.market-dropdown-menu--normal {
  max-width: 280px;
}

/* compact：排序之类的短选项，宽度按内容自适应；最小宽度由 inline style 的
   min-width（trigger 实际宽度）保证；最大宽度避免超出视口。 */
.market-dropdown-menu--compact {
  width: max-content;
  max-width: min(280px, calc(100vw - 16px));
}

.dark .market-dropdown-menu.market-dropdown-teleported {
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.48);
}

.market-dropdown-menu .market-dropdown-list {
  max-height: 288px;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
}

.market-dropdown-menu .market-dropdown-item {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 8px;
  background: transparent;
  color: var(--text-primary);
  font-size: 12.5px;
  text-align: left;
  transition: background-color 140ms ease;
}

.market-dropdown-menu .market-dropdown-item:hover {
  background: var(--surface-hover);
}

.market-dropdown-menu .market-dropdown-item.is-selected {
  background: var(--surface-active);
  font-weight: 600;
}

.market-dropdown-menu .market-dropdown-foot {
  margin-top: 4px;
  padding: 6px 10px 4px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
}

.market-dropdown-pop-enter-active,
.market-dropdown-pop-leave-active {
  transition: opacity 140ms ease, transform 140ms ease;
}

.market-dropdown-pop-enter-from,
.market-dropdown-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
