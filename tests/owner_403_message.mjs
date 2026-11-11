import assert from "node:assert/strict";
import { api, ApiError } from "../commonplace/web/static/api.js";

const which = process.argv[2];
const reply = (status, body, { json = true } = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  json: async () => { if (!json) throw new SyntaxError("Unexpected token <"); return body; },
});
const reason = "This card came from a restricted connection and has not been released, so it cannot be handed off.";
const step = "Release it first: the board's coordinator does that. Then try again.";
const responses = {
  explained: reply(403, { error: reason, next_step: step, code: "handoff_refused" }),
  json_error_only: reply(403, { error: "Only cards in Todo or Doing can be handed off." }),
  bare: reply(403, null, { json: false }),
  ok_html: reply(200, null, { json: false }),
};
globalThis.fetch = async () => responses[which];
globalThis.document = { dispatchEvent() {} };

const attempt = () => api("/api/cards/7/handoff", { method: "POST", body: { to: "anyone" } });
if (which === "ok_html") {
  await assert.rejects(attempt, SyntaxError);
} else {
  const error = await attempt().then(() => assert.fail("expected a refusal"), (caught) => caught);
  assert.ok(error instanceof ApiError);
  assert.equal(error.status, 403);
  if (which === "explained") assert.equal(error.message, `${reason} ${step}`);
  if (which === "json_error_only") assert.equal(error.message, "Only cards in Todo or Doing can be handed off.");
  if (which === "bare") {
    assert.match(error.message, /not allowed to do that/);
    assert.match(error.message, /ask the workspace owner/);
    assert.ok(!/Unexpected token|Request failed/.test(error.message));
  }
}
