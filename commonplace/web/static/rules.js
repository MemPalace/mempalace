import { escape as e } from "./api.js";

const kinds = ["permission", "limit", "instruction", "style"];
const scopes = ["global", "board", "repo"];
const agentName = (agent) => agent === "*" ? "All agents" : String(agent).replace(/^conn:/, "");
const agentRef = (agent) => agent === "*" || agent.startsWith("conn:") ? agent : `conn:${agent}`;
const isActive = (rule) => rule.status !== "retired" || (rule.retired_by === "owner" && Number(rule.pending) === 1);
const activeRules = (rules) => rules.filter(isActive);
export const waitingCount = (rules = []) => activeRules(rules).filter((rule) => rule.status === "proposed" || Number(rule.pending) === 1).length;

const rewrite = "Keep its meaning and its strength (must / may / never). Don't add new obligations.\nName the trigger, the action and any exception. One to three sentences.";
export function clarifyPrompt(rule) {
  return `Rewrite this rule for an AI agent so it has exactly one reading.\n${rewrite}\n\nAgent: ${rule.agent}    Kind: ${rule.kind}    Scope: ${rule.scope}${rule.scope !== "global" && rule.scope_ref ? ` ${rule.scope_ref}` : ""}\nRule (quoted, verbatim):\n"""\n${rule.text}\n"""\n\nReturn only the improved rule text, nothing else.`;
}
export function clarifySetPrompt(agent, rules) {
  return `Rewrite these rules for an AI agent so each has exactly one reading.\n${rewrite}\nCheck for rules that conflict or overlap.\n\nAgent: ${agent}\nRules (quoted, verbatim):\n"""\n${rules.filter((rule) => rule.status !== "retired").map((rule) => `${rule.key}: ${rule.text}`).join("\n")}\n"""\n\nReturn only <key>: <improved text> lines, nothing else.`;
}
export function parseSetReply(text, rules) {
  const byKey = new Map(rules.filter((rule) => rule.status !== "retired").map((rule) => [rule.key, rule.id])), matches = new Map();
  for (const line of String(text).split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim(), value = line.slice(colon + 1).trim();
    if (byKey.has(key) && value) matches.set(byKey.get(key), { rule_id: byKey.get(key), text: value });
  }
  return [...matches.values()];
}
export function desktopBridge() {
  const bridge = globalThis.window?.commonplaceDesktop;
  return bridge?.version === 1 && typeof bridge.ask === "function" ? bridge : null;
}

// Hirschberg's LCS keeps memory linear even for long rule texts.
function lengths(a, b) {
  let row = new Uint32Array(b.length + 1);
  for (const word of a) {
    const next = new Uint32Array(b.length + 1);
    for (let j = 0; j < b.length; j++) next[j + 1] = word === b[j] ? row[j] + 1 : Math.max(row[j + 1], next[j]);
    row = next;
  }
  return row;
}
function commonWords(a, b) {
  if (!a.length || !b.length) return [];
  if (a.length === 1) return b.includes(a[0]) ? [a[0]] : [];
  const mid = Math.floor(a.length / 2), left = lengths(a.slice(0, mid), b), right = lengths(a.slice(mid).reverse(), [...b].reverse());
  let split = 0;
  for (let j = 1; j <= b.length; j++) if (left[j] + right[b.length - j] > left[split] + right[b.length - split]) split = j;
  return [...commonWords(a.slice(0, mid), b.slice(0, split)), ...commonWords(a.slice(mid), b.slice(split))];
}
export function wordDiff(before, after) {
  if (before === after) return e(before);
  const a = String(before ?? "").match(/\s+|\S+/g) || [], b = String(after ?? "").match(/\s+|\S+/g) || [];
  let i = 0, j = 0, html = "";
  const changes = (endA, endB) => {
    if (endA > i) html += `<del>${e(a.slice(i, endA).join(""))}</del>`;
    if (endB > j) html += `<ins>${e(b.slice(j, endB).join(""))}</ins>`;
    i = endA; j = endB;
  };
  for (const word of commonWords(a, b)) {
    changes(a.indexOf(word, i), b.indexOf(word, j));
    html += e(word); i++; j++;
  }
  changes(a.length, b.length);
  return html;
}

