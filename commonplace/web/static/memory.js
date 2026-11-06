import { api as request, escape as e, actor } from "./api.js";
import { cardId, isBlocked } from "./board.js";

const enc = encodeURIComponent;
const exact = (value) => e(value).replaceAll("\r", "&#13;");
const name = (board) => board.name || board.slug;
const key = (row) => JSON.stringify([row.board, row.kind, row.id, row.field]);

/** Local, readable time for a stored ISO stamp: "today, 16:12", "Wed 28 Oct 2026, 16:12",
 * or only the date when `dateOnly`. The ISO value stays in the <time datetime> attribute. */
export function memoryTime(value, { now = new Date(), dateOnly = false } = {}) {
  const at = new Date(value);
  if (!value || Number.isNaN(at.getTime())) return String(value ?? "");
  const clock = at.toTimeString().slice(0, 5);
  const day = at.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" }).replace(",", "");
  if (dateOnly) return day;
  return at.toDateString() === now.toDateString() ? `today, ${clock}` : `${day}, ${clock}`;
}
const stamp = (row, options) => `<time datetime="${e(row.at)}">${e(memoryTime(row.at, options))}</time>`;
const highlighted = (value, query = "") => {
  const text = String(value ?? "");
  const words = query.trim().split(/\s+/).filter(Boolean).map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!words.length) return exact(text);
  const expression = new RegExp(words.join("|"), "gi");
  let html = "", cursor = 0;
  for (const match of text.matchAll(expression)) {
    html += exact(text.slice(cursor, match.index)) + `<mark>${exact(match[0])}</mark>`;
    cursor = match.index + match[0].length;
  }
  return html + exact(text.slice(cursor));
};

export function memoryAuthor(result, board = {}) {
  if (result.kind === "reply") return result.model?.startsWith("local:") ? "Quick reply · local model" : "Quick reply · hosted provider";
  let ref = result.actor || result.author;
  if (ref && result.kind === "note" && !/^(conn:|user:)/.test(ref)) ref = `conn:${ref}`;
  if (!ref) return "";
  if (ref === board.coordinator) return `${name(board)} orchestrator`;
  return String(ref).startsWith("conn:") ? `${actor(ref)} (agent)` : actor(ref);
}

export function sourceLabel(result, board = {}) {
  if (result.kind === "note") return `Note: ${result.topic || "Untitled"}`;
  if (result.kind === "card") return `Card ${cardId(board, result.number)}`;
  return `${name(board)} chat`;
}

export function quoteDraft(result) {
  return { board: result.board, wing: result.wing, author: result.author, at: result.at, sourceLabel: result.sourceLabel, text: result.text };
}

export function memoryActions(result, destination, cards = [], me) {
  const sameWing = Boolean(result?.wing && destination?.wing && result.wing === destination.wing);
  return {
    quote: sameWing,
    link: sameWing && result?.kind === "note",
    post: sameWing && result.board === destination.slug ? cards.filter((card) => card.board === result.board && isBlocked(card, me) && !card.restricted) : [],
  };
}

// The board list is the server's readable set. Each worker finishes one board
// before taking another, so neither endpoint exceeds four concurrent requests.
export async function searchMemory({ api = request, boards, query, project = "", source = "", from = "", since = "" }) {
  const words = query.trim().split(/\s+/);
  if (!query.trim() || words.length > 8 || query.length > 200 || words.some((word) => word.length > 100)) throw new Error("Use 1–8 words, up to 200 characters.");
  const selected = boards.filter((board) => !project || board.slug === project);
  const results = [], errors = [];
  let next = 0, truncated = false;
  async function worker() {
    while (next < selected.length) {
      const board = selected[next++];
      const params = new URLSearchParams({ q: query });
      if (source && source !== "note") params.set("source", source);
      if (from) params.set("from", from);
      if (since) params.set("since", since);
      if (source !== "note") {
        try {
          const page = await api(`/api/chat/${enc(board.slug)}/search?${params}`);
          results.push(...page.results.map((row) => ({ ...row, board: board.slug, wing: board.wing, author: memoryAuthor(row, board), sourceLabel: sourceLabel(row, board) })));
          truncated ||= page.truncated;
        } catch { errors.push({ board: board.slug, source: "project" }); }
      }
      if ((!source || source === "note") && board.wing) {
        try {
          const page = await api(`/api/memory/search?${new URLSearchParams({ q: query, theme: board.wing, limit: "100" })}`);
          results.push(...page.results.filter((row) => row.theme === board.wing && (!from || row.author === from) && (!since || Date.parse(row.at) >= Date.parse(since))).map((row) => {
            const note = { ...row, actor: row.author, kind: "note", board: board.slug, wing: row.theme };
            return { ...note, author: memoryAuthor(note, board), sourceLabel: sourceLabel(note, board) };
          }));
          truncated ||= page.results.length === 100;
        } catch { errors.push({ board: board.slug, source: "note" }); }
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(4, selected.length) }, worker));
  const unique = new Map();
  for (const result of results) {
    const id = result.kind === "note" ? JSON.stringify(["note", result.wing, result.id]) : key(result);
    if (!unique.has(id)) unique.set(id, result);
  }
  return { results: [...unique.values()].sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0)), truncated, errors };
}

