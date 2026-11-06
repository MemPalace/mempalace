import { api as request, escape as e } from "./api.js";

const styles = `
.create-project { width: 520px; max-width: calc(100vw - 24px); max-height: calc(100dvh - 32px); overflow-y: auto; margin: auto; padding: 24px; border: 0; border-radius: 12px; background: var(--surface); color: var(--text); box-shadow: 0 24px 60px rgba(0,0,0,.25); }
.create-project::backdrop { background: rgba(23,24,28,.42); }
.create-project form { display: flex; flex-direction: column; gap: 16px; }
.create-project h2 { margin: 0; font-size: 20px; line-height: 26px; font-weight: 650; }
.create-project .lead { margin: -8px 0 0; color: var(--text-2); }
.create-project .lbl { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 600; }
.create-project .field { width: 100%; min-height: 36px; padding: 7px 10px; border: 1px solid var(--line-strong); border-radius: var(--r); background: var(--surface); color: var(--text); font-size: 13.5px; line-height: 20px; font-weight: 400; }
.create-project .callout { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; border-radius: var(--r); background: var(--accent-weak); box-shadow: inset 3px 0 0 var(--accent); font-size: 13px; }
.create-project .callout h3 { margin: 0; font-size: 13px; font-weight: 650; }
.create-project .acts { display: flex; justify-content: flex-end; gap: 8px; }
.create-project [role=alert] { color: var(--block); overflow-wrap: anywhere; }
@media (max-width: 699px) { .create-project { padding: 18px; } .create-project .acts { flex-wrap: wrap; } .create-project .btn { min-height: 44px; } .create-project .field { min-height: 44px; font-size: 16px; } }
`;

// Constructed stylesheets respect the app's no-inline-style document policy.
if (globalThis.document?.adoptedStyleSheets) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(styles);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

export function onboardingView({ first = true, name = "" } = {}) {
  return `<dialog class="create-project" role="dialog" aria-modal="true" aria-labelledby="create-project-heading" aria-describedby="create-project-intro">
    <form data-form="create-project">
      <h2 id="create-project-heading">${first ? "Create your first project" : "Create project"}</h2>
      <p class="lead" id="create-project-intro">A project is one board, one chat and one memory. You can make more later.</p>
      <label class="lbl">Project name<input class="field" name="name" value="${e(name)}" maxlength="1000" required autofocus></label>
      <div class="callout"><h3>No agent yet? That's fine.</h3><span>You can add cards and write in chat right away. When you connect your first agent, it becomes this project's orchestrator (its lead agent) automatically.</span></div>
      <p role="alert" hidden></p>
      <div class="acts"><button class="btn ghost" type="button" data-action="cancel-project">Cancel <kbd>Esc</kbd></button><button class="btn primary" type="submit">Create project <kbd>↵</kbd></button></div>
    </form>
  </dialog>`;
}

// Native showModal supplies background inertness and Escape.
// The caller may dispose on navigation and refresh its board list on creation.
export function mountOnboarding(mount, { api = request, onCreated = (board) => { location.hash = `#/board/${encodeURIComponent(board.slug)}`; }, onClose = () => {}, opener = mount.ownerDocument.activeElement, ...view } = {}) {
  const host = mount.ownerDocument.createElement("div");
  host.innerHTML = onboardingView(view);
  mount.append(host);
  const dialog = host.querySelector("dialog");
  const input = dialog.querySelector('[name="name"]');
  const submit = dialog.querySelector('[type="submit"]');
  const error = dialog.querySelector('[role="alert"]');
  let closed = false, busy = false;
  function close() {
    if (closed) return;
    closed = true;
    if (dialog.open) dialog.close();
    host.remove();
    if (opener?.isConnected) opener.focus();
    onClose();
  }
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll('input:not([disabled]), button:not([disabled])')];
    const first = controls[0], last = controls.at(-1);
    const active = mount.ownerDocument.activeElement;
    if (event.shiftKey && active === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
  });
  dialog.addEventListener("close", close);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
  dialog.addEventListener("click", (event) => {
    if (event.target.closest('[data-action="cancel-project"]')) { event.stopPropagation(); close(); }
  });
  dialog.addEventListener("submit", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (busy || closed) return;
    const name = input.value.trim();
    if (!name) { error.textContent = "Enter a project name"; error.hidden = false; input.focus(); return; }
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 100) || `project-${Date.now().toString(36)}`;
    busy = true;
    submit.disabled = true;
    error.hidden = true;
    let board;
    try {
      board = await api("/api/boards", { method: "POST", body: { name, slug } });
    } catch {
      if (!closed) { error.textContent = "Could not create the project. Try again."; error.hidden = false; }
    } finally {
      busy = false;
      submit.disabled = false;
    }
    if (board && !closed) { close(); onCreated(board); }
  });
  dialog.showModal();
  input.focus();
  return { close, destroy: close };
}
