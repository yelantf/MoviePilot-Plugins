import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import Config from './Config.vue'
import Page from './Page.vue'

const Container = defineComponent({ template: '<div><slot /></div>' })
const Button = defineComponent({
  props: ['disabled', 'loading'],
  emits: ['click'],
  template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
})
const components = Object.fromEntries([
  'VCard', 'VCardTitle', 'VCardText', 'VCardActions', 'VRow', 'VCol', 'VSpacer',
  'VAlert', 'VTable', 'VProgressLinear', 'VSwitch', 'VSelect', 'VAutocomplete', 'VTextarea', 'VTextField',
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
const Textarea = defineComponent({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<textarea :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
})

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

describe('custom configuration actions', () => {
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
    expect(api.post.mock.calls[0][1]).toMatchObject({ operation: 'preview', download_directory: '/b/downloads' })
    await wrapper.findAll('button').find(button => button.text().includes('确认并恢复')).trigger('click')
    await flushPromises()
    expect(api.post.mock.calls[1][1]).toMatchObject({ operation: 'confirm', download_directory: '/b/downloads' })
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
      global: { components: { ...components, VSelect: Select, VTextarea: Textarea } },
    })
    await flushPromises()
    expect(config.find('select[aria-label="选择有版本的剧集"]').element.value).toBe('2')
    expect(config.find('select[aria-label="选择该剧集版本（全集优先）"]').element.value).toBe('2:site7')
    expect(config.find('textarea').element.value).toBe(JSON.stringify({ quality: 'WEB-DL', sites: [7] }, null, 2))
    expect(config.text()).toContain('可修改规则草稿，再预览匹配')
    expect(window.sessionStorage.length).toBe(0)

    await config.find('textarea').setValue('{"quality":"BluRay","sites":[7]}')
    await config.findAll('button').find(button => button.text().includes('预览匹配')).trigger('click')
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
    expect(config.find('textarea').element.value).toBe(JSON.stringify({ sites: [5] }, null, 2))
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
      global: { components: { ...components, VSelect: Select, VTextarea: Textarea } },
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
    await wrapper.find('textarea').setValue('{"quality":"old"}')
    expect(preview.attributes('disabled')).toBeUndefined()

    await series.setValue('3')
    expect(version.element.value).toBe('')
    expect(version.findAll('option').map(option => option.text())).toEqual(['请选择', '乙版本'])
    expect(wrapper.find('textarea').element.value).toBe('')
    expect(preview.attributes('disabled')).toBeDefined()

    await version.setValue('3:c')
    expect(preview.attributes('disabled')).toBeUndefined()
    await preview.trigger('click')
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
