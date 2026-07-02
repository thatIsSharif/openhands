#!/bin/bash
# PreToolUse hook: Block dangerous/destructive commands in automation.
#
# Triggered before every tool execution. OpenHands sets OPENHANDS_EVENT_TYPE
# and OPENHANDS_TOOL_NAME as env vars, and also sends the full event as JSON
# on stdin (event_type, tool_name, tool_input.command, session_id,
# working_dir). We use the env vars for the cheap gate check and stdin for
# the actual command string, then check it against destructive patterns.
#
# Input:  JSON on stdin, e.g.
#         {"event_type":"PreToolUse","tool_name":"terminal",
#          "tool_input":{"command":"..."}, "session_id":"...", "working_dir":"..."}
# Output: {"decision": "deny", "reason": "..."} on stdout + exit 2 to block
#         exit 0 to allow
set -euo pipefail

# OpenHands sets these env vars directly (per docs) — no need to parse them
# out of stdin JSON.
EVENT_TYPE="${OPENHANDS_EVENT_TYPE:-}"
TOOL_NAME="${OPENHANDS_TOOL_NAME:-}"

# Only inspect terminal tool commands on PreToolUse
if [ "$EVENT_TYPE" != "PreToolUse" ] || [ "$TOOL_NAME" != "terminal" ]; then
    exit 0
fi

# The command itself only comes via the stdin JSON payload, so read that.
input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // ""')

if [ -z "$command" ]; then
    exit 0
fi

deny() {
    # $1 = reason
    printf '{"decision": "deny", "reason": %s}\n' "$(printf '%s' "$1" | jq -Rs .)"
    exit 2
}

BRANCHES='(main|master|production|prod|live|primary|release)'

# ────────────────────────────────────────────────────────────────────────
# GIT: block committing/pushing while sitting directly on a protected branch
# (state-based — catches plain "git commit" / "git push" with no branch
# name in the command text at all, unlike the pattern rules below)
# ────────────────────────────────────────────────────────────────────────

