import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, test } from 'node:test'

import * as autosave from '../lib/autosave.js'
import { TranscriptLog, recordFor, transcriptFileName } from '../lib/transcript.js'
import { createAgent, createHost, tick } from './host.mjs'

let dir
beforeEach(async () => {
  dir = await mkdtemp(path.join(tmpdir(), 'mempalace-dsh-'))
})
afterEach(async () => {
  await rm(dir, { recursive: true, force: true })
})

const userSaid = (seq, text) => ({
  type: 'user/message',
  seq,
  time: 1_760_000_000_000 + seq,
  surfaceOp: 'append',
  data: { id: `m${seq}`, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } },
})

const assistantSaid = (seq, blocks) => ({
  type: 'assistant/message',
  seq,
  time: 1_760_000_000_000 + seq,
  surfaceOp: 'append',
  data: { turn: 1, step: 1, stream: [], message: { id: `m${seq}`, role: 'assistant', content: blocks, source: { kind: 'model' } } },
})

const readLines = async (file) =>
  (await readFile(file, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line))

const hookCalls = (host) => host.spawns.filter((spec) => spec.argv.includes('hook'))

async function settle(host) {
  for (let i = 0; i < 20; i += 1) await tick(5)
}

test('only words the user wrote, and assistant text, become transcript records', () => {
  const pluginContext = { ...userSaid(1, 'harness context'), data: { ...userSaid(1, 'x').data, source: { kind: 'plugin', plugin: 'hooks-claude-code' } } }
  const toolResult = { ...userSaid(2, 'tool output'), data: { ...userSaid(2, 'x').data, source: { kind: 'tool' } } }
  const summary = { ...assistantSaid(3, [{ type: 'text', text: 'summary' }]), surfaceOp: { op: 'replace', startSeq: 0, endSeq: 2 } }
  const reasoningOnly = assistantSaid(4, [{ type: 'reasoning', text: 'thinking' }])

  for (const event of [pluginContext, toolResult, summary, reasoningOnly, { type: 'turn/start', seq: 5, data: {} }]) {
    assert.equal(recordFor(event, 's', '/w'), undefined, JSON.stringify(event).slice(0, 80))
  }

  assert.deepEqual(recordFor(userSaid(6, '  keep my   spacing\n'), 's', '/w'), {
    type: 'user',
    seq: 6,
    timestamp: new Date(1_760_000_000_006).toISOString(),
    session_id: 's',
    cwd: '/w',
    message: { role: 'user', content: '  keep my   spacing\n' },
  })
  const mixed = assistantSaid(7, [
    { type: 'reasoning', text: 'hidden' },
    { type: 'text', text: 'first' },
    { type: 'tool_call', name: 'bash' },
    { type: 'text', text: 'second' },
  ])
  assert.equal(recordFor(mixed, 's', '/w').message.content, 'first\nsecond')
})

