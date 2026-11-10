// A single failed wake-up used to leave the session memory-less for its whole
// life: the section's text provider only re-reads `memory.text`, and the read
// ran exactly once. These two tests pin the retry (and its cap).
//
// Both wait on what the plugin has done, never on a fixed number of
// milliseconds: whether a read lands before or after a given assembly depends
// on how busy the machine is, and the plugin is right either way.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import * as recall from '../lib/recall.js'
import { createAgent, createHost, render, tick, waitFor } from './host.mjs'

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

const SECTION = /## MemPalace memory/

/** Assemble, a step at a time, until `done(rendered)` holds; fails after `timeoutMs`. */
async function assembleUntil(assemble, done, { timeoutMs = 5000, what }) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const rendered = render(await assemble())
    if (done(rendered)) return rendered
    assert.ok(Date.now() < deadline, `timed out after ${timeoutMs} ms waiting for ${what}`)
    await tick(5)
  }
}

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
  await waitFor(() => calls === 1, { what: 'the first wake-up read' })

  // The first read fails, so the first assembly has no memory, whether it
  // waited on that read or found it already failed and started the retry.
  const first = render(await assemble())
  assert.doesNotMatch(first, SECTION)

  const recovered = await assembleUntil(assemble, (text) => SECTION.test(text), {
    what: 'the retried read to reach an assembly',
  })
  assert.match(recovered, /recovered on a later assembly/)
  assert.equal(calls, 2, 'one retry, and none once the memory is in')

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
  await waitFor(() => calls === 1, { what: 'the first wake-up read' })

  await assembleUntil(assemble, () => calls >= 3, { what: 'the third read' })
  // Give the third read time to fail, then keep assembling. An assembly that
  // still finds it running starts nothing, so the count holds either way; the
  // pause lets most runs also exercise the cap itself.
  await tick(20)
  for (let i = 0; i < 6; i += 1) {
    await assemble()
    await tick(5)
  }

  assert.equal(calls, 3, 'cap must hold: one start read plus two retries')
  await host.unload()
})
