import { defineComponent, h, resolveComponent } from 'vue'

function renderNode(node, actions) {
  if (node.component === 'th' && node._sort_field && actions.onSortHeader) {
    const active = actions.sortField === node._sort_field
    const direction = active ? (actions.sortDescending ? 'descending' : 'ascending') : 'none'
    return h('th', { ...(node.props || {}), 'aria-sort': direction }, [
      h('button', {
        type: 'button',
        class: ['candidate-sort-header', active && 'candidate-sort-active'],
        'data-sort-field': node._sort_field,
        title: '点击排序；再次点击切换方向；第三次恢复默认顺序',
        onClick: () => actions.onSortHeader(node._sort_field),
      }, [
        h('span', node.text || ''),
        h('span', { class: 'candidate-sort-indicator', 'aria-hidden': 'true' },
          active ? (actions.sortDescending ? '↓' : '↑') : '↕'),
      ]),
    ])
  }
  const component = node.component.startsWith('V') ? resolveComponent(node.component) : node.component
  const children = (node.content || []).map(child => renderNode(child, actions))
  const candidateId = node.component === 'VBtn' && node.props?.['data-candidate-id']
  const nodeProps = candidateId && actions.onSelectCandidate
    ? { ...node.props, onClick: () => actions.onSelectCandidate(candidateId) }
    : node.props || {}
  if (typeof component === 'string') {
    return h(component, nodeProps, children.length ? children : node.text || '')
  }
  return h(component, nodeProps, children.length ? { default: () => children } : undefined)
}

export default defineComponent({
  name: 'SchemaNode',
  props: {
    node: { type: Object, required: true },
    onSelectCandidate: { type: Function, default: undefined },
    onSortHeader: { type: Function, default: undefined },
    sortField: { type: String, default: 'original' },
    sortDescending: { type: Boolean, default: true },
  },
  setup(props) {
    return () => renderNode(props.node, props)
  },
})
