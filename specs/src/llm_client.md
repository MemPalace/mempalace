# Behavior Specification: `llm_client.py`

Minimal provider abstraction for LLM-assisted entity refinement. Exposes three interchangeable providers (`ollama`, `openai-compat`, `anthropic`) behind a uniform `classify` / `check_available` interface, plus a factory and a local-vs-external endpoint heuristic. Communication is over HTTP using only standard-library facilities — no external SDK dependency is part of the contract (mempalace/llm_client.py:L1-L22).

## Errors and Result Types

`LLMError` is the single error type raised for any provider failure — transport, parse, auth, or missing model (mempalace/llm_client.py:L106-L107).

A successful classification returns a response value carrying four fields: `text` (string), `model` (string), `provider` (string), and `raw` (the full parsed provider response object) (mempalace/llm_client.py:L110-L115).

## Local vs External Endpoint Heuristic

`_endpoint_is_local(url)` returns whether a URL's host is on the user's machine or private network. A None/empty/unparseable URL is treated as local — a defensive default since no endpoint means no external request can occur yet; if the URL host parses to empty it is also local (mempalace/llm_client.py:L44-L68). A host is local when it equals `localhost`, `127.0.0.1`, or `::1` (mempalace/llm_client.py:L41-L70), ends in `.local` (mempalace/llm_client.py:L71-L72), starts with `10.` or `192.168.` (mempalace/llm_client.py:L73-L76), is in `172.16.0.0`–`172.31.255.255` i.e. second octet 16–31 inclusive (mempalace/llm_client.py:L77-L85), is in the Tailscale CGNAT range `100.64.0.0/10` i.e. first octet 100 with second octet 64–127 inclusive (mempalace/llm_client.py:L86-L99), or begins with `fc`/`fd` for IPv6 unique-local addresses (mempalace/llm_client.py:L100-L102). Host comparison is case-insensitive (lowercased) (mempalace/llm_client.py:L64-L64). Anything else — public IPs and FQDNs — is external (mempalace/llm_client.py:L59-L103). If URL parsing raises, the result is non-local (False) (mempalace/llm_client.py:L65-L66).

## Provider Base Interface

Every provider is constructed with `model`, optional `endpoint`, optional `api_key`, a `timeout` (default 120 seconds), and an optional `api_key_source` (mempalace/llm_client.py:L124-L141). `api_key_source` records provenance of the key: `"flag"` when an explicit key argument was passed, `"env"` when it was resolved from an environment variable, and None when no key is in play — used downstream to gate a consent prompt for env-resolved keys (mempalace/llm_client.py:L136-L141).

`classify(system, user, json_mode=True, think=None)` maps a (system, user) prompt pair to a structured response. `think` toggles reasoning emission for thinking-capable models and is honored only by the Ollama provider; other providers accept but ignore it (mempalace/llm_client.py:L143-L158).

`check_available()` returns a `(ok, message)` pair where `ok` is a boolean and `message` is a string — a fast reachability probe (mempalace/llm_client.py:L160-L162).

`is_external_service` is a read-only boolean derived purely from the endpoint via the local heuristic: True when the endpoint is not local. It is endpoint-driven regardless of provider class (mempalace/llm_client.py:L164-L176).

## HTTP Transport Contract

All POST calls send a JSON-encoded body with header `Content-Type: application/json` merged with provider-supplied headers, and return the parsed JSON response (mempalace/llm_client.py:L179-L188). On an HTTP error status it raises `LLMError` with message `HTTP <code> from <url>: <detail>`, where detail is up to the first 500 characters of the response body (falling back to the HTTP reason) (mempalace/llm_client.py:L189-L195). On a transport/OS error it raises `LLMError` `Cannot reach <url>: <error>` (mempalace/llm_client.py:L196-L197). On malformed JSON it raises `LLMError` `Malformed response from <url>: <error>` (mempalace/llm_client.py:L198-L199).

## Ollama Provider

Name is `ollama`; default endpoint is `http://localhost:11434`, used when no endpoint is supplied; default timeout is 180 seconds; an optional `num_ctx` context-size parameter is retained (mempalace/llm_client.py:L205-L222).

`check_available()` GETs `<endpoint>/api/tags` with a 5-second timeout; on any transport/parse error it returns `(False, "Cannot reach Ollama at <endpoint>: <error>")` (mempalace/llm_client.py:L224-L229). It collects the `name` of each entry under `models` and considers the model present if either the bare model name or `<model>:latest` matches; otherwise it returns `(False, "Model '<model>' not loaded in Ollama. Run: ollama pull <model>")`. On success it returns `(True, "ok")` (mempalace/llm_client.py:L230-L238).

