// Frozen console interface; implemented by U6.
export const CONSOLE_COPY = Object.freeze({
  "loading": "Loading agents…",
  "noAgentsOwner": "No agents connected. Create one in Settings → Agents.",
  "noAgentsMember": "No agents act as you in this workspace.",
  "showTools": "Show tool calls",
  "stopTools": "Stop showing tool calls",
  "toolsOff": "Tool calls are off for this agent. Only you can see them when on.",
  "toolsPrivate": "Tool calls and output are shown only to the person this agent acts for.",
  "waiting": "Waiting for activity. Tool calls appear as they happen.",
  "noBoard": "No board or memory actions yet.",
  "outputHeading": "Output",
  "activityHeading": "Activity",
  "noOutput": "No replies or comments yet.",
  "unavailable": "Console unavailable. Retrying…"
});

export function markRedactions(escapedHtml) {
  return escapedHtml.replace(/\[(?:redacted|omitted):[a-z_]+\]/g, (marker) => `<mark class="redacted">${marker}</mark>`);
}

function seen(at) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(at)) / 60000));
  if (!Number.isFinite(minutes)) return "not seen";
  return `seen ${minutes < 1 ? "now" : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`}`;
}

export function agentListHTML(agents, selectedId, e) {
  const text = (value) => markRedactions(e(value));
  return agents.map((agent) => {
    const status = ["working", "idle", "quiet", "unused"].includes(agent.status) ? agent.status : "unused";
    return `<li class="console-agent${agent.id === selectedId ? " selected" : ""}">
      <button class="console-select" data-action="console-select" data-id="${e(agent.id)}" aria-pressed="${agent.id === selectedId}">
        <span class="console-dot ${status}" role="img" aria-label="${status}"></span>
        <span class="console-agent-name">${text(agent.name)}</span>
        <span class="console-agent-meta">${text(agent.client)} · ${seen(agent.last_seen)}</span>
      </button>
      ${agent.own && agent.can_toggle ? `<button class="console-record" data-action="console-record" data-id="${e(agent.id)}" data-record="${agent.recording ? "0" : "1"}">${e(agent.recording ? CONSOLE_COPY.stopTools : CONSOLE_COPY.showTools)}</button>` : ""}
    </li>`;
  }).join("");
}

export function streamItemsHTML(items, e) {
  const text = (value) => markRedactions(e(value));
  return items.map((item) => `<div class="console-item" data-id="${e(item.id)}">
    <time datetime="${e(item.at)}">${text(item.at)}</time>
    <div>${item.source === "tool" ? `▸ ${text(item.name)} · ${text(item.outcome)} · ${text(item.ms)} ms` : `${text(item.kind)} · ${text(item.text)}${item.truncated ? " …" : ""}`}</div>
  </div>`).join("");
}

export function outputItemsHTML(items, e) {
  const text = (value) => markRedactions(e(value));
  return items.map((item) => `<article class="console-item" data-id="${e(item.id)}">
    <div class="console-output-meta">${text(item.kind)} · ${item.card == null ? text(item.board) : `#${text(item.card)}`} <time datetime="${e(item.at)}">${text(item.at)}</time></div>
    <div class="console-output-text">${text(item.text)}${item.truncated ? " …" : ""}</div>
  </article>`).join("");
}

export function consoleView({ role, agents, selected, items, output, tools, error, loading }, e) {
  const agent = agents.find((row) => row.id === selected);
  const privateAgent = agent && (!agent.own || tools === "private");
  const activity = privateAgent ? items.filter((item) => item.source !== "tool") : items;
  return `<section class="console-view" aria-label="Console">
    ${error ? `<p class="console-error" role="status">${e(CONSOLE_COPY.unavailable)}</p>` : ""}
    ${loading && !agents.length ? `<p class="console-message" role="status">${e(CONSOLE_COPY.loading)}</p>` : !agents.length ? `<p class="console-message">${e(role === "owner" ? CONSOLE_COPY.noAgentsOwner : CONSOLE_COPY.noAgentsMember)}</p>` : `
    <nav class="console-agents" aria-label="Agents"><ul>${agentListHTML(agents, selected, e)}</ul></nav>
    ${agent ? `<div class="console-panels">
      <section class="console-panel console-output" aria-labelledby="console-output-heading">
        <h2 id="console-output-heading">${e(CONSOLE_COPY.outputHeading)}</h2>
        <div class="console-output-items">${privateAgent ? `<p class="console-message">${e(CONSOLE_COPY.toolsPrivate)}</p>` : output.length ? outputItemsHTML(output, e) : `<p class="console-message">${e(CONSOLE_COPY.noOutput)}</p>`}</div>
      </section>
      <section class="console-panel console-activity" aria-labelledby="console-activity-heading">
        <h2 id="console-activity-heading">${e(CONSOLE_COPY.activityHeading)}</h2>
        ${!privateAgent && tools === "off" ? `<p class="console-message">${e(CONSOLE_COPY.toolsOff)}</p>` : ""}
        <div class="console-stream" tabindex="0" aria-label="Activity stream">${activity.length ? streamItemsHTML(activity, e) : `<p class="console-message">${e(CONSOLE_COPY.noBoard)}</p>${!privateAgent && tools === "on" ? `<p class="console-message">${e(CONSOLE_COPY.waiting)}</p>` : ""}`}</div>
        <button class="console-new" data-action="console-bottom" hidden>New activity ↓</button>
      </section>
    </div>` : ""}`}
  </section>`;
}

export function createConsole({ mount, api, e, setTimer = setTimeout, clearTimer = clearTimeout, hidden = () => document.hidden, onSelect = () => {} }) {
  let state, timer = null, active = false, generation = 0, visit = 0;
  let cursor = "", ticks = 0, delay = 2000, loaded = false, newActivity = false, wanted = null;
  const pending = new Set();
  const current = (token) => active && token === generation;
  const atBottom = (node) => !node || node.scrollHeight - node.scrollTop - node.clientHeight <= 24;

  function cancelTimer() {
    if (timer !== null) clearTimer(timer);
    timer = null;
  }

  function render(added = false, reset = false) {
    const root = mount();
    if (!root) return;
    const old = root.querySelector(".console-stream");
    const stick = reset || atBottom(old), top = old?.scrollTop || 0;
    const outputTop = root.querySelector(".console-output-items")?.scrollTop || 0;
    const agentLeft = root.querySelector(".console-agents")?.scrollLeft || 0;
    if (stick) newActivity = false;
    else if (added) newActivity = true;
    root.innerHTML = consoleView(state, e);
    const stream = root.querySelector(".console-stream");
    const button = root.querySelector('[data-action="console-bottom"]');
    if (button) button.hidden = !newActivity;
    if (stream) {
      stream.scrollTop = stick ? stream.scrollHeight : top;
      stream.onscroll = () => {
        if (atBottom(stream)) {
          newActivity = false;
          if (button) button.hidden = true;
        }
      };
    }
    const output = root.querySelector(".console-output-items");
    if (output) output.scrollTop = reset ? 0 : outputTop;
    const agents = root.querySelector(".console-agents");
    if (agents) agents.scrollLeft = agentLeft;
    root.querySelectorAll('[data-action="console-record"]').forEach((button) => {
      button.disabled = pending.has(button.dataset.id);
    });
  }

  function select(id) {
    state.selected = id;
    state.items = [];
    state.output = [];
    cursor = "";
    newActivity = false;
    const agent = state.agents.find((row) => row.id === id);
    state.tools = !agent?.own ? "private" : agent.recording ? "on" : "off";
    render(false, true);
  }

  function schedule(token, wait = delay) {
    if (!current(token)) return;
    cancelTimer();
    timer = setTimer(() => {
      timer = null;
      return tick(token);
    }, wait);
  }

  async function refreshAgents(token) {
    const result = await api("/api/console");
    if (!current(token)) return;
    state.role = result.role;
    state.agents = result.agents;
    loaded = true;
    const selected = state.agents.find((row) => row.id === state.selected);
    // A requested agent the viewer cannot see is simply absent from this list: the first one opens instead.
    if (!selected) select(state.agents.find((row) => row.id === wanted)?.id || state.agents[0]?.id || null);
    else {
      state.tools = !selected.own ? "private" : selected.recording ? "on" : "off";
      if (!selected.own) {
        state.output = [];
        state.items = state.items.filter((item) => item.source !== "tool");
      }
    }
  }

  async function tick(token, forceList = false) {
    if (!current(token)) return;
    if (hidden()) { schedule(token, 2000); return; }
    try {
      if (!loaded || forceList || ticks % 5 === 0) await refreshAgents(token);
      if (!current(token)) return;
      ticks++;
      for (let page = 0; state.selected && page < 3; page++) {
        if (hidden()) break;
        const query = `agent=${encodeURIComponent(state.selected)}${cursor ? `&after=${encodeURIComponent(cursor)}` : ""}`;
        const result = await api(`/api/console/stream?${query}`);
        if (!current(token)) return;
        const ids = new Set(state.items.map((item) => item.id));
        const added = result.items.filter((item) => !ids.has(item.id));
        state.items.push(...added);
        cursor = result.cursor;
        state.tools = result.tools;
        if (Array.isArray(result.output)) state.output = result.output;
        state.loading = false;
        state.error = "";
        render(added.length > 0);
        if (!result.more) break;
      }
      if (!current(token)) return;
      state.loading = false;
      state.error = "";
      delay = 2000;
      render();
    } catch {
      if (!current(token)) return;
      state.loading = false;
      state.error = CONSOLE_COPY.unavailable;
      delay = Math.min(delay * 2, 30000);
      render();
    }
    schedule(token);
  }

  function restart(forceList = false) {
    cancelTimer();
    return tick(++generation, forceList);
  }

  async function toggle(agent, record) {
    const session = visit;
    pending.add(agent.id);
    cancelTimer();
    generation++;
    render();
    try {
      await api(`/api/console/agents/${encodeURIComponent(agent.id)}`, { method: "POST", body: { record } });
      if (active && visit === session) await restart(true);
    } catch {
      if (active && visit === session) {
        state.error = CONSOLE_COPY.unavailable;
        delay = Math.min(delay * 2, 30000);
        render();
        schedule(generation);
      }
    } finally {
      if (active && visit === session) {
        pending.delete(agent.id);
        render();
      }
    }
  }

  return {
    show(agent = null) {
      if (active) {
        if (agent && agent !== state.selected && state.agents.some((row) => row.id === agent)) {
          select(agent);
          return restart();
        }
        render();
        return;
      }
      wanted = agent;
      active = true;
      visit++;
      state = { role: "member", agents: [], selected: null, items: [], output: [], tools: "off", error: "", loading: true };
      cursor = ""; ticks = 0; delay = 2000; loaded = false; newActivity = false;
      pending.clear();
      render(false, true);
      return restart();
    },
    stop() {
      active = false;
      generation++;
      cancelTimer();
    },
    click(action, element) {
      if (!active) return false;
      if (action === "console-select") {
        const id = element?.dataset.id;
        if (id !== state.selected && state.agents.some((row) => row.id === id)) {
          select(id);
          onSelect(id);
          void restart();
        }
        return true;
      }
      if (action === "console-bottom") {
        const stream = mount()?.querySelector(".console-stream");
        if (stream) stream.scrollTop = stream.scrollHeight;
        newActivity = false;
        const button = mount()?.querySelector('[data-action="console-bottom"]');
        if (button) button.hidden = true;
        return true;
      }
      if (action === "console-record") {
        const agent = state.agents.find((row) => row.id === element?.dataset.id);
        const record = element?.dataset.record;
        if (agent?.own && agent.can_toggle && !pending.has(agent.id) && ["0", "1"].includes(record) && !hidden()) {
          void toggle(agent, record === "1");
        }
        return true;
      }
      return false;
    },
  };
}
