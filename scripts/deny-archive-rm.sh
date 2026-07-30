#!/bin/bash
# PreToolUse(Bash) hook — deny commands that would destroy unreconstructable
# evidence.
#
# WHY A HOOK AND NOT A PERMISSION RULE:
# permissions.deny matches EXACT or PREFIX only — Bash(git push --force *).
# "deny when a path ANYWHERE in the command contains research_archive" is a
# containment test, not a prefix, so it cannot be written as a rule at all.
# Bash(rm:*research_archive*) is not merely malformed, it is inexpressible —
# and worse than absent, because it reads as protection. A hook is the only
# mechanism that can inspect the command string.
#
# Protects:
#   research_archive/  evaluation + review bundles, inputs to decisions
#                      already made
#   docs/V*            historical editions that must not be modernised
#                      (AGENTS.md section 5), and the committed archive the
#                      SQLite plane self-heals from at boot
#
# THREAT MODEL — read this before "hardening" it:
# This guards MISTAKES, not an adversary. A session that means to delete the
# archive can trivially get past it: `bash -c 'rm ...'`, `xargs rm`,
# `find ... -exec rm {} +`, a python one-liner, a renamed variable holding
# the path. Those are deliberately NOT covered. Chasing them would mean
# either a parser that is wrong in new ways, or a denylist so broad it
# blocks ordinary work and gets removed within a week — which is strictly
# worse than a narrow guard that is honest about its scope.
# The real protection against a determined agent is that the archive is
# committed and pushed; this hook only stops the fat-finger and the
# plausible-looking cleanup command.
#
# Contract: read hook JSON on stdin, print a deny object to stdout ONLY when
# blocking, exit 0 either way. Silence means "no opinion" — the normal
# permission flow continues.
set -uo pipefail

cmd=$(jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -z "$cmd" ] && exit 0

PROTECTED='research_archive|docs/V'

touches_protected() {
  printf '%s' "$1" | grep -qE "$PROTECTED"
}

# `rm` or `mv` as a COMMAND WORD: start of string, or after ; & | ( && ||
# Avoids matching "confirm", "remove", or a path that merely contains "rm".
is_rm_or_mv() {
  printf '%s' "$1" | grep -qE '(^|[;&|(]|&&|\|\|)[[:space:]]*(rm|mv)([[:space:]]|$)'
}

# `git clean -f...` — repo-wide and irreversible for anything untracked.
# Tracked archive files survive it, but research bundles that have not been
# committed yet do not, and that is exactly the window that matters.
is_git_clean_force() {
  printf '%s' "$1" | grep -qE 'git[[:space:]]+clean\b[^;&|]*-[a-zA-Z]*f'
}

# A TRUNCATING redirect into a protected path: `> file`.
# `>>` (append) is deliberately allowed — it cannot destroy existing bytes.
# The sed collapses >> first so it cannot be mistaken for >.
has_truncating_redirect() {
  printf '%s' "$1" \
    | sed 's/>>/@@APPEND@@/g' \
    | grep -qE ">[[:space:]]*[^[:space:];&|]*($PROTECTED)"
}

reason=""
if is_rm_or_mv "$cmd" && touches_protected "$cmd"; then
  reason="an rm/mv touching a protected path"
elif is_git_clean_force "$cmd"; then
  reason="a forced git clean, which destroys uncommitted research bundles"
elif has_truncating_redirect "$cmd"; then
  reason="a truncating redirect (>) into a protected path"
fi

if [ -n "$reason" ]; then
  # printf, not a heredoc: the reason is interpolated.
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
  printf '%s' '"permissionDecision":"deny","permissionDecisionReason":'
  printf '"Denied by deny-archive-rm hook: %s. research_archive/ and docs/V* hold evidence bundles, historical doc editions, and the committed archive the SQLite plane rebuilds from at boot — none of it reconstructable. Use cp if you meant to ADD to the archive, or >> to append. If a deletion is genuinely intended, Son does it by hand outside an agent session."}}' "$reason"
  echo
fi
exit 0
