#!/bin/bash
# setup_multi_session_hooks.sh
# Configures MemPalace auto-save hooks for multiple AI sessions to share a single palace
#
# Usage: ./setup_multi_session_hooks.sh [--palace PATH] [--dry-run]
#
# This script adds Stop and PreCompact hooks to:
# - Claude Code (~/.claude/settings.json)
# - Gemini CLI (~/.gemini/settings.json)
# - Qwen (~/.qwen/settings.json)
#
# All sessions will read/write to the same memory palace.

set -e

PALACE_PATH="${1:-$HOME/.mempalace}"
DRY_RUN=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --palace)
            PALACE_PATH="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "Usage: $0 [--palace PATH] [--dry-run]"
            exit 1
            ;;
    esac
done

HOOKS_DIR="$PALACE_PATH/src/hooks"
SAVE_HOOK="$HOOKS_DIR/mempal_save_hook.sh"
PRECOMPACT_HOOK="$HOOKS_DIR/mempal_precompact_hook.sh"

echo "=== MemPalace Multi-Session Hook Setup ==="
echo "Palace path: $PALACE_PATH"
echo "Save hook: $SAVE_HOOK"
echo "PreCompact hook: $PRECOMPACT_HOOK"
echo ""

# Verify hooks exist
if [[ ! -f "$SAVE_HOOK" ]]; then
    echo "Error: Save hook not found at $SAVE_HOOK"
    echo "Make sure MemPalace is installed: pip install mempalace"
    exit 1
fi

if [[ ! -f "$PRECOMPACT_HOOK" ]]; then
    echo "Error: PreCompact hook not found at $PRECOMPACT_HOOK"
    exit 1
fi

# Hook configuration template
CLAUDE_HOOKS='
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "'"$SAVE_HOOK"'",
            "timeout": 30
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "'"$PRECOMPACT_HOOK"'",
            "timeout": 30
          }
        ]
      }
    ]
'

configure_claude() {
    local CONFIG_FILE="$HOME/.claude/settings.json"
    echo "Configuring Claude Code..."
    
    if [[ "$DRY_RUN" == true ]]; then
        echo "[DRY RUN] Would add hooks to $CONFIG_FILE"
        return
    fi
    
    # Create directory if needed
    mkdir -p "$(dirname "$CONFIG_FILE")"
    
    # Check if hooks already exist
    if grep -q "mempal_save_hook" "$CONFIG_FILE" 2>/dev/null; then
        echo "  Hooks already configured in $CONFIG_FILE"
        return
    fi
    
    # Add hooks to existing settings or create new file
    if [[ -f "$CONFIG_FILE" ]]; then
        # Insert hooks before closing brace
        sed -i 's/}$/,'"$(echo "$CLAUDE_HOOKS" | sed 's/[&/\]/\\&/g')"'\n}/g' "$CONFIG_FILE"
    else
        echo '{"hooks": {'"$CLAUDE_HOOKS"'}}' > "$CONFIG_FILE"
    fi
    
    echo "  ✓ Added hooks to $CONFIG_FILE"
}

configure_gemini() {
    local CONFIG_FILE="$HOME/.gemini/settings.json"
    echo "Configuring Gemini CLI..."
    
    if [[ "$DRY_RUN" == true ]]; then
        echo "[DRY RUN] Would add hooks to $CONFIG_FILE"
        return
    fi
    
    mkdir -p "$(dirname "$CONFIG_FILE")"
    
    if grep -q "mempal_save_hook" "$CONFIG_FILE" 2>/dev/null; then
        echo "  Hooks already configured in $CONFIG_FILE"
        return
    fi
    
    # Gemini uses AfterAgent instead of Stop
    GEMINI_HOOKS='
    "hooks": {
      "AfterAgent": [
        {
          "matcher": "",
          "hooks": [
            {
              "type": "command",
              "command": "'"$SAVE_HOOK"'",
              "timeout": 30000
            }
          ]
        }
      ],
      "PreCompact": [
        {
          "matcher": "",
          "hooks": [
            {
              "type": "command",
              "command": "'"$PRECOMPACT_HOOK"'",
              "timeout": 30000
            }
          ]
        }
      ]
    }'
    
    if [[ -f "$CONFIG_FILE" ]]; then
        sed -i 's/}$/,'"$(echo "$GEMINI_HOOKS" | sed 's/[&/\]/\\&/g')"'\n}/g' "$CONFIG_FILE"
    else
        echo '{"hooks": {'"$GEMINI_HOOKS"'}}' > "$CONFIG_FILE"
    fi
    
    echo "  ✓ Added hooks to $CONFIG_FILE"
}

configure_qwen() {
    local CONFIG_FILE="$HOME/.qwen/settings.json"
    echo "Configuring Qwen..."
    
    if [[ "$DRY_RUN" == true ]]; then
        echo "[DRY RUN] Would add hooks to $CONFIG_FILE"
        return
    fi
    
    mkdir -p "$(dirname "$CONFIG_FILE")"
    
    if grep -q "mempal_save_hook" "$CONFIG_FILE" 2>/dev/null; then
        echo "  Hooks already configured in $CONFIG_FILE"
        return
    fi
    
    # Qwen config
    QWEN_HOOKS='
    "hooks": {
      "Stop": [
        {
          "matcher": "*",
          "hooks": [
            {
              "type": "command",
              "command": "'"$SAVE_HOOK"'",
              "timeout": 30
            }
          ]
        }
      ],
      "PreCompact": [
        {
          "matcher": "*",
          "hooks": [
            {
              "type": "command",
              "command": "'"$PRECOMPACT_HOOK"'",
              "timeout": 30
            }
          ]
        }
      ]
    }'
    
    if [[ -f "$CONFIG_FILE" ]]; then
        sed -i 's/}$/,'"$(echo "$QWEN_HOOKS" | sed 's/[&/\]/\\&/g')"'\n}/g' "$CONFIG_FILE"
    else
        echo '{"hooks": {'"$QWEN_HOOKS"'}}' > "$CONFIG_FILE"
    fi
    
    echo "  ✓ Added hooks to $CONFIG_FILE"
}

# Main
echo ""
configure_claude
configure_gemini
configure_qwen

echo ""
echo "=== Setup Complete ==="
echo "All sessions will now share the same MemPalace at: $PALACE_PATH/palace/"
echo ""
echo "To verify, start any AI session and run:"
echo "  mempalace search 'test'"
echo ""
echo "Hooks will automatically save memories when sessions stop or compact."

if [[ "$DRY_RUN" == true ]]; then
    echo ""
    echo "[DRY RUN] No changes were made. Remove --dry-run to apply."
fi
