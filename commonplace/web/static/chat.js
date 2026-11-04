export const CHAT_COPY = Object.freeze({
  title: "Chat", loading: "Loading chat…", unavailable: "Chat is unavailable. Try again.",
  noProjects: "Choose a project to chat with its orchestrator.", empty: "Send a message to your project's orchestrator.",
  placeholder: "Message the orchestrator…", send: "Send", basic: "Basic replies (no model)",
  model: "Answers by {provider}/{model}", statusPrivate: "status private", decisions: "Decisions",
  noOrchestratorOwner: "No orchestrator for this project yet. Choose one above.",
  noOrchestratorMember: "No orchestrator for this project yet. Ask the workspace owner to choose one.",
});

// Escape first: only the fixed redaction wrapper becomes markup.
const marked = (value, e) => e(value).replace(/\[(?:redacted|omitted):[a-z_]+\]/g, (marker) => `<mark class="redacted">${marker}</mark>`);
const cardLink = (number, e) => Number.isSafeInteger(number) && number > 0 ? `<a href="#/card/${number}">Card #${e(number)}</a>` : "";

export function chatHTML(page, me, e) {
  const replies = new Map();
  for (const reply of page.replies || []) {
    if (!replies.has(reply.message_id)) replies.set(reply.message_id, []);
    replies.get(reply.message_id).push(reply);
  }
  return (page.messages || []).map((message) => `<li class="chat-message">
    <article><div class="chat-stamp">${marked(message.actor === `user:${me?.user}` ? "You" : message.actor, e)} <time datetime="${e(message.at)}">${e(message.at)}</time></div><p>${marked(message.text, e)}</p></article>
    ${(replies.get(message.id) || []).sort((a, b) => a.id - b.id).map((reply) => `<article class="chat-reply"><div class="chat-stamp">Orchestrator${reply.model ? ` · ${marked(reply.model, e)}` : ""}</div><p>${marked(reply.text, e)}</p>${cardLink(reply.task, e)}</article>`).join("")}
    ${(page.linked || []).filter((card) => card.message_id === message.id).map((card) => cardLink(card.number, e)).join(" ")}
  </li>`).join("") || `<li class="chat-hint">${e(CHAT_COPY.empty)}</li>`;
}

export function statusHTML(status, e) {
  const agent = status.orchestrator, front = status.front;
  return `${agent ? `<span>${marked(agent.name, e)} · ${marked(agent.visible ? agent.status : CHAT_COPY.statusPrivate, e)}</span>${agent.visible && agent.console === "#/console" ? ' <a href="#/console">Console</a>' : ""}` : ""}
    ${front ? `<span>${e(front.mode === "model" ? `Answers by ${front.model}` : front.mode === "off" ? "Front replies off" : CHAT_COPY.basic)}</span>` : ""}
    ${status.needs_you > 0 ? `<a href="#/decide">${e(CHAT_COPY.decisions)} (${e(status.needs_you)})</a>` : ""}
    ${(status.starts || []).map((job) => `<span>${cardLink(job.task, e)} · ${marked(job.state, e)}${job.machine_label ? ` · ${marked(job.machine_label, e)}` : ""}</span>`).join("")}`;
}

