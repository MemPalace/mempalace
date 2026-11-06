// The dock owns placement and focus; the chat controller owns messages and drafts.
export function layoutMode(width) {
  return width >= 1100 ? "slot" : width >= 1024 ? "push" : width >= 700 ? "sheet" : "phone";
}

export function nextFocus(items, current, backwards = false) {
  if (!items.length) return null;
  const index = items.indexOf(current);
  if (index < 0) return items[backwards ? items.length - 1 : 0];
  return items[(index + (backwards ? -1 : 1) + items.length) % items.length];
}

// Select whole shell regions, including the header and phone navigation, not just cards.
export function inertTargets(mode) {
  return mode === "sheet" || mode === "phone" ? [".rail", "#main", ".center", ".m-tabs"] : [];
}

export function toggleLabel({ label = "Chat", unread = 0, hasOrchestrator = true, open = false } = {}) {
  if (!hasOrchestrator) return `${label} · no orchestrator yet`;
  const count = Number.isSafeInteger(unread) && unread > 0 ? unread : 0;
  return !open && count ? `${label} ${count} new ${count === 1 ? "reply" : "replies"}` : label;
}

const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const focusableSelector = 'a[href], button, input, textarea, select, [tabindex], [contenteditable="true"]';
const typing = (el) => el?.isContentEditable || !!el?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])');

