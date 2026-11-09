<template>
  <div class="preview-results" tabindex="-1" aria-label="规则预览结果">
    <div class="text-subtitle-1 mb-2">预览结果：通过 {{ preview.matched_count }} 条，排除 {{ preview.rejected_count }} 条</div>
    <v-alert v-if="stale" type="warning" variant="tonal" density="compact" class="mb-3">
      规则、站点或目录已修改；以下是上次预览，请重新预览后确认。
    </v-alert>
    <v-alert v-if="preview.search_keyword_notice" :type="preview.search_keyword_verified ? 'info' : 'warning'"
      variant="tonal" density="compact" class="mb-3">
      <div>确认后的订阅搜索词：<strong>{{ preview.search_keyword || '默认使用剧名及别名' }}</strong>
        （{{ preview.keyword_choice?.startsWith('keyword:') ? '手动选择' :
          preview.search_keyword_changed ? '调整' : '保持原设置' }}）</div>
      <div v-if="preview.search_keyword_changed">原搜索词：{{ preview.original_search_keyword || '默认使用剧名及别名' }}。</div>
      {{ preview.search_keyword_notice }}
    </v-alert>
    <v-alert v-if="preview.rule_groups_auto_disabled" type="warning" variant="tonal" density="compact" class="mb-3">
      {{ preview.rule_group_notice }}
    </v-alert>
    <p v-if="preview.rule_group_comparison" class="text-body-2 mb-3">
      规则组对比（{{ preview.rule_group_comparison.source === '原订阅' ? '原订阅' : '系统默认' }}：
      {{ preview.rule_group_comparison.names.join('、') }}）：
      考虑规则组通过 {{ preview.rule_group_comparison.with_groups_count }} 条，
      不使用规则组通过 {{ preview.rule_group_comparison.without_groups_count }} 条。
    </p>
    <v-alert v-if="preview.search_diagnostics?.site_failures?.length" type="warning"
      variant="tonal" density="compact" class="mb-3">
      该次搜索有失败或暂缓的站点，结果可能不完整。失败站点仅在本条订阅本轮停止后续查询；其他订阅独立尝试，站点自身的访问限制仍生效。
      <div v-for="site in preview.search_diagnostics.site_failures" :key="site.site_id">
        {{ site.site_name }}：{{ site.reason }}。
        <span v-if="site.until">{{ preview.search_cached ? '当时最早可重试' : '最早可重试' }} {{ localTime(site.until) }}。</span>
      </div>
    </v-alert>
    <v-alert v-else-if="preview.search_cached && preview.search_result_count === 0" type="warning"
      variant="tonal" density="compact" class="mb-3">
      当前站点范围的搜索缓存中没有资源；本次复用了空结果，建议手动重新搜索以获取最新资源。
      <v-btn variant="text" size="small" :disabled="disabled" @click="emit('refresh-search')">{{ refreshLabel }}</v-btn>
    </v-alert>
    <v-alert v-else-if="preview.search_result_count === 0" type="info"
      variant="tonal" density="compact" class="mb-3">
      本轮没有取得目标季资源；可能搜索未命中，或资源被宿主媒体识别过滤。版本列表保留的历史候选不代表本轮仍能搜到。
    </v-alert>
    <v-alert v-if="preview.search_diagnostics?.supplemental_failed" type="warning"
      variant="tonal" density="compact" class="mb-3">
      原订阅搜索验证失败，当前保留带季查询取得的资源；无法据此判断必须修改搜索词，跨季候选也可能不完整。
    </v-alert>
    <p class="text-caption text-medium-emphasis mb-2">
      预览生成于 {{ preview.searched_at }} · {{ preview.search_cached ? `复用 ${age(preview.search_age_seconds)}前的搜索结果`
        : preview.search_trigger === 'cache_miss' ? '没有可复用缓存，本次已触发搜索'
        : preview.search_trigger === 'manual' ? '本次手动重新搜索' : '本次重新搜索' }}
    </p>
    <p v-if="preview.search_source" class="text-caption text-medium-emphasis mb-2">
      搜索结果来自{{ preview.search_source }} · {{ preview.search_result_at }} ·
      {{ preview.search_sites?.length ? `搜索站点：${preview.search_sites.join('、')}` : '搜索全部站点' }}。
      使用该范围最新一次完整搜索，历史候选仍保留展示；资源是否仍存在以重新搜索为准。
    </p>
    <p v-if="preview.timings" class="text-body-2 mb-3">
      总耗时 {{ seconds(preview.timings.total_seconds) }} 秒：媒体识别 {{ seconds(preview.timings.recognition_seconds) }} 秒，
      站点搜索与资源解析 {{ seconds(preview.timings.search_seconds) }} 秒，
      规则筛选 {{ seconds(preview.timings.filter_seconds) }} 秒，目录计算 {{ seconds(preview.timings.directory_seconds) }} 秒。
    </p>
    <details v-if="preview.search_diagnostics?.rounds?.length" class="mb-3">
      <summary>搜索词与目标季结果（{{ preview.search_cached ? '原缓存搜索轮次' : '本轮搜索' }}）</summary>
      <ul class="ms-5 mt-2">
        <li v-for="(round, index) in preview.search_diagnostics.rounds" :key="index">
          {{ round.kind === 'target_season' ? '带季搜索' : round.kind === 'original_search' ? '原订阅搜索' : '合集补搜' }}：{{ round.keyword }} ·
          媒体匹配 {{ round.identified_count }} 条，目标季保留 {{ round.result_count }} 条 · {{ seconds(round.seconds) }} 秒
          <span v-if="round.site_keywords?.length"> · 实际关键词：{{ round.site_keywords.join('、') }}</span>
          <span v-if="round.requests_paced"> · 请求遵守站点间隔；实际请求 {{ round.request_count ?? 0 }} 次，复用相同查询 {{ round.reused_request_count ?? 0 }} 次</span>
          <span v-else-if="round.budget_shared"> · 旧搜索使用系统共享站点预算</span>
          <span v-for="wait in round.site_waits || []" :key="wait.site_id">
            · {{ wait.site_name || `站点 ${wait.site_id}` }} 等待 {{ seconds(wait.seconds) }} 秒
          </span>
        </li>
      </ul>
    </details>
    <p v-if="preview.request_seconds !== undefined" class="text-caption text-medium-emphasis mb-3">
      含设置保存和列表刷新，本次操作共 {{ seconds(preview.request_seconds) }} 秒。
    </p>
    <details v-if="preview.effective_rules?.length" class="mb-3">
      <summary>本次生效条件（含原订阅及宿主默认条件）</summary>
      <ul class="ms-5 mt-2">
        <li v-for="rule in preview.effective_rules" :key="rule.field">
          {{ rule.label }}：{{ rule.value }}<span v-if="rule.source">（{{ rule.source }}）</span>
        </li>
      </ul>
    </details>
    <div v-if="preview.rejection_summary?.length" class="mb-3">
      <div class="text-body-2">排除原因（每条资源按首个未满足条件计数）：</div>
      <ul class="ms-5 mt-2">
        <li v-for="item in preview.rejection_summary" :key="item.reason">{{ item.reason }} · {{ item.count }} 条</li>
      </ul>
    </div>
    <p class="text-caption text-medium-emphasis mb-2">
      当前展示前 {{ preview.matched_shown ?? Math.min(preview.matched_count, 80) }} 条通过、
      前 {{ preview.rejected_shown ?? Math.min(preview.rejected_count, 40) }} 条排除资源；跨站相同版本合并展示，统计按资源条数计算。
      <span v-if="preview.rule_groups_disabled">确认后不使用规则组，按站点及基础条件追更。</span>
      <span v-else-if="preview.rule_groups_checked">本次预览已考虑规则组，正式追更沿用相同选择。</span>
      <span v-else>这条历史预览未考虑规则组，请重新预览。</span>
    </p>
    <div v-if="preview.table" class="overflow-x-auto">
      <schema-node :node="preview.table" />
    </div>
  </div>
</template>

<script setup>
import SchemaNode from './schemaNode'

defineProps({
  preview: { type: Object, required: true },
  stale: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  refreshLabel: { type: String, default: '重新搜索并预览' },
})
const emit = defineEmits(['refresh-search'])
const seconds = value => Number(value || 0).toFixed(2)
const localTime = value => value ? new Date(value * 1000).toLocaleString() : '未知'
const age = value => {
  const seconds = Math.max(0, Number(value || 0))
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`
  return `${(seconds / 3600).toFixed(1)} 小时`
}
</script>
