import { defineComponent, h, resolveComponent } from 'vue'

function renderNode(node, onSelectCandidate) {
  const component = node.component.startsWith('V') ? resolveComponent(node.component) : node.component
  const children = (node.content || []).map(child => renderNode(child, onSelectCandidate))
  const candidateId = node.component === 'VBtn' && node.props?.['data-candidate-id']
  const nodeProps = candidateId && onSelectCandidate
    ? { ...node.props, onClick: () => onSelectCandidate(candidateId) }
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
  },
  setup(props) {
    return () => renderNode(props.node, props.onSelectCandidate)
  },
})