test('a turn files the transcript through the dsh stop hook without making the turn wait', async () => {
  const host = createHost({ reply: () => ({ delayMs: 30, stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { agent, session } = createAgent({ id: 'sess/1', cwd: dir })

  host.emit('session/event', session, userSaid(1, 'remember that we ship on Friday'))
  host.emit('session/event', session, assistantSaid(2, [{ type: 'text', text: 'Noted: Friday.' }]))
  const [returned] = host.emit('agent/turn-stopping', { agent, turn: 1, signal: new AbortController().signal })
  assert.equal(returned, undefined, 'turn-stopping schedules and returns')
  assert.equal(hookCalls(host).length, 0, 'no hook has started by the time the turn closes')

  await settle(host)
  const [call] = hookCalls(host)
  assert.deepEqual(call.argv, ['/usr/local/bin/mempalace', 'hook', 'run', '--hook', 'stop', '--harness', 'dsh'])
  const file = path.join(dir, transcriptFileName('sess/1'))
  assert.deepEqual(JSON.parse(call.stdio.stdin.data), {
    session_id: 'sess/1',
    transcript_path: file,
    cwd: dir,
    hook_event_name: 'Stop',
    stop_hook_active: false,
  })
  assert.equal(call.cwd, dir)

  const lines = await readLines(file)
  assert.deepEqual(
    lines.map((line) => [line.seq, line.message.role, line.message.content, line.cwd]),
    [
      [1, 'user', 'remember that we ship on Friday', dir],
      [2, 'assistant', 'Noted: Friday.', dir],
    ],
  )
})

test('compaction runs the precompact hook and never shrinks the transcript', async () => {
  const host = createHost({ reply: () => ({ stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { session } = createAgent()

  host.emit('session/event', session, userSaid(1, 'first words'))
  host.emit('session/event', session, { type: 'compaction/start', seq: 2, time: Date.now(), data: {} })
  host.emit('session/event', session, { ...assistantSaid(3, [{ type: 'text', text: 'summary of first words' }]), surfaceOp: { op: 'replace', startSeq: 1, endSeq: 1 } })
  host.emit('session/event', session, userSaid(4, 'after compaction'))
  await settle(host)

  assert.deepEqual(hookCalls(host).map((spec) => spec.argv[4]), ['precompact'])
  const lines = await readLines(path.join(dir, transcriptFileName('session-1')))
  assert.deepEqual(lines.map((line) => line.message.content), ['first words', 'after compaction'])
})

test('a disposed session gets its session-end hook', async () => {
  const host = createHost({ reply: () => ({ stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { session } = createAgent()
  host.emit('session/event', session, userSaid(1, 'short but useful'))
  host.emit('session/disposed', session)
  await settle(host)

  const [call] = hookCalls(host)
  assert.equal(call.argv[4], 'session-end')
  assert.equal(JSON.parse(call.stdio.stdin.data).hook_event_name, 'SessionEnd')
})

test('hooks run one at a time per session, and repeats already queued collapse', async () => {
  const host = createHost({ reply: () => ({ delayMs: 40, stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { agent, session } = createAgent()
  host.emit('session/event', session, userSaid(1, 'hello'))

  host.emit('agent/turn-stopping', { agent, turn: 1 })
  await tick(10)
  for (let turn = 2; turn <= 5; turn += 1) host.emit('agent/turn-stopping', { agent, turn })
  await tick(10)
  assert.equal(hookCalls(host).length, 1, 'the second run waits for the first')

  await tick(150)
  assert.equal(hookCalls(host).length, 2, 'four queued stops ran as one')
})

test('a session with nothing said yet runs no hook', async () => {
  const host = createHost()
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { agent, session } = createAgent()
  host.emit('session/event', session, { type: 'turn/start', seq: 1, time: Date.now(), data: {} })
  host.emit('agent/turn-stopping', { agent, turn: 1 })
  await settle(host)
  assert.equal(host.spawns.length, 0)
})

test('subagent sessions are not filed unless opted in', async () => {
  const host = createHost({ reply: () => ({ stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const child = createAgent({ id: 'child', origin: 'subagent' })
  host.emit('session/event', child.session, userSaid(1, 'delegated prompt'))
  host.emit('agent/turn-stopping', { agent: child.agent, turn: 1 })
  await settle(host)
  assert.equal(host.spawns.length, 0)
  await assert.rejects(readFile(path.join(dir, transcriptFileName('child'))), { code: 'ENOENT' })
})

test('a legacy block decision is reported, not silently lost', async () => {
  const host = createHost({ reply: () => ({ stdout: 'log noise\n{"decision": "block", "reason": "save now"}\n' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { agent, session } = createAgent()
  host.emit('session/event', session, userSaid(1, 'hello'))
  host.emit('agent/turn-stopping', { agent, turn: 1 })
  await settle(host)
  assert.match(host.warnings.join('\n'), /hooks\.silent_save is false/)
})

test('a failing hook is logged and the next turn still saves', async () => {
  let calls = 0
  const host = createHost({ reply: () => (++calls === 1 ? { exitCode: 2, stderr: 'palace locked' } : { stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { agent, session } = createAgent()
  host.emit('session/event', session, userSaid(1, 'hello'))
  host.emit('agent/turn-stopping', { agent, turn: 1 })
  await settle(host)
  host.emit('agent/turn-stopping', { agent, turn: 2 })
  await settle(host)

  assert.equal(hookCalls(host).length, 2)
  assert.match(host.warnings[0], /hook stop failed for session-1: exit 2: palace locked/)
})

test('a resumed session appends after what an earlier process wrote, never duplicating it', async () => {
  const file = path.join(dir, 'resumed.jsonl')
  const earlier = new TranscriptLog(file)
  await earlier.append(recordFor(userSaid(1, 'one'), 'resumed', '/w'))
  await earlier.append(recordFor(userSaid(2, 'two'), 'resumed', '/w'))

  const later = new TranscriptLog(file)
  await later.append(recordFor(userSaid(2, 'two'), 'resumed', '/w'))
  await later.append(recordFor(userSaid(3, 'three'), 'resumed', '/w'))

  assert.deepEqual((await readLines(file)).map((line) => line.seq), [1, 2, 3])
})

test('a line torn by a crash is left alone and the next record starts fresh', async () => {
  const file = path.join(dir, 'torn.jsonl')
  const intact = JSON.stringify(recordFor(userSaid(1, 'intact'), 'torn', '/w'))
  await writeFile(file, `${intact}\n{"type":"user","seq":2,"mess`)

  const log = new TranscriptLog(file)
  await log.append(recordFor(userSaid(3, 'after the crash'), 'torn', '/w'))

  const lines = (await readFile(file, 'utf8')).split('\n')
  assert.equal(lines[0], intact)
  assert.equal(lines[1], '{"type":"user","seq":2,"mess')
  assert.equal(JSON.parse(lines[2]).message.content, 'after the crash')
})

test('a session whose workspace is gone still runs its hook, with its own cwd in the payload', async () => {
  const host = createHost({ reply: () => ({ stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const gone = path.join(dir, 'deleted-workspace')
  const { session } = createAgent({ cwd: gone })
  host.emit('session/event', session, userSaid(1, 'hello'))
  host.emit('session/disposed', session)
  await settle(host)

  const [call] = hookCalls(host)
  assert.equal(call.cwd, homedir())
  assert.equal(JSON.parse(call.stdio.stdin.data).cwd, gone, 'the hook still derives the wing from the session cwd')
})

test('every session id gets its own transcript file, even on a case-insensitive filesystem', () => {
  const ids = ['a/b', 'a_b', 'a~002fb', 'Session-1', 'session-1', 'session-e4db4ca6-efe7-4ee5-92e9-d5ae15ae6159']
  const names = ids.map(transcriptFileName)
  assert.equal(new Set(names.map((name) => name.toLowerCase())).size, ids.length)
  for (const name of names) assert.match(name, /^[a-z0-9_~-]+\.jsonl$/)
  assert.equal(transcriptFileName('session-e4db4ca6-efe7-4ee5-92e9-d5ae15ae6159'), 'session-e4db4ca6-efe7-4ee5-92e9-d5ae15ae6159.jsonl')
})

test('a relative transcriptDir is resolved once, so the hook reads the file the plugin wrote', async () => {
  const host = createHost({ reply: () => ({ stdout: '{}' }) })
  const previous = process.cwd()
  process.chdir(dir)
  try {
    autosave.apply(host.ctx, { transcriptDir: 'relative-transcripts' })
  } finally {
    process.chdir(previous)
  }
  const { session } = createAgent({ cwd: previous })
  host.emit('session/event', session, userSaid(1, 'hello'))
  host.emit('session/disposed', session)
  await settle(host)

  const { transcript_path: transcriptPath } = JSON.parse(hookCalls(host)[0].stdio.stdin.data)
  assert.ok(path.isAbsolute(transcriptPath), transcriptPath)
  assert.ok(transcriptPath.endsWith(path.join('relative-transcripts', transcriptFileName('session-1'))), transcriptPath)
  assert.equal((await readLines(transcriptPath))[0].message.content, 'hello')
})

test('unloading waits for a hook still running', async () => {
  const host = createHost({ reply: () => ({ delayMs: 60, stdout: '{}' }) })
  autosave.apply(host.ctx, { transcriptDir: dir })
  const { agent, session } = createAgent()
  host.emit('session/event', session, userSaid(1, 'hello'))
  host.emit('agent/turn-stopping', { agent, turn: 1 })
  await tick(10)

  const started = Date.now()
  await host.unload()
  assert.ok(Date.now() - started >= 30, 'unload joined the in-flight hook')
})
