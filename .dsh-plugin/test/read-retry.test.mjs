// A single failed wake-up used to leave the session memory-less for its whole
// life: the section's text provider only re-reads `memory.text`, and the read
// ran exactly once. These two tests pin the retry (and its cap).

import assert from 'node:assert/strict'
import { test } from 'node:test'

import * as recall from '../lib/recall.js'
import { createAgent, createHost, render, tick } from './host.mjs'

const WAKEUP = [
  'Wake-up text (~20 tokens):',
  '==================================================',
  '## L0 — IDENTITY',
  'I am Atlas.',
  '',
  '## L1 — ESSENTIAL STORY',
  '  - recovered on a later assembly',
  '',
].join('\n')

test('a failed first read is retried on a later assembly and recovers', async () => {
  let calls = 0
  const host = createHost({
    reply: (spec) => {
      if (!spec.argv.includes('wake-up')) return {}
      calls += 1
      return calls === 1 ? { exitCode: 1, stderr: 'palace busy' } : { stdout: WAKEUP }
    },
  })
  recall.apply(host.ctx, { firstAssemblyBudgetMs: 50 })

  const { agent, assemble } = createAgent({ cwd: '/work/My-Project' })
  host.emit('agent/created', { agent, source: 'resume' })
  await tick(10)

  // First assembly sees the failure and kicks the retry; it still resolves.
  const first = render(await assemble())
  assert.doesNotMatch(first, /## MemPalace memory/)

  await tick(10)
  const second = render(await assemble())
  assert.match(second, /## MemPalace memory/)
  assert.match(second, /recovered on a later assembly/)
  assert.equal(calls, 2)

  await host.unload()
})

test('a permanently broken palace is retried at most three times', async () => {
  let calls = 0
  const host = createHost({
    reply: (spec) => {
      if (!spec.argv.includes('wake-up')) return {}
      calls += 1
      return { exitCode: 1, stderr: 'still broken' }
    },
  })
  recall.apply(host.ctx, { firstAssemblyBudgetMs: 50 })

  const { agent, assemble } = createAgent({ cwd: '/work/My-Project' })
  host.emit('agent/created', { agent, source: 'startup' })
  await tick(10)

  for (let i = 0; i < 6; i += 1) {
    await assemble()
    await tick(5)
  }

  assert.equal(calls, 3, 'cap must hold: one start read plus two retries')
  await host.unload()
})
