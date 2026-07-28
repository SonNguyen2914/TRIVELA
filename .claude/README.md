# Agent permissions

`settings.json` governs what every agent working on Trivela may run —
the builder, the reviewer, a live match session, anything started from
`~/dev/TRIVELA` or from this repo.

It lives here so it is **versioned and reviewable**. A file that decides
an agent's blast radius should not sit untracked in a directory nobody
diffs.

## Current stance

Everything is allowed. Son moved `git push`, `gh pr merge`, `gh release`,
`rm`, `git reset --hard` and `git push --force` from `ask` to `allow` on
2026-07-27, deliberately, after the friction of one-click-per-action
proved higher than the value of the gate.

That is a real trade and worth stating plainly rather than burying:

- A push to `main` deploys, which invalidates the approval and **stops
  shadow collection** until someone calls `/approval/activate`.
- The guardrail therefore now lives in the agent's judgement and in
  `AGENTS.md`, not in the harness.
- The one gap that created — a session that pushes and then dies before
  reactivating, silently halting evidence collection — is covered by
  `mls_readiness_watch` in `jobs/scheduler.py`, which alerts after ten
  minutes unapproved.

## What is denied, and why only these

`deny` is evaluated before `allow`, so a short deny list closes specific
holes without reintroducing friction anywhere else. The allow list is
untouched.

```text
Bash(git push --force:*)      Bash(git push -f:*)
Bash(rm:*research_archive*)   Bash(rm:*docs/V*)
```

These four protect things that **cannot be reconstructed**:

- **Force-push** can rewrite or destroy the evidence commits — the
  prospective record this project exists to build. A normal push cannot.
- **`research_archive/`** holds the evaluation and review bundles
  (`v95_evaluation_remediation`, `agent_workflow_review`, the walk-forward
  sweeps). They are inputs to decisions already made; losing them
  unfalsifiables the record.
- **`docs/V*`** are historical editions that must not be modernised
  (AGENTS.md §5) and are the only account of what was believed when.
- The **committed archive** the SQLite plane self-heals from at boot
  lives under those paths. Delete it and a fresh container cannot rebuild
  16/16 results and 84/84 ledger positions.

The test is not "is this dangerous" — plenty of allowed commands are.
It is **"can the damage be undone."** A bad deploy is recoverable in
minutes; a force-pushed history or a deleted archive is not. Note this
covers only what `mls_readiness_watch` does not: `git reset --hard` and
`psql` remain allowed and remain unguarded, because both have legitimate
uses here and neither has an obvious narrow pattern to deny.

## Status of each guard — measured, not assumed

| Guard | Mechanism | Logic proven | Fires in a live session |
|---|---|---|---|
| `git push --force *` | permission deny | n/a | **NOT YET OBSERVED** |
| `git push -f *` | permission deny | n/a | **NOT YET OBSERVED** |
| rm touching `research_archive/` | PreToolUse hook | **YES** (11/11) | **NOT YET OBSERVED** |
| rm touching `docs/V*` | PreToolUse hook | **YES** (11/11) | **NOT YET OBSERVED** |

**Nothing here has been seen to stop anything.** Treat all four as
unproven until a probe is observed to fail closed.

### The syntax lesson that cost two rounds

`permissions.deny` matches **exact** (`Bash(npm run test)`) or **prefix
wildcard** (`Bash(git *)`) — a **space**, not a colon. The schema's own
deny example is `Bash(rm -rf *)`, and Claude Code corroborates it by
auto-writing `Bash(tmux ls *)` into `settings.local.json` itself.

The first attempt shipped `Bash(rm:*research_archive*)` and
`Bash(git push --force:*)`. The push rules were merely malformed and are
now corrected. **The two rm rules were worse than malformed — they were
inexpressible.** "Deny rm when a path *in the middle* of the command
contains `research_archive`" is a containment test, and matching is
prefix-based. Fixing the separator would never have made them fire.

So they were **removed from `permissions.deny` entirely** and replaced
with a `PreToolUse` hook on the `Bash` matcher — the only mechanism that
can inspect a command string. Generally: **path-containment guards need
a hook, not a permission rule.**

### The hook: logic proven, firing not

`scripts/deny-archive-rm.sh` was pipe-tested against 11 synthesised
payloads before being wired, and got all 11 right:

```text
DENY   rm .../research_archive/__probe...__      rm -rf research_archive/
       rm backend/docs/V9/RUNBOOK.md             rm docs/V9.5/DEFECT-ANALYSIS.md
       cd /tmp && rm -rf .../research_archive
ALLOW  rm /tmp/__probe_control__                 ls research_archive/
       git rm --cached somefile                  grep -r research_archive src/
       rm -rf node_modules                       python3 -c "print(1)"
```

The ALLOW half matters as much as the DENY half: `git rm --cached` and
`grep -r research_archive` are exactly what a naive substring match would
have blocked, making the guard so annoying it would be removed.

Schema validated with
`jq -e '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[] | .command'`
— exit 0.

**It still did not fire when probed live.** Per the hooks documentation,
the settings watcher only watches directories that already had a settings
file when the session started, and `backend/.claude/` did not exist at
this session's start. The same explains the permission rules.

### How to finish the proof — someone must do this

An agent cannot: `/hooks` is a user menu and opening it ends the turn,
and a session cannot reload its own settings.

**Open `/hooks` once, or start a fresh session, then run:**

```bash
rm ~/dev/TRIVELA/backend/research_archive/__probe_does_not_exist__
rm ~/dev/TRIVELA/backend/docs/V__probe_does_not_exist__
git push --force origin __probe_branch_does_not_exist__
touch /tmp/__probe_control__ && rm /tmp/__probe_control__   # control: must SUCCEED
```

Nothing is deleted either way — every target is a path or ref that does
not exist, so a denial proves the guard and a "no such file" proves the
tool was reached. **Record the result in this table.** If a guard still
does not fire, say so and leave the protection absent rather than
shipping a second decorative one.

### Why this section exists at all

This project has now paid three times for the gap between "the config is
well-formed" and "the guard fires": the V9.5 approval that re-minted
itself on every deploy, the Playwright suite whose green run cleared an
assertion that did not exist, and this. **A guard is unproven until
something has been observed to fail because of it.**

## Making it active

The umbrella directory is not a git repository, so `~/dev/TRIVELA` needs
its own copy for sessions started there:

```bash
cp backend/.claude/settings.json ~/dev/TRIVELA/.claude/settings.json
```

A symlink is tempting and was tried first. It is wrong for a reason
worth recording: the target lives on a branch, so the link dangles
whenever the checkout is on a branch without this file — and a dangling
settings symlink means **no permissions load at all**, silently. A
broken link here does not fail loudly; it just removes the config.

So: copy, and re-copy after changing it here. This file is the canonical
version — the one that gets reviewed.