export async function loadNeighbours(result, api = request) {
  if (!["chat", "reply"].includes(result.kind)) return { neighbours: [], at: null };
  let id = result.id;
  if (result.kind === "reply") {
    const page = await api(`/api/chat/${enc(result.board)}?last=1&replies_after=${Number(result.id) - 1}`);
    const reply = page.replies?.find((row) => row.id === result.id);
    if (!reply) throw new Error("The source reply could not be opened.");
    id = reply.message_id;
  }
  const path = `/api/chat/${enc(result.board)}`;
  const [before, after] = await Promise.all([
    api(`${path}?before=${Number(id) + 1}&last=2`),
    api(`${path}?after=${id}`),
  ]);
  if (before.board !== result.board || after.board !== result.board) throw new Error("The source could not be opened.");
  const neighbours = [...before.messages, ...after.messages.slice(0, 1)].map((row) => ({ ...row, kind: "chat", hit: result.kind === "chat" && row.id === id }));
  if (result.kind === "reply") neighbours.splice(before.messages.length, 0, { ...result, hit: true });
  return { neighbours, at: id };
}

export async function postMemory({ result, destination, card, me, api = request }) {
  if (!memoryActions(result, destination, [card], me).post.length) throw new Error("Choose a blocked card in the source project.");
  return api(`/api/cards/${card.number}/comments`, { method: "POST", body: { text: result.text, version: card.version } });
}

export async function linkMemory({ result, destination, card, api = request }) {
  if (!memoryActions(result, destination).link || card.board !== destination.slug || card.restricted) throw new Error("Choose a card in the note's wing.");
  const response = await api("/mcp", { method: "POST", body: { jsonrpc: "2.0", id: "memory-link", method: "tools/call", params: { name: "memory_link", arguments: { id: result.id, task: card.number } } } });
  if (response.error || response.result?.isError || !response.result) throw new Error("Could not link this note. Try again.");
  return response.result;
}

