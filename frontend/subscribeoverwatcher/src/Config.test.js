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

  it('keeps the existing detail tables visible in the custom page', async () => {
    const api = { get: vi.fn().mockResolvedValue({ page: [
      { component: 'VAlert', props: { text: '已纳入管理 1 条剧集订阅' } },
      { component: 'VTable', content: [
        { component: 'tbody', content: [
          { component: 'tr', content: [{ component: 'td', text: '样例剧 S01' }] },
        ] },
      ] },
    ] }) }
    const wrapper = mount(Page, { props: { api }, global: { components } })
    await flushPromises()

    expect(wrapper.text()).toContain('已纳入管理 1 条剧集订阅')
    expect(wrapper.text()).toContain('样例剧 S01')
  })
})
