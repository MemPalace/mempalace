// A fake harness host for the plugin tests.
//
// Shaped after the DSH 0.1.5 APIs the rows use, as declared in the installed
// typings: `agent/session-start` and `agent/turn-stopping` (dsh-agent),
// `session/event` and `session/disposed` (dsh-session), `SystemPrompt.section`,
// `.variable` and the `system-prompt/assemble` waterfall (dsh-system-prompt),
// and `SubprocessRuntime.spawn` (dsh-subprocess). One behaviour matters above
// the rest: like `SystemPrompt.assemble`, `assemble()` resolves section text and
// variables BEFORE it runs the waterfall.

export function createHost({ reply } = {}) {
  const listeners = new Map()
  const disposers = []
  const warnings = []
  const spawns = []

  const logger = {
    warn: (...args) => warnings.push(args.join(' ')),
    info() {},
    debug() {},
  }

  const ctx = {
    on(event, handler) {
      if (!listeners.has(event)) listeners.set(event, [])
      listeners.get(event).push(handler)
      return () => {
        const list = listeners.get(event)
        list.splice(list.indexOf(handler), 1)
      }
    },
    effect(execute) {
      const dispose = execute()
      disposers.push(dispose)
      return dispose
    },
    logger: () => logger,
    subprocess: {
      async resolveExecutable(command) {
        return `/usr/local/bin/${command}`
      },
      spawn(spec) {
        spawns.push(spec)
        return fakeHandle(spec, reply?.(spec) ?? {})
      },
    },
  }

  return {
    ctx,
    spawns,
    warnings,
    /** Dispatch an event to every listener; returns what each returned. */
    emit: (event, ...args) => (listeners.get(event) ?? []).map((handler) => handler(...args)),
    /** Run every effect disposer, as unloading the plugin would. */
    unload: () => Promise.all(disposers.map((dispose) => dispose())),
  }
}

// `until` is a promise the test controls: the run ends when it settles, never on
// a timer, so a test can hold a run open for exactly as long as it needs.
function fakeHandle(spec, { exitCode = 0, stdout = '', stderr = '', delayMs = 0, hang = false, until }) {
  const reader = (text) => ({ readFrom: (from) => ({ text: text.slice(from), nextOffset: text.length, lossy: false }) })
  const done = new Promise((resolve) => {
    const aborted = () => resolve({ exitCode: null, signal: 'SIGTERM' })
    if (spec.signal?.aborted) return aborted()
    spec.signal?.addEventListener('abort', aborted, { once: true })
    if (until !== undefined) until.then(() => resolve({ exitCode, signal: null }))
    else if (!hang) setTimeout(() => resolve({ exitCode, signal: null }), delayMs)
  })
  return {
    stdin: undefined,
    stdout: undefined,
    stderr: undefined,
    collected: { stdout: reader(stdout), stderr: reader(stderr) },
    done,
    terminate() {},
    waitForExit: async () => true,
  }
}

/** A live agent with its own scoped context and a prompt registry to assemble from. */
export function createAgent({ id = 'session-1', cwd = '/work/My-Project', origin } = {}) {
  const sections = new Map()
  const variables = new Map()
  const assemblers = []

  const header = { version: 3, id, createdAt: Date.now(), cwd, isSeeded: false, ...(origin ? { origin } : {}) }
  const session = { id, header }

  const agentCtx = {
    systemPrompt: {
      section(section) {
        if (sections.has(section.name)) throw new Error(`duplicate prompt section "${section.name}"`)
        sections.set(section.name, section)
        return () => sections.delete(section.name)
      },
      variable(name, provider) {
        if (variables.has(name)) throw new Error(`duplicate prompt variable "${name}"`)
        variables.set(name, provider)
        return () => variables.delete(name)
      },
    },
    on(event, handler) {
      if (event !== 'system-prompt/assemble') throw new Error(`unexpected agent-scoped listener: ${event}`)
      assemblers.push(handler)
      return () => assemblers.splice(assemblers.indexOf(handler), 1)
    },
  }

  async function assemble(context = {}) {
    const assembly = {
      sections: [...sections.values()]
        .sort((a, b) => a.order - b.order)
        .map((section) => ({
          name: section.name,
          text: typeof section.text === 'function' ? section.text(context) : section.text,
        })),
      contexts: [],
      tools: [],
      variables: Object.fromEntries([...variables].map(([name, provider]) => [name, provider(context)])),
    }
    const run = (index) =>
      index < assemblers.length ? assemblers[index](assembly, context, () => run(index + 1)) : Promise.resolve(assembly)
    return run(0)
  }

  return { agent: { id, session, ctx: agentCtx }, session, sections, variables, assemble }
}

/** `renderPrompt` semantics: strict one-pass `{{name}}` interpolation, empty sections dropped. */
export function render(assembly) {
  return assembly.sections
    .map((section) =>
      section.text.replace(/\{\{([a-z][a-z0-9_]*)\}\}/g, (_, name) => {
        const value = assembly.variables[name]
        if (value === undefined) throw new Error(`unknown prompt variable "${name}"`)
        return value
      }),
    )
    .filter((text) => text.length > 0)
    .join('\n\n')
}

export const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms))

/** Ends a wait that never comes. It is not a budget: nothing asserts it is fast. */
export const HANG_GUARD_MS = 30_000

/** Poll until `condition` (sync or async) holds; the guard only ends a test whose condition never comes true. */
export async function waitFor(condition) {
  const end = Date.now() + HANG_GUARD_MS
  while (!(await condition())) {
    if (Date.now() > end) throw new Error('condition never became true')
    await tick(2)
  }
}

/**
 * Whether `promise` settles on its own. The plugin code under test then has no
 * timer left that could end the wait, so a build that waits never settles and
 * the hang guard answers false, however fast or slow the host is.
 */
export async function settles(promise) {
  let timer
  const guard = new Promise((resolve) => {
    timer = setTimeout(() => resolve(false), HANG_GUARD_MS)
  })
  try {
    return await Promise.race([promise.then(() => true, () => true), guard])
  } finally {
    clearTimeout(timer)
  }
}

/**
 * One full turn of the event loop. A step that waits only on promises has
 * happened by then, on any host; a step that waits on I/O or a timer may not
 * have. Use it only where the step a broken build would take is promise-only.
 */
export const loopTurn = () => new Promise((resolve) => setImmediate(resolve))
