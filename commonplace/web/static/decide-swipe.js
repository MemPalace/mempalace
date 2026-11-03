// Swipe on the yes/no card: right is yes, left is no. Touch and pen only; the buttons
// and keys do the same, so a mouse drag does nothing and reduced motion turns it off.
export const SWIPE = { commit: 0.35, flick: 0.2, speed: 0.5, lock: 10, arm: 300, stamp: 0.1, tilt: 7, spring: 150, fly: 180 };

// The first 10px decide the axis. A mostly vertical start is a scroll and never answers.
export function swipeAxis(dx, dy) {
  if (Math.hypot(dx, dy) < SWIPE.lock) return null;
  return Math.abs(dy) > Math.abs(dx) ? "y" : "x";
}

// Past 35% of the card width commits, and so does a flick over 0.5 px/ms past 20%.
export function swipeResult(dx, width, speed = 0) {
  if (!width) return null;
  const ratio = Math.abs(dx) / width;
  const flick = ratio >= SWIPE.flick && Math.abs(speed) > SWIPE.speed && Math.sign(speed) === Math.sign(dx);
  if (ratio >= SWIPE.commit || flick) return dx > 0 ? "yes" : "no";
  return null;
}

export function swipeLook(dx, width) {
  const ratio = width ? dx / width : 0;
  const size = Math.abs(ratio);
  const side = size >= SWIPE.stamp ? (dx > 0 ? "yes" : "no") : "";
  return {
    side,
    tilt: Math.max(-SWIPE.tilt, Math.min(SWIPE.tilt, ratio * SWIPE.tilt * 2)),
    stamp: Math.max(0, Math.min(1, (size - SWIPE.stamp) / (SWIPE.commit - SWIPE.stamp))),
    fill: Math.min(1, size / 0.5),
    armed: size >= SWIPE.commit,
  };
}

export function swipeAllowed(pointerType, now, armedAt) {
  return (pointerType === "touch" || pointerType === "pen") && now >= armedAt;
}

export function bindSwipe(card, dock, { armedAt, onCommit, onSettle, labels }) {
  let start = null, axis = null, samples = [], busy = false;
  const track = dock?.querySelector(".decide-track");
  const label = track?.querySelector(".label");
  const paint = (dx) => {
    const width = card.offsetWidth;
    const look = swipeLook(dx, width);
    card.style.transform = `translateX(${dx}px) rotate(${look.tilt}deg)`;
    card.style.setProperty("--stamp", String(look.stamp));
    card.dataset.drag = look.side;
    if (dock) {
      dock.dataset.drag = look.side;
      dock.style.setProperty("--fill", String(look.fill));
      dock.dataset.armed = String(look.armed);
    }
    if (label) label.textContent = look.armed ? (look.side === "yes" ? labels.yes : labels.no) : "";
  };
  const clear = () => {
    card.dataset.drag = "";
    card.style.removeProperty("--stamp");
    if (dock) { dock.dataset.drag = ""; dock.dataset.armed = "false"; dock.style.removeProperty("--fill"); }
    if (label) label.textContent = "";
  };
  const spring = () => {
    card.style.transition = `transform ${SWIPE.spring}ms ease-out`;
    card.style.transform = "";
    clear();
    setTimeout(() => { card.style.transition = ""; onSettle?.(); }, SWIPE.spring);
  };
  card.addEventListener("pointerdown", (event) => {
    if (busy || !event.isPrimary || !swipeAllowed(event.pointerType, performance.now(), armedAt)) return;
    start = { x: event.clientX, y: event.clientY, id: event.pointerId };
    axis = null;
    samples = [{ x: event.clientX, t: event.timeStamp }];
    // Protect the card from queue repaints while the contact's direction is pending.
    card.classList.add("dragging");
  });
  card.addEventListener("pointermove", (event) => {
    if (!start || event.pointerId !== start.id) return;
    const dx = event.clientX - start.x, dy = event.clientY - start.y;
    if (!axis) {
      axis = swipeAxis(dx, dy);
      if (axis === "y") { start = null; card.classList.remove("dragging"); onSettle?.(); return; }
      if (axis === "x") card.setPointerCapture?.(event.pointerId);
    }
    if (axis !== "x") return;
    samples.push({ x: event.clientX, t: event.timeStamp });
    samples = samples.filter((sample) => event.timeStamp - sample.t <= 100);
    paint(dx);
  });
  const end = (event, cancelled) => {
    if (!start || event.pointerId !== start.id) return;
    const dx = event.clientX - start.x;
    const was = axis;
    start = null; axis = null;
    card.classList.remove("dragging");
    if (was !== "x") { onSettle?.(); return; }
    // A drag is not a tap: keep the title link from opening on release.
    const swallow = (click) => { click.preventDefault(); click.stopPropagation(); };
    card.addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => card.removeEventListener("click", swallow, { capture: true }), 400);
    const first = samples[0], last = samples[samples.length - 1];
    const speed = first && last && last.t > first.t ? (last.x - first.x) / (last.t - first.t) : 0;
    const result = cancelled ? null : swipeResult(dx, card.offsetWidth, speed);
    if (!result) { spring(); return; }
    busy = true;
    if (result === "no") { card.style.transform = ""; clear(); onCommit("no"); return; }
    card.style.transition = `transform ${SWIPE.fly}ms ease-in`;
    card.style.transform = `translateX(${card.offsetWidth * 1.4}px) rotate(${SWIPE.tilt * 1.5}deg)`;
    setTimeout(() => onCommit("yes"), SWIPE.fly);
  };
  card.addEventListener("pointerup", (event) => end(event, false));
  card.addEventListener("pointercancel", (event) => end(event, true));
}
