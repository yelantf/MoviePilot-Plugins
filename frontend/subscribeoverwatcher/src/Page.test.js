import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import Page from './Page.vue'

const Container = defineComponent({ template: '<div><slot /></div>' })
const Button = defineComponent({
  emits: ['click'],
  template: '<button @click="$emit(\'click\')"><slot /></button>',
})
const components = {
  VSpacer: Container,
  VProgressLinear: Container,
  VBtn: Button,
  VAlert: defineComponent({ props: ['text'], template: '<div>{{ text }}<slot /></div>' }),
  VTable: defineComponent({ template: '<table><slot /></table>' }),
}

function section(id, rows) {
  const meta = { id, title: '#' + id + ' 剧集 S01', status: '待确认',
    candidate_count: rows.length, row_count: rows.length, warning: false }
  return [
    { component: 'VAlert', _subscription: meta, props: { text: '最近扫描完成' } },
    { component: 'VTable', _subscription: meta, _candidate_table: true, content: [
      { component: 'thead', content: [{ component: 'tr', content: [{ component: 'th', text: '资源' }] }] },
      { component: 'tbody', content: rows.map(({ title, seeders, size, seen, site }) => ({
        component: 'tr', _sort: { title, seeders, size_bytes: size, last_seen: seen, site },
        content: [{ component: 'td', text: title }],
      })) },
    ] },
  ]
}

function titles(wrapper, id) {
  return wrapper.findAll('[data-subscription-id="' + id + '"] tbody tr')
    .map(row => row.find('td').text())
}

describe('data page subscription sections', () => {
  it('folds shows and sorts each subscription independently without moving unknown values to the top', async () => {
    const page = [
      { component: 'VAlert', props: { text: '当前管理 2 条' } },
      { component: 'VTable', _overview: { title: '管理概览 · 2 条' }, content: [
        { component: 'tbody', content: [{ component: 'tr', content: [{ component: 'td', text: '管理记录' }] }] },
      ] },
      ...section('2', [
        { title: 'Alpha', seeders: 1, size: 100, seen: '2026-11-11T00:00:00Z', site: '甲站' },
        { title: 'Beta', seeders: 9, size: 300, seen: '2026-11-13T00:00:00Z', site: '乙站' },
        { title: 'Unknown', seeders: null, size: null, seen: '', site: '' },
      ]),
      ...section('3', [
        { title: 'Other A', seeders: 20, size: 200, seen: '2026-11-12T00:00:00Z', site: '丙站' },
        { title: 'Other B', seeders: 2, size: 400, seen: '2026-11-10T00:00:00Z', site: '丁站' },
      ]),
    ]
    const api = { get: vi.fn().mockResolvedValue({ page }) }
    const wrapper = mount(Page, { props: { api }, global: { components } })
    await flushPromises()

    expect(wrapper.text()).toContain('当前管理 2 条')
    expect(wrapper.findAll('.section-control')).toHaveLength(3)
    expect(wrapper.text()).not.toContain('管理记录')
    expect(titles(wrapper, '2')).toEqual([])
    await wrapper.find('[data-subscription-id="2"] .section-control').trigger('click')
    expect(titles(wrapper, '2')).toEqual(['Alpha', 'Beta', 'Unknown'])
    await wrapper.find('[data-subscription-id="2"] select').setValue('seeders')
    expect(titles(wrapper, '2')).toEqual(['Beta', 'Alpha', 'Unknown'])
    await wrapper.find('[data-subscription-id="2"] .sort-direction').trigger('click')
    expect(titles(wrapper, '2')).toEqual(['Alpha', 'Beta', 'Unknown'])

    await wrapper.find('[data-subscription-id="3"] .section-control').trigger('click')
    expect(titles(wrapper, '3')).toEqual(['Other A', 'Other B'])
    await wrapper.find('[data-subscription-id="2"] select').setValue('last_seen')
    expect(titles(wrapper, '2')).toEqual(['Beta', 'Alpha', 'Unknown'])
    expect(titles(wrapper, '3')).toEqual(['Other A', 'Other B'])

    await wrapper.findAll('button').find(button => button.text() === '刷新').trigger('click')
    await flushPromises()
    expect(titles(wrapper, '2')).toEqual(['Beta', 'Alpha', 'Unknown'])
    expect(titles(wrapper, '3')).toEqual(['Other A', 'Other B'])
    wrapper.unmount()
  })
})
