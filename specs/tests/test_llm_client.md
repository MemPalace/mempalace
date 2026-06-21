# Behavior Spec: `tests/test_llm_client.py`

This is a test module. It pins the observable contract of the LLM-client subsystem
(`mempalace.llm_client`): a provider factory, an HTTP-POST-JSON helper, three concrete
providers (Ollama, OpenAI-compatible, Anthropic), an error type, and provider properties
for privacy classification and API-key provenance. All HTTP is mocked; no network or
running Ollama is required (tests/test_llm_client.py:L1-L6). The spec below describes the
contract of the system under test as asserted by these tests, implementable in any language.

## Public surface under test

Imported public names: `AnthropicProvider`, `LLMError`, `OllamaProvider`,
`OpenAICompatProvider`, `_http_post_json`, `get_provider` (tests/test_llm_client.py:L13-L20).

## Factory: `get_provider(kind, model, *, endpoint=, api_key=)`

- `get_provider("ollama", model)` returns an `OllamaProvider` whose `.model` equals the
  passed model and whose `.endpoint` equals `OllamaProvider.DEFAULT_ENDPOINT`
  (tests/test_llm_client.py:L26-L30).
- `get_provider("openai-compat", model, endpoint=...)` returns an `OpenAICompatProvider`
  (tests/test_llm_client.py:L33-L35).
- `get_provider("anthropic", model, api_key=...)` returns an `AnthropicProvider` whose
  `.api_key` equals the passed key (tests/test_llm_client.py:L38-L41).
- An unknown provider kind raises `LLMError` with a message containing "Unknown provider"
  (tests/test_llm_client.py:L44-L46).

## HTTP helper: `_http_post_json(url, body_dict, headers_dict, *, timeout)`

- On success it POSTs and returns the parsed JSON object of the response body; e.g. a
  response body of `{"ok": true}` yields the object `{"ok": True}`
  (tests/test_llm_client.py:L52-L59).
- An HTTP error response (non-2xx status) is wrapped as `LLMError`; for a 404 the message
  contains "HTTP 404" (tests/test_llm_client.py:L62-L69).
- A connection/URL failure (host unreachable) is wrapped as `LLMError` with a message
  containing "Cannot reach" (tests/test_llm_client.py:L72-L77).
- A response body that is not valid JSON is wrapped as `LLMError` with a message containing
  "Malformed" (tests/test_llm_client.py:L80-L87).

## `OllamaProvider`

Construction takes `model=` (tests/test_llm_client.py:L108). The default endpoint is
`http://localhost:11434` (tests/test_llm_client.py:L341-L343).

### `check_available() -> (ok: bool, msg: str)`

Queries the Ollama tags endpoint, whose response shape is
`{"models": [{"name": ...}, ...]}` (tests/test_llm_client.py:L102-L104).

- If the configured model name appears exactly among tag names, returns `(True, "ok")`
  (tests/test_llm_client.py:L101-L111).
- A bare model name matches a tag with a `:latest` suffix: model `"mymodel"` matches tag
  `"mymodel:latest"` and returns ok=True (tests/test_llm_client.py:L114-L123).
- If the model is absent from the tag list, returns ok=False and a message containing the
  remediation hint `"ollama pull <model>"` (e.g. `"ollama pull absent"`)
  (tests/test_llm_client.py:L126-L136).
- If Ollama is unreachable, returns ok=False with a message containing "Cannot reach Ollama"
  (tests/test_llm_client.py:L139-L146).

### `classify(system, user, *, json_mode=False) -> response`

POSTs to the chat endpoint whose URL ends with `/api/chat`
(tests/test_llm_client.py:L155-L163). The chat response shape is
`{"message": {"content": <text>}}` (tests/test_llm_client.py:L93-L98).

- The request body contains `"model"` equal to the provider model and, when `json_mode=True`,
  contains `"format": "json"` (tests/test_llm_client.py:L159-L162).
- The returned response object has `.provider == "ollama"` and `.text` equal to the model's
  message content (tests/test_llm_client.py:L164-L165).
- If the returned message content is empty, raises `LLMError` with a message containing
  "Empty response" (tests/test_llm_client.py:L168-L172).

### Provider properties

- `api_key` is `None` and `api_key_source` is `None` (Ollama uses no key)
  (tests/test_llm_client.py:L481-L485).
- `is_external_service` is `False` for the default localhost endpoint
  (tests/test_llm_client.py:L340-L348).

## `OpenAICompatProvider`

Construction takes `model=`, optional `endpoint=`, optional `api_key=`
(tests/test_llm_client.py:L195, L227, L234). The chat response shape is
`{"choices": [{"message": {"content": <text>}}]}` (tests/test_llm_client.py:L178-L184).

### URL resolution

- When `endpoint` has no `/v1` suffix (e.g. `http://h:1234`), the request URL is
  `<endpoint>/v1/chat/completions` (tests/test_llm_client.py:L187-L197).
- When `endpoint` already ends in `/v1` (e.g. `http://h:1234/v1`), the URL is not doubled:
  it resolves to `http://h:1234/v1/chat/completions` (tests/test_llm_client.py:L200-L210).

### `classify(system, user, *, json_mode=False)`

