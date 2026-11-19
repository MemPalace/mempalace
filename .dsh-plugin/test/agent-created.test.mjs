// Regression test for the DSH 0.1.x -> 0.2.x event rename.
//
// `agent/session-start` does not exist in DSH 0.2.x; the creation announcement
// is `agent/created`. recall.js listens on both, so this proves the 0.2.x path
// actually registers the memory section, for both plausible payload shapes.

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
  '  - memory reaches the prompt through agent/created',
  '',
].join('\n')

const reply = (spec) => (spec.argv.includes('wake-up') ? { stdout: WAKEUP } : {})

for (const [label, wrap] of [
  ['{ agent } payload (DSH 0.2.x shape)', (agent) => ({ agent })],
  ['bare agent payload', (agent) => agent],
]) {
  test(`agent/created with ${label} registers the memory section`, async () => {
    const host = createHost({ reply })
    recall.apply(host.ctx, { firstAssemblyBudgetMs: 500 })

    const { agent, assemble } = createAgent({ cwd: '/work/My-Project' })
    host.emit('agent/created', wrap(agent))
    await tick(5)

    const text = render(await assemble())
    assert.match(text, /## MemPalace memory/)
    assert.match(text, /memory reaches the prompt through agent\/created/)

    await host.unload()
  })
}

test('a second event for the same agent does not register twice', async () => {
  const host = createHost({ reply })
  recall.apply(host.ctx, { firstAssemblyBudgetMs: 500 })

  const { agent, assemble } = createAgent({ cwd: '/work/My-Project' })
  host.emit('agent/created', { agent })
  host.emit('agent/session-start', { agent })
  await tick(5)

  const text = render(await assemble())
  assert.equal(text.match(/## MemPalace memory/g)?.length, 1)

  await host.unload()
})
