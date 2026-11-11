import assert from 'node:assert/strict'
import { test } from 'node:test'
import { questionPreview, questionTurns, readingTurn, upsertAnalysis } from './src/history.ts'

test('analysis stays at its creating turn through polling, followups, new tasks and failures', () => {
  const user = { role: 'user', content: 'classify' }
  const reply = { role: 'assistant', content: 'created' }
  const queued = { id: 'first', task_type: 'classification', status: 'queued' }
  let history = upsertAnalysis([user, reply], queued)
  assert.deepEqual(history, [user, reply, { role: 'analysis', id: 'first', analysis: queued }])

  const later = [{ role: 'user', content: 'followup' }, { role: 'assistant', content: 'answer' }]
  history = [...history, ...later]
  const running = { ...queued, status: 'running', progress: 50 }
  const beforePoll = history
  history = upsertAnalysis(history, running)
  assert.equal(beforePoll[2].analysis, queued) // React state is never mutated.
  assert.equal(history[0], user)
  assert.equal(history[1], reply)
  assert.equal(history[2].analysis, running)
  assert.deepEqual(history.slice(3), later)

  const completed = { ...queued, status: 'succeeded', result: { model_version: 'first-model' } }
  history = upsertAnalysis(history, completed)
  history = upsertAnalysis(history, completed) // Current-route direct answers return the same envelope.
  assert.equal(history.length, 5)
  assert.equal(history[2].analysis, completed)

  const newTurn = [{ role: 'user', content: 'cluster' }, { role: 'assistant', content: 'created second' }]
  const second = { id: 'second', task_type: 'clustering', status: 'queued' }
  history = upsertAnalysis([...history, ...newTurn], second)
  const pendingMessage = { role: 'user', content: 'message added while polling' }
  history = [...history, pendingMessage]
  const failed = { ...second, status: 'failed', error: 'worker failed' }
  history = upsertAnalysis(history, failed)
  assert.equal(history.length, 9)
  assert.equal(history[2].analysis, completed)
  assert.deepEqual(history.slice(3, 7), [...later, ...newTurn])
  assert.equal(history[7].analysis, failed)
  assert.equal(history[8], pendingMessage)
  assert.equal(history.filter(entry => entry.role === 'analysis').length, 2)

  history = upsertAnalysis(history, failed) // Failed followups reuse the original slot too.
  assert.equal(history.length, 9)
  assert.equal(history[7].analysis.error, 'worker failed')
  assert.equal(history[2].analysis.result.model_version, 'first-model')
})

test('navigation uses only user turns with stable history indices and Unicode previews', () => {
  const history = [{ role: 'user', content: 'first' }, { role: 'assistant', content: 'reply' }]
  const queued = { id: 'analysis', status: 'queued' }
  const later = [...upsertAnalysis(history, queued), { role: 'user', content: 'second' }]
  const expected = [{ index: 0, content: 'first' }, { index: 3, content: 'second' }]
  assert.deepEqual(questionTurns(later), expected)
  assert.deepEqual(questionTurns(upsertAnalysis(later, { ...queued, status: 'succeeded' })), expected)
  assert.equal(questionPreview('  简短\n问题  '), '简短 问题')
  assert.equal(questionPreview('😀'.repeat(20)), '😀'.repeat(20))
  assert.equal(questionPreview('😀'.repeat(21)), '😀'.repeat(20) + '…')
  assert.equal(questionPreview(''), '')
})

test('reading turn follows scroll position through long cards, with a last-turn bottom override', () => {
  assert.equal(readingTurn([], 100, false), -1)
  assert.equal(readingTurn([], 100, true), -1)
  assert.equal(readingTurn([200, 5000, 5300], 100, false), 0)
  assert.equal(readingTurn([-4000, 800, 1100], 100, false), 0)
  assert.equal(readingTurn([-4000, 100, 400], 100, false), 1)
  assert.equal(readingTurn([-4000, -50, 80], 100, false), 2)
  assert.equal(readingTurn([-4000, 800, 1100], 100, true), 2)
})
