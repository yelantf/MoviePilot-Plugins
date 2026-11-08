import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import Config from './Config.vue'
import Page from './Page.vue'
import RuleEditor from './RuleEditor.vue'
import PreviewResults from './PreviewResults.vue'

const Container = defineComponent({ template: '<div><slot /></div>' })
const Button = defineComponent({
  props: ['disabled', 'loading'],
  emits: ['click'],
  template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
})
const components = Object.fromEntries([
  'VCard', 'VCardTitle', 'VCardText', 'VCardActions', 'VRow', 'VCol', 'VSpacer',
  'VAlert', 'VTable', 'VProgressLinear', 'VSwitch', 'VSelect', 'VAutocomplete', 'VCombobox', 'VTextField',
].map(name => [name, Container]))
components.VBtn = Button
components.VAlert = defineComponent({
  props: ['text'],
  template: '<div>{{ text }}<slot /></div>',
})
const Select = defineComponent({
  props: ['modelValue', 'items', 'label', 'disabled'],
  emits: ['update:modelValue'],
  template: `<select :aria-label="label" :disabled="disabled" :value="modelValue ?? ''"
    @change="$emit('update:modelValue', $event.target.value)">
    <option value="">请选择</option>
    <option v-for="item in items" :key="item.value" :value="item.value">{{ item.title }}</option>
  </select>`,
})
async function setRuleDraft(wrapper, value) {
  wrapper.findComponent(RuleEditor).vm.$emit('update:modelValue', value)
  await flushPromises()
}
const applyCalls = api => api.post.mock.calls.filter(([url]) => url.endsWith('/apply'))

function snapshot({ managed, historical, result = null }) {
  return {
    conf: [{ component: 'VForm', content: [
      { component: 'VSelect', props: { model: 'historical_subscription', items: historical } },
      { component: 'VAlert', props: { text: managed } },
    ] }],
    model: {
      enabled: true, historical_subscription: historical.map(item => item.value),
      _last_confirmation: result,
    },
  }
}

function candidateSnapshot() {
  const data = snapshot({ managed: '已纳入管理 1 条剧集订阅', historical: [] })
  const rules = { quality: { candidate: 'WEB-DL / WEBRip' }, include: { candidate: 'TEAM' }, sites: [5] }
  data.conf[0].content.push(
    { component: 'VSelect', props: { model: 'candidate_subscription', items: [
      { title: '#2 剧集甲', value: '2' },
    ] } },
    { component: 'VSelect', props: { model: 'candidate', items: [
      { title: '甲版本', value: '2:a', subscription_id: '2', rules },
    ] } },
  )
  Object.assign(data.model, { candidate_subscription: '2', candidate: '2:a', rule_mode: 'candidate',
    rule_json: JSON.stringify(rules, null, 2) })
  return data
}

function previewSnapshot(draft) {
  return {
    choice: '2:a', subscription_id: '2', rule_mode: 'candidate', download_directory: '', draft_json: draft,
    matched_count: 1, rejected_count: 45, matched_shown: 1, rejected_shown: 40,
    search_cached: true, search_age_seconds: 10, search_result_count: 46, search_trigger: 'cache',
    timings: { total_seconds: 0.4, recognition_seconds: 0, search_seconds: 0, filter_seconds: 0.3, directory_seconds: 0.1 },
    effective_rules: [{ field: 'include', label: '包含', value: 'TEAM', source: '候选规格' }],
    rejection_summary: [{ reason: '缺少包含标识：TEAM', count: 45 }],
    table: { component: 'VTable', content: [{ component: 'tbody', content: [
      { component: 'tr', content: [{ component: 'td', text: '通过资源标题' }] },
      { component: 'tr', content: [{ component: 'td', text: '排除资源标题' },
        { component: 'td', text: '缺少包含标识：TEAM' }] },
    ] }] },
  }
}