`classify` POSTs to `<endpoint>/api/chat`. The request body sets `model`, a two-message `messages` array (system then user), `stream: false`, and `options` containing `temperature: 0.1` plus `num_ctx` when configured (mempalace/llm_client.py:L240-L258). When `json_mode` is true it adds `format: "json"` (mempalace/llm_client.py:L259-L260). When `think` is explicitly true or false it adds `think: <bool>`; when None the field is omitted (mempalace/llm_client.py:L261-L266). The response text is read from `message.content`; an empty/missing text raises `LLMError` `Empty response from Ollama (model=<model>)` (mempalace/llm_client.py:L267-L271).

## OpenAI-Compatible Provider

Name is `openai-compat`. It targets any OpenAI-compatible `/v1/chat/completions` endpoint; the API key comes from the explicit argument or the `OPENAI_API_KEY` environment variable, setting `api_key_source` to `"flag"` or `"env"` accordingly (None if neither) (mempalace/llm_client.py:L277-L307).

URL resolution: with no endpoint, `classify` raises `LLMError` `openai-compat provider requires --llm-endpoint` (mempalace/llm_client.py:L309-L311). The endpoint has trailing slashes stripped; if it already ends in `/chat/completions` it is used as-is, otherwise `/v1` is appended if not already present and then `/chat/completions` is appended (mempalace/llm_client.py:L312-L317).

`check_available()` returns `(False, "no --llm-endpoint configured")` when no endpoint is set (mempalace/llm_client.py:L319-L321). Otherwise it strips a trailing `/chat/completions` and/or `/v1`, then GETs `<base>/v1/models` with a 5-second timeout, sending `Authorization: Bearer <key>` if a key is present; transport failure returns `(False, "Cannot reach <endpoint>: <error>")`, success returns `(True, "ok")` (mempalace/llm_client.py:L322-L332).

`classify` POSTs a body with `model`, a system+user `messages` array, and `temperature: 0.1`; with `json_mode` true it adds `response_format: {"type": "json_object"}` (mempalace/llm_client.py:L334-L350). It sends `Authorization: Bearer <key>` only when a key is present (mempalace/llm_client.py:L351-L353). The response text is read from `choices[0].message.content`; a missing/mis-shaped path raises `LLMError` `Unexpected response shape: <error>`, and empty text raises `LLMError` `Empty response from openai-compat (model=<model>)` (mempalace/llm_client.py:L354-L361). The `think` argument is ignored (mempalace/llm_client.py:L339-L339).

## Anthropic Provider

Name is `anthropic`; default endpoint is `https://api.anthropic.com`; the API version header value is `2023-06-01` (mempalace/llm_client.py:L367-L370). The API key comes from the explicit argument or the `ANTHROPIC_API_KEY` environment variable, setting `api_key_source` to `"flag"`/`"env"`/None as above (mempalace/llm_client.py:L372-L393).

`check_available()` returns `(False, "ANTHROPIC_API_KEY not set (use --llm-api-key or env)")` when no key is present; otherwise it returns `(True, "ok")` without any network probe — deliberately avoiding a billable request, so an invalid key only surfaces on the first real call (mempalace/llm_client.py:L395-L400).

`classify` raises `LLMError` `Anthropic provider requires ANTHROPIC_API_KEY env or --llm-api-key` if no key is present (mempalace/llm_client.py:L409-L410). JSON mode is requested at the prompt level: when `json_mode` is true the system prompt is appended with `"\n\nRespond with valid JSON only, no prose."` (mempalace/llm_client.py:L411-L413). It POSTs to `<endpoint>/v1/messages` a body with `model`, `max_tokens: 2048`, `temperature: 0.1`, a `system` string, and a single user `messages` entry (mempalace/llm_client.py:L414-L420). Headers are `X-API-Key: <key>` and `anthropic-version: 2023-06-01` (mempalace/llm_client.py:L421-L424). The response text is the concatenation of all `content` blocks whose `type` is `text`, joining their `text` fields; a mis-shaped response raises `LLMError` `Unexpected response shape: <error>` and empty text raises `LLMError` `Empty response from Anthropic (model=<model>)` (mempalace/llm_client.py:L425-L436). The `think` argument is ignored (mempalace/llm_client.py:L407-L407).

## Provider Factory

A registry maps the three names (`ollama`, `openai-compat`, `anthropic`) to their provider implementations (mempalace/llm_client.py:L442-L446). `get_provider(name, model, endpoint=None, api_key=None, timeout=120, **provider_kwargs)` constructs the named provider, forwarding extra keyword arguments (e.g. `num_ctx` for Ollama) to its constructor; providers ignore unrecognized extras. An unknown name raises `LLMError` `Unknown provider '<name>'. Choices: <sorted list of names>` (mempalace/llm_client.py:L449-L465).
