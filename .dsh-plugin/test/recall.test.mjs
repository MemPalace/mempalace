import assert from 'node:assert/strict'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'

import { projectWing, yamlWing } from '../lib/palace.js'
import * as recall from '../lib/recall.js'
import { createAgent, createHost, render, tick } from './host.mjs'

const WAKEUP = [
  'Wake-up text (~40 tokens):',
  '==================================================',
  '## L0 — IDENTITY',
  'I am Atlas, working with Igor.',
  '',
  '## L1 — ESSENTIAL STORY',
  '  - chose the append-only transcript (dsh plugin)',
  '',
].join('\n')

const wakeUp = (stdout, extra = {}) => (spec) => (spec.argv.includes('wake-up') ? { stdout, ...extra } : {})

test('parseWakeup strips the banner and keeps the memory verbatim', () => {
  assert.equal(
    recall.parseWakeup(WAKEUP),
    '## L0 — IDENTITY\nI am Atlas, working with Igor.\n\n## L1 — ESSENTIAL STORY\n  - chose the append-only transcript (dsh plugin)',
  )
  assert.equal(recall.parseWakeup(WAKEUP.replaceAll('\n', '\r\n')), recall.parseWakeup(WAKEUP))
})

test('parseWakeup drops CLI hints, never stored words that mention them', () => {
  const empty = [
    'Wake-up text (~20 tokens):',
    '=====',
    '## L0 — IDENTITY\nNo identity configured. Create ~/.mempalace/identity.txt',
    '',
    '## L1 — No palace found. Run: mempalace mine <dir>',
  ].join('\n')
  assert.equal(recall.parseWakeup(empty), '')

  const busy = 'Wake-up text (~9 tokens):\n=====\n## L0 — IDENTITY\nI am Atlas.\n\n## Palace is busy — another MemPalace process holds the write lock.\n'
  assert.equal(recall.parseWakeup(busy), '## L0 — IDENTITY\nI am Atlas.')

  const quoted = WAKEUP.replace('chose the append-only', 'fixed "No palace found" being shown for a locked palace; chose the append-only')
  assert.match(recall.parseWakeup(quoted), /fixed "No palace found" being shown/)
})

test('the first model call of a session waits for its memory and gets it', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP, { delayMs: 50 }) })
  recall.apply(host.ctx, {})
  const { agent, assemble } = createAgent()

  host.emit('agent/session-start', { agent, source: 'startup' })
  const prompt = render(await assemble())

  assert.match(prompt, /^## MemPalace memory\n\nYour stored memory for wing `my_project`/)
  assert.match(prompt, /I am Atlas, working with Igor\./)
  assert.match(prompt, /`mcp__mempalace__palace_query`, passing a PQL `query` such as `FIND "terms" LIMIT 5` to search every wing/)
  assert.match(prompt, /`FIND "terms" IN my_project LIMIT 5` for this wing only/)
  assert.deepEqual(host.spawns[0].argv, ['/usr/local/bin/mempalace', 'wake-up', '--wing', 'my_project'])
})

test('pointed at the full server, the memory section gives plain wing guidance instead of PQL', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, { searchTool: 'mcp__mempalace__mempalace_search' })
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  const prompt = render(await assemble())
  assert.match(prompt, /`mcp__mempalace__mempalace_search` \(leave out its `wing` argument to search every wing\)/)
  assert.doesNotMatch(prompt, /FIND/)
})

test('later assemblies serve the cached memory without another read', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, {})
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })
  await assemble()

  const prompt = render(await assemble())
  assert.match(prompt, /I am Atlas/)
  assert.equal(host.spawns.length, 1)
})

test('memory is delivered through a variable, so stored {{braces}} never break assembly', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP.replace('I am Atlas', 'I template with {{unknown_name}} and {{')) })
  recall.apply(host.ctx, {})
  const { agent, assemble, sections } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  const prompt = render(await assemble())
  assert.match(prompt, /I template with \{\{unknown_name\}\} and \{\{/)
  assert.equal(sections.get(recall.SECTION_NAME).text(), recall.SECTION_TEXT)
})

