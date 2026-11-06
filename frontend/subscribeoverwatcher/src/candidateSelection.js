const STORAGE_KEY = 'subscribeoverwatcher:pending-candidate'
const MAX_AGE_MS = 5 * 60 * 1000
export const CANDIDATE_SELECTION_EVENT = 'subscribeoverwatcher:candidate-selection'

export function queueCandidateSelection(candidateId) {
  if (typeof candidateId !== 'string' || !/^[1-9]\d*:[^:]+$/.test(candidateId)) return false
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ candidateId, at: Date.now() }))
    return true
  } catch {
    return false
  }
}

export function takeCandidateSelection() {
  try {
    const stored = window.sessionStorage.getItem(STORAGE_KEY)
    window.sessionStorage.removeItem(STORAGE_KEY)
    if (!stored) return ''
    const selection = JSON.parse(stored)
    const age = Date.now() - selection.at
    return typeof selection.candidateId === 'string' &&
      /^[1-9]\d*:[^:]+$/.test(selection.candidateId) &&
      Number.isFinite(age) && age >= 0 && age <= MAX_AGE_MS
      ? selection.candidateId : ''
  } catch {
    return ''
  }
}
