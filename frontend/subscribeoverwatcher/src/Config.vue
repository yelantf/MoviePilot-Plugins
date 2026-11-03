<template>
  <div class="subscribe-overwatcher pa-3 pa-sm-4">
    <v-alert v-if="feedback.section === 'settings'" :type="feedback.type" variant="tonal"
      density="comfortable" class="mb-4">{{ feedback.text }}</v-alert>
    <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-4" />

    <template v-else>
      <v-card variant="outlined" class="mb-4">
        <v-card-title class="text-subtitle-1">基础设置</v-card-title>
        <v-card-text>
          <v-row>
            <v-col cols="12" sm="6">
              <v-switch v-model="form.enabled" label="接管新增剧集订阅" density="comfortable" hide-details />
            </v-col>
            <v-col cols="12" sm="6">
              <v-switch v-model="form.confirm_full_pack" label="全集种子也需确认" density="comfortable" hide-details />
            </v-col>
            <v-col cols="12" sm="6">
              <v-switch v-model="form.daily_notice" label="每天推送待确认订阅汇总" density="comfortable" hide-details />
            </v-col>
            <v-col cols="6" sm="3">
              <v-select v-model="form.notice_hour" label="通知小时" :items="options.notice_hour"
                density="comfortable" hide-details />
            </v-col>
            <v-col cols="6" sm="3">
              <v-select v-model="form.notice_minute" label="通知分钟" :items="options.notice_minute"
                density="comfortable" hide-details />
            </v-col>
          </v-row>
          <v-alert type="info" variant="tonal" density="compact" class="mt-3">
            开启全集确认后，新剧集暂停；关闭后仍拦截分集下载，只允许符合原规则的整季资源自动下载。
            E01 候选每 4 小时更新。
          </v-alert>
        </v-card-text>
        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" @click="emit('switch')">查看详情与预览结果</v-btn>
          <v-spacer />
          <v-btn color="primary" variant="tonal" :loading="busy === 'save'" :disabled="!!busy"
            @click="submit('save')">保存设置</v-btn>
        </v-card-actions>
      </v-card>

      <v-alert type="info" variant="tonal" density="compact" class="mb-4">{{ managedSummary }}</v-alert>

      <v-card variant="outlined" class="mb-4">
        <v-card-title class="text-subtitle-1">纳入历史剧集</v-card-title>
        <v-card-text>
          <v-autocomplete v-model="form.historical_subscription" label="选择历史剧集（可多选，按进度排序）"
            :items="options.historical_subscription" multiple chips closable-chips clearable
            density="comfortable" hide-details no-data-text="暂无可纳入管理的历史剧集订阅" />
          <v-alert v-if="feedback.section === 'history'" :type="feedback.type" variant="tonal"
            density="compact" class="mt-3">{{ feedback.text }}</v-alert>
        </v-card-text>
        <v-card-actions class="px-4 pb-4">
          <v-spacer />
          <v-btn color="primary" variant="tonal" :loading="busy === 'adopt'"
            :disabled="!!busy || !form.historical_subscription.length" @click="submit('adopt')">
            纳入所选剧集
          </v-btn>
        </v-card-actions>
      </v-card>

      <v-card variant="outlined" class="mb-4">
        <v-card-title class="text-subtitle-1">候选规则确认</v-card-title>
        <v-card-text>
          <v-select v-model="form.candidate" label="选择一个版本（全集优先）" :items="options.candidate"
            clearable density="comfortable" hide-details class="mb-4" />
          <v-textarea v-model="form.rule_json" label="草稿规则 JSON；留空使用候选规格"
            rows="3" auto-grow density="comfortable" hide-details />
          <v-alert v-if="feedback.section === 'candidate'" :type="feedback.type" variant="tonal"
            density="compact" class="mt-3">{{ feedback.text }}</v-alert>
        </v-card-text>
        <v-card-actions class="px-4 pb-4 flex-wrap ga-2">
          <v-spacer />
          <v-btn color="info" variant="tonal" :loading="busy === 'preview'"
            :disabled="!!busy || !form.candidate" @click="submit('preview')">预览匹配</v-btn>
          <v-btn color="primary" variant="tonal" :loading="busy === 'confirm'"
            :disabled="!!busy || !form.candidate" @click="submit('confirm')">确认并恢复</v-btn>
        </v-card-actions>
      </v-card>

      <v-row>
        <v-col cols="12" md="6">
          <v-card variant="outlined" class="mb-4 h-100">
            <v-card-title class="text-subtitle-1">直接批准</v-card-title>
            <v-card-text>
              <v-select v-model="form.direct_subscription" label="选择待确认订阅"
                :items="options.direct_subscription" clearable density="comfortable" hide-details />
              <v-alert v-if="feedback.section === 'approve'" :type="feedback.type" variant="tonal"
                density="compact" class="mt-3">{{ feedback.text }}</v-alert>
            </v-card-text>
            <v-card-actions class="px-4 pb-4">
              <v-spacer />
              <v-btn color="primary" variant="tonal" :loading="busy === 'direct_approve'"
                :disabled="!!busy || !form.direct_subscription" @click="submit('direct_approve')">
                批准并保留原规则
              </v-btn>
            </v-card-actions>
          </v-card>
        </v-col>
        <v-col cols="12" md="6">
          <v-card variant="outlined" class="mb-4 h-100">
            <v-card-title class="text-subtitle-1">撤销批准</v-card-title>
            <v-card-text>
              <v-select v-model="form.revoke_subscription" label="选择已批准订阅"
                :items="options.revoke_subscription" clearable density="comfortable" hide-details />
              <v-alert v-if="feedback.section === 'revoke'" :type="feedback.type" variant="tonal"
                density="compact" class="mt-3">{{ feedback.text }}</v-alert>
            </v-card-text>
            <v-card-actions class="px-4 pb-4">
              <v-spacer />
              <v-btn color="warning" variant="tonal" :loading="busy === 'revoke'"
                :disabled="!!busy || !form.revoke_subscription" @click="submit('revoke')">
                撤销并暂停
              </v-btn>
            </v-card-actions>
          </v-card>
        </v-col>
      </v-row>

      <v-card variant="outlined" class="mb-4">
        <v-card-title class="text-subtitle-1">回刷已入库集数</v-card-title>
        <v-card-text>
          <v-alert type="info" variant="tonal" density="compact" class="mb-4">
            先预览已批准订阅的当前规则，再选择资源和集数提交。插件不删除旧文件，最终替换由 MoviePilot 整理流程决定。
          </v-alert>
          <v-row>
            <v-col cols="12" md="8">
              <v-select v-model="form.replace_subscription" label="选择已批准订阅"
                :items="options.replace_subscription" clearable density="comfortable" hide-details />
            </v-col>
            <v-col cols="12" md="4" class="d-flex align-center justify-end">
              <v-btn color="info" variant="tonal" :loading="busy === 'preview_replace'"
                :disabled="!!busy || !form.replace_subscription" @click="submit('preview_replace')">
                预览可回刷资源
              </v-btn>
            </v-col>
            <v-col cols="12" md="8">
              <v-select v-model="form.replace_resource" label="选择预览通过的资源"
                :items="options.replace_resource" clearable density="comfortable" hide-details />
            </v-col>
            <v-col cols="12" md="4">
              <v-text-field v-model="form.replace_episodes" label="重新下载集数（如 1,3-5）"
                density="comfortable" hide-details />
            </v-col>
          </v-row>
          <v-alert v-if="feedback.section === 'replace'" :type="feedback.type" variant="tonal"
            density="compact" class="mt-3">{{ feedback.text }}</v-alert>
        </v-card-text>
        <v-card-actions class="px-4 pb-4">
          <v-spacer />
          <v-btn color="warning" variant="tonal" :loading="busy === 'submit_replace'"
            :disabled="!!busy || !form.replace_resource || !String(form.replace_episodes || '').trim()"
            @click="submit('submit_replace')">提交所选资源和集数</v-btn>
        </v-card-actions>
      </v-card>
    </template>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'

