import { api as request, escape as e } from "./api.js";

// Mirrors commonplace/boards/task_types.py: the server stays the authority, this only gives early feedback.
const SLUG = /^[a-z][a-z0-9-]{0,47}$/;
const hint = "Use lowercase letters, digits and hyphens, starting with a letter (at most 48 characters).";

export function parseTypes(text) {
  const labels = [...new Set(String(text).split(/[\s,]+/).filter(Boolean))];
  const bad = labels.find((label) => !SLUG.test(label));
  if (bad) throw new Error(`"${bad}" is not a valid task type. ${hint}`);
  return labels.sort();
}

export function cardTypeRows(card) {
  const type = card.task_type || "";
  // The owner flag that gates priority gates task types too: both are person-owner writes.
  if (!card.can_prioritize) return type ? `<dt>Task type</dt><dd>${e(type)}</dd>` : "";
  return `<dt>Task type</dt><dd><input id="card-task-type" aria-label="Card task type" value="${e(type)}" maxlength="48" placeholder="Any agent" autocomplete="off"><button class="btn sm" data-card-type-save>Set</button><br><small>Only agents you allow for this type can take the card. Leave empty for any agent.</small><span role="alert" data-card-type-error></span></dd>`;
}

/** Saves the card's task type from the details panel; returns the updated card, or null when nothing changed. */
export async function saveCardType(mount, card, api = request) {
  const input = mount.querySelector("#card-task-type"), error = mount.querySelector("[data-card-type-error]");
  const value = input.value.trim();
  if (error) error.textContent = "";
  if (value === (card.task_type || "")) return null;
  try {
    if (value && !SLUG.test(value)) throw new Error(`"${value}" is not a valid task type. ${hint}`);
    return await api(`/api/cards/${card.number}`, { method: "PATCH", body: { version: card.version, task_type: value } });
  } catch (failure) {
    if (error) error.textContent = failure.message;
    return null;
  }
}

export function agentTypesControl(connection) {
  return `<details data-agent-types data-agent-id="${e(connection.id)}"><summary>Task types</summary><label>Allowed task types (separated by spaces or commas)<input data-agent-types-input disabled autocomplete="off" placeholder="Loading…"></label><small>This agent can take cards of these types and cards without a type. Leave empty to allow untyped cards only.</small><button type="button" data-agent-types-save>Save task types</button><span role="status" data-agent-types-status></span></details>`;
}

const path = (id) => `/api/task-types/connections/${encodeURIComponent(id)}`;

async function load(details, api) {
  const input = details.querySelector("[data-agent-types-input]"), status = details.querySelector("[data-agent-types-status]");
  if (details.dataset.loaded) return;
  details.dataset.loaded = "true";
  try {
    const result = await api(path(details.dataset.agentId));
    input.value = result.allowed_types.join(" ");
    input.placeholder = "No task types";
    input.disabled = false;
  } catch (failure) {
    delete details.dataset.loaded;
    status.textContent = failure.message;
  }
}

async function save(button, api) {
  const details = button.closest("[data-agent-types]"), input = details.querySelector("[data-agent-types-input]"), status = details.querySelector("[data-agent-types-status]");
  if (input.disabled) return;
  status.textContent = "";
  try {
    const result = await api(path(details.dataset.agentId), { method: "POST", body: { allowed_types: parseTypes(input.value) } });
    input.value = result.allowed_types.join(" ");
    status.textContent = "Saved";
  } catch (failure) {
    status.textContent = failure.message;
  }
}

/** Per-agent grants load when their section is first opened and save from the same section. */
export function installAgentTypes(target, api = request) {
  // The toggle event does not bubble, so it is observed while capturing.
  target.addEventListener("toggle", (event) => {
    const details = event.target.closest?.("[data-agent-types]");
    if (details?.open) void load(details, api);
  }, true);
  target.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-agent-types-save]");
    if (button) void save(button, api);
  });
}

if (typeof document !== "undefined") installAgentTypes(document);
