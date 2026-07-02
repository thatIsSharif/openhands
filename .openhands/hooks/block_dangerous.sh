#!/bin/bash
# PreToolUse hook: Block dangerous git commands in automation.
#
# Triggered before every tool execution. Reads the tool input from stdin,
# checks for patterns that could destroy main/master/production branches,
# and denies them with a clear reason shown in the UI.
#
# Input:  JSON on stdin with {tool_input: {command: "..."}}
# Output: {"decision": "deny", "reason": "..."} on stdout + exit 2 to block
#         exit 0 to allow

set -euo pipefail

# Only inspect terminal tool commands
EVENT_TYPE="${OPENHANDS_EVENT_TYPE:-}"
TOOL_NAME="${OPENHANDS_TOOL_NAME:-}"
if [ "$EVENT_TYPE" != "PreToolUse" ] || [ "$TOOL_NAME" != "terminal" ]; then
    exit 0
fi

# Read JSON input and extract the command
input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // ""')

# If no command, allow
if [ -z "$command" ]; then
    exit 0
fi

# ── Block patterns (same as AutomationSecurityAnalyzer patterns) ────────

# git branch -D/-d main/master/production/live/primary
# Match: "git branch -D main", "git branch -d master"
if echo "$command" | grep -qE 'git branch -[dD] (main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Deleting a protected branch (main/master/production) is blocked by automation security policy."}'
    exit 2
fi

# git push --force/-f to main/master/production/live/primary
# Match: "git push --force origin main", "git push -f origin main", "git push origin +main"
if echo "$command" | grep -qE 'git push.*--force.* (main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Force-pushing to a protected branch (main/master/production) is blocked by automation security policy."}'
    exit 2
fi
if echo "$command" | grep -qE 'git push.* -f .* (main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Force-pushing to a protected branch (main/master/production) is blocked by automation security policy."}'
    exit 2
fi
if echo "$command" | grep -qE 'git push [^ ]+ \+(main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Force-pushing to a protected branch is blocked by automation security policy."}'
    exit 2
fi

# git push origin --delete main/master/production/live/primary
# Match: "git push origin --delete main"
if echo "$command" | grep -qE 'git push [^ ]+ --delete (main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Deleting a protected branch from remote is blocked by automation security policy."}'
    exit 2
fi

# git push origin :main (delete remote branch with colon syntax)
# Match: "git push origin :main"
if echo "$command" | grep -qE 'git push [^ ]+ :(main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Deleting a protected branch from remote is blocked by automation security policy."}'
    exit 2
fi

# git reset --hard origin/main (destroy local changes tracking main)
# Match: "git reset --hard origin/main"
if echo "$command" | grep -qE 'git reset --hard [^ /]+/(main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Hard-resetting to a protected branch is blocked by automation security policy."}'
    exit 2
fi

# git checkout -B main (force-create/reset local branch)
# Match: "git checkout -B main"
if echo "$command" | grep -qE 'git checkout -B (main|master|production|live|primary)( |$|")'; then
    echo '{"decision": "deny", "reason": "Force-checking out a protected branch is blocked by automation security policy."}'
    exit 2
fi

# gh repo delete
if echo "$command" | grep -qE 'gh repo delete'; then
    echo '{"decision": "deny", "reason": "Deleting a GitHub repository is blocked by automation security policy."}'
    exit 2
fi

# High-risk destructive: rm -rf /
if echo "$command" | grep -qE 'rm -rf /([ ]|$|")'; then
    echo '{"decision": "deny", "reason": "Recursive root deletion is blocked by automation security policy."}'
    exit 2
fi

# Allow everything else
exit 0
