"""Minimal example: Strands Agent with MemPalace long-term memory.

Prerequisites:
    pip install strands-agents mempalace
    mempalace init ~/my-project    # create a palace
    mempalace mine ~/my-project    # populate with content (optional)

Run:
    python example.py
"""

from strands import Agent
from strands.models import BedrockModel

from memory_tools import MEMORY_TOOLS

# Use any model — Bedrock, OpenAI, local vLLM, etc.
model = BedrockModel(model_id="us.amazon.nova-lite-v1:0", region_name="us-east-1")

agent = Agent(
    model=model,
    system_prompt="You are a helpful assistant with long-term memory.",
    tools=MEMORY_TOOLS,
)

# Store a preference — agent decides to call mp_memory_store
print("--- Storing a preference ---")
agent("Remember that I prefer weekly reports on Monday mornings.", user_id="demo-user")

# Recall later — agent decides to call mp_memory_recall
print("\n--- Recalling ---")
agent("What do you know about my preferences?", user_id="demo-user")