if echo "$command" | grep -qE '^git[[:space:]]+commit\b|^git[[:space:]]+push\b'; then
    project_dir="${OPENHANDS_PROJECT_DIR:-}"
    if [ -n "$project_dir" ] && [ -d "$project_dir" ]; then
        current_branch=$(cd "$project_dir" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
        if echo "$current_branch" | grep -qE "^${BRANCHES}\$"; then
            if echo "$command" | grep -qE '^git[[:space:]]+commit\b'; then
                deny "Direct commits on protected branch '${current_branch}' are blocked; use a feature branch and open a PR."
            fi
            if echo "$command" | grep -qE '^git[[:space:]]+push\b'; then
                deny "Pushing directly from protected branch '${current_branch}' is blocked; use a feature branch and open a PR."
            fi
        fi
    fi
fi

# ────────────────────────────────────────────────────────────────────────
# GIT: protected-branch destruction
# ────────────────────────────────────────────────────────────────────────

# git branch -D/-d main/master/...
echo "$command" | grep -qE "git branch -[dD] ${BRANCHES}( |\$|\")" &&
    deny "Deleting a protected branch (main/master/production) is blocked."

# git push --force / -f / --force-with-lease to protected branch
echo "$command" | grep -qE "git push.*--force([^-]|\$).* ${BRANCHES}( |\$|\")" &&
    deny "Force-pushing to a protected branch is blocked."
echo "$command" | grep -qE "git push.* -f .* ${BRANCHES}( |\$|\")" &&
    deny "Force-pushing to a protected branch is blocked."
echo "$command" | grep -qE "git push.*--force-with-lease.* ${BRANCHES}( |\$|\")" &&
    deny "Force-pushing (with lease) to a protected branch is blocked."

# git push origin +main (refspec force syntax)
echo "$command" | grep -qE "git push [^ ]+ \+${BRANCHES}( |\$|\")" &&
    deny "Force-pushing to a protected branch is blocked."

# git push --mirror / --all --force (can nuke every ref on remote)
echo "$command" | grep -qE 'git push.*--mirror' &&
    deny "Mirror-pushing (can overwrite/delete all remote refs) is blocked."

# git push origin --delete / :branch (remote branch deletion)
echo "$command" | grep -qE "git push [^ ]+ --delete ${BRANCHES}( |\$|\")" &&
    deny "Deleting a protected branch from remote is blocked."
echo "$command" | grep -qE "git push [^ ]+ :${BRANCHES}( |\$|\")" &&
    deny "Deleting a protected branch from remote is blocked."

# git reset --hard origin/main (blow away local history tracking protected branch)
echo "$command" | grep -qE "git reset --hard [^ /]+/${BRANCHES}( |\$|\")" &&
    deny "Hard-resetting to a protected branch is blocked."

# git reset --hard with no ref at all against a protected branch checkout is hard to
# detect statically, so also block bare `git reset --hard` combined with clean -fdx
# (classic "erase everything uncommitted" combo)
echo "$command" | grep -qE 'git reset --hard' && echo "$command" | grep -qE 'git clean -[a-zA-Z]*[fd][a-zA-Z]*[fd]' &&
    deny "Combining 'git reset --hard' with 'git clean -fd' wipes all local changes and untracked files; blocked."

# git clean -fdx / -xfd / -dfx etc (deletes untracked + ignored files, e.g. .env, node_modules)
echo "$command" | grep -qE 'git clean[[:space:]]+-[a-zA-Z]*[fF][a-zA-Z]*[xX]|git clean[[:space:]]+-[a-zA-Z]*[xX][a-zA-Z]*[fF]' &&
    deny "'git clean -fdx' deletes untracked AND ignored files; blocked."

# git checkout -B / switch -C (force-create/reset a protected branch ref)
echo "$command" | grep -qE "git checkout -B ${BRANCHES}( |\$|\")" &&
    deny "Force-checking out a protected branch is blocked."
echo "$command" | grep -qE "git switch -[cC] ${BRANCHES}( |\$|\")" &&
    deny "Force-creating/resetting a protected branch via 'git switch -C' is blocked."

# git filter-branch / filter-repo / rebase --root on protected branch context (history rewrite)
echo "$command" | grep -qE 'git filter-branch|git filter-repo' &&
    deny "Rewriting repository history (filter-branch/filter-repo) is blocked."

# git tag -d / push --delete tag (less severe, but still destructive to shared refs)
echo "$command" | grep -qE 'git push [^ ]+ --delete[[:space:]]+refs/tags' &&
    deny "Deleting a remote tag is blocked."

# ────────────────────────────────────────────────────────────────────────
# GITHUB CLI: destructive repo/org operations
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qE 'gh repo delete' &&
    deny "Deleting a GitHub repository is blocked."

echo "$command" | grep -qE 'gh repo archive' &&
    deny "Archiving a GitHub repository is blocked (irreversible from automation)."

echo "$command" | grep -qE 'gh api.*-X[[:space:]]*DELETE' &&
    deny "Raw DELETE calls via 'gh api' are blocked; use an approved gh subcommand instead."

echo "$command" | grep -qE 'gh secret (delete|remove)' &&
    deny "Deleting repository/organization secrets is blocked."

echo "$command" | grep -qE 'gh workflow disable' &&
    deny "Disabling GitHub Actions workflows is blocked."

echo "$command" | grep -qE 'gh release delete' &&
    deny "Deleting a GitHub release is blocked."

echo "$command" | grep -qE 'gh pr close.*--delete-branch|gh pr merge.*--delete-branch' &&
    deny "Deleting the source branch on PR close/merge is blocked; review before deleting branches."

# ────────────────────────────────────────────────────────────────────────
# JIRA / ATLASSIAN CLI (if you're shelling out to acli / jira-cli / curl)
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qiE '(jira|acli).*(delete|remove).*(issue|project|sprint|board)' &&
    deny "Deleting Jira issues/projects/sprints/boards from automation is blocked."

echo "$command" | grep -qE "curl.*-X[[:space:]]*DELETE.*atlassian\.net" &&
    deny "Raw DELETE calls against an atlassian.net API endpoint are blocked."

# ────────────────────────────────────────────────────────────────────────
# FILESYSTEM: wipes, permission bombs, disk-level operations
# ────────────────────────────────────────────────────────────────────────

# rm -rf / (and variants like rm -fr /, rm -Rf /)
echo "$command" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*[[:space:]]+/([[:space:]]|$|")' &&
    deny "Recursive root deletion is blocked."
echo "$command" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*[fF][a-zA-Z]*[rR][a-zA-Z]*[[:space:]]+/([[:space:]]|$|")' &&
    deny "Recursive root deletion is blocked."

# rm -rf on sensitive/high-blast-radius paths: home dir, /etc, /var, /usr, /*, ~, .
echo "$command" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*[[:space:]]+(/etc|/var|/usr|/boot|/root|/home|~|\$HOME)([[:space:]/]|$|")' &&
    deny "Recursive deletion of a critical system/home directory is blocked."
echo "$command" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*[[:space:]]+/\*' &&
    deny "Recursive deletion via '/*' glob is blocked."
echo "$command" | grep -qE 'rm[[:space:]]+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*[[:space:]]+\.[[:space:]]*($|")' &&
    deny "Recursive deletion of the current working directory ('rm -rf .') is blocked."

# Overwriting block devices / disks
echo "$command" | grep -qE '\bdd[[:space:]].*of=/dev/' &&
    deny "Writing directly to a block device with 'dd' is blocked."
echo "$command" | grep -qE '\bmkfs(\.[a-zA-Z0-9]+)?[[:space:]]' &&
    deny "Formatting a filesystem with 'mkfs' is blocked."
echo "$command" | grep -qE '>[[:space:]]*/dev/(sd|nvme|xvd|hd)[a-z]' &&
    deny "Writing directly to a disk device is blocked."

# chmod/chown recursive on root or wide-open permissions
echo "$command" | grep -qE 'chmod[[:space:]]+-R[[:space:]]+(777|000)[[:space:]]+/([[:space:]]|$|")' &&
    deny "Recursively chmod'ing the root filesystem is blocked."
echo "$command" | grep -qE 'chown[[:space:]]+-R.*[[:space:]]/([[:space:]]|$|")' &&
    deny "Recursively chown'ing the root filesystem is blocked."

# fork bomb
echo "$command" | grep -qE ':\(\)\{[[:space:]]*:\|:&[[:space:]]*\};:' &&
    deny "Fork bomb pattern detected and blocked."

# truncating/overwriting arbitrary files via shell redirection to system paths
echo "$command" | grep -qE '>[[:space:]]*/etc/(passwd|shadow|sudoers)' &&
    deny "Overwriting critical system auth files is blocked."

# ────────────────────────────────────────────────────────────────────────
# SECRETS / CREDENTIAL EXFIL (common automation footguns)
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qE '\benv\b[[:space:]]*(\||>)|printenv[[:space:]]*(\||>)|cat[[:space:]]+.*\.env([[:space:]]|$)' &&
    deny "Dumping environment variables / .env contents is blocked (potential secret exfiltration)."

echo "$command" | grep -qE 'aws[[:space:]]+configure[[:space:]]+get|cat[[:space:]]+.*(\.aws/credentials|\.ssh/id_)' &&
    deny "Reading cloud/SSH credential files is blocked."

# ────────────────────────────────────────────────────────────────────────
# REMOTE CODE EXECUTION: curl/wget piped straight into a shell
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qE '(curl|wget)[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(bash|sh|zsh|python[0-9.]*|perl)([[:space:]]|$)' &&
    deny "Piping a remote script directly into a shell interpreter is blocked; download and review first."

# ────────────────────────────────────────────────────────────────────────
# DATABASES: destructive SQL invoked from the shell
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qiE '\b(drop[[:space:]]+(database|schema|table)|truncate[[:space:]]+table)\b' &&
    deny "Destructive SQL (DROP DATABASE/SCHEMA/TABLE or TRUNCATE) is blocked."

echo "$command" | grep -qiE 'mongo(sh)?.*\.(dropDatabase|drop)\(\)' &&
    deny "MongoDB drop operation is blocked."

echo "$command" | grep -qiE 'redis-cli.*flushall|redis-cli.*flushdb' &&
    deny "Redis FLUSHALL/FLUSHDB is blocked."

# ────────────────────────────────────────────────────────────────────────
# CONTAINERS / INFRA-AS-CODE / CLOUD: wide-blast-radius teardown
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qE 'docker[[:space:]]+(system[[:space:]]+prune|volume[[:space:]]+prune)([[:space:]]+.*-a\b|[[:space:]]+.*--all\b)?' &&
    deny "Docker system/volume prune (can delete all containers/volumes/images) is blocked."

echo "$command" | grep -qE 'kubectl[[:space:]]+delete[[:space:]]+(namespace|ns|node|pv|pvc)\b' &&
    deny "Deleting a Kubernetes namespace/node/persistent volume is blocked."
echo "$command" | grep -qE 'kubectl.*delete.*--all\b' &&
    deny "'kubectl delete --all' (bulk resource deletion) is blocked."

echo "$command" | grep -qE 'terraform[[:space:]]+destroy' &&
    deny "'terraform destroy' is blocked."
echo "$command" | grep -qE 'terraform[[:space:]]+apply.*-auto-approve' &&
    deny "Unattended 'terraform apply -auto-approve' is blocked; requires manual review."

echo "$command" | grep -qE 'aws[[:space:]]+s3[[:space:]]+rm.*--recursive' &&
    deny "Recursive S3 bucket deletion is blocked."
echo "$command" | grep -qE 'aws[[:space:]]+s3[[:space:]]+rb.*--force' &&
    deny "Force-deleting an S3 bucket is blocked."
echo "$command" | grep -qE 'aws[[:space:]]+rds[[:space:]]+delete-db-instance' &&
    deny "Deleting an RDS database instance is blocked."
echo "$command" | grep -qE 'aws[[:space:]]+ec2[[:space:]]+terminate-instances' &&
    deny "Terminating EC2 instances is blocked."

echo "$command" | grep -qE 'gcloud[[:space:]]+.*delete' &&
    deny "'gcloud ... delete' operations are blocked from automation."

echo "$command" | grep -qE '\baz[[:space:]]+.*delete' &&
    deny "Azure CLI delete operations are blocked from automation."

# ────────────────────────────────────────────────────────────────────────
# PACKAGE PUBLISHING (irreversible on most registries)
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qE 'npm[[:space:]]+(publish|unpublish|deprecate)' &&
    deny "npm publish/unpublish/deprecate is blocked from automation."
echo "$command" | grep -qE '\btwine[[:space:]]+upload\b' &&
    deny "Publishing a PyPI package is blocked from automation."
echo "$command" | grep -qE 'cargo[[:space:]]+publish' &&
    deny "Publishing a crates.io package is blocked from automation."

# ────────────────────────────────────────────────────────────────────────
# SYSTEM: crontab wipe, shutdown/reboot from an automation context
# ────────────────────────────────────────────────────────────────────────

echo "$command" | grep -qE 'crontab[[:space:]]+-r([[:space:]]|$)' &&
    deny "Wiping the crontab ('crontab -r') is blocked."
echo "$command" | grep -qE '\b(shutdown|reboot|poweroff|halt)\b' &&
    deny "System shutdown/reboot commands are blocked from automation."

# Allow everything else
exit 0
