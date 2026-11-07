<template>
  <div class="rule-editor">
    <v-alert v-if="invalidDraft" type="warning" variant="tonal" density="compact" class="mb-3">
      已保存的规则无法读取，请重新选择版本或重置为候选规则。
    </v-alert>
    <v-row>
      <v-col v-for="field in specFields" :key="field.name" cols="12" md="4">
        <v-select :model-value="selection(field.name)" :items="specOptions(field.name)" :label="field.label"
          :disabled="disabled" density="comfortable" hide-details
          @update:model-value="selectSpec(field.name, $event)" />
        <v-text-field v-if="selection(field.name) === CUSTOM" :model-value="customValue(field.name)"
          :label="`自定义${field.label}正则`" :disabled="disabled" density="comfortable" hide-details
          class="mt-3" @update:model-value="updateRule(field.name, $event || '')" />
      </v-col>
      <v-col v-for="field in textFields" :key="field.name" cols="12" md="6">
        <v-combobox :model-value="textValue(field.name)" :items="textOptions(field.name)" :label="field.label"
          item-title="title" item-value="value" return-object clearable :disabled="disabled"
          density="comfortable" hint="可选择候选标识、保留原订阅，或直接输入关键字/正则表达式。"
          persistent-hint @update:model-value="updateText(field.name, $event)" />
      </v-col>
      <v-col cols="12" md="6">
        <v-text-field :model-value="siteSummary" label="站点" readonly :disabled="disabled"
          density="comfortable" hint="与上方站点范围同步。" persistent-hint />
      </v-col>
      <v-col cols="12" md="6">
        <v-select :model-value="groupSelection" :items="groupOptions" label="规则组" :disabled="disabled"
          density="comfortable" :hint="groupHint" persistent-hint
          @update:model-value="updateRule('filter_groups', $event === NO_GROUPS ? [] : { subscription: true })" />
      </v-col>
    </v-row>
    <div class="d-flex justify-end mt-2">
      <v-btn variant="text" size="small" :disabled="disabled" @click="resetRules">重置为候选规则</v-btn>
    </div>
    <p class="text-caption text-medium-emphasis mt-2 mb-0">
      常见质量默认采用订阅卡片的内置选项；候选匹配方式和明确输入的自定义正则可继续使用。
      制作组可在“包含”中选择，也可输入 TEAM|GROUP 等正则。
      “保留原订阅”不回填该项；“全部”或清空会清除订阅该项，宿主默认条件仍可能生效。
    </p>
  </div>
</template>

<script setup>
import { computed, reactive, watch } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  candidate: { type: Object, default: () => ({}) },
  options: { type: Object, default: () => ({}) },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
const KEEP = '@subscription'
const CANDIDATE = '@candidate'
const CUSTOM = '@custom'
const NO_GROUPS = '@no-groups'
const groupOptions = [
  { title: '保留原订阅 / 默认规则组', value: KEEP },
  { title: '清空并不使用规则组（确认后生效）', value: NO_GROUPS },
]
const specFields = [
  { name: 'quality', label: '质量' }, { name: 'resolution', label: '分辨率' }, { name: 'effect', label: '特效' },
]
const textFields = [
  { name: 'include', label: '包含（关键字、正则式）' }, { name: 'exclude', label: '排除（关键字、正则式）' },
]
const customEditing = reactive({})
const parsed = computed(() => {
  try {
    const changes = JSON.parse(props.modelValue || '{}')
    return changes && !Array.isArray(changes) && typeof changes === 'object' ? changes : null
  } catch { return null }
})
const invalidDraft = computed(() => parsed.value === null)
const rules = computed(() => ({ ...props.candidate.rules, ...parsed.value }))
const groupSelection = computed(() => Array.isArray(rules.value.filter_groups) && !rules.value.filter_groups.length
  ? NO_GROUPS : KEEP)
const groupHint = computed(() => groupSelection.value === NO_GROUPS
  ? '确认后不再应用规则组，继续按站点、质量、分辨率、特效和包含/排除条件追更。'
  : `当前：${props.candidate.original_filter_groups?.join('、') || '宿主默认规则组'}。预览不模拟规则组。`)
const siteSummary = computed(() => {
  const sites = rules.value.sites ?? props.candidate.original_sites ?? []
  if (!sites.length) return '不限站点'
  const candidateSite = props.candidate.site_id ?? props.candidate.rules?.sites?.[0]
  return sites.map(id => id === candidateSite && props.candidate.site_name
    ? `${props.candidate.site_name}（${id}）` : `站点 ${id}`).join('、')
})

function candidateName(field) {
  const value = props.candidate.rules?.[field]
  return value?.candidate || props.candidate.rule_names?.[field] || '候选规格'
}
function specOptions(field) {
  const items = [{ title: '保留原订阅', value: KEEP }]
  const original = props.candidate.rule_patterns?.[field] ?? props.candidate.rules?.[field]
  if (original && !(props.options[field] || []).some(item => item.value === original)) {
    items.push({ title: `${candidateName(field)}（候选规格）`, value: CANDIDATE })
  }
  items.push(...(props.options[field] || [{ title: '全部', value: null }]).map(item => ({
    ...item, value: item.value ?? '',
  })))
  items.push({ title: '自定义正则', value: CUSTOM })
  return items
}
function selection(field) {
  const value = rules.value[field]
  if (value?.subscription || value === undefined) return KEEP
  if (customEditing[field]) return CUSTOM
  if (value === null || value === '') return ''
  if ((props.options[field] || []).some(item => item.value === value)) return value
  if (value?.candidate) {
    const standard = props.candidate.rules?.[field]
    return (props.options[field] || []).some(item => item.value === standard) ? standard : CANDIDATE
  }
  if (value === props.candidate.rule_patterns?.[field] || value === props.candidate.rules?.[field]) return CANDIDATE
  return CUSTOM
}
function customValue(field) {
  const value = rules.value[field]
  return typeof value === 'string' ? value : ''
}
function updateRule(field, value) {
  emit('update:modelValue', JSON.stringify({ ...parsed.value, [field]: value }, null, 2))
}
function selectSpec(field, value) {
  customEditing[field] = value === CUSTOM
  if (value === KEEP) updateRule(field, { subscription: true })
  else if (value === CANDIDATE) updateRule(field, props.candidate.rule_patterns?.[field] ?? props.candidate.rules[field])
  else if (value === CUSTOM) updateRule(field, customValue(field))
  else updateRule(field, value || null)
}
function textOptions(field) {
  const items = [{ title: '保留原订阅', value: { subscription: true } }]
  if (props.candidate.rules?.[field]) {
    items.push({ title: `${candidateName(field)}（候选标识）`, value: props.candidate.rules[field] })
  }
  if (field === 'include' && props.candidate.team_pattern) {
    items.push({ title: `只用制作组：${props.candidate.team}`, value: props.candidate.team_pattern })
  }
  return items
}
function textValue(field) {
  const value = rules.value[field]
  if (value === undefined || value?.subscription) return textOptions(field)[0]
  if (value?.candidate || value && value === props.candidate.rules?.[field]) return textOptions(field)[1] || value
  return value
}
function updateText(field, value) {
  updateRule(field, value && typeof value === 'object' && Object.hasOwn(value, 'value') ? value.value : value)
}
function resetRules() {
  for (const field of specFields) customEditing[field.name] = false
  emit('update:modelValue', '')
}
watch(() => props.candidate.value, () => {
  for (const field of specFields) customEditing[field.name] = false
})
</script>
