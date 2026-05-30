# OpenCode Plugin: MemPalace

[![npm version](https://img.shields.io/npm/v/opencode-plugin-mempalace.svg)](https://www.npmjs.com/package/opencode-plugin-mempalace)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A community open-source plugin that integrates [MemPalace](https://github.com/milla-jovovich/mempalace)'s "lifetime memory" (L0-L3 memory stack, AAAK compression, auto context saving) into the [OpenCode](https://opencode.ai) terminal assistant.

This plugin ensures your AI assistant has a long-term memory across sessions by seamlessly hooking into OpenCode's lifecycle events to fetch, inject, and save contexts related to your specific workspace.

## 🌟 Features

- **Zero-Config Auto-Initialization**: Opens a new folder? The plugin automatically initializes a MemPalace database for it in the background.
- **Auto-Injection (Wake-up)**: Automatically wakes up MemPalace on session initialization to inject L0 (Global Identity) and L1 (Critical Facts) directly into the AI's System Prompt.
- **Pre-Compaction Rescue**: Adds your core memory context back right before OpenCode compresses the conversation, ensuring crucial details are never lost.
- **Silent Background Mining**: Quietly exports and saves your conversational history into your MemPalace database as you chat, preserving decisions for future usage without spending extra tokens on MCP tool calls.
- **Crash Safety & Idle Auto-Save**: Never lose your context, even if you close the terminal early!
  - _Idle Auto-Save_: If your session is deleted or you simply stop chatting (idle), any un-saved messages are softly mined in the background.
  - _Crash Safety_: If you force-quit the terminal (`Ctrl+C`), the plugin intercepts the exit signal and performs a synchronous emergency save.

## 📋 Requirements

- OpenCode AI Terminal
- Python 3.9+
- MemPalace installed globally (`pip install mempalace` or `python3 -m pip install mempalace`)

## 🚀 Installation

1. Install the official MemPalace CLI on your system:

   ```bash
   python3 -m pip install mempalace
   ```

2. Add this plugin to your OpenCode configuration. Open `~/.config/opencode/opencode.json` (or your project's local `opencode.json`) and add the package name to the plugins array:

   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "plugin": ["opencode-plugin-mempalace"]
   }
   ```

## ⚙️ Configuration (Optional)

You can pass configuration options to the plugin to customize its behavior. Currently supported options:

- `threshold` (default: 15): The number of chat messages required before the plugin triggers a background auto-save (mining) of your conversation.

```json
{
  "plugin": [["opencode-plugin-mempalace", { "threshold": 20 }]]
}
```

## 🛠️ How It Works

- The plugin wraps the `mempalace` CLI via the `execa` package.
- It acts as the "subconscious" of your AI, rather than just an active tool.
- Hooks used:
  - `experimental.chat.system.transform`: Injects memory.
  - `experimental.session.compacting`: Rescues memory from truncation.
  - `chat.message`: Tracks conversation length and triggers background mining.
  - `event`: Listens for `session.idle` and `session.deleted` for soft-exit saving.
  - `process.on('exit' | 'SIGINT' | 'SIGTERM')`: Intercepts hard process exits for emergency synchronous saving.
- **Workspace Isolation**: It infers the wing name intelligently from the workspace path (e.g. `/projects/my-app` -> `wing_my-app`). Your memory stays isolated per project!

## 🧑‍💻 Development

This project is built with TypeScript (ESM) and relies on a strict TDD approach.

```bash
npm install
npm run lint
npm run test
npm run build
```

## 📄 License

MIT
