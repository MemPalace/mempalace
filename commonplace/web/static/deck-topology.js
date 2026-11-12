// Pixel geometry for the hub-and-machines map. No DOM, browser state or measured fonts.
import { escape as e } from './api.js';

const STATES = new Set(['ready', 'waiting', 'off', 'hub']);
const state = value => STATES.has(value) ? value : 'hub';
// A deliberately conservative glyph budget (including wide Unicode) keeps both lines inside
// their reserved box without a font-measurement dependency. Full text stays in the accessible name.
const shorten = (value, width) => {
  const chars = Array.from(String(value ?? '').replace(/\s+/g, ' '));
  let used = 0, end = 0;
  for (const char of chars) {
    const next = /[^\x20-\x7e]/.test(char) ? 16 : /[MW@%]/.test(char) ? 13 : 8;
    if (used + next > width - 16) break;
    used += next; end++;
  }
  return end < chars.length ? chars.slice(0, end).join('') + '…' : chars.join('');
};

export function layout({ nodes = [], links = [], width = 860, height = 620, phone = false }) {
  const radius = phone ? 23 : 28, labelWidth = phone ? 104 : 168, labelHeight = phone ? 54 : 40;
  const hub = nodes.find(n => n.type === 'hub') || nodes[0];
  const machines = nodes.filter(n => n !== hub), columns = Math.ceil(machines.length / 2);
  // Two outer layers, with labels facing away from the hub. More machines widen the field;
  // callers scroll it at 1:1 pixels rather than shrinking text or squeezing labels together.
  width = Math.ceil(Math.max(width, 24 + columns * labelWidth + Math.max(0, columns - 1) * 8, 2 * (radius + 12 + labelWidth)));
  height = Math.ceil(Math.max(height, 360));
  const cx = width / 2, cy = height / 2, outerY = radius + labelHeight + 24;
  const positioned = [], labels = [];
  function place(n, x, y, side) {
    const box = { id: n.id, x: side === 'beside' ? x + radius + 12 : x - labelWidth / 2, y: side === 'above' ? y - radius - 12 - labelHeight : side === 'below' ? y + radius + 12 : y - labelHeight / 2, width: labelWidth, height: labelHeight };
    const node = { ...n, x, y, radius, label: box, displayName: shorten(n.name, labelWidth), displayRole: phone && n.role === 'Waiting for attestation' ? 'Waiting for' : n.role === 'Waiting for attestation' ? n.role : shorten(n.role, labelWidth), roleExtra: phone && n.role === 'Waiting for attestation' ? 'attestation' : '' };
    positioned.push(node); labels.push(box);
  }
  if (hub) place(hub, cx, cy, 'beside');
  for (const [layer, rows] of [machines.slice(0, columns), machines.slice(columns)].entries()) {
    rows.forEach((n, i) => {
      const x = rows.length === 1 ? cx : labelWidth / 2 + 12 + i * (width - labelWidth - 24) / (rows.length - 1);
      place(n, x, layer ? height - outerY : outerY, layer ? 'below' : 'above');
    });
  }
  const byId = new Map(positioned.map(n => [n.id, n])), segments = [], dots = [];
  for (const link of links) {
    const a = byId.get(link.from), b = byId.get(link.to);
    if (!a || !b || a === b) continue;
    // The graph is a star. Fan out through the label-free space above/below the hub;
    // the small first bend keeps even the widest field clear of its beside label.
    const center = a === positioned[0] ? a : b, leaf = center === a ? b : a;
    const bend = { x: center.x + Math.max(-80, Math.min(80, (leaf.x - center.x) * .6)), y: center.y + (leaf.y < center.y ? -80 : 80) };
    const points = [center, bend, leaf], tone = state(link.state ?? leaf.state);
    for (let i = 1; i < points.length; i++) segments.push({ x1: points[i-1].x, y1: points[i-1].y, x2: points[i].x, y2: points[i].y, state: tone });
    for (const [node, toward] of [[center, bend], [leaf, bend]]) {
      const length = Math.hypot(toward.x-node.x, toward.y-node.y), offset = node.radius + 10;
      dots.push({ x: node.x + (toward.x-node.x) * offset / length, y: node.y + (toward.y-node.y) * offset / length, r: 4, state: tone });
    }
  }
  return { width, height, phone, nodes: positioned, segments, dots, labels };
}

export function device(type = 'machine', size = 34) {
  const paths = type === 'hub'
    ? '<circle cx="20" cy="20" r="13"/><circle cx="20" cy="20" r="4"/><path d="M20 7v9M20 24v9M7 20h9M24 20h9"/>'
    : '<rect x="7" y="8" width="26" height="10" rx="1.5"/><rect x="7" y="22" width="26" height="10" rx="1.5"/><path d="M11 13h.01M11 27h.01M16 13h6M16 27h6"/>';
  return `<svg class="dc-device" width="${size}" height="${size}" viewBox="0 0 40 40" aria-hidden="true">${paths}</svg>`;
}

export function render(result, { selected } = {}) {
  const lines = result.segments.map(s => `<line class="dc-link dc-${state(s.state)}" x1="${s.x1}" y1="${s.y1}" x2="${s.x2}" y2="${s.y2}"/>`).join('');
  const dots = result.dots.map(d => `<circle class="dc-dot dc-${state(d.state)}" cx="${d.x}" cy="${d.y}" r="${d.r}"/>`).join('');
  const nodes = result.nodes.map(n => `<g class="dc-node${n.id === selected ? ' dc-selected' : ''}" data-compute-node="${e(n.id)}" tabindex="0" role="button" aria-pressed="${n.id === selected}" aria-label="${e(n.name)}, ${e(n.role)}"><title>${e(n.name)}, ${e(n.role)}</title><circle class="dc-pad" cx="${n.x}" cy="${n.y}" r="${n.radius}"/><g transform="translate(${n.x - 17} ${n.y - 17})">${device(n.type)}</g><text class="dc-name" x="${n.label.x + n.label.width / 2}" y="${n.label.y + 15}" text-anchor="middle">${e(n.displayName)}</text><text class="dc-role" x="${n.label.x + n.label.width / 2}" y="${n.label.y + 32}" text-anchor="middle">${e(n.displayRole)}${n.roleExtra ? `<tspan x="${n.label.x + n.label.width / 2}" dy="14">${e(n.roleExtra)}</tspan>` : ''}</text></g>`).join('');
  return `<svg class="dc-topology" width="${result.width}" height="${result.height}" viewBox="0 0 ${result.width} ${result.height}" role="group" aria-label="Machines enrolled with Commonplace">${lines}${dots}${nodes}</svg>`;
}
