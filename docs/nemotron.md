# Nemotron embeddings

MemPalace can use a local Nemotron 3 Embed server through its `openai-compat`
backend. The `/v1/embeddings` API takes strings. Configure the two prefixes
from the [NVIDIA model card](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16)
so indexing sends `passage: ` and search sends `query: `.

Start a compatible vLLM embedding server that exposes `/v1/embeddings`. Use
the model ID reported by its `/v1/models` endpoint. For example, if you
start the server with the served name `nemotron-embed`, run MemPalace with
these settings on the same host:

```bash
export MEMPALACE_EMBEDDING_MODEL=openai-compat
export MEMPALACE_EMBEDDING_API_URL=http://127.0.0.1:8000/v1
export MEMPALACE_EMBEDDING_API_MODEL=nemotron-embed
export MEMPALACE_EMBEDDING_API_QUERY_PREFIX='query: '
export MEMPALACE_EMBEDDING_API_DOCUMENT_PREFIX='passage: '
export MEMPALACE_PALACE_PATH="$HOME/.mempalace/palace-nemotron"

mempalace mine /path/to/project
mempalace search "Where are the project settings?" --results 5
```

The equivalent keys in `config.json` are `embedding_model`,
`embedding_api_url`, `embedding_api_model`, `embedding_api_query_prefix`,
and `embedding_api_document_prefix`. Preserve the trailing space in both
prefixes. If the server requires authentication, set
`MEMPALACE_EMBEDDING_API_KEY` in the MemPalace process environment.

Use a fresh palace when switching embedding models or keeping a second
index. To convert an existing palace, back it up, configure the endpoint
and prefixes, then run `mempalace repair rebuild-index --yes` with that
palace selected. Rebuild again if the endpoint starts serving different
weights under the same model ID. Size collection chunks for the embedding
server's context window.
