#!/bin/bash
# PreToolUse(Bash) hook — deny `rm` that touches unreconstructable evidence.
#
# WHY A HOOK AND NOT A PERMISSION RULE:
# permissions.deny matches EXACT or PREFIX only — Bash(git push --force *).
# "deny rm when a path ANYWHERE in the command contains research_archive"
# is a containment test, not a prefix, so it cannot be written as a rule at
# all. A rule like Bash(rm:*research_archive*) is not merely malformed, it
# is inexpressible — and worse than absent, because it reads as protection.
# A hook is the only mechanism that can inspect the command string.
#
# Protects:
#   research_archive/  evaluation + review bundles, inputs to decisions
#                      already made
#   docs/V*            historical editions that must not be modernised
#                      (AGENTS.md section 5), and the committed archive the
#                      SQLite plane self-heals from at boot
#
# Contract: read hook JSON on stdin, print a deny object to stdout ONLY when
# blocking, exit 0 either way. Silence means "no opinion" — the normal
# permission flow continues.
set -uo pipefail

cmd=$(jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -z "$cmd" ] && exit 0

# `rm` as a command word: start of string, or after ; & | or `(`.
# Avoids matching "confirm ..." or a path that merely contains "rm".
is_rm() {
  printf '%s' "$1" | grep -qE '(^|[;&|(]|&&|\|\|)[[:space:]]*rm([[:space:]]|$)'
}

# The protected paths, matched anywhere in the command.
touches_protected() {
  printf '%s' "$1" | grep -qE 'research_archive|docs/V'
}

if is_rm "$cmd" && touches_protected "$cmd"; then
  cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Denied by deny-archive-rm hook: this rm touches research_archive/ or docs/V*, which hold evidence bundles, historical doc editions, and the committed archive the SQLite plane rebuilds from at boot. None of it can be reconstructed. If a deletion here is genuinely intended, Son must do it by hand outside an agent session."
  }
}
JSON
fi
exit 0
