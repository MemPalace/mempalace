// Shared plumbing for the MemPalace DeepSeek Harness plugin: row settings, the
// one path through which the plugin runs the `mempalace` CLI, and the two
// location rules it needs (the project's wing, the harness home).
//
// Nothing here imports @deepseek-ai/*. The rows are plain function-form Cordis
// plugins, and every harness API they use arrives through `ctx`.

import { statSync } from 'node:fs'
import { readFile, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import path from 'node:path'

/** Executable resolved through the harness subprocess provider. */
export const DEFAULT_COMMAND = 'mempalace'

/** Ceiling on one CLI read; a cold interpreter start alone is 1-2s. */
export const DEFAULT_TIMEOUT_MS = 30_000

/**
 * How long a session's first model call may wait for its memory. A cold
 * `mempalace wake-up` (ChromaDB import plus the L1 read) measured 7.4s on a
 * Windows development machine; a budget below that ships the first call bare.
 */
export const DEFAULT_FIRST_ASSEMBLY_BUDGET_MS = 10_000

/** The light server's `palace_query` as `dsh-mcp-client` names it for serverName `mempalace`. */
export const DEFAULT_SEARCH_TOOL = 'mcp__mempalace__palace_query'

const STDOUT_MAX_BYTES = 1024 * 1024
const STDERR_MAX_BYTES = 64 * 1024
const GRACE_MS = 2_000

/** Normalise one row's config; unknown keys are ignored. */
export function resolveSettings(config) {
  const raw = config !== null && typeof config === 'object' ? config : {}
  return {
    command: text(raw.command) ?? DEFAULT_COMMAND,
    commandArgs: Array.isArray(raw.commandArgs) ? raw.commandArgs.filter((arg) => typeof arg === 'string') : [],
    env: stringRecord(raw.env),
    timeoutMs: positive(raw.timeoutMs) ?? DEFAULT_TIMEOUT_MS,
    wing: text(raw.wing),
    subagents: raw.subagents === true,
    firstAssemblyBudgetMs: nonNegative(raw.firstAssemblyBudgetMs) ?? DEFAULT_FIRST_ASSEMBLY_BUDGET_MS,
    searchTool: text(raw.searchTool) ?? DEFAULT_SEARCH_TOOL,
    transcriptDir: text(raw.transcriptDir),
  }
}

/** Sessions a row acts on: top-level ones, plus subagent children when opted in. */
export function isTracked(session, settings) {
  const header = session?.header
  if (header === undefined || header === null) return false
  return header.origin !== 'subagent' || settings.subagents
}

const executables = new WeakMap()

async function executableFor(ctx, settings) {
  const cached = executables.get(settings)
  if (cached !== undefined) return cached
  try {
    const resolved = await ctx.subprocess.resolveExecutable(settings.command, settings.env)
    executables.set(settings, resolved)
    return resolved
  } catch {
    // Not installed (yet): hand the bare name to spawn so it reports the real
    // error, and resolve again next time rather than caching the miss.
    return settings.command
  }
}

/**
 * Run `mempalace <args>` through the harness subprocess seam and collect it.
 *
 * Never throws. A missing executable, a non-zero exit, a timeout and an abort
 * all come back as `{ ok: false, error }`, so a broken palace degrades this
 * plugin and never the conversation.
 */
export async function runCli(ctx, settings, args, options = {}) {
  const { cwd, stdin, signal, timeoutMs = settings.timeoutMs } = options
  const timeout = AbortSignal.timeout(timeoutMs)
  const combined = signal === undefined ? timeout : AbortSignal.any([signal, timeout])
  const failure = (error, stdout = '', stderr = '') => ({
    ok: false,
    stdout,
    stderr,
    error: timeout.aborted ? `timed out after ${timeoutMs}ms` : signal?.aborted ? 'aborted' : error,
  })

  try {
    const executable = await executableFor(ctx, settings)
    const handle = ctx.subprocess.spawn({
      argv: [executable, ...settings.commandArgs, ...args],
      cwd: spawnCwd(cwd),
      stdio: {
        stdin: stdin === undefined ? 'ignore' : { data: stdin },
        stdout: { maxBytes: STDOUT_MAX_BYTES },
        stderr: { maxBytes: STDERR_MAX_BYTES },
      },
      graceMs: GRACE_MS,
      signal: combined,
      // Windows pipes default to the ANSI code page; stored words are UTF-8.
      env: { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', ...settings.env },
    })
    const outcome = await handle.done
    const stdout = handle.collected.stdout?.readFrom(0).text ?? ''
    const stderr = handle.collected.stderr?.readFrom(0).text ?? ''
    if (outcome.exitCode === 0) return { ok: true, stdout, stderr }
    return failure(`exit ${outcome.exitCode ?? outcome.signal}`, stdout, stderr)
  } catch (error) {
    return failure(error instanceof Error ? error.message : String(error))
  }
}

/**
 * The session's workspace when it still exists, else the home directory. A
 * missing cwd fails the spawn as ENOENT on the executable itself, and a session
 * can outlive its workspace (a session-end hook runs after a checkout is gone).
 */
function spawnCwd(cwd) {
  if (typeof cwd === 'string' && cwd.length > 0) {
    try {
      if (statSync(cwd).isDirectory()) return cwd
    } catch {
      // Moved or deleted since the session began.
    }
  }
  return homedir()
}

/** One-line failure description: the error plus the tail of stderr. */
export function describeFailure(result) {
  const stderr = result.stderr.trim()
  if (stderr.length === 0) return result.error
  const tail = stderr.length > 400 ? `…${stderr.slice(-400)}` : stderr
  return `${result.error}: ${tail}`
}

/**
 * The wing `mempalace mine <cwd>` files this project's drawers under, so recall
 * reads the wing the project actually lives in. Mirrors `miner.load_config`:
 * `wing:` from mempalace.yaml (or the legacy mempal.yaml), else the directory
 * name through `config.normalize_wing_name`.
 *
 * @returns the wing, or undefined to wake up the whole palace: when the
 *   directory yields no usable name, or when the configured `wing:` uses YAML
 *   this reader cannot read exactly (guessing would read a wing the miner
 *   never files into).
 */
export async function projectWing(cwd) {
  if (typeof cwd !== 'string' || cwd.length === 0) return undefined
  const dir = path.resolve(cwd)
  for (const file of ['mempalace.yaml', 'mempal.yaml']) {
    const config = path.join(dir, file)
    // A regular file only, as the miner checks: reading a FIFO would block
    // until a writer appears, and this runs before any CLI timeout applies.
    if (!(await isRegularFile(config))) continue
    let yaml
    try {
      yaml = await readFile(config, 'utf8')
    } catch {
      break
    }
    const wing = yamlWing(yaml)
    if (wing === null) return undefined
    return wing ?? normalizeWingName(path.basename(dir))
  }
  return normalizeWingName(path.basename(dir))
}

async function isRegularFile(file) {
  try {
    return (await stat(file)).isFile()
  } catch {
    return false
  }
}

/**
 * The top-level `wing:` scalar of a mempalace.yaml, read the way YAML reads it.
 *
 * @returns the wing; undefined when there is no top-level `wing:` or it is
 *   empty; null when the value uses YAML this reader does not handle (flow or
 *   block syntax, anchors, tags, an unknown escape, a quote left open).
 */
export function yamlWing(yaml) {
  const match = /^wing:(?:[ \t]+(.*))?$/m.exec(yaml.replace(/\r\n?/g, '\n'))
  if (match === null) return undefined
  return yamlScalar(match[1] ?? '')
}

const DOUBLE_QUOTED_ESCAPES = { '"': '"', '\\': '\\', '/': '/', t: '\t', n: '\n' }

function yamlScalar(raw) {
  const value = raw.trim()
  if (value.length === 0) return undefined
  if (value[0] === "'" || value[0] === '"') return quotedScalar(value)
  if (/^[[{&*!|>%@`]/.test(value)) return null
  // In a plain scalar a comment starts only at a `#` preceded by whitespace.
  const plain = value.replace(/(^|[ \t])#.*$/, '').trim()
  return plain.length > 0 ? plain : undefined
}

function quotedScalar(value) {
  const quote = value[0]
  let result = ''
  for (let i = 1; i < value.length; i += 1) {
    const char = value[i]
    if (char === quote) {
      // Inside single quotes, a doubled quote is one literal quote.
      if (quote === "'" && value[i + 1] === "'") {
        result += "'"
        i += 1
        continue
      }
      const rest = value.slice(i + 1)
      // After the closing quote YAML allows only whitespace and a comment.
      if (rest.trim().length > 0 && !/^[ \t]+#/.test(rest)) return null
      return result.length > 0 ? result : undefined
    }
    if (quote === '"' && char === '\\') {
      const escaped = DOUBLE_QUOTED_ESCAPES[value[i + 1]]
      if (escaped === undefined) return null
      result += escaped
      i += 1
      continue
    }
    result += char
  }
  return null
}

/** Port of `mempalace.config.normalize_wing_name`. */
export function normalizeWingName(name) {
  const wing = name.toLowerCase().replaceAll(' ', '_').replaceAll('-', '_').replace(/^_+|_+$/g, '')
  return wing.length > 0 ? wing : undefined
}

/** The harness data root, resolved the way `dsh-home-paths` does: $DSH_HOME, else ~/.dsh. */
export function dshHome(env = process.env) {
  const configured = env.DSH_HOME?.trim()
  if (!configured) return path.join(homedir(), '.dsh')
  if (configured === '~') return homedir()
  if (configured.startsWith('~/') || configured.startsWith('~\\')) return path.join(homedir(), configured.slice(2))
  return path.resolve(configured)
}

function text(value) {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : undefined
}

function positive(value) {
  return Number.isFinite(value) && value > 0 ? value : undefined
}

function nonNegative(value) {
  return Number.isFinite(value) && value >= 0 ? value : undefined
}

function stringRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return {}
  return Object.fromEntries(Object.entries(value).filter(([, entry]) => typeof entry === 'string'))
}
