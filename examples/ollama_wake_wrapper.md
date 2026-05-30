# Ollama + MemPalace Wake-Up Wrapper

This example shows how to connect MemPalace memory with a local LLM using Ollama.

## What it does

- Runs `mempalace wake-up`
- Injects the retrieved context into a prompt
- Sends the combined prompt to a local model (Gemma, Llama, etc.)

## Usage

```bash
chmod +x examples/ollama_wake_wrapper.sh

# Interactive
./examples/ollama_wake_wrapper.sh

# Non-interactive
./examples/ollama_wake_wrapper.sh "summarize my canon"
```

## Model Selection

Default model:

```bash
MODEL=gemma4:26b
```

Override per run:

```bash
MODEL=llama3.1:8b ./examples/ollama_wake_wrapper.sh "analyze this"
```

## Why this matters

MemPalace provides memory retrieval, but does not include a built-in execution loop for local models.

This wrapper demonstrates a minimal integration pattern:

```
memory (MemPalace) → context injection → local model (Ollama)
```

This allows:
- Reduced prompt size
- Lower token usage
- More consistent behavior
- Fully local execution (no API calls)

## Notes

- Works entirely offline
- Requires Ollama installed and running
- Requires MemPalace initialized and mined

## Next steps

- Add streaming output support
- Add multi-query retrieval
- Integrate with LiteLLM routing
- Build a full orchestration layer