export function createDock({ root, api, e = escapeHTML, chat }) {
  const shell = typeof root === "function" ? root() : root;
  const doc = shell.ownerDocument, win = doc.defaultView;
  let header, toggle, announcement, pane, content, about, scrim, heading, statusLine;
  let board, label = "Chat", unread = 0, hasOrchestrator = false;
  let opened = false, inDetails = false, opener = null, openerKey = null, returnRow = null, generation = 0;
  let context, contextHidden, wasTwo, strip = null, shownBoard = null;
  const inertBefore = new Map(), modalBefore = new Map();
  const make = (tag, className, parent) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    parent.append(node);
    return node;
  };
  const controls = () => [...shell.querySelectorAll('[aria-controls="chat"]')];
  const projectName = () => board?.name || board?.slug || "Project";
  const shown = (el) => !!el?.isConnected && !el.closest('[hidden], [inert]') && el.getClientRects().length > 0;
  // A view may re-render its opener while the chat is open: remember how to find it again.
  const keyOf = (el) => {
    if (el?.id) return { id: el.id };
    const attr = [...(el?.attributes || [])].find((a) => a.name.startsWith("data-"));
    // Keep the value too: a rebuilt list has many [data-needs-select] rows, and only one is this.
    return attr ? { attr: attr.name, value: el.getAttribute(attr.name) ?? "" } : null;
  };
  const quoted = (value) => `"${String(value).replace(/["\\]/g, (c) => `\\${c}`)}"`;
  const refind = (key) => {
    if (!key) return null;
    try {
      if (key.id) return doc.getElementById(key.id);
      const selector = `[${key.attr}=${quoted(key.value)}]`;
      return shell.querySelector(selector) || doc.querySelector(selector);
    } catch { return null; }
  };
  // After a close the view may redraw the opener once more (unread counts, tabs): until the
  // person moves focus or points elsewhere, a lost focus goes back to the redrawn opener.
  let keeper = null, keeperKey = null;
  function release() { keeper?.disconnect(); keeper = null; }
  function keepFocus(key) {
    release();
    if (!key || typeof win.MutationObserver !== "function") return;
    keeper = new win.MutationObserver(() => {
      const active = doc.activeElement;
      if (active && active !== doc.body && active.isConnected) return;
      const again = refind(key);
      if (shown(again)) again.focus();
    });
    keeper.observe(shell, { childList: true, subtree: true });
  }
  function moved(event) {
    if (keeper && (event.type === "pointerdown" || !refind(keeperKey)?.contains(event.target))) release();
  }
  const focusables = (region) => [...region.querySelectorAll(focusableSelector)].filter((el) =>
    !el.disabled && el.tabIndex >= 0 && !el.closest('[hidden], [inert], [aria-hidden="true"]') && el.getClientRects().length);
  function setExpanded() {
    for (const control of controls()) control.setAttribute("aria-expanded", String(opened));
  }
  function paint() {
    if (!toggle) return;
    const full = toggleLabel({ label, unread, hasOrchestrator, open: opened });
    const suffix = full.slice(label.length);
    toggle.innerHTML = `<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M2.5 3.5h11v7H7.2L4.3 13v-2.5H2.5z"/></svg>${e(label)}${suffix ? ` <span class="ct-chip ${hasOrchestrator ? "new" : "off"}">${e(suffix.trim())}</span>` : ""} <kbd aria-hidden="true">/</kbd>`;
    toggle.setAttribute("aria-label", full);
    setExpanded();
    const count = !opened && hasOrchestrator && Number.isSafeInteger(unread) && unread > 0 ? unread : 0;
    const text = count ? `${count} new ${count === 1 ? "reply" : "replies"} in ${projectName()} chat` : "";
    if (announcement.textContent !== text) announcement.textContent = text;
    heading.textContent = `${projectName()} chat`;
  }
  function restoreModal() {
    for (const [el, wasInert] of inertBefore) {
      if (!wasInert) el.removeAttribute("inert");
    }
    inertBefore.clear();
    for (const [el, attrs] of modalBefore) {
      for (const [name, value] of attrs) {
        if (value === null) el.removeAttribute(name); else el.setAttribute(name, value);
      }
    }
    modalBefore.clear();
  }
  function place() {
    restoreModal();
    const mode = layoutMode(win.innerWidth);
    const active = opened ? pane : inDetails ? context : null;
    if (!active) return;
    active.dataset.layout = mode;
    const selectors = inertTargets(mode);
    if (!selectors.length) return;
    modalBefore.set(active, ["role", "aria-modal", "tabindex"].map((name) => [name, active.getAttribute(name)]));
    active.setAttribute("role", "dialog");
    active.setAttribute("aria-modal", "true");
    active.setAttribute("tabindex", "-1");
    for (const el of shell.querySelectorAll(selectors.join(", "))) {
      // Never make an ancestor of the active sheet inert.
      if (el.contains(active) || active.contains(el)) continue;
      inertBefore.set(el, el.hasAttribute("inert"));
      el.setAttribute("inert", "");
    }
    if (!active.contains(doc.activeElement)) {
      if (opened) focusComposer(); else (focusables(active)[0] || active).focus();
    }
  }
  function focusComposer() {
    chat.focusComposer?.();
    if (!pane.contains(doc.activeElement)) (pane.querySelector("textarea") || focusables(pane)[0] || pane).focus();
  }
  function paintStrip(value) {
    strip = value;
    about.replaceChildren();
    about.hidden = !value;
    if (!value) return;
    make("span", "chat-about-text", about).textContent = value.text;
    const back = make("button", "btn sm", about);
    back.type = "button";
    back.dataset.closeChat = "";
    back.textContent = value.backLabel || "Back to the source";
  }
  function build() {
    if (pane) return;
    scrim = make("div", "chat-scrim", shell);
    scrim.setAttribute("aria-hidden", "true");
    pane = make("aside", "chatpane", shell);
    pane.id = "chat";
    pane.hidden = true;
    pane.setAttribute("aria-labelledby", "chat-h");
    pane.setAttribute("tabindex", "-1");
    const bar = make("div", "chat-head", pane);
    const icon = make("span", "ch-ic", bar);
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 3.5h11v7H7.2L4.3 13v-2.5H2.5z"/></svg>';
    const title = make("div", "ch-t", bar);
    heading = make("h2", "", title);
    heading.id = "chat-h";
    // The chat's one-line status ("Orchestrator connected", "Connected, not answering since 14:02").
    statusLine = make("p", "ch-status", title);
    statusLine.hidden = true;
    chat.watchHeader?.((text) => { statusLine.textContent = text; statusLine.hidden = !text; });
    const closeButton = make("button", "btn ghost sm", bar);
    closeButton.type = "button";
    closeButton.id = "chat-close";
    closeButton.dataset.closeChat = "";
    closeButton.innerHTML = '<svg class="i" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8"/></svg>Close chat';
    closeButton.setAttribute("aria-keyshortcuts", "Escape ]");
    about = make("div", "chat-about", pane);
    about.setAttribute("role", "note");
    about.hidden = true;
    content = make("div", "chat-dock-content", pane);
    content.id = "chat-content";
  }
  function mount(headerEl, options) {
    if (header && header !== headerEl) {
      close();
      toggle.remove();
      announcement.remove();
    }
    if (board && board.slug !== options.board.slug) {
      close();
      chat.stop();
      shownBoard = null;
    }
    header = headerEl;
    board = options.board;
    label = options.label || "Chat";
    unread = options.unread ?? 0;
    hasOrchestrator = options.hasOrchestrator ?? !!board.coordinator;
    build();
    toggle = header.querySelector("#chat-toggle") || make("button", "chat-toggle", header);
    toggle.id = "chat-toggle";
    toggle.type = "button";
    toggle.setAttribute("aria-controls", "chat");
    toggle.setAttribute("aria-keyshortcuts", "/");
    announcement = header.querySelector("[data-chat-announcement]") || make("span", "sr", header);
    announcement.dataset.chatAnnouncement = "";
    announcement.setAttribute("role", "status");
    announcement.setAttribute("aria-atomic", "true");
    paint();
    return toggle;
  }
  function open({ from = toggle, strip: nextStrip = null, at, quote, focus = null, resume = false } = {}) {
    if (!board || !toggle?.isConnected) return Promise.resolve();
    release();
    const token = ++generation, reopening = !opened && !resume && shownBoard === board.slug;
    if (!opened && !inDetails) {
      context = shell.querySelector("#ctx") || shell.querySelector(".context");
      contextHidden = context?.hidden;
      wasTwo = shell.classList.contains("two");
    }
    opener = from || toggle;
    openerKey = keyOf(opener);
    if (opener?.setAttribute) {
      opener.setAttribute("aria-controls", "chat");
      opener.setAttribute("aria-expanded", "true");
    }
    opened = true;
    inDetails = false;
    returnRow = null;
    paintStrip(nextStrip);
    if (context) context.hidden = true;
    shell.classList.remove("ctx-pushed");
    shell.classList.add("chat-open");
    pane.hidden = false;
    paint();
    place();
    let result;
    // Reading resumes after a close: the divider moves to what was read before.
    if (reopening) chat.reopened?.();
    chat.setOpen?.(true);
    if (shownBoard !== board.slug || at !== undefined) {
      shownBoard = board.slug;
      result = chat.show(board.slug, { at, quote, name: board.name });
    } else if (!resume) {
      chat.setQuote?.(quote ?? null);
    }
    const focusOpen = () => {
      if (focus?.isConnected && pane.contains(focus)) {
        focus.scrollIntoView({ block: "nearest" });
        focus.focus();
      } else focusComposer();
    };
    focusOpen();
    return Promise.resolve(result).then(() => {
      if (opened && token === generation && (pane.contains(doc.activeElement) || doc.activeElement === opener)) focusOpen();
    });
  }
  function close({ toDetails = false, row = null } = {}) {
    if (!opened && !inDetails) return;
    generation++;
    opened = false;
    // A closed pane is not read: the controller stops marking chat seen.
    chat.setOpen?.(false);
    inDetails = toDetails && !!context;
    returnRow = inDetails ? row : null;
    pane.hidden = true;
    shell.classList.remove("chat-open");
    shell.classList.toggle("ctx-pushed", inDetails);
    if (context) context.hidden = inDetails ? false : contextHidden;
    if (!inDetails) shell.classList.toggle("two", wasTwo);
    paint();
    place();
    if (inDetails) {
      (context.querySelector("[data-details-title]") || focusables(context)[0] || context).focus();
    } else {
      const again = shown(opener) ? opener : refind(openerKey);
      const target = shown(again) ? again : toggle;
      if (target?.isConnected) target.focus();
      keeperKey = target === toggle ? { id: toggle.id } : openerKey;
      keepFocus(keeperKey);
      opener = null;
      openerKey = null;
      paintStrip(null);
    }
  }
  function backToChat() {
    const row = returnRow, original = opener, originalStrip = strip;
    return open({ from: original, strip: originalStrip, focus: row, resume: true });
  }

  function click(event) {
    const target = event.target.closest?.('[aria-controls="chat"], [data-close-chat], [data-back-chat], [data-action="close-panel"]');
    if (!target) {
      if (event.target === scrim) {
        if (inDetails) backToChat(); else close();
      }
      return;
    }
    if (target.matches('[data-back-chat], [data-action="close-panel"]') && inDetails) {
      event.preventDefault();
      backToChat();
    } else if (target.matches("[data-close-chat]")) {
      event.preventDefault();
      close();
    } else if (target.getAttribute("aria-controls") === "chat") {
      event.preventDefault();
      if (target === toggle && opened) close(); else open({ from: target });
    }
  }
  function keydown(event) {
    if (event.defaultPrevented || event.isComposing) return;
    const active = opened ? pane : inDetails ? context : null;
    // A nested dialog or palette keeps its own keys and focus trap.
    const otherModal = [...doc.querySelectorAll('[aria-modal="true"]:not([hidden])')].find((el) => el !== active);
    if (otherModal) return;
    if (event.key === "Tab" && active?.getAttribute("aria-modal") === "true") {
      event.preventDefault();
      (nextFocus(focusables(active), doc.activeElement, event.shiftKey) || active).focus();
      return;
    }
    if (event.key === "Escape" && active?.contains(event.target)) {
      event.preventDefault();
      if (inDetails) backToChat(); else close();
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey || typing(event.target)) return;
    if (shell.dataset.singleKeys === "off" || doc.querySelector('[data-single-keys][aria-checked="false"]')) return;
    if (!toggle?.isConnected || toggle.closest("[hidden]")) return;
    if (event.key === "/") { event.preventDefault(); open(); }
    if (event.key === "]" && active) {
      event.preventDefault();
      if (inDetails) backToChat(); else close();
    }
  }
  shell.addEventListener("click", click);
  doc.addEventListener("keydown", keydown);
  doc.addEventListener("focusin", moved);
  doc.addEventListener("pointerdown", moved, true);
  win.addEventListener("resize", place);
  return {
    mount, open, close, backToChat,
    isOpen: () => opened,
    update(options = {}) {
      if ("unread" in options) unread = options.unread;
      if ("hasOrchestrator" in options) hasOrchestrator = options.hasOrchestrator;
      paint();
    },
    destroy() {
      close();
      release();
      doc.removeEventListener("focusin", moved);
      doc.removeEventListener("pointerdown", moved, true);
      chat.stop();
      shell.removeEventListener("click", click);
      doc.removeEventListener("keydown", keydown);
      win.removeEventListener("resize", place);
      toggle?.remove();
      announcement?.remove();
      pane?.remove();
      scrim?.remove();
    },
  };
}
