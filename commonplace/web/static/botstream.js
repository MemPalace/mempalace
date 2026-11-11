// Stream client for bot chat: an SSE frame parser and the message sender. No DOM, no globals
// beyond fetch/TextDecoder, so tests drive both with fakes.

/** Incremental SSE parser. Feed it strings cut anywhere; onFrame({event, data}) runs per complete frame.
 * Comment lines (": ping") and frames without data are dropped. Several data lines join with "\n". */
export function createFrameParser(onFrame) {
  let buffer = "", event = "", data = [];
  const line = (text) => {
    if (text === "") {
      if (data.length) onFrame({ event: event || "message", data: data.join("\n") });
      event = ""; data = [];
    } else if (text[0] === ":") return;
    else {
      const at = text.indexOf(":");
      const field = at < 0 ? text : text.slice(0, at);
      let value = at < 0 ? "" : text.slice(at + 1);
      if (value[0] === " ") value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
  };
  return {
    push(chunk) {
      buffer += chunk;
      // A trailing "\r" may be half of a "\r\n" that the next chunk completes.
      const held = buffer.endsWith("\r") ? "\r" : "";
      const lines = (held ? buffer.slice(0, -1) : buffer).split(/\r\n|\n|\r/);
      buffer = lines.pop() + held;
      for (const text of lines) line(text);
    },
    /** The connection ended: an unterminated last line is complete, an unterminated frame is not. */
    end() {
      if (buffer) line(buffer.replace(/\r$/, ""));
      buffer = "";
      event = ""; data = [];
    },
  };
}

/** A send that never reached a stored message: HTTP refusal before the stream, or no connection. */
export class SendError extends Error {
  constructor(status, code, text) {
    super(text);
    this.status = status;
    this.code = code;
  }
}

const EVENTS = new Set(["ack", "delta", "action", "block", "done", "error", "running"]);
const REFUSALS = {
  invalid: "That message cannot be sent.",
  busy: "The bot is still answering. Wait for it, or stop it.",
  rate: "Too many messages for now. Try again in a minute.",
  conflict: "This message id was already used for different text.",
  network: "Could not reach the server. Your text is kept.",
};

async function refusal(response) {
  let payload = null;
  try { payload = await response.json(); } catch { /* a proxy or bare status line */ }
  const code = typeof payload?.code === "string" ? payload.code : response.status === 404 ? "not_found" : response.status === 429 ? "rate" : "http";
  const text = typeof payload?.error === "string" && payload.error ? payload.error : REFUSALS[code] || `Request failed (${response.status})`;
  return new SendError(response.status, code, text);
}

/** POST one message and relay the turn's stream events to onEvent(name, data).
 * Resolves {state, acked}: "done" | "error" | "running" (replay of a turn still running: poll),
 * or "dropped" (connection ended after ack without a final event: poll too).
 * Rejects with SendError when nothing was stored: 400 invalid, 404, 409 busy|conflict, 429 busy|rate, no connection.
 * Aborting the signal after ack resolves "dropped"; before ack it rejects with code "aborted". */
export async function sendMessage({ botId, text, requestId, onEvent = () => {}, signal, fetchImpl = (...args) => fetch(...args) }) {
  let response;
  try {
    response = await fetchImpl(`/api/bots/${encodeURIComponent(botId)}/messages`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ text, request_id: requestId }),
      signal,
    });
  } catch (error) {
    throw new SendError(0, signal?.aborted ? "aborted" : "network", REFUSALS.network);
  }
  if (response.status === 401) globalThis.document?.dispatchEvent?.(new Event("session-expired"));
  if (!response.ok) throw await refusal(response);
  if (!response.body?.getReader) throw new SendError(0, "network", REFUSALS.network);

  let acked = false, final = null;
  const parser = createFrameParser(({ event, data }) => {
    if (final || !EVENTS.has(event)) return;
    let body;
    try { body = JSON.parse(data); } catch { return; }
    if (event === "ack") acked = true;
    if (event === "done" || event === "error" || event === "running") final = event;
    onEvent(event, body);
  });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.end();
  } catch {
    // A dropped connection is not a failed turn: the server keeps working and stores the reply.
  }
  if (final) return { state: final, acked };
  if (!acked) throw new SendError(0, signal?.aborted ? "aborted" : "network", REFUSALS.network);
  return { state: "dropped", acked };
}