export function createChat({ mount, api, e }) {
  let active = false, generation = 0, timer = null, delay = 2000, state;
  const drafts = new Map();
  let lastProject = null;
  const current = (token) => active && token === generation;
  function cancel() { if (timer !== null) clearTimeout(timer); timer = null; }
  function render() {
    const root = mount();
    if (!root) return;
    const old = root.querySelector(".chat-stream");
    const bottom = !old || old.scrollHeight - old.scrollTop - old.clientHeight < 60;
    const top = old?.scrollTop || 0;
    // Keep the composer mounted so polling never interrupts typing or selection.
    if (!root.querySelector(".chat-view")) {
      root.innerHTML = `<section class="chat-view" aria-label="Chat"><header class="chat-header"><label>Project<select data-chat-project></select></label><a data-chat-board>Board</a><div class="chat-status" data-chat-status></div></header><p data-chat-error role="status" hidden></p><ol class="chat-stream" aria-label="Messages" tabindex="0">${chatHTML(state.page, state.me, e)}</ol><form class="chat-compose"><label class="chat-hint" for="chat-text">${e(CHAT_COPY.placeholder)}</label><textarea id="chat-text" name="text" rows="2" required></textarea><button class="blockbtn">${e(CHAT_COPY.send)}</button></form></section>`;
      const form = root.querySelector(".chat-compose");
      if (form) {
        form.onsubmit = send;
        form.elements.text.value = drafts.get(state.slug) || "";
        form.elements.text.oninput = () => drafts.set(state.slug, form.elements.text.value);
      }
    }
    const view = root.querySelector(".chat-view");
    if (view) view.dataset.board = state.slug || "";
    const picker = root.querySelector("[data-chat-project]");
    if (picker) {
      picker.innerHTML = '<option value="">Choose a project</option>' + state.projects.map((project) => `<option value="${e(project.slug)}" ${project.slug === state.slug ? "selected" : ""}>${marked(project.name || project.slug, e)}</option>`).join("");
      picker.onchange = () => { location.hash = `#/chat/${encodeURIComponent(picker.value)}`; };
    }
    const board = root.querySelector("[data-chat-board]");
    if (board) { board.hidden = !state.slug; board.href = `#/board/${encodeURIComponent(state.slug)}`; }
    const status = root.querySelector("[data-chat-status]");
    if (status) status.innerHTML = state.status ? statusHTML(state.status, e) : "";
    const error = root.querySelector("[data-chat-error]");
    if (error) { error.hidden = !state.error; error.textContent = state.error; }
    const stream = root.querySelector(".chat-stream");
    if (stream) {
      stream.innerHTML = state.slug ? chatHTML(state.page, state.me, e) : `<li>${e(CHAT_COPY.noProjects)}</li>`;
      stream.scrollTop = bottom ? stream.scrollHeight : top;
    }
    const form = root.querySelector(".chat-compose");
    if (form) { form.hidden = !state.slug; form.querySelector("button").disabled = !!state.sending; }
  }
  function schedule(token) {
    if (!current(token)) return;
    cancel();
    timer = setTimeout(() => { timer = null; void tick(token); }, delay);
  }
  function merge(rows, added) {
    const byId = new Map(rows.map((row) => [row.id, row]));
    for (const row of added || []) byId.set(row.id, row);
    return [...byId.values()].sort((a, b) => a.id - b.id);
  }
  async function tick(token) {
    if (!current(token)) return;
    if (document.hidden) { schedule(token); return; }
    try {
      if (!state.loaded) {
        const list = await api("/api/chat");
        if (!current(token)) return;
        state.projects = list.projects || [];
        if (!state.slug) state.slug = state.projects.find((p) => p.slug === lastProject)?.slug || list.default || (state.projects.length === 1 ? state.projects[0].slug : null);
        if (state.slug && !state.projects.some((project) => project.slug === state.slug)) throw new Error("Project unavailable");
        const field = mount()?.querySelector(".chat-compose textarea");
        if (field) field.value = drafts.get(state.slug) || "";
        state.loaded = true;
      }
      if (state.slug) {
        const path = `/api/chat/${encodeURIComponent(state.slug)}`;
        const query = state.page.cursor ? `after=${state.page.cursor}&replies_after=${state.page.reply_cursor || 0}` : "last=50";
        const [page, status] = await Promise.all([api(`${path}?${query}`), api(`${path}/status`)]);
        if (!current(token)) return;
        const linked = new Map((state.page.linked || []).map((card) => [card.number, card]));
        for (const card of page.linked || []) linked.set(card.number, card);
        state.page = { ...page, linked: [...linked.values()], messages: merge(state.page.messages, page.messages), replies: merge(state.page.replies, page.replies) };
        state.me = page.me;
        state.status = status;
        lastProject = state.slug;
      }
      state.error = ""; delay = 2000;
    } catch {
      if (!current(token)) return;
      state.error = CHAT_COPY.unavailable; delay = Math.min(delay * 2, 30000);
    }
    render(); schedule(token);
  }
  async function send(event) {
    event.preventDefault();
    if (state.sending || !state.slug) return;
    const form = event.currentTarget, text = form.elements.text.value;
    if (!text.trim()) return;
    const token = ++generation;
    cancel(); state.sending = true; render();
    try {
      await api(`/api/boards/${encodeURIComponent(state.slug)}/chat`, { method: "POST", body: { text } });
      if (!current(token)) return;
      if (form.elements.text.value === text) { form.elements.text.value = ""; drafts.delete(state.slug); }
      state.sending = false;
      await tick(token);
    } catch {
      if (!current(token)) return;
      state.error = CHAT_COPY.unavailable; state.sending = false; render(); schedule(token);
    }
  }
  function visibility() { if (active && !state.sending && !document.hidden) { cancel(); void tick(++generation); } }
  return {
    show(slug) {
      if (active && state.requested === slug) return;
      this.stop(); active = true;
      state = { requested: slug, slug, projects: [], loaded: false, page: { messages: [], replies: [], cursor: 0, reply_cursor: 0 }, status: null, error: CHAT_COPY.loading };
      const root = mount();
      if (root) root.innerHTML = "";
      document.addEventListener("visibilitychange", visibility);
      render();
      return tick(++generation);
    },
    stop() {
      active = false; generation++; cancel();
      document.removeEventListener("visibilitychange", visibility);
    },
  };
}
