<template>
  <div class="pa-3 pa-sm-4">
    <div class="d-flex align-center mb-4 ga-2">
      <span class="text-subtitle-1">剧集守门员</span>
      <v-spacer />
      <v-btn variant="tonal" color="primary" :loading="loading" @click="refreshPage">刷新</v-btn>
      <v-btn variant="tonal" @click="emit('switch')">配置</v-btn>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
    <v-progress-linear v-if="loading && !page.length" indeterminate color="primary" />
    <div v-for="(node, index) in page" :key="index" class="mb-3 overflow-x-auto">
      <schema-node :node="node" :on-select-candidate="selectCandidate" />
    </div>
  </div>
</template>

<script setup>
import { defineComponent, h, onMounted, ref, resolveComponent } from 'vue'
import { CANDIDATE_SELECTION_EVENT, queueCandidateSelection } from './candidateSelection'

const PLUGIN_ID = 'SubscribeOverwatcher'
const props = defineProps({ api: { type: [Object, Function], required: true } })
const emit = defineEmits(['switch'])
const page = ref([])
const loading = ref(false)
const error = ref('')

function renderNode(node, onSelectCandidate) {
  const component = node.component.startsWith('V') ? resolveComponent(node.component) : node.component
  const children = (node.content || []).map(child => renderNode(child, onSelectCandidate))
  const candidateId = node.component === 'VBtn' && node.props?.['data-candidate-id']
  const nodeProps = candidateId
    ? { ...node.props, onClick: () => onSelectCandidate(candidateId) }
    : node.props || {}
  if (typeof component === 'string') {
    return h(component, nodeProps, children.length ? children : node.text || '')
  }
  return h(component, nodeProps, children.length ? { default: () => children } : undefined)
}

const SchemaNode = defineComponent({
  name: 'SchemaNode',
  props: {
    node: { type: Object, required: true },
    onSelectCandidate: { type: Function, required: true },
  },
  setup(nodeProps) {
    return () => renderNode(nodeProps.node, nodeProps.onSelectCandidate)
  },
})

function selectCandidate(candidateId) {
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
