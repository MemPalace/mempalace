// The session transcript MemPalace's hook runner reads.
//
// DSH stores sessions zstd-compressed, and its own hook bridge hands hooks an
// empty `transcript_path`, so the plugin keeps a transcript of its own: one
// append-only JSONL file per session, built from the harness's post-commit
// `session/event` feed. Append-only is the point. Compaction later replaces the
// model's view of old turns, but those turns are already in this file, verbatim,
// and nothing here ever rewrites or deletes a line.

import { appendFile, mkdir, readFile } from 'node:fs/promises'
import path from 'node:path'

/**
 * A session id as a transcript file name. The encoding is injective even on
 * case-insensitive filesystems, so two sessions never share a file, and it uses
 * only characters the hook runner's path validator and every filesystem accept:
 * `a-z`, `0-9`, `_` and `-` pass through, and any other UTF-16 code unit
 * (uppercase letters and `~` included) becomes `~` plus four hex digits.
 */
export function transcriptFileName(sessionId) {
  const encoded = String(sessionId).replace(
    /[^a-z0-9_-]/g,
    (char) => `~${char.charCodeAt(0).toString(16).padStart(4, '0')}`,
  )
  return `${encoded}.jsonl`
}

/** The text blocks of a message, verbatim; reasoning, tool calls and images are not text. */
export function textOf(content) {
  if (!Array.isArray(content)) return ''
  return content
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
}

/**
 * The transcript record for one session event, or undefined when the event is
 * not conversation.
 *
 * Only events that APPENDED to the session surface count: a replacement is a
 * compaction summary, the model's rewrite of history rather than anything
 * anyone said. A user-role message counts only when the user wrote it. Harness
 * context, plugin injections and tool results are user-role messages too, and
 * filing them would put the harness's words in the user's mouth.
 */
export function recordFor(event, sessionId, cwd) {
  if (event?.surfaceOp !== 'append' || !Number.isSafeInteger(event.seq)) return undefined
  let message
  if (event.type === 'user/message' && event.data?.source?.kind === 'user') message = event.data
  else if (event.type === 'assistant/message') message = event.data?.message
  else return undefined

  const content = textOf(message?.content)
  if (content.trim().length === 0) return undefined
  return {
    type: message.role,
    seq: event.seq,
    timestamp: new Date(Number.isFinite(event.time) ? event.time : Date.now()).toISOString(),
    session_id: String(sessionId),
    cwd: cwd ?? '',
    message: { role: message.role, content },
  }
}

/** One session's transcript file. Appends run strictly in arrival order. */
export class TranscriptLog {
  /**
   * @param file - absolute path of the session's JSONL transcript.
   * @param onError - called with a failed append's error; the log keeps going.
   */
  constructor(file, onError = () => {}) {
    this.file = file
    this.onError = onError
    /** Highest seq on disk; undefined until the file is first read. */
    this.lastSeq = undefined
    this.needsNewline = false
    this.tail = Promise.resolve()
  }

  /** True once the file holds at least one record. */
  get hasRecords() {
    return this.lastSeq !== undefined && this.lastSeq >= 0
  }

  /** Queue one record. Never rejects. */
  append(record) {
    this.tail = this.tail.then(() => this.write(record)).catch((error) => this.onError(error))
    return this.tail
  }

  /** Settles after every queued append has been attempted. */
  flushed() {
    return this.tail
  }

  async write(record) {
    if (this.lastSeq === undefined) await this.load()
    // A seq already on disk was written by an earlier process for this session.
    if (record.seq <= this.lastSeq) return
    await mkdir(path.dirname(this.file), { recursive: true })
    const line = `${JSON.stringify(record)}\n`
    await appendFile(this.file, this.needsNewline ? `\n${line}` : line, 'utf8')
    this.needsNewline = false
    this.lastSeq = record.seq
  }

  async load() {
    let existing
    try {
      existing = await readFile(this.file, 'utf8')
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error
      this.lastSeq = -1
      return
    }
    let last = -1
    for (const line of existing.split('\n')) {
      if (line.trim().length === 0) continue
      try {
        const seq = JSON.parse(line).seq
        if (Number.isSafeInteger(seq) && seq > last) last = seq
      } catch {
        // A line torn by a crash mid-append. It stays as it is; the next
        // record starts on a fresh line after it.
      }
    }
    this.lastSeq = last
    this.needsNewline = existing.length > 0 && !existing.endsWith('\n')
  }
}
