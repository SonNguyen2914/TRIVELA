#!/bin/bash
# Launch the independent Codex review against FIXED commit ranges.
#
# Usage:
#   scripts/launch-review.sh <BE_BASE> <BE_TARGET> [<FE_BASE> <FE_TARGET>]
#   scripts/launch-review.sh --clean
#
# Why this exists: agent-prompts/codex-review.md ships with literal
# <REPO>/<BASE_COMMIT>/<CLAUDE_COMMIT> placeholders. Piping it raw starts
# a reviewer that has no range and is explicitly forbidden from guessing
# one — so the range ends up supplied by hand, out of band, and the
# review stops being reproducible. This resolves them mechanically.
#
# Read-only is enforced by Codex's own sandbox, not by this script.
set -euo pipefail

BACKEND="$HOME/dev/TRIVELA/backend"
FRONTEND="$HOME/dev/TRIVELA/frontend"
WT_BE=/tmp/trivela-review-backend
WT_FE=/tmp/trivela-review-frontend
# Overridable so the launcher itself can be exercised without spending a
# review, and so a PATH install keeps working if Codex ever ships one.
CODEX="${CODEX:-/Applications/ChatGPT.app/Contents/Resources/codex}"

# Cleanup is repository-aware: each repo owns its own worktree and
# cannot remove the other's.
clean() {
  git -C "$BACKEND"  worktree remove "$WT_BE" 2>/dev/null || true
  git -C "$FRONTEND" worktree remove "$WT_FE" 2>/dev/null || true
  git -C "$BACKEND"  worktree prune
  git -C "$FRONTEND" worktree prune
  echo "cleaned. remaining worktrees:"
  git -C "$BACKEND"  worktree list
  git -C "$FRONTEND" worktree list
}

[ "${1:-}" = "--clean" ] && { clean; exit 0; }

if [ $# -ne 2 ] && [ $# -ne 4 ]; then
  sed -n '3,6p' "$0" >&2
  exit 2
fi

BE_BASE="$1"; BE_TARGET="$2"
FE_BASE="${3:-}"; FE_TARGET="${4:-}"

[ -x "$CODEX" ] || {
  echo "codex not found at $CODEX" >&2
  echo "it ships inside the ChatGPT desktop app and is not on PATH" >&2
  exit 1
}

# Refuse to start on a range that does not exist. A reviewer that
# discovers a bad commit halfway through has already wasted the review.
verify() {
  git -C "$1" rev-parse --verify --quiet "$2^{commit}" >/dev/null || {
    echo "commit $2 not found in $1" >&2; exit 1; }
}
verify "$BACKEND" "$BE_BASE"; verify "$BACKEND" "$BE_TARGET"
if [ -n "$FE_BASE" ]; then
  verify "$FRONTEND" "$FE_BASE"; verify "$FRONTEND" "$FE_TARGET"
fi

clean >/dev/null 2>&1 || true
git -C "$BACKEND" worktree add --detach "$WT_BE" "$BE_TARGET" >/dev/null
[ -n "$FE_BASE" ] && \
  git -C "$FRONTEND" worktree add --detach "$WT_FE" "$FE_TARGET" >/dev/null

PROMPT=$(mktemp)
trap 'rm -f "$PROMPT"' EXIT
{
  echo "## Review inputs — these REPLACE the placeholders below"
  echo
  echo '```text'
  echo "Repository:   github.com/SonNguyen2914/TRIVELA (backend)"
  echo "Base commit:  $BE_BASE"
  echo "Claude commit: $BE_TARGET"
  echo "Review range: $BE_BASE..$BE_TARGET"
  echo "Your worktree: $WT_BE (detached, already at the target)"
  echo '```'
  echo
  if [ -n "$FE_BASE" ]; then
    echo '```text'
    echo "Repository:   github.com/SonNguyen2914/namson-dev (frontend)"
    echo "Base commit:  $FE_BASE"
    echo "Claude commit: $FE_TARGET"
    echo "Review range: $FE_BASE..$FE_TARGET"
    echo "Its worktree: $WT_FE — a SEPARATE repository; you must cd there,"
    echo "  git commands from the backend worktree cannot see its commits."
    echo '```'
  else
    echo "The frontend has NO review range this session — record that explicitly."
  fi
  echo
  echo "Verify both ancestries yourself; do not take the above on trust."
  echo
  echo "---"
  echo
  cat "$BACKEND/agent-prompts/codex-review.md"
} > "$PROMPT"

echo "backend  $BE_BASE..$BE_TARGET -> $WT_BE"
[ -n "$FE_BASE" ] && echo "frontend $FE_BASE..$FE_TARGET -> $WT_FE"
echo "starting read-only review..."
echo

"$CODEX" exec --sandbox read-only -C "$WT_BE" "$(cat "$PROMPT")"

echo
echo "review finished. clean up with: scripts/launch-review.sh --clean"