describe('custom configuration actions', () => {
  it('scans an empty managed show, shows progress, and refreshes its versions', async () => {
    const initial = snapshot({ managed: '已纳入管理 1 条剧集订阅', historical: [] })
    initial.conf[0].content.push({ component: 'VSelect', props: { model: 'scan_subscription', items: [
      { title: '#3 新剧集', value: '3' },
    ] } })
    const fresh = structuredClone(initial)
    fresh.conf[0].content.push(
      { component: 'VSelect', props: { model: 'candidate_subscription', items: [{ title: '#3 新剧集', value: '3' }] } },
      { component: 'VSelect', props: { model: 'candidate', items: [
        { title: '新扫描的版本', value: '3:a', subscription_id: '3', rules: { sites: [5] } },
      ] } },
    )
    let resolveScan
    let reads = 0
    const api = {
      get: vi.fn().mockImplementation(async () => ++reads === 1 ? initial : fresh),
      post: vi.fn().mockImplementation((_url, submitted) => {
        fresh.model._last_confirmation = { request_id: submitted.operation_request_id, success: true }
        return new Promise(resolve => { resolveScan = resolve })
      }),
    }
    const wrapper = mount(Config, { props: { api }, global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    const button = wrapper.findAll('button').find(item => item.text() === '扫描所选剧集')
    expect(button.element.disabled).toBe(true)
    await wrapper.find('select[aria-label="选择已纳入的待确认剧集"]').setValue('3')
    await button.trigger('click')
    expect(wrapper.text()).toContain('已触发所选剧集扫描')
    expect(button.element.disabled).toBe(true)
    expect(applyCalls(api)[0][1]).toMatchObject({ operation: 'scan', scan_subscription: '3', candidate: '' })
    resolveScan({ success: true, message: '扫描完成，本轮发现候选 1 条' })
    await flushPromises()
    expect(wrapper.text()).toContain('扫描完成，本轮发现候选 1 条')
    expect(wrapper.text()).not.toContain('已触发所选剧集扫描')
    expect(wrapper.find('select[aria-label="选择该剧集版本（全集优先）"]').text()).toContain('新扫描的版本')
    expect(button.element.disabled).toBe(false)
    wrapper.unmount()
  })

  it('keeps another show and its unsaved rule selections after a manual scan', async () => {
    const initial = candidateSnapshot()
    initial.conf[0].content.push({ component: 'VSelect', props: { model: 'scan_subscription', items: [
      { title: '#3 新剧集', value: '3' },
    ] } })
    const fresh = structuredClone(initial)
    fresh.model.rule_json = ''
    fresh.model.candidate = ''
    fresh.model.candidate_subscription = ''
    let reads = 0
    const api = {
      get: vi.fn().mockImplementation(async () => ++reads === 1 ? initial : fresh),
      post: vi.fn().mockImplementation(async (_url, submitted) => {
        fresh.model._last_confirmation = { request_id: submitted.operation_request_id, success: true }
        return { success: true, message: '扫描完成' }
      }),
    }
    const wrapper = mount(Config, { props: { api }, global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    const draft = '{"include":"MY.*TEAM","sites":[5]}'
    await setRuleDraft(wrapper, draft)
    await wrapper.find('select[aria-label="选择已纳入的待确认剧集"]').setValue('3')
    await wrapper.findAll('button').find(item => item.text() === '扫描所选剧集').trigger('click')
    await flushPromises()
    expect(wrapper.find('select[aria-label="选择该剧集版本（全集优先）"]').element.value).toBe('2:a')
    expect(wrapper.findComponent(RuleEditor).props('modelValue')).toBe(draft)
    expect(wrapper.text()).toContain('扫描完成')
    wrapper.unmount()
  })

  it('shows a manual scan failure and releases its busy state for retry', async () => {
    const data = snapshot({ managed: '已纳入管理 1 条剧集订阅', historical: [] })
    data.model.scan_subscription = '3'
    data.conf[0].content.push({ component: 'VSelect', props: { model: 'scan_subscription', items: [
      { title: '#3 新剧集', value: '3' },
    ] } })
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockImplementation(async (_url, submitted) => {
        data.model._last_confirmation = { request_id: submitted.operation_request_id, success: false }
        return { success: false, message: '已有候选扫描正在进行，请稍后重试' }
      }),
    }
    const wrapper = mount(Config, { props: { api }, global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    const button = wrapper.findAll('button').find(item => item.text() === '扫描所选剧集')
    await button.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已有候选扫描正在进行，请稍后重试')
    expect(button.element.disabled).toBe(false)
    wrapper.unmount()
  })

  it('shows shared budget waiting separately from a failed site', () => {
    const preview = { ...previewSnapshot(''), search_diagnostics: {
      rounds: [{ keyword: 'Show S07', kind: 'target_season', identified_count: 1, result_count: 1,
        seconds: 17, budget_shared: true, site_waits: [{ site_id: 5, site_name: '天空', seconds: 15 }] }],
      site_failures: [{ site_id: 7, site_name: '另一站', reason: '站点搜索间隔尚未结束，本轮暂缓', until: 1800000000 }],
    } }
    const wrapper = mount(PreviewResults, { props: { preview }, global: { components } })
    expect(wrapper.text()).toContain('请求共用系统站点间隔')
    expect(wrapper.text()).toContain('天空 等待 15.00 秒')
    expect(wrapper.text()).toContain('最早可重试')
    expect(wrapper.text()).toContain('另一站：站点搜索间隔尚未结束，本轮暂缓')
    wrapper.unmount()
  })

  it.each([
    [false, null, '保持原设置', '原订阅搜索已召回所选版本'],
    [true, 'Show S07', '调整', '原订阅搜索未召回，带季查询已召回'],
  ])('shows the planned native search keyword and whether confirmation changes it (%s)', (changed, keyword, label, notice) => {
    const preview = { ...previewSnapshot(''), search_keyword: keyword, search_keyword_changed: changed,
      search_keyword_verified: true, search_keyword_notice: notice }
    const wrapper = mount(PreviewResults, { props: { preview }, global: { components } })
    expect(wrapper.text()).toContain(`确认后的订阅搜索词：${keyword || '默认使用剧名及别名'}`)
    expect(wrapper.text()).toContain(label)
    expect(wrapper.text()).toContain(notice)
    expect(wrapper.text().includes('原搜索词：')).toBe(changed)
    wrapper.unmount()
  })

  it('shows failed sites and actual search terms instead of suggesting immediate retries for an empty cache', () => {
    const preview = { ...previewSnapshot(''), search_result_count: 0, matched_count: 0, rejected_count: 0,
      search_diagnostics: { rounds: [{ keyword: 'All Creatures Great and Small S07', kind: 'target_season',
        identified_count: 3, result_count: 0, seconds: 15 }],
      site_failures: [{ site_id: 5, site_name: '天空', reason: '站点限制访问或登录失效', until: 1800000000 }] } }
    const wrapper = mount(PreviewResults, { props: { preview }, global: { components } })
    expect(wrapper.text()).toContain('天空：站点限制访问或登录失效')
    expect(wrapper.text()).toContain('结果可能不完整')
    expect(wrapper.text()).toContain('All Creatures Great and Small S07')
    expect(wrapper.text()).toContain('媒体匹配 3 条，目标季保留 0 条')
    expect(wrapper.text()).not.toContain('建议手动重新搜索')
    expect(wrapper.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('views and refreshes the data page and returns to the same unsaved settings', async () => {
    const data = candidateSnapshot()
    let resourceTitle = '历史预览资源'
    const api = {
      get: vi.fn().mockImplementation(async url => url === 'plugin/page/SubscribeOverwatcher'
        ? { data: { page: [{ component: 'VTable', content: [{ component: 'tbody', content: [
          { component: 'tr', content: [{ component: 'td', text: resourceTitle }] },
        ] }] }] } } : data),
      post: vi.fn(),
    }
    const wrapper = mount(Config, { props: { api },
      global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    await wrapper.find('select[aria-label="站点范围"]').setValue('all_sites')
    const draft = '{"quality":"CUSTOM.*SOURCE","exclude":"sample|preview","sites":[]}'
    await setRuleDraft(wrapper, draft)
    const editorElement = wrapper.find('.rule-editor').element

    await wrapper.findAll('button').find(button => button.text() === '查看数据页面').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(Page).text()).toContain('历史预览资源')
    expect(wrapper.find('.pa-3.pa-sm-4').attributes('style')).toContain('display: none')
    resourceTitle = '最新预览资源'
    await wrapper.findComponent(Page).findAll('button').find(button => button.text() === '刷新').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(Page).text()).toContain('最新预览资源')
    await wrapper.findComponent(Page).findAll('button').find(button => button.text() === '返回设置').trigger('click')
    await flushPromises()

    expect(wrapper.findComponent(Page).exists()).toBe(false)
    expect(wrapper.find('.pa-3.pa-sm-4').attributes('style') || '').not.toContain('display: none')
    expect(wrapper.find('.rule-editor').element).toBe(editorElement)
    expect(wrapper.findComponent(RuleEditor).props('modelValue')).toBe(draft)
    expect(wrapper.find('select[aria-label="站点范围"]').element.value).toBe('all_sites')
    expect(api.get.mock.calls.filter(([url]) => url === 'plugin/form/SubscribeOverwatcher')).toHaveLength(1)
    expect(api.post).not.toHaveBeenCalled()
    expect(wrapper.emitted('switch')).toBeUndefined()
    wrapper.unmount()
  })

  it.each([false, true])('returns a data-page candidate to settings without discarding other inputs (refresh fails: %s)', async refreshFails => {
    window.sessionStorage.clear()
    const initial = candidateSnapshot()
    initial.conf[0].content.push({ component: 'VSelect', props: { model: 'download_directory', items: [
      { title: '所选目录', value: '/chosen' }, { title: '服务器默认目录', value: '/saved' },
    ] } })
    const fresh = structuredClone(initial)
    const rules = { quality: 'Blu-?Ray', sites: [7] }
    fresh.conf[0].content.find(node => node.props?.model === 'candidate').props.items.push({
      title: '新扫描的版本', value: '2:b', subscription_id: '2', rules,
    })
    fresh.model.download_directory = '/saved'
    let formReads = 0
    const api = {
      get: vi.fn().mockImplementation(async url => {
        if (url === 'plugin/page/SubscribeOverwatcher') return { page: [{
          component: 'VBtn', props: { 'data-candidate-id': '2:b' },
          content: [{ component: 'span', text: '选择新版本' }],
        }] }
        if (++formReads === 1) return initial
        if (refreshFails) throw new Error('刷新版本失败')
        return fresh
      }),
      post: vi.fn(),
    }
    const wrapper = mount(Config, { props: { api },
      global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    await wrapper.find('select[aria-label="下载目录（留空保留原目录）"]').setValue('/chosen')
    const draft = '{"include":"MY.*TEAM","sites":[5]}'
    await setRuleDraft(wrapper, draft)
    await wrapper.findAll('button').find(button => button.text() === '查看数据页面').trigger('click')
    await flushPromises()
    await wrapper.findComponent(Page).find('button[data-candidate-id="2:b"]').trigger('click')
    await flushPromises()

    expect(wrapper.findComponent(Page).exists()).toBe(false)
    expect(wrapper.find('select[aria-label="下载目录（留空保留原目录）"]').element.value).toBe('/chosen')
    expect(wrapper.find('select[aria-label="选择该剧集版本（全集优先）"]').element.value).toBe(refreshFails ? '2:a' : '2:b')
    expect(wrapper.findComponent(RuleEditor).props('modelValue')).toBe(refreshFails ? draft : JSON.stringify(rules, null, 2))
    expect(wrapper.text()).toContain(refreshFails ? '刷新版本失败' : '可修改规则草稿，再预览匹配')
    expect(api.post).not.toHaveBeenCalled()
    expect(window.sessionStorage.length).toBe(0)
    expect(wrapper.emitted('switch')).toBeUndefined()
    wrapper.unmount()
  })

  it('changes only sites, preserves rule selections, and can reset an invalid legacy draft without showing JSON', async () => {
    const data = candidateSnapshot()
    const api = { get: vi.fn().mockResolvedValue(data), post: vi.fn().mockResolvedValue({ success: false }) }
    const wrapper = mount(Config, { props: { api },
      global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    const draft = { ...JSON.parse(data.model.rule_json), exclude: 'sample|preview', effect: null }
    await setRuleDraft(wrapper, JSON.stringify(draft))
    const scope = wrapper.find('select[aria-label="站点范围"]')
    await scope.setValue('all_sites')
    expect(JSON.parse(wrapper.findComponent(RuleEditor).props('modelValue'))).toEqual({ ...draft, sites: [] })
    await scope.setValue('candidate')
    expect(JSON.parse(wrapper.findComponent(RuleEditor).props('modelValue'))).toEqual(draft)
    await setRuleDraft(wrapper, '{broken')
    await scope.setValue('all_sites')
    expect(wrapper.findComponent(RuleEditor).props('modelValue')).toBe('{broken')
    expect(wrapper.text()).toContain('已保存的规则无法读取')
    expect(wrapper.find('textarea').exists()).toBe(false)
    await wrapper.findAll('button').find(button => button.text() === '重置为候选规则').trigger('click')
    expect(wrapper.findComponent(RuleEditor).props('modelValue')).toBe('')
    wrapper.unmount()
  })

  it('shows resources, rejection totals and timings in config, scrolls to the result, and supports a new search', async () => {
    const data = candidateSnapshot()
    data.model.search_cache_hours = 0
    const scroll = vi.fn()
    const previousScroll = window.HTMLElement.prototype.scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = scroll
    const api = {
      get: vi.fn().mockImplementation(async () => data),
      post: vi.fn().mockImplementation(async (url, submitted) => {
        if (url.endsWith('/preview-status')) return { success: true, status: 'cached' }
        const preview = previewSnapshot(submitted.rule_json)
        data.model._candidate_preview = preview
        data.model._last_confirmation = { request_id: submitted.operation_request_id, success: true }
        data.model.rule_json = submitted.rule_json
        return { success: true, message: '预览完成', preview }
      }),
    }
    const wrapper = mount(Config, { props: { api },
      global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    expect(wrapper.find('select[aria-label="搜索结果复用时长"]').exists()).toBe(false)
    await wrapper.findAll('button').find(button => button.text() === '预览匹配').trigger('click')
    await flushPromises()
    expect(wrapper.find('.preview-results').text()).toContain('通过 1 条，排除 45 条')
    expect(wrapper.findAll('td').map(cell => cell.text())).toContain('通过资源标题')
    expect(wrapper.findAll('td').map(cell => cell.text())).toContain('排除资源标题')
    expect(wrapper.text()).toContain('缺少包含标识：TEAM · 45 条')
    expect(wrapper.text()).toContain('站点搜索与资源解析 0.00 秒')
    expect(wrapper.text()).toContain('复用 10 秒前的搜索结果')
    expect(wrapper.text()).toContain('候选规格')
    expect(scroll).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    expect(wrapper.emitted('switch')).toBeUndefined()
    expect(applyCalls(api)[0][1]).not.toHaveProperty('search_cache_hours')
    expect(wrapper.text()).not.toContain('以下是上次预览')
    await setRuleDraft(wrapper, '{"include":"TEAM|GROUP"}')
    expect(wrapper.text()).toContain('以下是上次预览')
    await wrapper.findAll('button').find(button => button.text() === '重新搜索并预览').trigger('click')
    await flushPromises()
    expect(applyCalls(api)[1][1]).toMatchObject({ operation: 'preview', refresh_search: true })
    expect(wrapper.text()).not.toContain('以下是上次预览')
    wrapper.unmount()
    window.HTMLElement.prototype.scrollIntoView = previousScroll
  })

  it.each([false, true])('shows rule group comparison and only switches the draft after all matches are filtered (automatic: %s)', async automatic => {
    const data = candidateSnapshot()
    const notice = '系统默认规则组过滤了全部可匹配资源；已自动选择“不使用规则组”，确认后才会修改订阅。'
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockImplementation(async (url, submitted) => {
        if (url.endsWith('/preview-status')) return { success: true, status: 'cached' }
        const draft = JSON.parse(submitted.rule_json)
        if (automatic) draft.filter_groups = []
        const preview = { ...previewSnapshot(JSON.stringify(draft)),
          rule_groups_checked: true, rule_groups_disabled: automatic, rule_groups_auto_disabled: automatic,
          rule_group_notice: automatic ? notice : '',
          rule_group_comparison: { names: ['严格规则组'], source: '宿主默认',
            with_groups_count: automatic ? 0 : 1, without_groups_count: 2 },
        }
        return { success: true, message: '预览完成', preview }
      }),
    }
    const wrapper = mount(Config, { props: { api },
      global: { components: { ...components, VSelect: Select } } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '预览匹配').trigger('click')
    await flushPromises()
    const text = wrapper.find('.preview-results').text()
    expect(text).toContain(`考虑规则组通过 ${automatic ? 0 : 1} 条`)
    expect(text).toContain('不使用规则组通过 2 条')
    expect(text).toContain('系统默认： 严格规则组')
    expect(text.includes('已自动选择')).toBe(automatic)
    expect(wrapper.find('select[aria-label="规则组"]').element.value).toBe(automatic ? '@no-groups' : '@subscription')
    expect(wrapper.text()).not.toContain('以下是上次预览')
    expect(wrapper.text()).toContain('结果不符合预期时可选择“不使用规则组”')
    expect(api.post.mock.calls.filter(([url]) => url.endsWith('/apply'))).toHaveLength(1)
    wrapper.unmount()
  })

  it.each([
    ['preview', '预览匹配'], ['preview_replace', '预览可回刷资源'],
  ])('announces a cache miss while %s is still waiting for the search response', async (action, label) => {
    const data = candidateSnapshot()
    data.model.replace_subscription = '2'
    let finishSearch
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockImplementation((url) => url.endsWith('/preview-status')
        ? Promise.resolve({ data: { success: true, status: 'cache_miss' } })
        : new Promise(resolve => { finishSearch = resolve })),
    }
    const wrapper = mount(Config, { props: { api }, global: { components } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === label).trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="status"]').text()).toContain('没有可复用缓存，已触发搜索')
    expect(wrapper.find('[role="status"]').text()).toContain('正在等待站点响应和资源解析')
    expect(wrapper.find('.preview-results').exists()).toBe(false)
    expect(applyCalls(api)).toHaveLength(1)
    const preview = { ...previewSnapshot(data.model.rule_json), search_cached: false, search_trigger: 'cache_miss' }
    finishSearch({ success: true, message: '预览完成', preview })
    await flushPromises()
    expect(wrapper.find('.preview-results').text()).toContain('没有可复用缓存，本次已触发搜索')
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    expect(applyCalls(api)[0][1].operation).toBe(action)
    wrapper.unmount()
  })

  it.each([
    ['preview', '预览匹配', '重新搜索并预览'],
    ['preview_replace', '预览可回刷资源', '重新搜索回刷资源'],
  ])('warns about empty cached resources for %s and offers manual search beside the result', async (action, label, refreshLabel) => {
    const data = candidateSnapshot()
    data.model.replace_subscription = '2'
    let finishSearch
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockImplementation(url => url.endsWith('/preview-status')
        ? Promise.resolve({ success: true, status: 'cached_empty' })
        : new Promise(resolve => { finishSearch = resolve })),
    }
    const wrapper = mount(Config, { props: { api }, global: { components } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === label).trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="status"]').text()).toContain('搜索缓存为空')
    const emptyPreview = { ...previewSnapshot(data.model.rule_json), search_result_count: 0,
      matched_count: 0, rejected_count: 0, matched_shown: 0, rejected_shown: 0, table: null }
    finishSearch({ success: true, message: '预览完成', preview: emptyPreview })
    await flushPromises()
    expect(wrapper.find('.preview-results').text()).toContain('搜索缓存中没有资源')
    expect(wrapper.find('.preview-results').text()).toContain('建议手动重新搜索')
    expect(wrapper.find('.preview-results button').text()).toBe(refreshLabel)
    await wrapper.find('.preview-results button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="status"]').text()).toContain('已手动触发重新搜索')
    expect(applyCalls(api)[1][1]).toMatchObject({ operation: action, refresh_search: true })
    // 搜到了资源但全部被规则排除时，不应提示“空缓存”。
    const filteredPreview = { ...emptyPreview, search_result_count: 1, rejected_count: 1, rejected_shown: 1 }
    finishSearch({ success: true, message: '预览完成', preview: filteredPreview })
    await flushPromises()
    expect(wrapper.find('.preview-results').text()).not.toContain('搜索缓存中没有资源')
    expect(wrapper.find('.preview-results button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps a successful preview visible when refreshing the choice list fails', async () => {
    const data = candidateSnapshot()
    const api = {
      get: vi.fn().mockResolvedValueOnce(data).mockRejectedValue(new Error('刷新失败')),
      post: vi.fn().mockResolvedValue({ success: true, message: '预览完成', preview: previewSnapshot(data.model.rule_json) }),
    }
    const wrapper = mount(Config, { props: { api }, global: { components } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '预览匹配').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('通过资源标题')
    expect(wrapper.text()).toContain('预览完成；选择列表刷新失败')
    wrapper.unmount()
  })

  it('submits the chosen directory card with preview and confirmation and clears it when changing series', async () => {
    const data = snapshot({ managed: '已纳入管理 2 条剧集订阅', historical: [] })
    data.conf[0].content.push(
      { component: 'VSelect', props: { model: 'candidate_subscription', items: [
        { title: '#2 剧集甲', value: '2' }, { title: '#3 剧集乙', value: '3' },
      ] } },
      { component: 'VSelect', props: { model: 'candidate', items: [
        { title: '甲版本', value: '2:a', subscription_id: '2' },
        { title: '乙版本', value: '3:b', subscription_id: '3' },
      ] } },
      { component: 'VSelect', props: { model: 'download_directory', items: [
        { title: 'A盘 · /a/downloads', value: '/a/downloads' },
        { title: 'B盘 · /b/downloads', value: '/b/downloads' },
      ] } },
    )
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockResolvedValue({ success: true, message: '操作完成' }),
    }
    const wrapper = mount(Config, {
      props: { api }, global: { components: { ...components, VSelect: Select } },
    })
    await flushPromises()
    const series = wrapper.find('select[aria-label="选择有版本的剧集"]')
    const directory = wrapper.find('select[aria-label="下载目录（留空保留原目录）"]')
    expect(directory.attributes('disabled')).toBeDefined()
    await series.setValue('2')
    await wrapper.find('select[aria-label="选择该剧集版本（全集优先）"]').setValue('2:a')
    expect(directory.findAll('option').map(option => option.text())).toContain(
      'B盘 · /b/downloads')
    await directory.setValue('/b/downloads')
    await wrapper.findAll('button').find(button => button.text().includes('预览匹配')).trigger('click')
    await flushPromises()
    expect(applyCalls(api)[0][1]).toMatchObject({ operation: 'preview', download_directory: '/b/downloads' })
    await wrapper.findAll('button').find(button => button.text().includes('确认并恢复')).trigger('click')
    await flushPromises()
    expect(applyCalls(api)[1][1]).toMatchObject({ operation: 'confirm', download_directory: '/b/downloads' })
    await series.setValue('3')
    expect(directory.element.value).toBe('')
    wrapper.unmount()
  })

  it('opens a chosen page version in config with its site rules ready to edit', async () => {
    window.sessionStorage.clear()
    const pageApi = { get: vi.fn().mockResolvedValue({ page: [{
      component: 'VTable', content: [{ component: 'tbody', content: [{ component: 'tr', content: [
        { component: 'td', content: [{ component: 'VBtn', props: {
          'data-candidate-id': '2:site7',
        }, content: [{ component: 'span', text: '配置·另一个站' }] }] },
      ] }] }],
    }] }) }
    const page = mount(Page, { props: { api: pageApi }, global: { components } })
    await flushPromises()
    await page.find('button[data-candidate-id="2:site7"]').trigger('click')
    expect(page.emitted('switch')).toHaveLength(1)
    page.unmount()

    const data = snapshot({ managed: '已纳入管理 1 条：#2 剧集甲（待确认）', historical: [] })
    data.conf[0].content.push(
      { component: 'VSelect', props: { model: 'candidate_subscription', items: [
        { title: '#2 剧集甲 S01', value: '2' },
      ] } },
      { component: 'VSelect', props: { model: 'candidate', items: [
        { title: '样例站版本', value: '2:site5', subscription_id: '2', rules: { sites: [5] } },
        { title: '另一个站版本', value: '2:site7', subscription_id: '2',
          rules: { quality: 'WEB-DL', sites: [7] } },
      ] } },
    )
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockResolvedValue({ success: true, message: '预览完成' }),
    }
    const config = mount(Config, {
      props: { api },
      global: { components: { ...components, VSelect: Select } },
    })
    await flushPromises()
    expect(config.find('select[aria-label="选择有版本的剧集"]').element.value).toBe('2')
    expect(config.find('select[aria-label="选择该剧集版本（全集优先）"]').element.value).toBe('2:site7')
    expect(config.findComponent(RuleEditor).props('modelValue')).toBe(JSON.stringify({ quality: 'WEB-DL', sites: [7] }, null, 2))
    expect(config.text()).toContain('可修改规则草稿，再预览匹配')
    expect(window.sessionStorage.length).toBe(0)

    await setRuleDraft(config, '{"quality":"BluRay","sites":[7]}')
    await config.findAll('button').find(button => button.text().includes('预览匹配')).trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/SubscribeOverwatcher/apply', expect.objectContaining({
      operation: 'preview', candidate_subscription: '2', candidate: '2:site7',
      rule_json: '{"quality":"BluRay","sites":[7]}',
    }))
    await flushPromises()

    const activePage = mount(Page, { props: { api: { get: vi.fn().mockResolvedValue({ page: [{
      component: 'VBtn', props: { 'data-candidate-id': '2:site5' },
      content: [{ component: 'span', text: '配置·样例站' }],
    }] }) } }, global: { components } })
    await flushPromises()
    await activePage.find('button[data-candidate-id="2:site5"]').trigger('click')
    await flushPromises()
    expect(config.find('select[aria-label="选择该剧集版本（全集优先）"]').element.value).toBe('2:site5')
    expect(config.findComponent(RuleEditor).props('modelValue')).toBe(JSON.stringify({ sites: [5] }, null, 2))
    expect(window.sessionStorage.length).toBe(0)
    activePage.unmount()
    config.unmount()
  })

  it('selects a series before a version and clears the previous version when the series changes', async () => {
    const data = snapshot({ managed: '尚无已纳入管理的剧集订阅。', historical: [] })
    data.conf[0].content.push(
      { component: 'VSelect', props: { model: 'candidate_subscription', items: [
        { title: '#2 剧集甲 S01', value: '2' },
        { title: '#3 剧集乙 S01', value: '3' },
      ] } },
      { component: 'VSelect', props: { model: 'candidate', items: [
        { title: '甲版本 A', value: '2:a', subscription_id: '2' },
        { title: '甲版本 B', value: '2:b', subscription_id: '2' },
        { title: '乙版本', value: '3:c', subscription_id: '3' },
      ] } },
    )
    const api = {
      get: vi.fn().mockResolvedValue(data),
      post: vi.fn().mockResolvedValue({ success: true, message: '预览完成' }),
    }
    const wrapper = mount(Config, {
      props: { api },
      global: { components: { ...components, VSelect: Select } },
    })
    await flushPromises()

    const series = wrapper.find('select[aria-label="选择有版本的剧集"]')
    const version = wrapper.find('select[aria-label="选择该剧集版本（全集优先）"]')
    const preview = wrapper.findAll('button').find(button => button.text().includes('预览匹配'))
    expect(version.attributes('disabled')).toBeDefined()
    expect(preview.attributes('disabled')).toBeDefined()

    await series.setValue('2')
    expect(version.findAll('option').map(option => option.text())).toEqual(['请选择', '甲版本 A', '甲版本 B'])
    await version.setValue('2:a')
    await setRuleDraft(wrapper, '{"quality":"old"}')
    expect(preview.attributes('disabled')).toBeUndefined()

    await series.setValue('3')
    expect(version.element.value).toBe('')
    expect(version.findAll('option').map(option => option.text())).toEqual(['请选择', '乙版本'])
    expect(wrapper.findComponent(RuleEditor).exists()).toBe(false)
    expect(preview.attributes('disabled')).toBeDefined()

    await version.setValue('3:c')
    expect(preview.attributes('disabled')).toBeUndefined()
    await preview.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/SubscribeOverwatcher/apply', expect.objectContaining({
      operation: 'preview', candidate_subscription: '3', candidate: '3:c', rule_json: '',
    }))
  })

  it('keeps the page open, refreshes choices, and shows the action result', async () => {
    const initial = snapshot({
      managed: '尚无已纳入管理的剧集订阅。',
      historical: [{ title: '#1 样例剧', value: 1 }],
    })
    const after = snapshot({
      managed: '已纳入管理 1 条：#1 样例剧（待确认）',
      historical: [],
    })
    const api = {
      get: vi.fn().mockResolvedValueOnce(initial).mockImplementation(async () => ({
        ...after,
        model: {
          ...after.model,
          _last_confirmation: {
            request_id: api.post.mock.calls[0][1].operation_request_id,
            success: true,
            message: '订阅 1 已纳入管理',
          },
        },
      })),
      post: vi.fn().mockResolvedValue({ success: true, message: '订阅 1 已纳入管理' }),
    }
    const wrapper = mount(Config, { props: { api }, global: { components } })
    await flushPromises()
    const adopt = wrapper.findAll('button').find(button => button.text().includes('纳入所选剧集'))
    expect(adopt).toBeDefined()
    await adopt.trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledOnce()
    expect(api.post.mock.calls[0][0]).toBe('plugin/SubscribeOverwatcher/apply')
    expect(api.post.mock.calls[0][1]).toMatchObject({
      enabled: true, historical_subscription: [1], operation: 'adopt',
    })
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已纳入管理 1 条')
    expect(wrapper.text()).toContain('订阅 1 已纳入管理')
    const refreshedAdopt = wrapper.findAll('button').find(button => button.text().includes('纳入所选剧集'))
    expect(refreshedAdopt.attributes('disabled')).toBeDefined()
  })

  it('refreshes after a rejected action and leaves its selection available', async () => {
    const initial = snapshot({
      managed: '尚无已纳入管理的剧集订阅。',
      historical: [{ title: '#1 样例剧', value: 1 }],
    })
    const api = {
      get: vi.fn().mockResolvedValueOnce(initial).mockImplementation(async () => ({
        ...initial,
        model: {
          ...initial.model,
          _last_confirmation: {
            request_id: api.post.mock.calls[0][1].operation_request_id,
            success: false,
            message: '订阅 1 已由插件管理',
          },
        },
      })),
      post: vi.fn().mockResolvedValue({ success: false, message: '订阅 1 已由插件管理' }),
    }
    const wrapper = mount(Config, { props: { api }, global: { components } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('纳入所选剧集')).trigger('click')
    await flushPromises()

    expect(api.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('订阅 1 已由插件管理')
    expect(wrapper.findAll('button').find(button => button.text().includes('纳入所选剧集')).attributes('disabled'))
      .toBeUndefined()
  })

  it('links the selected candidate to its site detail page', async () => {
    const data = snapshot({ managed: '尚无已纳入管理的剧集订阅。', historical: [] })
    data.conf[0].content.push({ component: 'VSelect', props: {
      model: 'candidate', items: [{
        title: '样例资源', value: '1:resource', site_name: '样例站',
        page_url: 'https://tracker.example/details.php?id=42',
      }],
    } })
    data.model.candidate = '1:resource'
    const wrapper = mount(Config, {
      props: { api: { get: vi.fn().mockResolvedValue(data) } }, global: { components },
    })
    await flushPromises()

    const link = wrapper.find('a')
    expect(link.text()).toBe('样例站')
    expect(link.attributes()).toMatchObject({
      href: 'https://tracker.example/details.php?id=42',
      target: '_blank', rel: 'noopener noreferrer',
    })
  })

  it('keeps the existing detail tables visible in the custom page', async () => {
    const api = { get: vi.fn().mockResolvedValue({ page: [
      { component: 'VAlert', props: { text: '已纳入管理 1 条剧集订阅' } },
      { component: 'VTable', content: [
        { component: 'tbody', content: [
          { component: 'tr', content: [
            { component: 'td', text: '样例剧 S01' },
            { component: 'td', content: [{
              component: 'a', text: '样例站',
              props: { href: 'https://tracker.example/details.php?id=42', target: '_blank' },
            }] },
            { component: 'td', text: '资源标题' },
          ] },
        ] },
      ] },
    ] }) }
    const wrapper = mount(Page, { props: { api }, global: { components } })
    await flushPromises()

    expect(wrapper.text()).toContain('已纳入管理 1 条剧集订阅')
    expect(wrapper.text()).toContain('样例剧 S01')
    expect(wrapper.text()).toContain('资源标题')
    expect(wrapper.find('a').attributes('href')).toBe('https://tracker.example/details.php?id=42')
  })
})
