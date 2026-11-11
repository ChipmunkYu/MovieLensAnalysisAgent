import type { AnalysisEnvelope } from './types'

export type ChatEntry =
  | { role: 'user' | 'assistant'; content: string; tone?: 'normal' | 'error' }
  | { role: 'analysis'; id: string; analysis: AnalysisEnvelope }

export function questionTurns(history: ChatEntry[]) {
  return history.flatMap((entry, index) => entry.role === 'user' ? [{ index, content: entry.content }] : [])
}

export function questionPreview(content: string) {
  const characters = Array.from(content.replace(/\s+/gu, ' ').trim())
  return characters.slice(0, 20).join('') + (characters.length > 20 ? '…' : '')
}

export function readingTurn(tops: number[], readingLine: number, atBottom: boolean) {
  if (atBottom) return tops.length - 1
  let current = tops.length ? 0 : -1
  for (let index = 0; index < tops.length && tops[index] <= readingLine; index++) current = index
  return current
}

export function upsertAnalysis(history: ChatEntry[], analysis: AnalysisEnvelope): ChatEntry[] {
  const index = history.findIndex(entry => entry.role === 'analysis' && entry.id === analysis.id)
  const entry: ChatEntry = { role: 'analysis', id: analysis.id, analysis }
  return index < 0 ? [...history, entry] : history.map((current, position) => position === index ? entry : current)
}