const actionButton = (action, label, attrs = "", cls = "opt") => `<button type="button" class="${cls}" data-rule-action="${action}" ${attrs}>${e(label)}</button>`;
const choices = (values, current) => values.map((value) => `<option value="${e(value)}"${value === current ? " selected" : ""}>${e(value)}</option>`).join("");
function fields(rule = {}) {
  return `<label>Text<textarea name="text" required>${e(rule.text || "")}</textarea></label><label>Summary (optional)<textarea name="summary" maxlength="400">${e(rule.summary || "")}</textarea></label><div class="rule-fields"><label>Kind<select name="kind">${choices(kinds, rule.kind || "instruction")}</select></label><label>Scope<select name="scope">${choices(scopes, rule.scope || "global")}</select></label><label data-scope-ref${!rule.scope || rule.scope === "global" ? " hidden" : ""}>Scope reference<input name="scope_ref" value="${e(rule.scope_ref || "")}"${rule.scope && rule.scope !== "global" ? " required" : ""}></label></div>`;
}
function clarifyPanel(attrs, bridge) {
  return `<details class="rule-clarify" ${attrs}><summary class="rule-text-button">Help me clarify</summary><div class="rule-clarify-panel"><div class="opts">${actionButton("copy-prompt", "Copy prompt")}${bridge ? actionButton("agents", "Ask an agent") : ""}</div><div data-rule-ask></div><div data-rule-reply role="status" aria-live="polite"></div></div></details>`;
}
function suggestionView(rule, suggestion, owner) {
  const outdated = suggestion.base_version !== rule.version;
  return `<section class="rule-proposal" data-suggestion-id="${e(suggestion.id)}"><h4>${e(agentName(suggestion.from_agent))}</h4>${outdated ? '<div class="rule-chips"><span class="pill">outdated</span></div>' : ""}<p class="rule-text">${wordDiff(rule.text, suggestion.text)}</p>${owner ? `<div class="opts">${!outdated && rule.status !== "retired" ? actionButton("suggestion-accept", "Accept") : ""}${actionButton("suggestion-dismiss", "Dismiss")}</div>` : ""}</section>`;
}
function movePanel(rule, targets) {
  const others = targets.filter((agent) => agent !== rule.agent);
  return others.length ? `<details class="rule-move"><summary class="rule-text-button">Applies to…</summary><div class="rule-clarify-panel"><div class="opts">${others.map((agent) => actionButton("move", agentName(agent), `data-agent="${e(agent)}"`)).join("")}</div></div></details>` : "";
}
function ruleRow(rule, suggestions, owner, bridge, targets = []) {
  const waiting = isActive(rule) && (rule.status === "proposed" || Number(rule.pending) === 1), edited = rule.status === "edited" && rule.agent_text != null;
  const controls = `<div class="rule-actions">${owner ? `${rule.status === "proposed" && !rule.pending ? `${actionButton("accept", rule.agent === "*" ? "Accept for all agents" : "Accept", "", "blockbtn")}${rule.agent !== "*" ? actionButton("accept-all", "Accept for all agents", "", "rule-text-button") : ""}` : ""}${rule.status === "retired" ? actionButton("restore", "Restore", "", "rule-text-button") : `${actionButton("edit", "Edit", "", "rule-text-button")}${actionButton("retire", "Retire", "", "rule-text-button")}${movePanel(rule, targets)}`}` : ""}${actionButton("history", "History", "", "rule-text-button")}${clarifyPanel(`data-clarify-rule="${e(rule.id)}"`, owner && bridge)}</div>`;
  const proposer = owner && rule.agent === "*" && rule.source === "agent" && rule.agent_text_by ? `<span class="pill">Proposed by ${e(agentName(rule.agent_text_by))}</span>` : "";
  const fullId = `rule-full-${rule.id}`;
  const summary = rule.display_summary || rule.summary || String(rule.text).split(/(?<=[.!?])\s+/)[0].slice(0, 240);
  return `<article class="rule-row" data-rule-id="${e(rule.id)}" data-version="${e(rule.version)}"><div class="rule-chips"><span class="pill">${e(rule.kind)}</span>${rule.scope !== "global" ? `<span class="pill">${e(rule.scope)} ${e(rule.scope_ref)}</span>` : ""}<span class="pill${waiting ? " you" : ""}">${e(rule.status)}</span>${proposer}${rule.pending ? '<span class="pill you">Agent changed</span>' : ""}</div><div data-rule-body><button type="button" class="rule-summary" data-rule-action="expand" aria-expanded="false" aria-controls="${e(fullId)}"><span>${e(summary)}</span><span data-rule-expand-icon aria-hidden="true">+</span></button><div id="${e(fullId)}" data-rule-full hidden><p class="rule-text">${edited ? wordDiff(rule.agent_text, rule.text) : e(rule.text)}</p>${rule.pending ? `<section class="rule-proposal"><h4>${rule.agent_text_by && rule.agent_text_by !== rule.agent ? e(`${agentName(rule.agent_text_by)}’s wording`) : "Agent’s wording"}</h4><p class="rule-text">${wordDiff(rule.text, rule.agent_text)}</p>${owner ? `<div class="opts">${actionButton("accept_agent", "Use agent's")}${actionButton("keep_edit", "Keep mine")}</div>` : ""}</section>` : ""}${suggestions.map((suggestion) => suggestionView(rule, suggestion, owner)).join("")}</div>${controls}</div><div data-rule-history hidden></div></article>`;
}
export function rulesView({ rules = [], suggestions = [], agents = [], waiting = 0, waiting_by_agent = {} } = {}, { owner = true } = {}) {
  const bridge = desktopBridge();
  const groups = [...new Set([...agents.map(agentRef), ...rules.map((rule) => rule.agent)])].map((agent) => ({ agent, rules: rules.filter((rule) => rule.agent === agent) }));
  groups.sort((a, b) => (waiting_by_agent[b.agent] || 0) - (waiting_by_agent[a.agent] || 0) || agentName(a.agent).localeCompare(agentName(b.agent)));
  const targets = ["*", ...groups.map(({ agent }) => agent).filter((agent) => agent !== "*").sort((a, b) => agentName(a).localeCompare(agentName(b)))];
  return `<section class="rules-view" aria-label="Rules" data-waiting="${e(waiting)}">${owner ? `<section class="rule-new" id="rule-new" hidden><form class="form-tile" data-rule-form="new"><label>Applies to<select name="agent"><option value="*">All agents</option>${[...new Set([...agents.map(agentRef), ...rules.map((rule) => rule.agent)])].filter((agent) => agent !== "*").map((agent) => `<option value="${e(agent)}">${e(agentName(agent))}</option>`).join("")}</select></label>${fields()}<div class="opts"><button class="blockbtn">Save</button>${actionButton("cancel-new", "Cancel")}</div></form></section>` : ""}<div class="rule-tiles">${groups.map(({ agent, rules: group }) => `<section class="rule-tile" data-rule-agent="${e(agent)}"><header><h2>${e(agentName(agent))}</h2>${(waiting_by_agent[agent] || 0) > 0 ? `<span class="pill you">${e(waiting_by_agent[agent])}</span>` : ""}${clarifyPanel(`data-clarify-agent="${e(agent)}"`, owner && bridge)}</header>${activeRules(group).map((rule) => ruleRow(rule, suggestions.filter((s) => s.rule_id === rule.id && (!s.status || s.status === "open")), owner, bridge, targets)).join("") || '<p class="rule-empty">No active rules</p>'}${group.some((rule) => !isActive(rule)) ? `<details class="rule-retired"><summary>Retired (${group.filter((rule) => !isActive(rule)).length})</summary>${group.filter((rule) => !isActive(rule)).map((rule) => ruleRow(rule, suggestions.filter((s) => s.rule_id === rule.id && (!s.status || s.status === "open")), owner, bridge)).join("")}</details>` : ""}</section>`).join("") || '<p class="message">No rules</p>'}</div></section>`;
}

