export class ApiError extends Error {
  constructor(status, payload) {
    const reason = payload?.error || (status === 403 ? "You are not allowed to do that here. Reload the page; if it is still refused, ask the workspace owner." : `Request failed (${status})`);
    super(payload?.next_step ? `${reason} ${payload.next_step}` : reason);
    this.status = status;
    this.payload = payload;
  }
}

export async function api(path, options = {}) {
  const { timeoutMs = 0, ...requestOptions } = options;
  if (timeoutMs > 0) {
    const controller = new AbortController();
    const abort = () => controller.abort(requestOptions.signal.reason);
    if (requestOptions.signal?.aborted) abort();
    else requestOptions.signal?.addEventListener("abort", abort, { once: true });
    let timer;
    try {
      // Race the complete read, including JSON decoding; abort alone may not settle it.
      return await Promise.race([
        api(path, { ...requestOptions, signal: controller.signal }),
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            reject(new Error(`Request timed out: ${path}`));
            controller.abort();
          }, timeoutMs);
        }),
      ]);
    } finally {
      clearTimeout(timer);
      requestOptions.signal?.removeEventListener("abort", abort);
    }
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: { "Accept": "application/json", ...((options.body || ["POST", "PATCH", "DELETE"].includes(options.method?.toUpperCase())) ? { "Content-Type": "application/json" } : {}), ...options.headers },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = null;
  if (response.status !== 204) {
    try { payload = await response.json(); } catch (error) {
      // A refusal without a JSON body (a proxy or a bare status line) still gets a readable message.
      if (response.ok) throw error;
    }
  }
  if (!response.ok) {
    if (response.status === 401) document.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, payload);
  }
  return payload;
}

export const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const people = new Map();
export function setPeople(map) { people.clear(); for (const [ref, name] of Object.entries(map || {})) people.set(ref, name); }
export const actor = (value) => people.get(String(value)) || String(value || "—").replace(/^(conn:|user:)/, "");
export function elapsed(value) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000));
  if (!Number.isFinite(minutes)) return "—";
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}
export function safeLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

// A bare owner/repository uses GitHub; fully qualified repositories retain their host.
export function pullRequestUrl(card) {
  const direct = safeLink(card.pr_url);
  if (direct && /^\/.+\/.+\/(?:pull|pulls|merge_requests)\/[1-9]\d*\/?$/.test(new URL(direct).pathname)) return direct;
  if (!/^[1-9]\d*$/.test(String(card.pr_number || ""))) return null;
  let repo = String(card.repo || "").replace(/^https?:\/\//, "").replace(/\.git\/?$/, "").replace(/\/$/, "");
  if (/^[^/.]+\/[^/]+$/.test(repo)) repo = `github.com/${repo}`;
  if (!/^[a-z0-9.-]+\/[^?#\s/]+\/[^?#\s]+$/i.test(repo)) return null;
  return safeLink(`https://${repo}/pull/${card.pr_number}`);
}
