- **Tests:**

  - `tests/test_chat_model_stream.py` asserts a single request on `http_error` and every `bad_response`, refuses a paused Anthropic turn and OpenAI tool calls ending with a non-tool stop, and stops between the events of one frame on cancel; the bot test files cover a block that reads `gone` after access ended mid-turn, every free-text schema shape at registration, the fallback log codes, and a start the runner would refuse getting no chip.
