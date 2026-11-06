/** Resolve a hash without changing browser state or reading saved preferences.
 * boards is the readable board list; last is a previously visited board slug.
 * Return a hash string for the app dispatcher (including an open chat suffix).
 */
export function resolveRoute(hash, { boards = [], last } = {}) {
  const route = hash || "#/";
  if (route === "#/") {
    const board = boards.find((board) => board.slug === last) || boards[0];
    return board ? `#/board/${encodeURIComponent(board.slug)}` : "#/start";
  }
  const chat = /^#\/chat\/([^/?]+)(\?.*)?$/.exec(route);
  if (chat) return `#/board/${chat[1]}/chat${chat[2] || ""}`;
  return route;
}
