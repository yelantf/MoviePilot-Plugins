<template>
  <div class="pa-3 pa-sm-4">
    <div class="d-flex align-center mb-4 ga-2">
      <span class="text-subtitle-1">剧集守门员</span>
      <v-spacer />
      <v-btn variant="tonal" color="primary" :loading="loading" @click="refreshPage">刷新</v-btn>
      <v-btn variant="tonal" @click="emit('switch')">{{ embedded ? '返回设置' : '配置' }}</v-btn>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
    <v-progress-linear v-if="loading && !page.length" indeterminate color="primary" />
    <div v-for="block in blocks" :key="block.key" class="mb-3">
      <div v-if="block.kind === 'node'" class="overflow-x-auto">
        <schema-node :node="block.node" :on-select-candidate="selectCandidate" />
      </div>
      <div v-else class="section-card" :data-subscription-id="block.kind === 'subscription' ? block.meta.id : undefined">
        <button type="button" class="section-control" :aria-expanded="isExpanded(block.key)"
          @click="toggleSection(block.key)">
          <span aria-hidden="true">{{ isExpanded(block.key) ? '▾' : '▸' }}</span>
          <span class="section-title">{{ block.meta.title }}</span>
          <span v-if="block.kind === 'subscription'" class="text-caption text-medium-emphasis">
            {{ block.meta.status }}<template v-if="block.meta.status === '待确认'"> · 候选 {{ block.meta.candidate_count }} 条 / 展示 {{ block.meta.row_count }} 行</template>
          </span>
          <span v-if="block.meta.warning" class="text-caption text-warning">扫描有异常</span>
        </button>
        <div v-if="isExpanded(block.key)" class="section-body">
          <div v-if="block.kind === 'subscription' && hasSortableRows(block)" class="sort-toolbar">
            <label :for="'sort-' + block.meta.id">排序字段</label>
            <select :id="'sort-' + block.meta.id" :aria-label="block.meta.title + ' 排序字段'"
              class="sort-select" :value="sortField(block.meta.id)"
              @change="setSortField(block.meta.id, $event.target.value)">
              <option v-for="field in SORT_FIELDS" :key="field.value" :value="field.value">{{ field.title }}</option>
            </select>
            <button v-if="sortField(block.meta.id) !== 'original'" type="button" class="sort-direction"
              :aria-label="block.meta.title + ' 排序方向'" @click="toggleSortDirection(block.meta.id)">
              {{ sortDescending(block.meta.id) ? '降序' : '升序' }}
            </button>
          </div>
          <div v-for="(node, index) in block.nodes" :key="index" class="mb-3 overflow-x-auto">
            <schema-node :node="sortedNode(node, block.meta.id)" :on-select-candidate="selectCandidate" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { CANDIDATE_SELECTION_EVENT, queueCandidateSelection } from './candidateSelection'
import SchemaNode from './schemaNode'

const PLUGIN_ID = 'SubscribeOverwatcher'
const SORT_FIELDS = [
  { value: 'original', title: '原顺序（全集优先）' },
  { value: 'last_seen', title: '最后发现时间' },
  { value: 'seeders', title: '做种人数' },
  { value: 'size_bytes', title: '资源大小' },
  { value: 'title', title: '资源名称' },
  { value: 'site', title: '站点' },
]
const props = defineProps({
  api: { type: [Object, Function], required: true },
  embedded: { type: Boolean, default: false },
})
const emit = defineEmits(['switch', 'select-candidate'])
const page = ref([])
const loading = ref(false)
const error = ref('')
const expanded = ref(new Set())
const sortSettings = ref({})

const blocks = computed(() => {
  const result = []
  for (const [index, node] of page.value.entries()) {
    if (node?._subscription?.id != null) {
      const key = 'subscription:' + node._subscription.id
      const previous = result[result.length - 1]
      if (previous?.key === key) previous.nodes.push(node)
      else result.push({ kind: 'subscription', key, meta: node._subscription, nodes: [node] })
    } else if (node?._overview) {
      result.push({ kind: 'overview', key: 'overview', meta: node._overview, nodes: [node] })
    } else {
      result.push({ kind: 'node', key: 'node:' + index, node })
    }
  }
  return result
})

function isExpanded(key) {
  return expanded.value.has(key)
}

function toggleSection(key) {
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

function hasSortableRows(block) {
  return block.nodes.some(node => node._candidate_table &&
    node.content?.some(part => part.component === 'tbody' && part.content?.length > 1))
}

function sortField(id) {
  return sortSettings.value[id]?.field || 'original'
}

function sortDescending(id) {
  return sortSettings.value[id]?.descending ?? true
}

function setSortField(id, field) {
  sortSettings.value = { ...sortSettings.value,
    [id]: { field, descending: field !== 'title' && field !== 'site' } }
}

function toggleSortDirection(id) {
  sortSettings.value = { ...sortSettings.value,
    [id]: { field: sortField(id), descending: !sortDescending(id) } }
}

function sortValue(row, field) {
  const value = row._sort?.[field]
  if (field === 'last_seen') {
    const timestamp = Date.parse(value)
    return Number.isFinite(timestamp) ? timestamp : null
  }
  if (field === 'seeders' || field === 'size_bytes') {
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function sortedNode(node, id) {
  const field = sortField(id)
  if (!node._candidate_table || field === 'original') return node
  const descending = sortDescending(id)
  return { ...node, content: node.content.map(part => {
    if (part.component !== 'tbody') return part
    const rows = (part.content || []).map((row, index) => ({ row, index }))
    rows.sort((left, right) => {
      const a = sortValue(left.row, field)
      const b = sortValue(right.row, field)
      if (a == null && b == null) return left.index - right.index
      if (a == null) return 1
      if (b == null) return -1
      const order = typeof a === 'number' ? a - b : a.localeCompare(b, 'zh-CN', { numeric: true })
      return (descending ? -order : order) || left.index - right.index
    })
    return { ...part, content: rows.map(item => item.row) }
  }) }
}

function selectCandidate(candidateId) {
  if (props.embedded) {
    emit('select-candidate', candidateId)
    return
  }
  if (!queueCandidateSelection(candidateId)) {
    error.value = '无法暂存所选版本，请在配置页手动选择'
    return
  }
  emit('switch')
  window.dispatchEvent(new window.Event(CANDIDATE_SELECTION_EVENT))
}

async function refreshPage() {
  loading.value = true
  error.value = ''
  try {
    const response = await props.api.get(`plugin/page/${PLUGIN_ID}`)
    const data = response?.data?.page ? response.data : response
    page.value = data?.page || []
  } catch (caught) {
    error.value = caught?.message || '读取插件详情失败'
  } finally {
    loading.value = false
  }
}

onMounted(refreshPage)
</script>

<style scoped>
.section-card {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  overflow: hidden;
}

.section-control {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
  padding: 12px 14px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.section-control:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: -2px;
}

.section-title {
  font-weight: 600;
}

.section-body {
  padding: 12px;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.sort-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.sort-select, .sort-direction {
  padding: 5px 8px;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 6px;
  background: transparent;
  color: inherit;
}

.sort-direction {
  cursor: pointer;
}
</style>