async function copyText(text, button) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(text);
  } catch {
    const input = document.createElement("textarea"), active = document.activeElement;
    input.className = "rule-copy"; input.value = text; input.setAttribute("readonly", "");
    document.body.append(input); input.select();
    try { if (!document.execCommand("copy")) throw new Error("Could not copy"); }
    finally { input.remove(); active?.focus({ preventScroll: true }); }
  }
  const label = button.textContent; button.textContent = "Copied";
  setTimeout(() => { button.textContent = label; }, 1500);
}

// Keep rule events and requests local to this view; the shell supplies its API and refresh.
export function rulesHandlers(root, api, refresh, showError) {
  let payload = { rules: [], agents: [] }, owner = false;
  const pending = new Set();
  const refreshIfStale = async () => { if (ui.stale && !ui.busy()) await refresh(); };
  const ruleFor = (row) => payload.rules.find((rule) => String(rule.id) === row?.dataset.ruleId);
  async function mutate(key, element, work) {
    if (pending.has(key)) return;
    pending.add(key);
    const buttons = [...element.querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    // A finished write renders once even if a background refresh overtakes this one.
    try {
      const saved = await work();
      if (saved !== false) {
        const row = element.dataset?.ruleForm === "edit" ? element.closest("[data-rule-id]") : null;
        if (row && String(saved?.id) === row.dataset.ruleId && Number.isInteger(saved?.version)) {
          payload = { ...payload, rules: payload.rules.map((rule) => String(rule.id) === row.dataset.ruleId ? saved : rule) };
          row.dataset.version = String(saved.version);
          element.querySelector("[data-rule-current]").innerHTML = "";
        }
        ui.force = true; await refresh();
      }
    }
    catch (error) {
      if (element.dataset?.ruleForm === "edit") {
        const row = element.closest("[data-rule-id]"), current = error.payload?.current;
        if (error.status === 409 && !error.payload?.store_lost && row && current && String(current.id) === row.dataset.ruleId && Number.isInteger(current.version)) {
          payload = { ...payload, rules: payload.rules.map((rule) => String(rule.id) === row.dataset.ruleId ? current : rule) };
          row.dataset.version = String(current.version);
          const explanation = current.status === "retired" ? "Rule retired. Restore it before saving. Your draft is kept." : "Rule changed. Your draft is kept. Review the current rule and Save again.";
          element.querySelector("[data-rule-current]").innerHTML = `<section class="rule-proposal"><p class="rule-text">${e(explanation)}</p><h4>Current rule</h4><div class="rule-chips"><span class="pill">${e(agentName(current.agent))}</span><span class="pill">${e(current.status)}</span><span class="pill">${e(current.kind)}</span><span class="pill">${e(current.scope)}${current.scope_ref == null ? "" : ` ${e(current.scope_ref)}`}</span></div><p class="rule-text">${e(current.text)}</p><p class="rule-text quiet">Summary: ${e(current.summary ?? "None")}</p></section>`;
          showError("");
        } else showError(error.message);
      } else if (element.dataset?.ruleForm === "new" || error.payload?.store_lost) showError(error.message);
      else if (error.status === 409) { ui.force = true; await refresh(); showError("Rule changed. Review it and try again."); }
      else showError(error.message);
    } finally { pending.delete(key); buttons.forEach((button) => { button.disabled = false; }); await refreshIfStale(); }
  }
  const context = (panel) => {
    const rule = payload.rules.find((rule) => String(rule.id) === panel.dataset.clarifyRule);
    const rules = rule ? [rule] : payload.rules.filter((rule) => rule.agent === panel.dataset.clarifyAgent);
    return { rules, prompt: rule ? clarifyPrompt(rule) : clarifySetPrompt(panel.dataset.clarifyAgent, rules), single: !!rule };
  };
  root.addEventListener("toggle", async (event) => {
    if (event.target.matches("details") && !event.target.open) await refreshIfStale();
  }, true);
  root.addEventListener("change", (event) => {
    const form = event.target.closest("[data-rule-form]");
    if (!form || event.target.name !== "scope") return;
    const field = form.querySelector("[data-scope-ref]"), global = event.target.value === "global";
    field.hidden = global; field.querySelector("input").required = !global;
  });
  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-rule-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.ruleAction, row = button.closest("[data-rule-id]"), rule = ruleFor(row);
    const panel = button.closest(".rule-clarify");
    try {
      if (action === "expand" && rule) {
        const full = row.querySelector("[data-rule-full]");
        full.hidden = !full.hidden;
        button.setAttribute("aria-expanded", String(!full.hidden));
        button.querySelector("[data-rule-expand-icon]").textContent = full.hidden ? "+" : "−";
        if (full.hidden) await refreshIfStale();
      } else if (action === "new" && owner) {
        const form = root.querySelector(".rule-new");
        form.hidden = !form.hidden;
        button.setAttribute("aria-expanded", String(!form.hidden));
        if (!form.hidden) form.querySelector("textarea").focus();
        else await refreshIfStale();
      } else if (action === "copy-prompt") await copyText(context(panel).prompt, button);
      else if (action === "copy-reply") await copyText(panel.querySelector("[data-reply-text]").textContent, button);
      else if (action === "agents" && owner) {
        const bridge = desktopBridge(); if (!bridge) return;
        button.disabled = true;
        try {
          const agents = await bridge.agents();
          panel.querySelector("[data-rule-ask]").innerHTML = agents.length ? `<div class="rule-ask"><label>Agent<select data-ask-agent>${agents.map((agent) => `<option value="${e(agent.id)}">${e(agent.name)}</option>`).join("")}</select></label>${actionButton("ask", "Ask")}</div>` : '<p role="status">No agents available</p>';
        } finally { button.disabled = false; }
      } else if (action === "ask" && owner) {
        const bridge = desktopBridge(); if (!bridge) return;
        const select = panel.querySelector("[data-ask-agent]"), agent = select.value, name = select.selectedOptions[0].textContent;
        const { rules, prompt, single } = context(panel), reply = panel.querySelector("[data-rule-reply]");
        const baseVersions = new Map(rules.map((rule) => [rule.id, rule.version]));
        await mutate(`ask-${panel.dataset.clarifyRule || panel.dataset.clarifyAgent}`, panel, async () => {
          reply.textContent = `Asking ${name}…`;
          try {
            const answer = await bridge.ask({ agent, prompt, purpose: "rule-clarify" });
            if (typeof answer?.text !== "string" || !answer.text.trim()) throw new Error("Agent returned no rule text");
            const matches = single ? [{ rule_id: rules[0].id, text: answer.text }] : parseSetReply(answer.text, rules);
            if (!matches.length) {
              reply.innerHTML = `<p class="rule-text" data-reply-text>${e(answer.text)}</p>${actionButton("copy-reply", "Copy")}`;
              return false;
            }
            for (const match of matches) await api(`/api/rules/${encodeURIComponent(match.rule_id)}/suggestions`, { method: "POST", body: { text: match.text, from_agent: name, base_version: baseVersions.get(match.rule_id) } });
            reply.textContent = "";
          } catch (error) { reply.textContent = ""; throw error; }
        });
      } else if (action === "edit" && owner && rule) {
        row.querySelector("[data-rule-body]").innerHTML = `<form class="form-tile" data-rule-form="edit">${fields(rule)}<div data-rule-current role="status" aria-live="polite"></div><div class="opts"><button class="blockbtn">Save</button>${actionButton("cancel-edit", "Cancel")}</div></form>`;
        row.querySelector("textarea").focus();
      } else if (action === "cancel-edit") await refresh();
      else if (action === "cancel-new") { const panel = button.closest(".rule-new"); panel.hidden = true; panel.querySelector("form").reset(); panel.querySelector("[data-scope-ref]").hidden = true; panel.querySelector('[name="scope_ref"]').required = false; const trigger = root.querySelector('[data-rule-action="new"]'); trigger.setAttribute("aria-expanded", "false"); trigger.focus(); await refreshIfStale(); }
      else if (action === "history" && rule) {
        const history = row.querySelector("[data-rule-history]");
        if (!history.hidden) { history.hidden = true; await refreshIfStale(); return; }
        button.disabled = true;
        try {
          const result = await api(`/api/rules/${encodeURIComponent(rule.id)}/history`);
          const versions = Array.isArray(result) ? result : result.history || result.versions || [];
          history.innerHTML = `<h4>History</h4>${versions.map((version) => `<section><span class="quiet">${e(version.version)} · ${e(version.status)}</span><p class="rule-text">${e(version.text)}</p>${version.note ? `<p class="rule-text quiet">${e(version.note)}</p>` : ""}</section>`).join("")}`;
          history.hidden = false;
        } finally { button.disabled = false; }
      } else if (owner && action.startsWith("suggestion-")) {
        const suggestion = button.closest("[data-suggestion-id]").dataset.suggestionId;
        await mutate(`suggestion-${suggestion}`, row, () => api(`/api/rules/suggestions/${encodeURIComponent(suggestion)}`, { method: "POST", body: { action: action.slice(11), ...(action === "suggestion-accept" ? { version: Number(row.dataset.version) } : {}) } }));
      } else if (owner && rule && (action === "move" || action === "accept-all")) {
        const body = { agent: action === "move" ? button.dataset.agent : "*", version: Number(row.dataset.version), ...(action === "accept-all" ? { accept: true } : {}) };
        await mutate(`rule-${rule.id}`, row, () => api(`/api/rules/${encodeURIComponent(rule.id)}/agent`, { method: "POST", body }));
      } else if (owner && rule && ["accept", "retire", "restore", "accept_agent", "keep_edit"].includes(action)) {
        await mutate(`rule-${rule.id}`, row, () => api(`/api/rules/${encodeURIComponent(rule.id)}/action`, { method: "POST", body: { action, version: Number(row.dataset.version) } }));
      }
    } catch (error) { showError(error.message); }
  });
  root.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-rule-form]"); if (!form) return;
    event.preventDefault(); if (!owner) return;
    const row = form.closest("[data-rule-id]"), rule = ruleFor(row), body = Object.fromEntries(new FormData(form));
    body.scope_ref = body.scope === "global" ? null : body.scope_ref;
    if (form.dataset.ruleForm === "edit" && !rule) return;
    await mutate(rule ? `rule-${rule.id}` : "new-rule", form, () => api(rule ? `/api/rules/${encodeURIComponent(rule.id)}` : "/api/rules", { method: rule ? "PATCH" : "POST", body: rule ? { ...body, version: Number(row.dataset.version) } : body }));
  });
  const ui = {
    stale: false,
    force: false,
    update(next, options = {}) { payload = next; owner = options.owner ?? true; ui.stale = false; ui.force = false; },
    busy() { return pending.size > 0 || !!root.querySelector('.rules-view .rule-new:not([hidden]), .rules-view form[data-rule-form="edit"], .rules-view details[open], .rules-view [data-rule-history]:not([hidden]), .rules-view [data-rule-full]:not([hidden]), .rules-view .rule-clarify[open] [data-ask-agent], .rules-view .rule-clarify[open] [data-reply-text]'); },
  };
  return ui;
}
