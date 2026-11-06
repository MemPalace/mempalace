import { api, escape as e, elapsed } from "./api.js";

const clients = [["claude-code", "Claude Code"], ["codex", "Codex"], ["cursor", "Cursor"], ["generic", "Generic MCP / Grok"]];
const importConsent = "Import your past AI conversations and memory files into this workspace, word for word. This import permission cannot read stored threads or change existing memories, rules or settings. Sending exactly the same import again adds no duplicates.";
const options = (selected) => clients.map(([id, name]) => `<option value="${id}" ${id === selected ? "selected" : ""}>${name}</option>`).join("");
const active = (c) => c.auth === "pat" && !c.revoked_at;
const parents = (rows) => '<option value="">Top-level agent</option>' + rows.filter((c) => active(c) && (!c.expires_at || Date.parse(c.expires_at) > Date.now())).map((c) => `<option value="${e(c.id)}">${e(c.name)}</option>`).join("");

export function usedText(value) {
  const age = elapsed(value);
  return age === "now" ? "Used just now" : `Used ${age} ago`;
}

function readAccess(c, wings) {
  if (!wings.length) return "";
  return `<details data-agent-access><summary>Read access</summary>${wings.map((wing) => `<label><input type="checkbox" value="${e(wing)}" ${(c.read_wings || []).some((grant) => grant === "*" || grant === wing) ? "checked" : ""}>${e(wing)}</label>`).join("")}<button type="button" data-action="agent-wings" data-agent-mutation data-agent-id="${e(c.id)}">Save read access</button></details>`;
}

function agentRows(rows, wings = []) {
  return rows.map((c) => `<div class="setting-row"><span>${e(c.name)}<br><small>${e(c.client)} · ${c.revoked_at ? "Revoked" : c.expires_at && Date.parse(c.expires_at) <= Date.now() ? "Expired" : c.last_used_at ? usedText(c.last_used_at) : "Unused"}</small></span>${active(c) ? `${readAccess(c, wings)}<button type="button" data-action="agent-rotate" data-agent-mutation data-agent-id="${e(c.id)}" data-agent-name="${e(c.name)}">Rotate</button><button type="button" data-action="agent-revoke" data-agent-mutation data-agent-id="${e(c.id)}" data-agent-name="${e(c.name)}">Revoke</button>` : ""}</div>`).join("") || '<p class="message">No agents connected</p>';
}

export function agentsView(payload) {
  const rows = payload.connections || [];
  return `<section class="settings-section agents-settings"><h2>Agents</h2><div data-agent-reveal></div><form class="agent-form" data-form="agent"><label>Name<input name="name" required maxlength="120" autocomplete="off"></label><label>Client<select name="client">${options("claude-code")}</select></label><label class="agent-consent"><input type="checkbox" name="import">${e(importConsent)}</label><details><summary>Restrict access or set a parent</summary><label>Read wings (comma separated)<input name="read_wings" autocomplete="off"></label><label>Parent<select name="parent">${parents(rows)}</select></label></details><button class="blockbtn" data-agent-mutation>Create token</button></form><div data-agent-list>${agentRows(rows, payload.wings || [])}</div></section>`;
}

export function clientConfig(client, url, token) {
  const headers = { Authorization: `Bearer ${token}` };
  if (client === "codex") return `[mcp_servers.commonplace]\nurl = ${JSON.stringify(url)}\nhttp_headers = { Authorization = ${JSON.stringify(headers.Authorization)} }\n`;
  if (client === "generic") return JSON.stringify({ url, headers }, null, 2);
  return JSON.stringify({ mcpServers: { commonplace: { ...(client === "claude-code" ? { type: "http" } : {}), url, headers } } }, null, 2);
}

const destinations = { "claude-code": ".mcp.json", codex: "config.toml", cursor: ".cursor/mcp.json", generic: "MCP URL and headers" };

