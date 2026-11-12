## chat-and-voice

- **Paths:** `commonplace/chat/`, `commonplace/voice.py`, `commonplace/phone_push/`
- **Owns:**

  - The control-database `project_model_catalog` table, created beside the model settings, holds each board's list of models owners can pick for projects.

  - Person-message and space-created listeners (the chat front, project seats) share `events.is_stored` to confirm an event is the one the log holds.

  - The workspace chat front agent, server-local dictation and opt-in phone notifications.

  - The live delivery state of a person's newest chat message (`chat/live.py`, shown by `web/static/chatlive.js`): the control-database tables `chat_live_posts` (the event that carried a message) and `chat_live_reads` (how far a board's orchestrator has read), and the `live` object in the chat page.

  - The control-database `bot_model_calls` table (bot model requests per person and day, numbers only); `store.counts_today` adds it to project chat's count so both share one budget, and `store.take_bot_call` checks and counts one bot request in a single transaction.

  - `front.skipping()` is a thread-local context manager: a project-chat post made inside it on the same thread is ignored by the front agent's event handler (no receipt, model job, runner relay or fallback card). Bots wrap their forwarded posts in it.

  - `commonplace/chat/model.py` holds the shared connection code (`resolve_endpoint`, `Call`) behind both `complete` and the streaming client `commonplace/chat/model_stream.py`, which yields reply text, complete tool calls and one stop reason as the provider sends them.

- **Promises:**

  - The live state (`idle`, `delivered`, `working`, `answered`, `no_agent`, `no_answer`) is read from stored facts only: the wake delivery row, the orchestrator's own `space_read` and a later agent message. It calls no model or outside service, stores ids and times but no message text, describes only the caller's own message, stays inside the caller's workspace, and gives the agent's read time only to a caller who may see that agent.

  - Chat status counts cards in needs-me while ignoring per-board coordinator notices.
  - Opt-in phone needs-you evaluation includes actionable Approvals, so owner origin recovery still notifies after coordinator revocation.

  - Chat answers come from the configured provider or the poster's own runner, never a silent fallback; audio stays in request memory; push is opt-in.

  - `model_stream.stream` takes every limit from its caller (timeout for the whole call, output tokens, input characters, response bytes), makes no retry, uses the same origin, key-host and loopback checks as `complete`, and can be cancelled, which closes the socket.

  - Voice is switched per instance with `COMMONPLACE_VOICE` (default on) and picks each language's model with `COMMONPLACE_VOICE_MODEL_EN` and `COMMONPLACE_VOICE_MODEL_NL`; the defaults equal the previous fixed models. A switched-off instance refuses voice with 503, and the free-plan daily transcription cap applies to every clip.

- **How it fails:**

  - Without a provider or runner the chat shows a receipt and no answer; push delivery failures are recorded, not retried forever.

  - A message nobody picks up turns into `no_answer` (60 seconds after a successful wake, 5 minutes without a wake hook or after a read without a reply, at once when every wake attempt failed) and a board without a usable orchestrator shows `no_agent`; a failed read-cursor write is logged and never fails the agent's `space_read`.

  - A streaming call fails with only the codes `timeout`, `http_error`, `bad_response` and `stopped`, never provider error text; a tool call with a name that was not offered, or arguments that are not a JSON object, is a `bad_response`. So is an Anthropic `pause_turn` stop (a paused, partial reply never reads as final) and OpenAI-compatible tool calls that end with any stop other than `tool_calls`. A cancel takes effect between the events of one frame too.

  - An empty or invalid voice setting stops startup with a message naming the setting, and a missing model at load time is an error; neither falls back to another model.

- **Tests:**

  - The `tests/test_chat_` files, `tests/test_commonplace_voice.py`, the `tests/test_commonplace_phone_push_` files.
  - `tests/test_chat_live.py` covers delivery to the orchestrator, every live state, a member beside an owner and two workspaces with the same board and message id; `tests/test_chat_live_view.py` (`tests/chat_live_view.mjs`) covers the pane's text per state and role and that states and the answer arrive through the poll.

  - `tests/test_chat_model_stream.py` holds the streaming transport against loopback fake Anthropic and OpenAI-compatible servers: events arrive while the server holds the rest back, fragmented and parallel tool calls, wire shapes, caller limits, deadline, cancel, and the key-host and loopback refusals.
  - `tests/test_commonplace_voice.py` covers the instance switch, model settings, invalid-setting errors and the free-plan cap at, over and under its limit.
  - Coordinator-revocation recovery verifies the origin approval item and retains notification, priority and duplicate-suppression assertions.