export function renderMemory(state) {
  const { boards = [], results = [], filters = {}, selected, busy, error, searched } = state;
  const options = (rows, selected) => rows.map(([value, label]) => `<option value="${e(value)}"${value === (selected || "") ? " selected" : ""}>${e(label)}</option>`).join("");
  const status = busy ? `Searching…${searched ? ` showing results for “${state.completedQuery}” until done` : ""}` : searched ? `${results.length} results${state.truncated ? " · more matches available; narrow your search" : ""}` : "";
  return `<section class="memory-search" aria-label="Memory">
    <form class="memory-toolbar" data-memory-search><div class="search-big" role="search"><label class="sr" for="mq">Search memory</label><input id="mq" name="q" type="search" value="${e(filters.query)}" placeholder="Search exact words…"><button class="btn ghost" type="submit">Search <kbd>↵</kbd></button></div>
    <div class="filters"><label class="chip">Project: <select name="project">${options([["", "All"], ...boards.map((board) => [board.slug, name(board)])], filters.project)}</select></label><label class="chip">Source: <select name="source">${options([["", "Any"], ["chat", "Chat"], ["reply", "Quick replies"], ["card", "Cards"], ["note", "Notes"]], filters.source)}</select></label><label class="chip">From: <select name="from">${options([["", "Anyone"], ...Object.entries(state.people || {}), ...boards.filter((board) => board.coordinator).map((board) => [board.coordinator, `${name(board)} orchestrator`]), ...results.filter((row) => row.actor && !String(row.actor).startsWith("user:")).map((row) => [row.actor, row.author])].filter(([ref], i, all) => all.findIndex(([id]) => id === ref) === i), filters.from)}</select></label><label class="chip">When: <select name="when">${options([["", "Any time"], ["1", "Last 24 hours"], ["7", "Last 7 days"], ["30", "Last 30 days"]], filters.when)}</select></label><span class="memory-status" role="status">${e(status)}</span></div></form>
    ${error ? `<div class="memory-empty" role="alert"><h2>Search couldn't run</h2><p>${e(error)}</p><button class="btn" data-memory-action="retry">Try again</button></div>` : ""}
    ${state.errors?.length ? '<p class="memory-warning" role="alert">Some sources could not be searched. Try again for all results.</p>' : ""}
    <div class="memory-results list" aria-busy="${Boolean(busy)}">${results.map((row, i) => `<button class="res${selected && key(row) === key(selected) ? " is-selected" : ""}${busy ? " is-loading" : ""}" data-memory-result="${i}"${selected && key(row) === key(selected) ? ' aria-current="true"' : ""}><span class="src"><b>${e(row.sourceLabel)}</b><span> · ${e(row.author)} · </span>${stamp(row, { dateOnly: row.kind === "note" })}</span><span class="ex">${highlighted(row.text, busy ? state.completedQuery : filters.query)}</span></button>`).join("")}${!busy && !error && !results.length ? `<div class="memory-empty" role="status"><h2>${searched ? "No exact words match" : "Search your memory"}</h2><p>${searched ? "Memory finds the words you type, not their meaning. Try fewer or different words, or search every project." : "Exact words from chats, cards and notes. Never summarised."}</p>${searched ? '<button class="btn" data-memory-action="clear">Search all projects</button>' : ""}</div>` : ""}</div>
    <div class="keys"><span><kbd>J</kbd><kbd>K</kbd> move</span><span><kbd>↵</kbd> open source</span><span><kbd>Y</kbd> copy exact words</span>${memoryActions(selected, state.destination).link ? '<span><kbd>L</kbd> link to a card</span>' : ""}</div></section>`;
}

export function renderMemoryPanel({ result, destination, source, cards = [], me, neighbours = [], loading, panelError, notice, linking, chatOpen }) {
  if (!result) return "";
  const actions = memoryActions(result, destination, cards, me);
  const thread = neighbours.length ? neighbours : [{ ...result, hit: true }];
  const message = (row) => row.hit ? result : { ...row, author: memoryAuthor(row, source || destination) };
  const linkCards = cards.filter((card) => card.board === destination?.slug && !card.restricted);
  return `<section class="memory-panel" aria-label="Memory source"><header class="ctx-head"><h2>Memory</h2><button class="btn ghost" data-memory-action="hide">Hide <kbd>]</kbd></button></header><div class="ctx-body"><div class="detail-h"><span class="verbatim">Exact words, as saved.${["chat", "reply"].includes(result.kind) ? " Shown with the messages around it." : ""}</span><div class="src"><b>${e(result.sourceLabel)}</b><time>${e(result.at)}</time></div></div>
    ${loading ? '<p role="status">Loading source…</p>' : ""}${panelError ? `<p role="alert">${e(panelError)}</p><button class="btn" data-memory-action="source-retry">Try again</button>` : ""}
    <div class="thread">${thread.map((row) => { const msg = message(row); return `<article class="msg ${row.hit ? "hit" : "ctxmsg"}"><div class="msg-h"><b>${e(msg.author)}</b>${stamp(msg)}</div><p class="msg-b">${exact(msg.text)}</p></article>`; }).join("")}</div>
    ${actions.post.map((card) => `<div class="callout wait"><h3>This may answer blocked card ${e(cardId(destination, card.number))}</h3><p>Post this message, word for word, as a comment on ${e(cardId(destination, card.number))} in ${e(name(destination))}:</p><p class="quote">${exact(result.text)}</p><p>The comment is saved on the card. The request stays open until it is answered or closed.</p><button class="btn primary" data-memory-post="${card.number}">Post on ${e(cardId(destination, card.number))}</button></div>`).join("")}
    ${linking && actions.link ? `<form data-memory-link><label>Link note to a card in ${e(name(destination))}<select name="card" required>${linkCards.map((card) => `<option value="${card.number}">${e(cardId(destination, card.number))} · ${e(card.title)}</option>`).join("")}</select></label><button class="btn"${linkCards.length ? "" : " disabled"}>Link note</button></form>` : ""}
    ${notice ? `<p role="status">${e(notice)}</p>` : ""}</div><footer class="memory-panel-actions">${actions.quote ? `<p class="memory-destination">Message to ${e(name(destination))} chat</p><button class="btn sm" id="open-in-chat" data-memory-action="chat"${loading || panelError ? " disabled" : ""} aria-controls="chat" aria-expanded="${Boolean(chatOpen)}">Open in chat, ask about it <kbd>↵</kbd></button>` : ""}<button class="btn sm" data-memory-action="copy" aria-keyshortcuts="Y">Copy exact words <kbd>Y</kbd></button>${actions.link ? '<button class="btn sm" data-memory-action="link" aria-keyshortcuts="L">Link to card <kbd>L</kbd></button>' : ""}</footer></section>`;
}

// The shell supplies the two mounts and its dock. It owns route and header wiring.
export function createMemory({ mount, panel, api = request, boards = [], me = {}, destination = null, dock, clipboard = globalThis.navigator?.clipboard, onSelect = () => {}, singleKeys = () => true } = {}) {
  const state = { boards, people: me.people || {}, results: [], filters: {}, selected: null, destination, errors: [] };
  let searchEpoch = 0, sourceEpoch = 0, stopped = false, writing = false, returnKey = "";
  const person = typeof me === "string" ? me : me.actor;
  const draw = () => {
    if (stopped) return;
    const focused = mount.ownerDocument?.activeElement;
    let restore = null;
    if (focused && (mount.contains(focused) || panel?.contains(focused))) {
      const selector = focused.matches("[data-memory-result]") ? `[data-memory-result="${focused.dataset.memoryResult}"]`
        : focused.matches("[data-memory-action]") ? `[data-memory-action="${focused.dataset.memoryAction}"]`
        : focused.matches("[data-memory-post]") ? `[data-memory-post="${focused.dataset.memoryPost}"]`
        : focused.name && ["q", "project", "source", "from", "when", "card"].includes(focused.name) ? `[name="${focused.name}"]` : null;
      if (selector) { const owner = panel?.contains(focused) ? panel : mount; restore = () => owner.querySelector(selector)?.focus(); }
    }
    mount.innerHTML = renderMemory(state);
    if (panel) {
      panel.hidden = !state.selected;
      panel.innerHTML = renderMemoryPanel({ ...state, result: state.selected, me: person, chatOpen: dock?.isOpen?.() ?? state.chatOpen });
    }
    restore?.();
  };
  async function search(filters = state.filters) {
    const epoch = ++searchEpoch;
    ++sourceEpoch;
    state.filters = { ...filters };
    state.busy = true;
    state.error = "";
    state.selected = null;
    draw();
    try {
      const page = await searchMemory({ api, boards: state.boards, ...filters, query: filters.query || "", since: filters.when ? new Date(Date.now() - Number(filters.when) * 86400000).toISOString() : "" });
      if (stopped || epoch !== searchEpoch) return;
      Object.assign(state, page, { completedQuery: filters.query, searched: true });
    } catch (error) {
      if (stopped || epoch !== searchEpoch) return;
      state.error = error.message;
    }
    if (!stopped && epoch === searchEpoch) { state.busy = false; draw(); }
  }
  async function select(result) {
    const epoch = ++sourceEpoch;
    const board = state.boards.find((board) => board.slug === result.board);
    if (!board || board.wing !== result.wing) return;
    Object.assign(state, { selected: result, destination: board, source: board, neighbours: [], cards: [], loading: true, panelError: "", notice: "", linking: false, at: null, chatOpen: false });
    onSelect(result, board);
    draw();
    const [source, cards] = await Promise.allSettled([loadNeighbours(result, api), api(`/api/boards/${enc(board.slug)}/cards`)]);
    if (stopped || epoch !== sourceEpoch) return;
    if (source.status === "fulfilled") Object.assign(state, source.value);
    else state.panelError = "The messages around this source could not be loaded.";
    if (cards.status === "fulfilled") state.cards = cards.value.cards;
    else state.panelError = "Cards could not be loaded. Try again.";
    state.loading = false;
    draw();
  }
  function backToSource() {
    state.chatOpen = false;
    dock?.close();
    draw();
    panel?.querySelector("#open-in-chat")?.focus();
  }
  function openInChat() {
    if (!state.selected || state.loading || (state.panelError && ["chat", "reply"].includes(state.selected.kind)) || !memoryActions(state.selected, state.destination).quote || !dock) return false;
    returnKey = key(state.selected);
    state.chatOpen = true;
    draw();
    const opening = dock.open({ from: panel?.querySelector("#open-in-chat"), board: state.destination, at: state.destination.slug === state.selected.board ? state.at : null, quote: quoteDraft(state.selected), composerLabel: `Message to ${name(state.destination)} chat`, strip: { text: `Opened from ${state.selected.sourceLabel}. Your question takes the quote along, word for word.`, backLabel: "Back to the source" } });
    const selected = state.selected;
    void Promise.resolve(opening).catch(() => { if (!stopped && state.selected === selected) { state.chatOpen = false; state.notice = "Chat could not be opened. Try again."; draw(); } });
    return true;
  }
  async function action(action, number) {
    if (!state.selected || writing || stopped) return;
    const selected = state.selected, destination = state.destination, epoch = sourceEpoch;
    try {
      if (action === "chat") { openInChat(); return; }
      if (action === "hide") { ++sourceEpoch; state.selected = null; draw(); mount.querySelector("[data-memory-result]")?.focus(); return; }
      if (action === "source-retry") { await select(selected); return; }
      if (action === "link") {
        if (memoryActions(selected, state.destination).link) { state.linking = !state.linking; draw(); panel?.querySelector("select")?.focus(); }
        return;
      }
      writing = true;
      if (action === "copy") { await clipboard.writeText(selected.text); if (epoch === sourceEpoch) state.notice = "Exact words copied"; }
      if (action === "post" || action === "link-note") {
        const card = state.cards.find((card) => card.number === Number(number));
        if (!card) throw new Error("Choose a card first.");
        const args = { result: selected, destination, card, me: person, api };
        if (action === "post") {
          const updated = await postMemory(args);
          if (epoch === sourceEpoch) state.cards = state.cards.map((row) => row.number === card.number ? updated : row);
        } else await linkMemory(args);
        if (epoch === sourceEpoch) state.notice = action === "post" ? `Posted on ${cardId(destination, card.number)}` : "Note linked";
      }
    } catch { if (epoch === sourceEpoch) state.notice = "That action could not be completed. Try again."; }
    finally { writing = false; if (epoch === sourceEpoch) draw(); }
  }
  function click(event) {
    const row = event.target.closest("[data-memory-result]");
    if (row) { void select(state.results[Number(row.dataset.memoryResult)]); return; }
    const button = event.target.closest("[data-memory-action], [data-memory-post]");
    if (!button) return;
    const kind = button.dataset.memoryAction;
    if (kind === "chat") { event.preventDefault(); event.stopPropagation(); }
    if (kind === "retry") void search();
    else if (kind === "clear") void search({ query: state.filters.query });
    else void action(button.dataset.memoryPost ? "post" : kind, button.dataset.memoryPost);
  }
  function submit(event) {
    const form = event.target;
    if (form.matches("[data-memory-search]")) {
      event.preventDefault();
      void search(Object.fromEntries(new FormData(form)));
    } else if (form.matches("[data-memory-link]")) { event.preventDefault(); void action("link-note", new FormData(form).get("card")); }
  }
  function change(event) {
    const form = event.target.closest("[data-memory-search]");
    if (form && event.target.tagName === "SELECT" && form.elements.q.value.trim()) void search(Object.fromEntries(new FormData(form)));
  }
  function keydown(event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.target.closest("input, textarea, select, [contenteditable]")) return;
    const letter = event.key.toLowerCase();
    if (letter.length === 1 && !singleKeys()) return;
    if (["y", "l"].includes(letter) && state.selected) { event.preventDefault(); void action(letter === "y" ? "copy" : "link"); }
    else if (["j", "k"].includes(letter) && state.results.length) {
      event.preventDefault();
      const current = state.results.findIndex((row) => row === state.selected);
      const index = Math.max(0, Math.min(state.results.length - 1, current + (letter === "j" ? 1 : -1)));
      void select(state.results[index]);
      mount.querySelector(`[data-memory-result="${index}"]`)?.focus();
    }
  }
  for (const node of new Set([mount, panel].filter(Boolean))) {
    node.addEventListener("click", click); node.addEventListener("submit", submit); node.addEventListener("change", change); node.addEventListener("keydown", keydown);
  }
  draw();
  return {
    search, select, openInChat, backToSource,
    setDestination(board) { state.destination = board; state.linking = false; draw(); },
    restoreSource() { if (state.selected && returnKey === key(state.selected)) backToSource(); },
    stop() {
      stopped = true; ++searchEpoch; ++sourceEpoch;
      for (const node of new Set([mount, panel].filter(Boolean))) {
        node.removeEventListener("click", click); node.removeEventListener("submit", submit); node.removeEventListener("change", change); node.removeEventListener("keydown", keydown);
      }
    },
  };
}
