// The shell's project-settings wiring, run for real: the functions are sliced out of app.js and
// executed with fakes, so dropping a refresh from the app code fails an assertion here.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";

const app = await readFile(new URL("../commonplace/web/static/app.js", import.meta.url), "utf8");
function slice(start, end) {
  assert.ok(app.includes(start), `${start} missing`);
  const at = app.indexOf(start), stop = app.indexOf(end, at + start.length);
  assert.ok(stop > at, `${end} must follow ${start}`);
  return app.slice(at, stop);
}
const source = slice("function resumeNeedsAfterPanel(", "// The no-answer error's restart link") + "\n" + slice("async function openRestart(", "function cancelCard()") + "\n" + slice("function shell() {", "\nfunction ");
const projects = await import(new URL("../commonplace/web/static/projects.js", import.meta.url));
const e = (value) => String(value ?? "");
const plain = (value) => JSON.parse(JSON.stringify(value));

function shell({ base = "#/needs", paused = true } = {}) {
  const refreshes = [], pauses = [], mounts = [];
  const host = { id: "settings-host", focus() {} };
  const layout = { classList: { contains: () => false, toggle() {} } };
  const panel = { hidden: true, innerHTML: "", querySelector: () => host, addEventListener() {}, setAttribute() {}, removeAttribute() {} };
  const state = { base, screen: 1, needsUI: { paused: () => paused, pause: (value) => pauses.push(value) },
    workbenchChat: { board: { slug: "atlas" }, dock: { close() {}, backToChat() {} } } };
  const context = {
    state, e, enc: encodeURIComponent, panel, onNeeds: () => state.base === "#/needs" || state.base === "#/",
    voice: { stop() {} }, api: async () => ({}), detailsUI: null,
    currentBoard: () => ({ slug: "atlas" }),
    root: { querySelector: (selector) => selector === "#card-panel" ? panel : selector === ".shell" ? layout : null },
    refresh: async (options) => { refreshes.push(options); },
    navigation() {}, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    document: { activeElement: null }, CSS: { escape: (value) => value },
    MutationObserver: class { constructor(callback) { context.observed = callback; } observe(target, options) { context.watched = { target, options }; } disconnect() {} },
  };
  runInNewContext(source + "\nthis.shell = shell; this.resumeNeedsAfterPanel = resumeNeedsAfterPanel; this.openProjectSettings = typeof openProjectSettings === 'function' ? openProjectSettings : undefined; this.openRestart = openRestart;", context);
  const mountProject = async (target, options) => { mounts.push({ target, options }); };
  return { context, state, panel, refreshes, pauses, mounts, mountProject };
}

const cases = {
  // Back and the panel's close both end with Needs me re-reading its lists.
  panel_close() {
    const f = shell();
    f.panel.hidden = false;
    f.context.resumeNeedsAfterPanel(f.panel);
    assert.deepEqual(plain(f.pauses), [], "an open panel keeps Needs me paused");
    assert.deepEqual(plain(f.refreshes), [], "an open panel does not refresh");
    f.panel.hidden = true;
    f.context.resumeNeedsAfterPanel(f.panel);
    assert.deepEqual(plain(f.pauses), [false], "a closed panel resumes Needs me");
    assert.deepEqual(plain(f.refreshes), [{ background: true }], "closing the panel refreshes the lists");
    const idle = shell({ paused: false });
    idle.context.resumeNeedsAfterPanel(idle.panel);
    assert.deepEqual(plain(idle.refreshes), [], "nothing to resume, nothing to refresh");
  },
  // "Set the model" opens the role's picker, and Back or a saved model refreshes the lists.
  async needs_settings() {
    const f = shell();
    assert.equal(typeof f.context.openProjectSettings, "function", "openProjectSettings missing");
    await f.context.openProjectSettings("project-models", { dataset: { board: "atlas", role: "worker" } }, { mountProject: f.mountProject, settingsOptions: projects.settingsOptions });
    assert.equal(f.mounts.length, 1);
    const { options } = f.mounts[0];
    assert.equal(options.slug, "atlas");
    assert.equal(options.focus, "models");
    assert.equal(options.role, "worker", "the row's role reaches the picker");
    assert.deepEqual(plain(f.pauses), [true], "Needs me waits while settings use the panel");
    assert.equal(f.panel.hidden, false);
    assert.equal(typeof options.refresh, "function", "the shell hands a refresh to the settings");
    options.refresh();
    assert.deepEqual(plain(f.refreshes), [{ background: true }], "a saved model refreshes the lists");
    f.refreshes.length = 0;
    options.onBack();
    assert.equal(f.panel.hidden, true);
    assert.deepEqual(plain(f.refreshes), [{ background: true }], "Back refreshes the lists");
    // Off Needs me there is nothing to resume, but a saved model still refreshes the shell.
    const board = shell({ base: "#/board/atlas" });
    await board.context.openProjectSettings("coordinator", { dataset: { board: "atlas" } }, { mountProject: board.mountProject, settingsOptions: projects.settingsOptions });
    const offNeeds = board.mounts[0].options;
    assert.equal(offNeeds.onBack, undefined);
    offNeeds.refresh();
    assert.deepEqual(plain(board.refreshes), [{ background: true }]);
  },
  // The shell's own panel observer: when the settings panel closes it resumes Needs me and refreshes.
  shell_sync() {
    const f = shell();
    f.state.workbenchChat = { dispose() {} };
    f.context.shell();
    assert.equal(typeof f.context.observed, "function", "the shell watches its panel");
    assert.equal(f.context.watched?.target, f.panel, "the shell observes its panel");
    assert.deepEqual(plain(f.context.watched.options), { attributes: true, attributeFilter: ["hidden"] });
    f.panel.hidden = false;
    f.context.observed();
    assert.deepEqual(plain(f.pauses), [], "an open panel keeps Needs me paused");
    assert.deepEqual(plain(f.refreshes), []);
    f.panel.hidden = true;
    f.context.observed();
    assert.deepEqual(plain(f.pauses), [false], "the panel closing resumes Needs me");
    assert.deepEqual(plain(f.refreshes), [{ background: true }], "the panel closing refreshes the lists");
  },
  // The restart settings panel refreshes the same way.
  async restart() {
    const f = shell({ base: "#/board/atlas/chat" });
    await f.context.openRestart({}, { mountProject: f.mountProject, settingsOptions: projects.settingsOptions });
    assert.equal(f.mounts.length, 1);
    const { options } = f.mounts[0];
    assert.equal(options.restart, true);
    assert.equal(typeof options.refresh, "function", "the restart panel hands a refresh to the settings");
    options.refresh();
    assert.deepEqual(plain(f.refreshes), [{ background: true }]);
  },
};

assert.ok(process.argv[2] in cases, "unknown case");
await cases[process.argv[2]]();
process.stdout.write(`PASS ${process.argv[2]}\n`);
