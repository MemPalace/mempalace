import { readFile } from 'node:fs/promises';
const url = (s) => `data:text/javascript;base64,${Buffer.from(s).toString('base64')}`;
const api = url(await readFile(new URL('../commonplace/web/static/api.js', import.meta.url), 'utf8'));
const source = await readFile(new URL('../commonplace/web/static/board.js', import.meta.url), 'utf8');
const { cardTags } = await import(url(source.replace("'./api.js'", JSON.stringify(api))));
const base = { status: 'todo', need: 'idle', assignee: null };
console.log(JSON.stringify([
  cardTags({ ...base, waiting: { code: 'blocked_by', blockers: [{ number: 7, title: 'Dependency' }] } })[0],
  cardTags({ ...base, waiting: { code: 'project_limit', used: 1, cap: 1 } })[0],
]));
