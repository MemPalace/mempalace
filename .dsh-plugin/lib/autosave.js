// mempalace-autosave: file each conversation into the palace through
// MemPalace's own hook runner.
//
//   session/event         append user-written and assistant text to the transcript
//   agent/turn-stopping   mempalace hook run --hook stop        --harness dsh
//   compaction/start      mempalace hook run --hook precompact  --harness dsh
//   session/disposed      mempalace hook run --hook session-end --harness dsh
//
// Save intervals, diary checkpoints, wing derivation, transcript mining and
// daemon write routing all stay in MemPalace. This row supplies only what DSH
// cannot hand a hook itself, a readable transcript, and the moments to act on
// it. No listener waits on a save: `agent/turn-stopping` is a serial point the
// agent loop awaits, so it only schedules.

import path from 'node:path'

import { describeFailure, dshHome, isTracked, resolveSettings, runCli } from './palace.js'
import { TranscriptLog, recordFor, transcriptFileName } from './transcript.js'

export const name = 'mempalace-autosave'
export const inject = ['subprocess']

/** A precompact hook mines synchronously; give it room. */
export const HOOK_TIMEOUT_MS = 120_000
/** How long unloading the plugin waits for hook runs still in flight. */
export const DRAIN_TIMEOUT_MS = 10_000

const HOOK_EVENT_NAMES = { stop: 'Stop', precompact: 'PreCompact', 'session-end': 'SessionEnd' }

export function apply(ctx, config) {
  const settings = resolveSettings(config)
  const logger = ctx.logger('mempalace-autosave')
  // Absolute once: the hook runs with the session's workspace as its cwd, so a
  // relative directory would name a different file there than it does here.
  const root = path.resolve(settings.transcriptDir ?? path.join(dshHome(), 'mempalace', 'transcripts'))

  /** sessionId -> { id, cwd, log, queue, runner } */
  const sessions = new Map()
  const runners = new Set()
  let writeWarned = false

  function stateFor(session) {
    const id = String(session.id ?? session.header.id)
    let state = sessions.get(id)
    if (state === undefined) {
      const log = new TranscriptLog(path.join(root, transcriptFileName(id)), (error) => {
        const message = `could not write the transcript for ${id}: ${error instanceof Error ? error.message : error}`
        if (writeWarned) logger.debug(message)
        else logger.warn(message)
        writeWarned = true
      })
      state = { id, cwd: session.header.cwd, log, queue: [], runner: undefined }
      sessions.set(id, state)
    }
    return state
  }

  /** Queue a hook for a session. One runs at a time per session; a repeat already queued is dropped. */
  function schedule(state, hook) {
    if (!state.queue.includes(hook)) state.queue.push(hook)
    if (state.runner !== undefined) return
    const runner = drain(state).finally(() => {
      state.runner = undefined
      runners.delete(runner)
    })
    state.runner = runner
    runners.add(runner)
  }

  async function drain(state) {
    while (state.queue.length > 0) {
      const hook = state.queue.shift()
      await state.log.flushed()
      // Nothing has been said in this session yet, so there is nothing to file.
      if (!state.log.hasRecords) continue
      await runHook(state, hook)
    }
  }

  async function runHook(state, hook) {
    const payload = {
      session_id: state.id,
      transcript_path: state.log.file,
      cwd: state.cwd ?? '',
      hook_event_name: HOOK_EVENT_NAMES[hook],
      stop_hook_active: false,
    }
    const result = await runCli(ctx, settings, ['hook', 'run', '--hook', hook, '--harness', 'dsh'], {
      cwd: state.cwd,
      stdin: JSON.stringify(payload),
      timeoutMs: HOOK_TIMEOUT_MS,
    })
    if (!result.ok) {
      logger.warn(`mempalace hook ${hook} failed for ${state.id}: ${describeFailure(result)}`)
      return
    }
    if (hookOutput(result.stdout)?.decision === 'block') {
      logger.warn(
        `mempalace asked the model to write the ${hook} checkpoint itself (hooks.silent_save is false). ` +
          'The DSH plugin does not relay that request, so this checkpoint was not written; ' +
          'set hooks.silent_save to true in ~/.mempalace/config.json to have MemPalace write it directly.',
      )
    }
  }

  ctx.on('session/event', (session, event) => {
    if (!isTracked(session, settings)) return
    const state = stateFor(session)
    if (event?.type === 'compaction/start') {
      schedule(state, 'precompact')
      return
    }
    const record = recordFor(event, state.id, state.cwd)
    if (record !== undefined) state.log.append(record)
  })

  ctx.on('agent/turn-stopping', ({ agent }) => {
    if (!isTracked(agent?.session, settings)) return
    schedule(stateFor(agent.session), 'stop')
  })

  ctx.on('session/disposed', (session) => {
    if (!isTracked(session, settings)) return
    const state = sessions.get(String(session.id ?? session.header.id))
    if (state === undefined) return
    sessions.delete(state.id)
    schedule(state, 'session-end')
  })

  ctx.effect(
    () => async () => {
      if (runners.size === 0) return
      let timer
      const deadline = new Promise((resolve) => {
        timer = setTimeout(resolve, DRAIN_TIMEOUT_MS)
      })
      await Promise.race([Promise.allSettled([...runners]), deadline])
      clearTimeout(timer)
    },
    'mempalace-autosave: drain hook runs',
  )
}

/** The JSON object a hook printed, if its last line is one. */
function hookOutput(stdout) {
  const last = stdout.trim().split('\n').at(-1)
  if (last === undefined || last.length === 0) return undefined
  try {
    const value = JSON.parse(last)
    return value !== null && typeof value === 'object' ? value : undefined
  } catch {
    return undefined
  }
}