test('session-start re-fired by compact or clear registers nothing twice', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, {})
  const { agent, assemble } = createAgent()

  host.emit('agent/session-start', { agent, source: 'startup' })
  await assemble()
  assert.doesNotThrow(() => host.emit('agent/session-start', { agent, source: 'compact' }))
  assert.doesNotThrow(() => host.emit('agent/session-start', { agent, source: 'clear' }))

  assert.equal(host.spawns.length, 1)
  assert.equal(host.warnings.length, 0)
})

test('a slow palace costs the first call at most the budget, then memory lands', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP, { delayMs: 150 }) })
  recall.apply(host.ctx, { firstAssemblyBudgetMs: 20 })
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  const started = Date.now()
  const first = render(await assemble())
  assert.ok(Date.now() - started < 140, 'the first assembly did not wait for the whole read')
  assert.equal(first, '')

  await tick(200)
  assert.match(render(await assemble()), /I am Atlas/)
})

test('an aborted turn stops waiting for memory at once', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP, { delayMs: 500 }) })
  recall.apply(host.ctx, {})
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  const controller = new AbortController()
  const started = Date.now()
  const pending = assemble({ signal: controller.signal })
  controller.abort()
  await pending
  assert.ok(Date.now() - started < 200)
})

test('subagent sessions get no memory unless opted in', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, {})
  const child = createAgent({ id: 'child', origin: 'subagent' })
  host.emit('agent/session-start', { agent: child.agent, source: 'startup' })
  assert.equal(render(await child.assemble()), '')
  assert.equal(host.spawns.length, 0)

  const optedIn = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(optedIn.ctx, { subagents: true })
  const other = createAgent({ id: 'child-2', origin: 'subagent' })
  optedIn.emit('agent/session-start', { agent: other.agent, source: 'startup' })
  assert.match(render(await other.assemble()), /I am Atlas/)
})

test('a failing CLI leaves the prompt untouched and warns once', async () => {
  const host = createHost({ reply: () => ({ exitCode: 1, stderr: 'Traceback...\nModuleNotFoundError: chromadb' }) })
  recall.apply(host.ctx, {})

  for (const id of ['a', 'b']) {
    const { agent, assemble } = createAgent({ id })
    host.emit('agent/session-start', { agent, source: 'startup' })
    assert.equal(render(await assemble()), '')
  }
  assert.equal(host.warnings.length, 1)
  assert.match(host.warnings[0], /exit 1: Traceback\.\.\.\nModuleNotFoundError: chromadb/)
})

test('a hung CLI is cut off at the timeout', async () => {
  const host = createHost({ reply: () => ({ hang: true }) })
  recall.apply(host.ctx, { timeoutMs: 30, firstAssemblyBudgetMs: 1000 })
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  assert.equal(render(await assemble()), '')
  assert.match(host.warnings[0], /timed out after 30ms/)
})

test('the spawn spec is fully explicit, and Python is told to speak UTF-8', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, { command: 'python', commandArgs: ['-m', 'mempalace'], env: { MEMPALACE_PALACE_PATH: '/p' } })
  const workspace = await mkdtemp(path.join(tmpdir(), 'mempalace-dsh-'))
  const cwd = path.join(workspace, 'My-Project')
  await mkdir(cwd)
  try {
    const { agent, assemble } = createAgent({ cwd })
    host.emit('agent/session-start', { agent, source: 'startup' })
    await assemble()

    const [spec] = host.spawns
    assert.deepEqual(spec.argv, ['/usr/local/bin/python', '-m', 'mempalace', 'wake-up', '--wing', 'my_project'])
    assert.equal(spec.cwd, cwd)
    assert.deepEqual(spec.stdio, { stdin: 'ignore', stdout: { maxBytes: 1048576 }, stderr: { maxBytes: 65536 } })
    assert.equal(spec.graceMs, 2000)
    assert.ok(spec.signal instanceof AbortSignal)
    assert.deepEqual(spec.env, { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', MEMPALACE_PALACE_PATH: '/p' })
  } finally {
    await rm(workspace, { recursive: true, force: true })
  }
})