const PLUGIN_ID = 'SubscribeOverwatcher'
const fields = [
  'enabled', 'confirm_full_pack', 'daily_notice', 'notice_hour', 'notice_minute',
  'historical_subscription', 'candidate', 'rule_json', 'direct_subscription',
  'revoke_subscription', 'replace_subscription', 'replace_resource', 'replace_episodes',
]
const sections = {
  adopt: 'history', preview: 'candidate', confirm: 'candidate',
  direct_approve: 'approve', revoke: 'revoke',
  preview_replace: 'replace', submit_replace: 'replace',
}

const props = defineProps({
  api: { type: [Object, Function], required: true },
  initialConfig: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['switch'])

const form = reactive({
  enabled: false, confirm_full_pack: true, daily_notice: true,
  notice_hour: 9, notice_minute: 0, historical_subscription: [],
  candidate: '', rule_json: '', direct_subscription: '', revoke_subscription: '',
  replace_subscription: '', replace_resource: '', replace_episodes: '',
})
const options = reactive({
  notice_hour: [], notice_minute: [], historical_subscription: [], candidate: [],
  direct_subscription: [], revoke_subscription: [], replace_subscription: [], replace_resource: [],
})
const managedSummary = ref('正在读取已纳入管理的剧集…')
const loading = ref(true)
const busy = ref('')
const feedback = reactive({ section: '', type: 'info', text: '' })

watch(() => form.candidate, (candidate, previous) => {
  if (previous && candidate !== previous) form.rule_json = ''
})
watch(() => form.replace_subscription, (subscription, previous) => {
  if (previous && subscription !== previous) {
    form.replace_resource = ''
    form.replace_episodes = ''
  }
})

function walk(nodes, visit) {
  for (const node of nodes || []) {
    visit(node)
    walk(node.content, visit)
  }
}

function applySnapshot(snapshot) {
  const source = snapshot?.data?.conf ? snapshot.data : snapshot
  if (!source?.conf || !source?.model) throw new Error('配置数据未返回完整表单')
  const model = { ...props.initialConfig, ...source.model }
  for (const name of fields) {
    if (Object.prototype.hasOwnProperty.call(model, name)) form[name] = model[name]
  }
  if (!Array.isArray(form.historical_subscription)) {
    form.historical_subscription = form.historical_subscription ? [form.historical_subscription] : []
  }
  walk(source.conf, node => {
    const name = node.props?.model
    if (name && Object.prototype.hasOwnProperty.call(options, name)) {
      options[name] = node.props.items || []
    }
    const summary = node.props?.text
    if (typeof summary === 'string' &&
        (summary.startsWith('已纳入管理') || summary.startsWith('尚无已纳入管理'))) {
      managedSummary.value = summary
    }
  })
  return source.model._last_confirmation
}

async function refreshForm() {
  const snapshot = await props.api.get(`plugin/form/${PLUGIN_ID}`)
  return applySnapshot(snapshot)
}

function payload(action, requestId) {
  const values = {}
  for (const name of fields) values[name] = form[name]
  return { ...values, operation: action, operation_request_id: requestId }
}

async function submit(action) {
  if (busy.value) return
  busy.value = action
  feedback.section = ''
  feedback.text = ''
  const requestId = action === 'save' ? '' : `${Date.now()}-${Math.random().toString(36).slice(2)}`
  try {
    const rawResponse = await props.api.post(`plugin/${PLUGIN_ID}/apply`, payload(action, requestId))
    const response = rawResponse?.data?.success === undefined ? rawResponse : rawResponse.data
    const result = await refreshForm()
    feedback.section = sections[action] || 'settings'
    if (action !== 'save' && result?.request_id !== requestId) {
      feedback.type = 'error'
      feedback.text = '操作已提交，但未能确认结果，请查看插件详情页'
    } else {
      feedback.type = response?.success ? 'success' : 'error'
      feedback.text = response?.message || (response?.success ? '操作完成' : '操作失败')
    }
  } catch (error) {
    feedback.section = sections[action] || 'settings'
    feedback.type = 'error'
    feedback.text = error?.message || '操作失败，请稍后重试'
    try { await refreshForm() } catch { /* 保留当前输入供重试 */ }
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  try {
    await refreshForm()
  } catch (error) {
    feedback.section = 'settings'
    feedback.type = 'error'
    feedback.text = error?.message || '读取配置失败'
  } finally {
    loading.value = false
  }
})
</script>