- If no endpoint is configured, `classify` raises `LLMError` with a message containing
  "requires --llm-endpoint" (tests/test_llm_client.py:L213-L216).
- When an API key is present, the request carries header `Authorization: Bearer <key>`
  (tests/test_llm_client.py:L219-L229).
- When `json_mode=True`, the request body contains
  `"response_format": {"type": "json_object"}` (tests/test_llm_client.py:L238-L248).
- If the response does not contain the expected `choices[].message.content` shape, raises
  `LLMError` with a message containing "Unexpected response shape"
  (tests/test_llm_client.py:L251-L259).

### API-key resolution and provenance

- If `api_key` is not passed but env var `OPENAI_API_KEY` is set, `.api_key` falls back to
  that env value (tests/test_llm_client.py:L232-L235).
- When `api_key` is passed explicitly, `.api_key_source` is `"flag"` even if `OPENAI_API_KEY`
  is also set in the environment — the explicit flag wins
  (tests/test_llm_client.py:L441-L450).
- When `api_key` is not passed and `OPENAI_API_KEY` is used, `.api_key_source` is `"env"`
  (tests/test_llm_client.py:L453-L463).

### `is_external_service` (privacy heuristic)

A URL-based classification: local addresses are NOT external. Local includes localhost,
127.x, RFC1918 LAN ranges, and the Tailscale CGNAT range; everything else is external
(tests/test_llm_client.py:L330-L337).

- `http://localhost:1234`, `http://127.0.0.1:8000`, and `http://192.168.1.50:1234`
  (RFC1918 LAN) are all `is_external_service == False`
  (tests/test_llm_client.py:L351-L359).
- A cloud endpoint such as `https://api.openai.com` is `is_external_service == True`
  (tests/test_llm_client.py:L362-L369).
- Tailscale CGNAT range `100.64.0.0/10` (first octet 100 AND second octet 64-127 inclusive)
  is local: `100.64.0.1`, `100.100.50.50`, `100.127.255.254` are all `False`
  (tests/test_llm_client.py:L392-L408).
- Addresses in `100.x` outside CGNAT (second octet `< 64` or `> 127`) are external:
  `100.0.0.1`, `100.63.255.255`, `100.128.0.0`, `100.255.255.255` are all `True`
  (tests/test_llm_client.py:L411-L428).

## `AnthropicProvider`

Construction takes `model=` and optional `api_key=`
(tests/test_llm_client.py:L276, L299). The response shape is
`{"content": [{"type": "text", "text": <text>}, ...]}` (tests/test_llm_client.py:L265-L271).
The class exposes `API_VERSION` (tests/test_llm_client.py:L302).

### `check_available()` and key resolution

- If no key is passed and `ANTHROPIC_API_KEY` is unset, `check_available()` returns ok=False
  with a message containing "ANTHROPIC_API_KEY" (tests/test_llm_client.py:L274-L279).
- If `ANTHROPIC_API_KEY` is set, `.api_key` resolves to that value and `check_available()`
  returns ok=True (tests/test_llm_client.py:L282-L287).

### `classify(system, user)`

- The request carries header `X-Api-Key` equal to the configured key and header
  `Anthropic-Version` equal to `AnthropicProvider.API_VERSION`; the returned `.text` equals
  the response text (tests/test_llm_client.py:L290-L303).
- When the response contains multiple text blocks, their `text` values are concatenated in
  order with no inserted separator: blocks `"part one. "` and `"part two."` yield
  `"part one. part two."` (tests/test_llm_client.py:L306-L320).
- If no key is available (arg None and `ANTHROPIC_API_KEY` unset), `classify` raises
  `LLMError` with a message containing "requires ANTHROPIC_API_KEY"
  (tests/test_llm_client.py:L323-L327).

### Provenance and privacy

- `.api_key_source` is `"flag"` when the key is passed explicitly and `"env"` when resolved
  from `ANTHROPIC_API_KEY` (tests/test_llm_client.py:L466-L478).
- The default endpoint is `https://api.anthropic.com` and is always
  `is_external_service == True` (tests/test_llm_client.py:L372-L380).

## Observable contracts summary

- Error type `LLMError` is the single failure channel for factory, HTTP helper, and all
  provider `classify` paths; failure messages contain stable substrings asserted above
  (tests/test_llm_client.py:L44-L46, L62-L87, L168-L172, L213-L216, L251-L259, L323-L327).
- Request body fields that are contract: `model`, `format` (Ollama, value `"json"`),
  `response_format` (OpenAI-compat, value `{"type": "json_object"}`)
  (tests/test_llm_client.py:L161-L162, L248).
- Request headers that are contract: `Authorization: Bearer <key>` (OpenAI-compat),
  `X-Api-Key` and `Anthropic-Version` (Anthropic)
  (tests/test_llm_client.py:L229, L301-L302).
- Response object exposes `.provider` and `.text` (tests/test_llm_client.py:L164-L165, L303).
- Provider properties that are contract: `.model`, `.endpoint`, `.api_key`, `.api_key_source`
  (values `"flag"`/`"env"`/`None`), `.is_external_service` (bool)
  (tests/test_llm_client.py:L29-L30, L41, L447-L448, L461-L462, L485, L345).
