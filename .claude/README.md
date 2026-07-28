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

## ⚠️ NONE OF THESE FOUR RULES HAVE BEEN OBSERVED TO FIRE

**Demonstrated: 0 of 4. Asserted: 4 of 4.** Do not treat this deny list
as protection. It is well-formed JSON that has never been seen to stop
anything.

Probed live on 2026-07-28 from a session started at `~/dev/TRIVELA`. The
trick is to target a path or ref that does not exist, so the probe is
non-destructive whichever way it goes — a denial proves the guard, and a
"no such file" proves the tool was reached:

```bash
# control first — otherwise a denial proves nothing about the PATTERN
touch /tmp/__probe_control__ && rm /tmp/__probe_control__
# -> succeeded. rm is NOT blocked wholesale, so the probes below are
#    testing the pattern, not a blanket rule.

rm ~/dev/TRIVELA/backend/research_archive/__probe_does_not_exist__
# -> rm: ...: No such file or directory        NOT DENIED

rm ~/dev/TRIVELA/backend/docs/V__probe_does_not_exist__
# -> rm: ...: No such file or directory        NOT DENIED

git push --force origin __probe_branch_does_not_exist__
# -> error: src refspec ... does not match any NOT DENIED

git push -f origin __probe_branch_does_not_exist__
# -> error: src refspec ... does not match any NOT DENIED
```

Every probe reached the underlying tool. The harness did not intervene
once.

**Two candidate causes, and this repo cannot distinguish them:**

1. The session loaded its settings at startup, before the deny list
   existed, so it is running without them.
2. The pattern syntax does not match the way the harness compares
   commands.

A **fresh session** discriminates: start one at `~/dev/TRIVELA` and
re-run the four probes. If they are denied there, cause 1 and the rules
are fine going forward. If they still are not, cause 2 and the patterns
need rewriting — at which point say so here rather than leaving four
lines that read as a safety net.

The rules are deliberately left in place rather than deleted, because
they are correct in intent and may well be live in a new session. But
**a deny that does not deny is worse than no deny**, since it invites
exactly the confidence it has not earned. Until a probe is observed to
fail closed, the real guard on force-push and archive deletion is the
agent reading `AGENTS.md`, not this file.

This project has now paid twice to learn the difference between
"the config is well-formed" and "the guard fires" — the V9.5 approval
that minted itself on every deploy, and the Playwright suite whose
green run cleared an assertion that did not exist. Treat every guard as
unproven until something has been observed to fail because of it.

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
