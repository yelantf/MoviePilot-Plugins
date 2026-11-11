<template>
  <div class="subscribe-overwatcher">
    <div v-show="!dataPageVisible" class="pa-3 pa-sm-4">
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
              目标季候选每 4 小时更新。
            </v-alert>
          </v-card-text>
          <v-card-actions class="px-4 pb-4">
            <v-btn variant="text" @click="dataPageVisible = true">查看数据页面</v-btn>
            <v-spacer />
            <v-btn color="primary" variant="tonal" :loading="busy === 'save'" :disabled="!!busy"
              @click="submit('save')">保存设置</v-btn>
          </v-card-actions>
        </v-card>

        <v-alert type="info" variant="tonal" density="compact" class="mb-4">{{ managedSummary }}</v-alert>

        <v-card variant="outlined" class="mb-4">
          <v-card-title class="text-subtitle-1">手动扫描已纳入剧集</v-card-title>
          <v-card-text>
            <v-select v-model="form.scan_subscription" label="选择已纳入的待确认剧集"
              :items="options.scan_subscription" :disabled="!!busy" clearable density="comfortable"
              hide-details no-data-text="暂无已纳入管理的待确认剧集" />
            <p class="text-caption text-medium-emphasis mt-3 mb-0">
              没有候选的剧集也可选择。只重新搜索这一条剧集并更新版本列表，预览可复用扫描结果。
              各订阅独立扫描；失败仅影响本条订阅本轮，不对其他订阅设置失败冷却。请求仍遵守站点间隔和访问额度；已批准剧集可使用回刷资源预览。
            </p>
            <v-alert v-if="busy === 'scan'" type="info" variant="tonal" density="compact" class="mt-3"
              role="status" aria-live="polite">已触发所选剧集扫描；正在等待站点间隔、响应和资源解析，可能需要较长时间。</v-alert>
            <v-alert v-if="feedback.section === 'scan'" :type="feedback.type" variant="tonal"
              density="compact" class="mt-3">{{ feedback.text }}</v-alert>
          </v-card-text>
          <v-card-actions class="px-4 pb-4">
            <v-spacer />
            <v-btn color="info" variant="tonal" :loading="busy === 'scan'"
              :disabled="!!busy || !form.scan_subscription" @click="submit('scan')">扫描所选剧集</v-btn>
          </v-card-actions>
        </v-card>

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

        <v-card ref="candidateCard" variant="outlined" class="mb-4">
          <v-card-title class="text-subtitle-1">候选规则确认</v-card-title>
          <v-card-text>
            <v-alert v-if="handoffNotice" type="info" variant="tonal" density="compact" class="mb-4">
              {{ handoffNotice }}
            </v-alert>
            <v-row>
              <v-col cols="12" md="4">
                <v-select v-model="form.candidate_subscription" label="选择有版本的剧集"
                  :items="options.candidate_subscription" :disabled="!!busy" clearable density="comfortable" hide-details
                  no-data-text="暂无有版本的待确认剧集" @update:model-value="selectCandidateSubscription" />
              </v-col>
              <v-col cols="12" md="5">
                <v-select v-model="form.candidate" label="选择该剧集版本（全集优先）" :items="candidateChoices"
                  :disabled="!!busy || !form.candidate_subscription" clearable density="comfortable" hide-details
                  @update:model-value="selectCandidate" />
              </v-col>
              <v-col cols="12" md="3">
                <v-select v-model="form.rule_mode" label="站点范围" :items="ruleModes"
                  :disabled="!!busy" density="comfortable" hide-details @update:model-value="updateSiteScope" />
              </v-col>
            </v-row>
            <v-autocomplete v-if="form.rule_mode === 'custom'" v-model="form.rule_sites" label="自选站点（可多选）"
              :items="options.rule_sites" :filter-keys="['title']" multiple chips closable-chips clearable
              :disabled="!!busy || !selectedCandidate" density="comfortable" placeholder="输入站点名称筛选"
              no-data-text="没有匹配的站点" hint="输入名称可筛选站点；至少选一个，确认后写入订阅。"
              persistent-hint class="mt-3" @update:model-value="updateCustomSites" />
            <v-select v-model="form.keyword_choice" label="确认后的订阅搜索词"
              :items="keywordChoices" :disabled="!!busy || !selectedCandidate" density="comfortable"
              hint="剧名＋季号搜索词如“Show S01”；预览会验证它能否召回所选范围内符合规则的资源。"
              persistent-hint class="mt-3 mb-4" :class="{ 'preview-auto-changed': keywordPreviewAutoChanged }" />
            <p v-if="keywordPreviewAutoChanged" class="text-warning font-weight-bold mt-n2 mb-4" role="status">
              预览已自动切换搜索词为“使用剧名＋季号搜索词”；确认后才会修改订阅。
            </p>
            <p class="text-caption text-medium-emphasis mt-2 mb-4">
              候选比较带季查询与原订阅搜索，并收集包含目标季的合集；跳过全局搜索规则组及本订阅的站点和筛选规则，保留历史扫描版本。
              候选选择项和详情页均显示种子大小及精确字节数。跨站资源只在标题、字节大小、集数和解析规格一致时合并展示；这里仍逐站点选择。
              <span v-if="selectedCandidateUrl" class="ms-2">站点详情：</span>
              <a v-if="selectedCandidateUrl" :href="selectedCandidateUrl" target="_blank" rel="noopener noreferrer"
                class="text-primary text-decoration-underline">{{ selectedCandidateSite }}</a>
            </p>
            <div v-if="selectedCandidate" class="d-flex flex-wrap ga-2 mb-2" aria-live="polite">
              <v-chip size="small" variant="tonal">做种：{{ selectedCandidate.seeders_label || '未知' }}</v-chip>
              <v-chip size="small" variant="tonal">免费/促销：{{ selectedCandidate.promotion || '未知' }}</v-chip>
            </div>
            <p class="text-caption text-medium-emphasis mb-4">
              做种人数及免费/促销为最近搜索时的状态，重新搜索可更新；未提供数据的资源显示“未知”。
            </p>
            <v-select v-model="form.download_directory" label="下载目录（留空保留原目录）"
              :items="options.download_directory" :disabled="!!busy || !form.candidate_subscription" clearable
              density="comfortable" hint="确认只回填资源根目录；完整分类路径在预览中显示，实际下载由 MoviePilot 处理。"
              persistent-hint class="mb-4" />
            <v-alert v-if="form.download_directory && !selectedDownloadDirectory" type="warning"
              variant="tonal" density="compact" class="mb-4">所选下载目录已不在当前配置中，请重新选择并预览。</v-alert>
            <rule-editor v-if="selectedCandidate" v-model="form.rule_json" :candidate="selectedCandidate"
              :options="ruleOptions" :site-options="options.rule_sites" :site-mode="form.rule_mode"
              :selected-site-ids="form.rule_sites" :disabled="!!busy"
              :group-auto-changed="groupPreviewAutoChanged" />
            <p class="text-caption text-medium-emphasis mt-2 mb-0">
              可选候选站点、自选多个站点或不限站点；预览按所选范围搜索和筛选。
              搜索缓存持久保存，重启和更新后继续复用对应站点范围的最新结果；每次重新核对规则和下载目录，点击“重新搜索”获取新资源。
              默认沿用原订阅搜索方式；原搜索正常完成却未命中、剧名加季号查询命中时，预览会切换到剧名＋季号搜索词并标出改动。
              确认后的候选和确认预览保留 24 小时，期满后定期清理。
              默认预览应用原订阅或系统默认规则组；结果不符合预期时可选择“不使用规则组”。
            </p>
            <v-alert v-if="feedback.section === 'candidate'" :type="feedback.type" variant="tonal"
              density="compact" class="mt-3">{{ feedback.text }}</v-alert>
            <v-alert v-if="busy === 'preview'" type="info" variant="tonal" density="compact"
              class="mt-3" role="status" aria-live="polite">{{ previewProgress }}</v-alert>
          </v-card-text>
          <v-card-actions class="px-4 pb-4 flex-wrap ga-2">
            <v-spacer />
            <v-btn v-if="selectedPreview" variant="text" :disabled="!!busy || !selectedCandidate || siteSelectionMissing"
              @click="submit('preview', true)">重新搜索并预览</v-btn>
            <v-btn color="info" variant="tonal" :loading="busy === 'preview'"
              :disabled="!!busy || !selectedCandidate || siteSelectionMissing" @click="submit('preview')">预览匹配</v-btn>
            <v-btn color="primary" variant="tonal" :loading="busy === 'confirm'"
              :disabled="!!busy || !selectedCandidate || siteSelectionMissing" @click="submit('confirm')">确认并恢复</v-btn>
          </v-card-actions>
        </v-card>

        <v-card v-if="selectedPreview" ref="previewCard" variant="outlined" class="mb-4">
          <v-card-text>
            <preview-results :preview="selectedPreview" :stale="previewStale" :disabled="!!busy"
              @refresh-search="submit('preview', true)" />
          </v-card-text>
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
                <v-btn v-if="selectedReplacementPreview" variant="text" :disabled="!!busy"
                  @click="submit('preview_replace', true)">重新搜索回刷资源</v-btn>
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
            <v-alert v-if="busy === 'preview_replace'" type="info" variant="tonal" density="compact"
              class="mt-3" role="status" aria-live="polite">{{ previewProgress }}</v-alert>
            <preview-results v-if="selectedReplacementPreview" :preview="selectedReplacementPreview" class="mt-4"
              :disabled="!!busy" refresh-label="重新搜索回刷资源" @refresh-search="submit('preview_replace', true)" />
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
    <data-page v-if="dataPageVisible" :api="api" embedded @switch="dataPageVisible = false"
      @select-candidate="selectDataCandidate" />
  </div>