function reveal(section, result) {
  const region = section.querySelector("[data-agent-reveal]");
  const client = clients.some(([id]) => id === result.connection.client) ? result.connection.client : "generic";
  region.dataset.agentId = result.connection.id;
  region.innerHTML = `<div class="agent-secret"><h3>${e(result.connection.name)}</h3><p>Copy now. This token will not be shown again.</p><label>Token<input data-agent-token value="${e(result.token)}" readonly autocomplete="off" spellcheck="false"></label><button type="button" data-action="agent-copy" data-agent-copy="token">Copy token</button><label>MCP URL<input data-agent-url value="${e(result.mcp_url)}" readonly></label><button type="button" data-action="agent-copy" data-agent-copy="url">Copy URL</button><label>Authorization<input data-agent-header value="Bearer ${e(result.token)}" readonly autocomplete="off" spellcheck="false"></label><button type="button" data-action="agent-copy" data-agent-copy="header">Copy header</button><label>Client<select data-agent-client>${options(client)}</select></label><strong data-agent-destination>${destinations[client]}</strong><pre data-agent-config tabindex="0">${e(clientConfig(client, result.mcp_url, result.token))}</pre><button type="button" data-action="agent-copy" data-agent-copy="config">Copy config</button><button type="button" data-action="agent-dismiss">Done</button><span role="status" data-agent-copy-status></span></div>`;
  region.querySelector("[data-agent-token]").focus();
}

export function changeAgentClient(select) {
  const section = select.closest(".agents-settings");
  section.querySelector("[data-agent-destination]").textContent = destinations[select.value];
  section.querySelector("[data-agent-config]").textContent = clientConfig(select.value, section.querySelector("[data-agent-url]").value, section.querySelector("[data-agent-token]").value);
}

async function refresh(section) {
  const payload = await api("/api/connections");
  if (!section.isConnected) return;
  section.querySelector("[data-agent-list]").innerHTML = agentRows(payload.connections, payload.wings || []);
  const select = section.querySelector('[name="parent"]'), selected = select.value;
  select.innerHTML = parents(payload.connections);
  select.value = selected;
}

async function mutation(section, run) {
  if (section.dataset.busy) return;
  section.dataset.busy = "true";
  section.querySelectorAll("[data-agent-mutation]").forEach((button) => { button.disabled = true; });
  try { await run(); }
  finally {
    delete section.dataset.busy;
    section.querySelectorAll("[data-agent-mutation]").forEach((button) => { button.disabled = false; });
  }
}

export async function createAgent(form) {
  const section = form.closest(".agents-settings"), data = Object.fromEntries(new FormData(form));
  data.import = form.elements.namedItem("import").checked;
  if (!data.parent) delete data.parent;
  if (data.read_wings.trim()) data.read_wings = data.read_wings.split(",").map((wing) => wing.trim());
  else delete data.read_wings;
  await mutation(section, async () => {
    const result = await api("/api/connections", { method: "POST", body: data });
    if (!section.isConnected) return;
    form.reset(); reveal(section, result);
    await refresh(section);
  });
}

export async function agentAction(button) {
  const section = button.closest(".agents-settings"), action = button.dataset.action;
  if (action === "agent-dismiss") { section.querySelector("[data-agent-reveal]").replaceChildren(); return; }
  if (action === "agent-copy") {
    const kind = button.dataset.agentCopy;
    const node = section.querySelector(`[data-agent-${kind}]`), text = kind === "config" ? node.textContent : node.value;
    try { await navigator.clipboard.writeText(text); }
    catch { throw new Error("Copy failed. Select the value and copy it manually."); }
    section.querySelector("[data-agent-copy-status]").textContent = "Copied";
    return;
  }
  if (action === "agent-wings") {
    const read_wings = [...button.closest("[data-agent-access]").querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value);
    if (!read_wings.length) throw new Error("Choose at least one read wing.");
    await mutation(section, async () => {
      await api(`/api/connections/${encodeURIComponent(button.dataset.agentId)}`, { method: "PATCH", body: { read_wings } });
      await refresh(section);
    });
    return;
  }
  const rotate = action === "agent-rotate";
  if (!confirm(`${rotate ? "Rotate" : "Revoke"} ${button.dataset.agentName}? Its current token will stop working.`)) return;
  await mutation(section, async () => {
    const result = await api(`/api/connections/${encodeURIComponent(button.dataset.agentId)}${rotate ? "/rotate" : ""}`, { method: rotate ? "POST" : "DELETE", ...(rotate ? { body: {} } : {}) });
    if (!section.isConnected) return;
    if (rotate) reveal(section, result);
    else if (section.querySelector("[data-agent-reveal]").dataset.agentId === button.dataset.agentId) section.querySelector("[data-agent-reveal]").replaceChildren();
    await refresh(section);
  });
}

export function clearAgentSecret(root) {
  root.querySelector("[data-agent-reveal]")?.replaceChildren();
}