test('a workspace that no longer exists still gets memory, read from the home directory', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, {})
  const { agent, assemble } = createAgent({ cwd: path.join(tmpdir(), 'mempalace-dsh-gone', 'Moved-Project') })
  host.emit('agent/session-start', { agent, source: 'startup' })

  assert.match(render(await assemble()), /I am Atlas/)
  assert.equal(host.spawns[0].cwd, homedir())
  assert.deepEqual(host.spawns[0].argv.slice(1), ['wake-up', '--wing', 'moved_project'])
})

test('a configured wing wins over the workspace', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, { wing: 'people' })
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })
  await assemble()
  assert.deepEqual(host.spawns[0].argv.slice(1), ['wake-up', '--wing', 'people'])
})

test('unloading the plugin removes the section from live agents', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP) })
  recall.apply(host.ctx, {})
  const { agent, assemble, sections, variables } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })
  await assemble()

  await host.unload()
  assert.equal(sections.size, 0)
  assert.equal(variables.size, 0)
  assert.equal(render(await assemble()), '')
})

test('a palace slower than the budget delays only the first assembly, not every step', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP, { delayMs: 1500 }) })
  recall.apply(host.ctx, { firstAssemblyBudgetMs: 300 })
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  await assemble()
  const started = Date.now()
  assert.equal(render(await assemble()), '', 'memory has not arrived yet')
  assert.ok(Date.now() - started < 150, 'the second assembly did not wait the budget again')

  await tick(1500)
  assert.match(render(await assemble()), /I am Atlas/)
})

test('a turn cancelled before assembly does not wait, and leaves the wait for the next assembly', async () => {
  const host = createHost({ reply: wakeUp(WAKEUP, { delayMs: 800 }) })
  recall.apply(host.ctx, {})
  const { agent, assemble } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })

  const controller = new AbortController()
  controller.abort()
  const started = Date.now()
  await assemble({ signal: controller.signal })
  assert.ok(Date.now() - started < 400, 'an already-cancelled turn returned at once')

  assert.match(render(await assemble()), /I am Atlas/, 'the next assembly still waited for the memory')
})

test('a stored line that starts like a CLI hint stays in memory', () => {
  const stored = WAKEUP.replace(
    '  - chose the append-only transcript (dsh plugin)',
    'No palace found in the old notes, so we re-mined.\n  - chose the append-only transcript (dsh plugin)',
  )
  const memory = recall.parseWakeup(stored)
  assert.match(memory, /^No palace found in the old notes, so we re-mined\.$/m)
  assert.match(memory, /chose the append-only transcript/)
})

test('unloading the plugin cancels a wake-up still running, without warning about it', async () => {
  const host = createHost({ reply: () => ({ hang: true }) })
  recall.apply(host.ctx, { timeoutMs: 60_000 })
  const { agent } = createAgent()
  host.emit('agent/session-start', { agent, source: 'startup' })
  for (let i = 0; i < 100 && host.spawns.length === 0; i += 1) await tick(5)
  assert.equal(host.spawns.length, 1)

  const started = Date.now()
  await host.unload()
  assert.ok(Date.now() - started < 2000, 'unload did not wait for the CLI timeout')
  assert.equal(host.warnings.length, 0)
})

test('the project wing follows the miner: mempalace.yaml, then the normalised directory name', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'mempalace-dsh-'))
  try {
    const bare = path.join(root, 'Claude Code-Plugin')
    const configured = path.join(root, 'configured')
    const legacy = path.join(root, 'legacy')
    for (const dir of [bare, configured, legacy]) await mkdir(dir)
    await writeFile(path.join(configured, 'mempalace.yaml'), 'wing: "research" # pinned\nrooms:\n  - name: general\n')
    await writeFile(path.join(legacy, 'mempal.yaml'), "wing: 'old_name'\n")

    assert.equal(await projectWing(bare), 'claude_code_plugin')
    assert.equal(await projectWing(configured), 'research')
    assert.equal(await projectWing(legacy), 'old_name')
    assert.equal(await projectWing(''), undefined)
    assert.equal(yamlWing('rooms:\n  - wing: nested\n'), undefined)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
