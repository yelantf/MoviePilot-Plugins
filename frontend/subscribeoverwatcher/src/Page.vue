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
    <div v-for="(node, index) in page" :key="index" class="mb-3 overflow-x-auto">
      <schema-node :node="node" :on-select-candidate="selectCandidate" />
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { CANDIDATE_SELECTION_EVENT, queueCandidateSelection } from './candidateSelection'
import SchemaNode from './schemaNode'

const PLUGIN_ID = 'SubscribeOverwatcher'
const props = defineProps({
  api: { type: [Object, Function], required: true },
  embedded: { type: Boolean, default: false },
})
const emit = defineEmits(['switch', 'select-candidate'])
const page = ref([])
const loading = ref(false)
const error = ref('')

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
