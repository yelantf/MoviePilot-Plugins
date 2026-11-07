import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import RuleEditor from './RuleEditor.vue'

const Container = defineComponent({ template: '<div><slot /></div>' })
const Select = defineComponent({
  props: ['modelValue', 'items', 'label'], emits: ['update:modelValue'],
  template: `<select :aria-label="label" :value="modelValue"
    @change="$emit('update:modelValue', $event.target.value)">
    <option v-for="item in items" :key="item.value" :value="item.value">{{ item.title }}</option>
  </select>`,
})
const TextField = defineComponent({
  props: ['modelValue', 'label'], emits: ['update:modelValue'],
  template: `<input :aria-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
})
const Combo = defineComponent({
  props: ['modelValue', 'items', 'label'], emits: ['update:modelValue'],
  template: `<div><input :aria-label="label" :value="typeof modelValue === 'object' ? modelValue?.title ?? '' : modelValue"
    @input="$emit('update:modelValue', $event.target.value)" />
    <button v-for="item in items" :key="item.title" @click="$emit('update:modelValue', item)">{{ item.title }}</button>
  </div>`,
})
const Button = defineComponent({ emits: ['click'], template: '<button @click="$emit(\'click\')"><slot /></button>' })
function editor(modelValue = '') {
  const candidate = { value: '2:a', rules: {
    quality: { candidate: 'WEB-DL / WEBRip' }, resolution: { candidate: '1080p / 1080i' },
    include: { candidate: 'TEAM + CUSTOM' }, sites: [5],
  } }
  let wrapper
  wrapper = mount(RuleEditor, { props: {
    candidate, modelValue, options: {
      quality: [{ title: '全部', value: null }, { title: 'BluRay', value: 'Blu-?Ray' }],
      resolution: [{ title: '全部', value: null }, { title: '4K', value: '4K|2160p|x2160' }],
    },
    'onUpdate:modelValue': value => wrapper.setProps({ modelValue: value }),
  }, global: { components: {
    VRow: Container, VCol: Container, VAlert: Container, VSelect: Select,
    VTextField: TextField, VCombobox: Combo, VBtn: Button,
  } } })
  return wrapper
}

describe('subscription style rule editor', () => {
  it('shows candidate names and normal options and writes selected patterns without a JSON input', async () => {
    const wrapper = editor()
    expect(wrapper.find('select[aria-label="质量"]').element.value).toBe('@candidate')
    expect(wrapper.find('input[aria-label="包含（关键字、正则式）"]').element.value).toBe('TEAM + CUSTOM（候选标识）')
    expect(wrapper.find('textarea').exists()).toBe(false)
    await wrapper.find('select[aria-label="质量"]').setValue('Blu-?Ray')
    await wrapper.find('select[aria-label="分辨率"]').setValue('4K|2160p|x2160')
    await wrapper.find('input[aria-label="包含（关键字、正则式）"]').setValue('TEAM\\.(A|B)')
    await flushPromises()
    expect(JSON.parse(wrapper.props('modelValue'))).toEqual({
      quality: 'Blu-?Ray', resolution: '4K|2160p|x2160', include: 'TEAM\\.(A|B)',
    })
    await wrapper.findAll('button').find(button => button.text() === 'TEAM + CUSTOM（候选标识）').trigger('click')
    await flushPromises()
    expect(JSON.parse(wrapper.props('modelValue')).include).toEqual({ candidate: 'TEAM + CUSTOM' })
    wrapper.unmount()
  })

  it('supports keeping the original rule, clearing it, and explicit custom regex editing', async () => {
    const wrapper = editor()
    const quality = wrapper.find('select[aria-label="质量"]')
    await quality.setValue('@subscription')
    await flushPromises()
    expect(JSON.parse(wrapper.props('modelValue')).quality).toEqual({ subscription: true })
    await quality.setValue('@custom')
    await flushPromises()
    await wrapper.find('input[aria-label="自定义质量正则"]').setValue('WEB.*DL')
    await flushPromises()
    expect(JSON.parse(wrapper.props('modelValue')).quality).toBe('WEB.*DL')
    await quality.setValue('')
    await flushPromises()
    expect(JSON.parse(wrapper.props('modelValue')).quality).toBe(null)
    await wrapper.findAll('button').find(button => button.text() === '重置为候选规则').trigger('click')
    await flushPromises()
    expect(wrapper.props('modelValue')).toBe('')
    expect(quality.element.value).toBe('@candidate')
    wrapper.unmount()
  })

  it('loads an existing custom rule into an editable field without changing it', () => {
    const wrapper = editor('{"quality":"CUSTOM.*SOURCE","exclude":"sample|preview"}')
    expect(wrapper.find('select[aria-label="质量"]').element.value).toBe('@custom')
    expect(wrapper.find('input[aria-label="自定义质量正则"]').element.value).toBe('CUSTOM.*SOURCE')
    expect(wrapper.find('input[aria-label="排除（关键字、正则式）"]').element.value).toBe('sample|preview')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    wrapper.unmount()
  })
})
