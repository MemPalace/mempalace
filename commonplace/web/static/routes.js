/** The board screen a bare visit used to land on: the last visited readable board, else the
 * first one, else the first-run screen. Shared card links and "back from admin" still need a
 * project screen behind them, so they keep using this.
 */
export function boardRoute({ boards = [], last } = {}) {
  const board = boards.find((board) => board.slug === last) || boards[0];
  return board ? `#/board/${encodeURIComponent(board.slug)}` : "#/start";
}

/** Resolve a hash without changing browser state or reading saved preferences.
 * boards is the readable board list; last is a previously visited board slug.
 * A person who has a project lands on their bots; someone with none sees the first-run screen.
 * Old chat links keep opening the project chat. Return a hash string for the app dispatcher.
 */
export function resolveRoute(hash, { boards = [], last } = {}) {
  const route = hash || "#/";
  if (route === "#/") return boards.length ? "#/bots" : "#/start";
  const chat = /^#\/chat\/([^/?]+)(\?.*)?$/.exec(route);
  if (chat) return `#/board/${chat[1]}/chat${chat[2] || ""}`;
  return route;
}
