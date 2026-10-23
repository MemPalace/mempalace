// mempalace-recall: stored memory in each session's system prompt.
//
// At `agent/session-start` this row reads `mempalace wake-up` for the project's
// wing and registers one prompt section plus one prompt variable on that
// agent's own context, so both are agent-local and unwind with the agent.
// Prompt assembly then serves a cached string; the only I/O is the one read.
//
// The memory travels in a variable, not in the section text: DSH interpolates
// `{{name}}` references in section text strictly and throws on unknown ones,
// but never re-scans substituted values. Stored words are free to contain `{{`.
//
// The first model call is the hard part. DSH assembles the prompt before
// `agent/pre-step`, and resolves section text and variables before it runs the
// `system-prompt/assemble` waterfall. So the only place that can hold a
// session's first call for its memory is that waterfall, and the only way to
// deliver memory that arrived while it waited is to patch the assembly already
// built. The wait is bounded, happens once per session, and never fails a step.

import { appendFileSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

import { describeFailure, isTracked, projectWing, resolveSettings, runCli } from './palace.js'

/**
 * Temporary diagnostics for the "resumed session has no memory" investigation.
 * Appends one line per lifecycle event and per read outcome to
 * `$DSH_HOME/mempalace/recall-debug.log`, so the two candidate causes (event
 * never fired vs. read failed and cached empty) can be told apart from the file
 * instead of from inference. Never throws: diagnostics must not break the row.
 */
function debugLine(message) {
  // Off unless asked for: this is a diagnosis aid, not a log the row should
  // write on every session by default. `node --test` is filtered too, since the
  // suite drives the row hundreds of times and would bury production lines.
  if (process.env.NODE_TEST_CONTEXT || process.env.MEMPALACE_RECALL_DEBUG !== '1') return
  try {
    const home = process.env.DSH_HOME || path.join(homedir(), '.dsh')
    appendFileSync(path.join(home, 'mempalace', 'recall-debug.log'), `${new Date().toISOString()} ${message}\n`)
  } catch {
    // A missing directory or a read-only home must not affect memory.
  }
}

export const name = 'mempalace-recall'
export const inject = ['systemPrompt', 'subprocess']

export const SECTION_NAME = 'mempalace:memory'
export const VARIABLE_NAME = 'mempalace_memory'
/** After the first-party tool guidance, before structured-output and harness-source sections. */
export const SECTION_ORDER = 9800
export const SECTION_TEXT = `## MemPalace memory\n\n{{${VARIABLE_NAME}}}`

/**
 * How many times a session may (re)read its wake-up text.
 *
 * The row reads once at agent start. A single failure -- CLI error, palace
 * write lock held by another process, cold ChromaDB slower than the budget --
 * used to leave the whole session memory-less, which is indistinguishable from
 * "this project has no memory". Three attempts cover a restart race without
 * turning a permanently broken palace into a CLI spawn per model step.
 */
const MAX_READ_ATTEMPTS = 3

/** What `Layer0.render` prints when ~/.mempalace/identity.txt does not exist. */
const L0_PLACEHOLDER = '## L0 — IDENTITY\nNo identity configured. Create ~/.mempalace/identity.txt'
/** Blocks `mempalace wake-up` prints in place of L1 when the palace cannot be read. */
const L1_UNAVAILABLE = [/^## L1 — No palace found\b/, /^## Palace is busy\b/]

/**
 * The memory in `mempalace wake-up` output, verbatim, or '' when there is none.
 *
 * Drops the CLI banner, the L0 placeholder, and an L1 block that only says the
 * palace could not be read. Those are CLI hints, and a section re-sent on every
 * model step is the wrong place for them. Everything else is kept exactly.
 */
export function parseWakeup(stdout) {
  if (typeof stdout !== 'string') return ''
  const lines = stdout.replace(/\r\n/g, '\n').split('\n')
  let start = 0
  if (/^Wake-up text \(~\d+ tokens\):$/.test(lines[0] ?? '')) start = /^=+$/.test(lines[1] ?? '') ? 2 : 1

  // Blocks start only at a section heading, so a stored line that happens to
  // read like a CLI hint stays inside its block and is kept.
  const blocks = lines
    .slice(start)
    .join('\n')
    .split(/\n(?=## )/)
  const kept = blocks.filter((block) => {
    const trimmed = block.trim()
    return trimmed !== L0_PLACEHOLDER && !L1_UNAVAILABLE.some((pattern) => pattern.test(trimmed))
  })
  return kept.join('\n').replace(/\s+$/, '')
}

/** The value of the memory variable: the wake-up layer and how to go deeper. */
export function renderMemory(memory, wing, searchTool) {
  const scope = wing === undefined ? '' : ` for wing \`${wing}\``
  return [
    `Your stored memory${scope}, returned verbatim by MemPalace:`,
    '',
    memory,
    '',
    `This is only the wake-up layer. Before answering anything about past work, decisions, people or ` +
      `projects, ${searchInstruction(searchTool, wing)}, and use what it returns exactly as written. ` +
      `Never summarise or paraphrase stored words.`,
  ].join('\n')
}

/**
 * How to reach the rest of the palace with the configured tool. The light
 * server's `palace_query` takes a PQL string, so it gets an exact query to copy;
 * the terms stay quoted because PQL reads an unquoted `key:value` as a filter.
 */
export function searchInstruction(searchTool, wing) {
  if (/(^|__)palace_query$/.test(searchTool)) {
    const thisWing = wing === undefined ? '' : ` (or \`FIND "terms" IN ${wing} LIMIT 5\` for this wing only)`
    return (
      `search the full palace with \`${searchTool}\`, passing a PQL \`query\` such as ` +
      `\`FIND "terms" LIMIT 5\` to search every wing${thisWing}`
    )
  }
  return `search the full palace with \`${searchTool}\` (leave out its \`wing\` argument to search every wing)`
}

/**
 * Read and render one session's memory. Resolves '' on any failure; never
 * rejects. An aborted read is not a failure worth reporting.
 */
export async function readMemory(ctx, settings, cwd, onFailure = () => {}, signal) {
  const wing = settings.wing ?? (await projectWing(cwd))
  const args = wing === undefined ? ['wake-up'] : ['wake-up', '--wing', wing]
  const result = await runCli(ctx, settings, args, { cwd, signal })
  if (!result.ok) {
    if (!signal?.aborted) onFailure(`mempalace wake-up failed: ${describeFailure(result)}`)
    return ''
  }
  const memory = parseWakeup(result.stdout)
  return memory.length === 0 ? '' : renderMemory(memory, wing, settings.searchTool)
}

export function apply(ctx, config) {
  const settings = resolveSettings(config)
  const logger = ctx.logger('mempalace-recall')

  /** session-start fires again on the same agent after `compact` and `clear`. */
  const wired = new WeakSet()
  /** sessionId -> disposers for what this row registered on that agent. */
  const registrations = new Map()
  const reads = new Set()
  /**
   * Aborted when the plugin unloads, so a slow `wake-up` cannot hold the
   * unload open. Deliberately not tied to any turn's signal: cancelling one
   * turn must not cost the whole session its memory.
   */
  const unloading = new AbortController()
  let warned = false

  const onFailure = (message) => {
    debugLine(`read failed: ${message}`)
    // Loud once (a missing CLI is a setup problem worth seeing), quiet after.
    if (warned) logger.debug(message)
    else logger.warn(`${message} (further failures are logged at debug level)`)
    warned = true
  }

  // DSH renamed `agent/session-start` to `agent/created` between the 0.1.x line
  // this row was written against and 0.2.x. Listen on both so one build works on
  // either: `wired` already makes registration single-fire per agent, so the
  // event that does not exist simply never calls in.
  const onAgentReady = (payload) => {
    const agent = payload?.agent ?? payload
    const session = agent?.session
    const source = payload?.source ?? 'unknown'
    const tracked = isTracked(session, settings)
    debugLine(
      `event source=${source} session=${session?.id ?? '?'} cwd=${session?.header?.cwd ?? '?'} ` +
        `tracked=${tracked} wired=${agent !== undefined && wired.has(agent)}`,
    )
    if (!tracked || wired.has(agent)) return
    wired.add(agent)

    const memory = { text: '', pending: undefined, waited: false, attempts: 0 }

    /** Start (or restart) the wake-up read. A previous attempt is never awaited. */
    const startRead = () => {
      memory.attempts += 1
      const attempt = memory.attempts
      const read = readMemory(ctx, settings, session.header.cwd, onFailure, unloading.signal)
        .then((text) => {
          memory.text = text
          debugLine(`read ok attempt=${attempt} len=${text.length}`)
        })
        .catch((error) => {
          debugLine(
            `read threw attempt=${attempt}: ${error instanceof Error ? error.message : String(error)}`,
          )
          throw error
        })
        .finally(() => {
          memory.pending = undefined
          reads.delete(read)
        })
      memory.pending = read
      reads.add(read)
      return read
    }

    startRead()

    const disposers = []
    try {
      const { systemPrompt } = agent.ctx
      disposers.push(systemPrompt.variable(VARIABLE_NAME, () => memory.text))
      disposers.push(
        systemPrompt.section({
          name: SECTION_NAME,
          order: SECTION_ORDER,
          text: () => (memory.text.length > 0 ? SECTION_TEXT : ''),
        }),
      )
      disposers.push(
        agent.ctx.on('system-prompt/assemble', async (assembly, context, next) => {
          const signal = context?.signal
          // One assembly per session waits. A read slower than the budget must
          // not add the budget to every later step; later assemblies pick the
          // memory up from the providers once it lands. A turn already
          // cancelled does not wait, and leaves the wait to the next assembly.
          if (memory.pending !== undefined && !memory.waited && settings.firstAssemblyBudgetMs > 0 && !signal?.aborted) {
            memory.waited = true
            await settleWithin(memory.pending, settings.firstAssemblyBudgetMs, signal)
            if (memory.text.length > 0) deliver(assembly, memory.text)
          } else if (
            // The first read failed (CLI error, palace lock, cold-start
            // timeout) or came back empty. Without this branch one transient
            // failure leaves the whole session without memory, because the
            // section's text provider only ever re-reads `memory.text`. Retry on
            // a later assembly: no added latency, and capped so a permanently
            // broken palace cannot spawn a CLI on every single step.
            memory.text.length === 0 &&
            memory.pending === undefined &&
            memory.attempts < MAX_READ_ATTEMPTS &&
            !unloading.signal.aborted
          ) {
            memory.waited = true
            startRead()
          }
          return next()
        }),
      )
    } catch (error) {
      logger.warn(`could not register the memory section: ${error instanceof Error ? error.message : error}`)
    }
    registrations.set(session.id ?? session.header.id, disposers)
  }

  ctx.on('agent/created', onAgentReady)
  ctx.on('agent/session-start', onAgentReady)

  // The agent context unwinds its own registrations; only forget them here.
  ctx.on('session/disposed', (session) => {
    registrations.delete(session?.id ?? session?.header?.id)
  })

  ctx.effect(
    () => async () => {
      unloading.abort()
      for (const disposers of registrations.values()) {
        for (const dispose of disposers) {
          try {
            dispose()
          } catch {
            // Already unwound with its agent.
          }
        }
      }
      registrations.clear()
      await Promise.allSettled(reads)
    },
    'mempalace-recall: unregister memory sections',
  )
}

/** Put memory that arrived during the waterfall into an assembly already resolved without it. */
function deliver(assembly, text) {
  assembly.variables[VARIABLE_NAME] = text
  const section = assembly.sections.find((entry) => entry.name === SECTION_NAME)
  if (section !== undefined) section.text = SECTION_TEXT
  else assembly.sections.push({ name: SECTION_NAME, text: SECTION_TEXT })
}

/** Resolve when `promise` settles, `ms` elapses, or `signal` aborts, whichever is first. */
function settleWithin(promise, ms, signal) {
  // An abort that already happened fires no event; listening for it would wait out `ms`.
  if (signal?.aborted) return Promise.resolve()
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', finish)
      resolve()
    }
    const timer = setTimeout(finish, ms)
    signal?.addEventListener('abort', finish, { once: true })
    promise.then(finish, finish)
  })
}
