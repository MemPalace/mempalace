// The modules load from their real files, so an import a module gains later resolves without wiring here.
const load = (name) => import(new URL(`../commonplace/web/static/${name}`, import.meta.url));
const { cardTags, boardView } = await load('board.js');
const { waitingSection, whyStrip } = await load('waiting.js');
const { renderDetails } = await load('details.js');
const base = { number: 3, board: 'atlas', version: 1, title: 'Card', body: '', events: [], status: 'todo', need: 'idle', assignee: null };
const reasons = {
  blocked_by: 'Waiting on #7: Dependency',
  waits_on_person: 'Waiting on a person',
  claim_expired: 'Claim expired: the agent stopped reporting; its next update renews it, or hand the card off',
  plan_review: 'Waiting for plan review',
  not_released: 'Waiting for release',
  no_agent: 'No agent is connected to this project yet',
  project_limit: 'Waiting for a free slot: 1 of 1',
  checks_pending: 'Waiting for checks to finish',
};
const withReason = (code) => ({ ...base, waiting: { code, text: reasons[code] } });
const notes = [{ code: 'project_limit', count: 2, text: 'Agent limit reached (1 of 1 working): 2 cards wait for a free slot.' }, { code: 'no_agent', count: 1, text: 'No agent is connected, so 1 card cannot start.' }];
console.log(JSON.stringify({
  tags: Object.keys(reasons).map((code) => cardTags(withReason(code))[0]),
  available: cardTags(base).includes('Available for an agent'),
  availableWhenWaiting: cardTags(withReason('no_agent')).includes('Available for an agent'),
  details: Object.keys(reasons).map((code) => renderDetails(withReason(code), { me: 'user:a' }).includes(`<p>${reasons[code]}</p>`)),
  detailsPlain: renderDetails(base, { me: 'user:a' }).includes('data-details-waiting'),
  escaped: waitingSection({ waiting: { text: '<b>x</b>' } }),
  strip: whyStrip({ waiting_notes: notes }),
  stripEmpty: [whyStrip({}), whyStrip({ waiting_notes: [] }), whyStrip(undefined)],
  inBoard: boardView({ board: { slug: 'atlas', name: 'Atlas', waiting_notes: notes }, cards: [withReason('no_agent')], me: 'user:a' }).includes('data-why-strip'),
  noStripInBoard: boardView({ board: { slug: 'atlas', name: 'Atlas' }, cards: [base], me: 'user:a' }).includes('data-why-strip'),
}));