</template>

<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { CANDIDATE_SELECTION_EVENT, takeCandidateSelection } from './candidateSelection'
import PreviewResults from './PreviewResults.vue'
import RuleEditor from './RuleEditor.vue'
import DataPage from './Page.vue'

const PLUGIN_ID = 'SubscribeOverwatcher'
const fields = [
  'enabled', 'confirm_full_pack', 'daily_notice', 'notice_hour', 'notice_minute',
  'historical_subscription', 'scan_subscription', 'candidate_subscription', 'candidate', 'rule_mode', 'rule_sites', 'rule_json', 'keyword_choice', 'download_directory',
  'direct_subscription',
  'revoke_subscription', 'replace_subscription', 'replace_resource', 'replace_episodes',
]
const sections = {
  adopt: 'history', scan: 'scan', preview: 'candidate', confirm: 'candidate',
  direct_approve: 'approve', revoke: 'revoke',
  preview_replace: 'replace', submit_replace: 'replace',
}
const ruleModes = [
  { title: '只用所选站点', value: 'candidate' },
  { title: '自选站点', value: 'custom' },
  { title: '不限站点', value: 'all_sites' },
]

const props = defineProps({
  api: { type: [Object, Function], required: true },
  initialConfig: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['switch'])

const form = reactive({
  enabled: false, confirm_full_pack: true, daily_notice: true,
  notice_hour: 9, notice_minute: 0, historical_subscription: [],
  scan_subscription: '',
  candidate_subscription: '', candidate: '', rule_mode: 'candidate', rule_sites: [], rule_json: '', keyword_choice: 'original',
  download_directory: '',
  direct_subscription: '', revoke_subscription: '',
  replace_subscription: '', replace_resource: '', replace_episodes: '',
})
const options = reactive({
  notice_hour: [], notice_minute: [], historical_subscription: [],
  scan_subscription: [],
  candidate_subscription: [], candidate: [],
  rule_sites: [],
  download_directory: [],
  direct_subscription: [], revoke_subscription: [], replace_subscription: [], replace_resource: [],
})
const managedSummary = ref('正在读取已纳入管理的剧集…')
const loading = ref(true)
const formReady = ref(false)
const busy = ref('')
const previewProgress = ref('')
const candidateCard = ref(null)
const previewCard = ref(null)
const candidatePreview = ref(null)
const replacementPreview = ref(null)
const ruleOptions = ref({})
const handoffNotice = ref('')
const dataPageVisible = ref(false)
const feedback = reactive({ section: '', type: 'info', text: '' })
function candidateSubscriptionId(item) {
  return String(item.subscription_id ?? String(item.value).split(':')[0])
}

const candidateChoices = computed(() => options.candidate.filter(item =>
  form.candidate_subscription && candidateSubscriptionId(item) === form.candidate_subscription))
const selectedCandidate = computed(() => candidateChoices.value.find(item => item.value === form.candidate))
const siteSelectionMissing = computed(() => form.rule_mode === 'custom' && !form.rule_sites.length)
const keywordChoices = [
  { title: '沿用原订阅搜索方式', value: 'original' },
  { title: '使用剧名＋季号搜索词', value: 'seasonal' },
]
const selectedDownloadDirectory = computed(() => options.download_directory.find(
  item => item.value === form.download_directory))
const selectedCandidateUrl = computed(() => {
  const url = selectedCandidate.value?.page_url
  return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : ''
})
const selectedCandidateSite = computed(() => selectedCandidate.value?.site_name || '所选站点')
const selectedPreview = computed(() => candidatePreview.value?.choice === form.candidate
  ? candidatePreview.value : null)
const selectedReplacementPreview = computed(() => replacementPreview.value &&
  String(replacementPreview.value.subscription_id) === String(form.replace_subscription)
  ? replacementPreview.value : null)

function draftKey(raw) {
  try {
    const changes = JSON.parse(raw || '{}')
    if (!changes || Array.isArray(changes) || typeof changes !== 'object') return null
    const rules = { ...selectedCandidate.value?.rules, ...changes }
    if (form.rule_mode === 'all_sites') rules.sites = []
    if (form.rule_mode === 'custom') rules.sites = [...form.rule_sites].sort((a, b) => a - b)
    return JSON.stringify(Object.entries(rules).sort(([a], [b]) => a.localeCompare(b)))
  } catch { return null }
}
const previewStale = computed(() => selectedPreview.value && (
  selectedPreview.value.rule_mode !== form.rule_mode ||
  (selectedPreview.value.keyword_choice || 'original') !== form.keyword_choice ||
  (selectedPreview.value.download_directory || '') !== (form.download_directory || '') ||
  draftKey(form.rule_json) === null || draftKey(form.rule_json) !== draftKey(selectedPreview.value.draft_json)
))
const keywordPreviewAutoChanged = computed(() => !!selectedPreview.value?.keyword_choice_auto_changed && !previewStale.value)
const groupPreviewAutoChanged = computed(() => !!selectedPreview.value?.rule_groups_auto_disabled && !previewStale.value)

function selectCandidateSubscription(subscription) {
  form.candidate_subscription = subscription || ''
  form.candidate = ''
  form.rule_json = ''
  form.rule_sites = []
  form.keyword_choice = 'original'
  form.download_directory = ''
  handoffNotice.value = ''
}

function selectCandidate(candidate) {
  form.candidate = candidate || ''
  const rules = selectedCandidate.value?.rules || {}
  form.rule_json = Object.keys(rules).length ? JSON.stringify(rules, null, 2) : ''
  form.rule_sites = [...(rules.sites || [])]
  form.keyword_choice = 'original'
  if (form.rule_mode !== 'candidate') updateSiteScope(form.rule_mode)
  handoffNotice.value = ''
}

function updateSiteScope(mode, restoreDefault = true) {
  if (!keywordChoices.some(item => item.value === form.keyword_choice)) form.keyword_choice = 'original'
  if (!selectedCandidate.value && !form.rule_json) return
  try {
    const rules = form.rule_json.trim() ? JSON.parse(form.rule_json) : { ...selectedCandidate.value?.rules }
    if (!rules || Array.isArray(rules) || typeof rules !== 'object') throw new Error('invalid draft')
    if (mode === 'custom' && restoreDefault && !form.rule_sites.length) {
      form.rule_sites = [...(selectedCandidate.value?.rules?.sites || [])]
    }
    rules.sites = mode === 'all_sites' ? []
      : mode === 'custom' ? [...form.rule_sites]
      : selectedCandidate.value?.rules?.sites ?? null
    form.rule_json = JSON.stringify(rules, null, 2)
  } catch {
    feedback.section = 'candidate'
    feedback.type = 'warning'
    feedback.text = '已保存的规则无法读取，请重置为候选规则或重新选择版本。'
  }
}

function updateCustomSites(sites) {
  form.rule_sites = Array.isArray(sites) ? [...sites] : []
  updateSiteScope('custom', false)
}
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

function applySnapshot(snapshot, { preserveForm = false } = {}) {
  const source = snapshot?.data?.conf ? snapshot.data : snapshot
  if (!source?.conf || !source?.model) throw new Error('配置数据未返回完整表单')
  const model = { ...props.initialConfig, ...source.model }
  candidatePreview.value = source.model._candidate_preview || null
  replacementPreview.value = source.model._replacement_preview || null
  ruleOptions.value = source.model._rule_options || {}
  if (!preserveForm) {
    for (const name of fields) {
      if (Object.prototype.hasOwnProperty.call(model, name)) form[name] = model[name]
    }
    if (!Array.isArray(form.historical_subscription)) {
      form.historical_subscription = form.historical_subscription ? [form.historical_subscription] : []
    }
    if (!Array.isArray(form.rule_sites)) form.rule_sites = []
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
  if (!preserveForm) {
    const candidate = options.candidate.find(item => item.value === form.candidate)
    if (candidate) {
      form.candidate_subscription = candidateSubscriptionId(candidate)
    } else {
      if (form.candidate) {
        form.candidate = ''
        form.rule_json = ''
      }
      if (!options.candidate_subscription.some(item => item.value === form.candidate_subscription)) {
        form.candidate_subscription = ''
      }
    }
  }
  if (!keywordChoices.some(item => item.value === form.keyword_choice)) form.keyword_choice = 'original'
  return source.model._last_confirmation
}

function applyCandidateSelection(candidateId = takeCandidateSelection()) {
  if (!candidateId) return false
  handoffNotice.value = ''
  const candidate = options.candidate.find(item => item.value === candidateId)
  const subscriptionId = candidate && candidateSubscriptionId(candidate)
  if (!candidate || !options.candidate_subscription.some(item => item.value === subscriptionId)) {
    feedback.section = 'settings'
    feedback.type = 'warning'
    feedback.text = '所选版本已不在待确认列表，请重新选择'
    return false
  }
  if (form.candidate_subscription !== subscriptionId) form.download_directory = ''
  form.candidate_subscription = subscriptionId
  form.candidate = candidateId
  form.rule_mode = 'candidate'
  form.rule_sites = [...(candidate.rules?.sites || [])]
  form.keyword_choice = 'original'
  form.rule_json = Object.keys(candidate.rules || {}).length
    ? JSON.stringify(candidate.rules, null, 2) : ''
  feedback.section = ''
  feedback.text = ''
  handoffNotice.value = '已选中详情页里的版本。可修改规则草稿，再预览匹配。'
  return true
}

async function refreshForm(options) {
  const snapshot = await props.api.get(`plugin/form/${PLUGIN_ID}`)
  return applySnapshot(snapshot, options)
}

async function selectDataCandidate(candidateId) {
  if (busy.value) return
  dataPageVisible.value = false
  busy.value = 'select_data_candidate'
  try {
    await refreshForm({ preserveForm: true })
    if (applyCandidateSelection(candidateId)) {
      await nextTick()
      candidateCard.value?.$el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    }
  } catch (error) {
    feedback.section = 'settings'
    feedback.type = 'error'
    feedback.text = error?.message || '读取所选版本失败，请稍后重试'
  } finally {
    busy.value = ''
  }
}

function payload(action, requestId, refreshSearch) {
  const values = {}
  for (const name of fields) values[name] = form[name]
  return { ...values, operation: action, operation_request_id: requestId, refresh_search: refreshSearch }
}

function responseBody(raw) {
  return raw?.data?.success === undefined ? raw : raw.data
}

async function showSearchProgress(submitted) {
  if (submitted.refresh_search) {
    previewProgress.value = '已手动触发重新搜索；正在等待站点响应和资源解析，可能需要较长时间。'
    return
  }
  previewProgress.value = '正在检查当前站点范围是否有可复用的搜索缓存…'
  try {
    const status = responseBody(await props.api.post(`plugin/${PLUGIN_ID}/preview-status`, submitted))
    const missing = (status?.missing_sites || []).map(site => `${site.name}（${site.id}）`).join('、')
    const reused = (status?.reused_sites || []).map(site => `${site.name}（${site.id}）`).join('、')
    const messages = {
      cache_miss: submitted.rule_mode === 'all_sites' && submitted.operation === 'preview'
        ? `当前没有可复用的全站搜索缓存。数据页候选是独立保存的历史记录，不能代替完整搜索；缺失站点：${missing || '当前默认范围'}。已触发搜索，正在等待站点响应和资源解析。`
        : `当前站点范围没有可复用缓存，已触发搜索；缺失站点：${missing || '当前范围'}。正在等待站点响应和资源解析。`,
      partial_cache: `已有缓存：${reused}；缺失站点：${missing}。本次只搜索缺失站点，正在等待站点响应和资源解析。`,
      cached: '正在复用搜索缓存并按当前规则筛选，无需重新搜索站点。',
      cached_empty: '当前站点范围的搜索缓存为空，本次将复用空结果；建议手动重新搜索。',
    }
    previewProgress.value = status?.success && messages[status.status] ||
      '正在执行预览；缓存状态暂无法确认，若需要搜索，需等待站点响应和资源解析。'
  } catch {
    previewProgress.value = '正在执行预览；缓存状态暂无法确认，若需要搜索，需等待站点响应和资源解析。'
  }
}

async function submit(action, refreshSearch = false) {
  if (busy.value) return
  busy.value = action
  feedback.section = ''
  feedback.text = ''
  const requestId = action === 'save' ? '' : `${Date.now()}-${Math.random().toString(36).slice(2)}`
  const submitted = payload(action, requestId, refreshSearch)
  const started = performance.now()
  try {
    if (action === 'preview' || action === 'preview_replace') {
      await showSearchProgress(submitted)
      await nextTick()
    }
    const response = responseBody(await props.api.post(`plugin/${PLUGIN_ID}/apply`, submitted))
    let result
    let refreshFailed = false
    try { result = await refreshForm({ preserveForm: action === 'scan' }) } catch { refreshFailed = true }
    if (response?.preview) {
      if (action === 'preview') {
        candidatePreview.value = response.preview
        if (response.preview.draft_json) form.rule_json = response.preview.draft_json
        if (response.preview.keyword_choice) form.keyword_choice = response.preview.keyword_choice
      }
      else if (action === 'preview_replace') replacementPreview.value = response.preview
      response.preview.request_seconds = (performance.now() - started) / 1000
    }
    if (!selectedCandidate.value) handoffNotice.value = ''
    if (action === 'scan' && response?.success && !form.candidate_subscription
      && options.candidate_subscription.some(item => String(item.value) === String(submitted.scan_subscription))) {
      form.candidate_subscription = String(submitted.scan_subscription)
    }
    feedback.section = sections[action] || 'settings'
    if (action !== 'save' && !response?.preview && !refreshFailed && result?.request_id !== requestId) {
      feedback.type = 'error'
      feedback.text = '操作已提交，但未能确认结果，请查看插件详情页'
    } else {
      feedback.type = response?.success ? 'success' : 'error'
      feedback.text = response?.message || (response?.success ? '操作完成' : '操作失败')
      if (refreshFailed) feedback.text += '；选择列表刷新失败，请稍后刷新。'
    }
    if (action === 'preview' && response?.success && selectedPreview.value) {
      await nextTick()
      previewCard.value?.$el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
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

async function consumeSelectionAndScroll() {
  if (!formReady.value || !applyCandidateSelection()) return
  await nextTick()
  candidateCard.value?.$el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}

onMounted(async () => {
  window.addEventListener(CANDIDATE_SELECTION_EVENT, consumeSelectionAndScroll)
  try {
    await refreshForm()
    formReady.value = true
  } catch (error) {
    feedback.section = 'settings'
    feedback.type = 'error'
    feedback.text = error?.message || '读取配置失败'
  } finally {
    loading.value = false
    await consumeSelectionAndScroll()
  }
})
onActivated(consumeSelectionAndScroll)
onBeforeUnmount(() => window.removeEventListener(CANDIDATE_SELECTION_EVENT, consumeSelectionAndScroll))
</script>

<style scoped>
.preview-auto-changed :deep(.v-field) {
  outline: 3px solid rgb(var(--v-theme-warning));
  outline-offset: 2px;
  border-radius: 6px;
  background-color: rgba(var(--v-theme-warning), 0.12);
}
</style>
