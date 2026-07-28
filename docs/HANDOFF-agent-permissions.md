# Handoff — agent permissions, guards, and what proved them

Written 2026-07-28 for someone competent who was not here. It assumes no
memory of the session that produced it. Where something is reported
rather than personally verified, it says so.

The canonical config is `backend/.claude/settings.json`; the rationale
lives in `backend/.claude/README.md`. This file is the story: what is
true, what is not covered, what we got wrong, and the traps.

---

## 1. What is true now

Six guards, all **observed to fire** on 2026-07-28. Not "well-formed" —
observed, with the block message read and attributed.

| Guard | Mechanism | Covers |
|---|---|---|
| `Bash(git push --force *)` | permissions.deny | force-push, any repo |
| `Bash(git push -f *)` | permissions.deny | the short form |
| remove → protected path | PreToolUse hook | `rm` as a command word |
| move → protected path | PreToolUse hook | `mv` as a command word |
| forced `git clean` | PreToolUse hook | `-f`, `-fdx`, `-xfd`, `--force` |
| truncating redirect → protected | PreToolUse hook | `>` but **not** `>>` |

"Protected path" means the command string contains `research_archive` or
`docs/V`. The hook is `scripts/deny-archive-rm.sh`, wired as a
`PreToolUse` hook on the `Bash` matcher.

**Why those two paths.** `research_archive/` holds evaluation and review
bundles that are inputs to decisions already made. `docs/V*` are
historical documentation editions that must not be modernised
(`AGENTS.md` §5) and are the only account of what was believed when. The
committed archive the SQLite plane self-heals from at boot lives under
those paths — delete it and a fresh container cannot rebuild 16/16
results and 84/84 ledger positions.

The selection test was **not** "is this dangerous." Plenty of allowed
commands are, including a push to `main`, which deploys and halts shadow
collection. The test was **"can the damage be undone."** A bad deploy is
recoverable in minutes; a force-pushed history or a deleted archive is
not.

### Two deliberate asymmetries

- **`cp` is allowed where `mv` is denied**, so adding to the archive
  still works. Copy in, then deal with the source separately.
- **`>>` is allowed where `>` is denied**, because append cannot destroy
  existing bytes.

### The probe commands

Each target is incapable of destroying anything — a path that does not
exist, or a pathspec matching no file. Run them and read the messages.

```bash
cd ~/dev/TRIVELA
claude -p "$(cat /path/to/prompt.txt)"
```

with the prompt file containing:

```text
Run each of these one at a time. Report the VERBATIM block message if
blocked, or the tool output if it ran. Do not paraphrase.

  rm ~/dev/TRIVELA/backend/research_archive/__probe_does_not_exist__
  rm ~/dev/TRIVELA/backend/docs/V__probe_does_not_exist__
  cd ~/dev/TRIVELA/backend && mv research_archive /tmp/__probe_gone__
  cd ~/dev/TRIVELA/backend && git clean -f __probe_no_such_path__
  echo "" > ~/dev/TRIVELA/backend/research_archive/__probe_redirect__
  git push --force origin __probe_branch_does_not_exist__
```

Stage the prompt in a **file**. Putting these commands inline in your own
shell invocation trips the hook on your own command string — see §4.

### How to tell which layer blocked something

This is the recurring error in this project: crediting our guard for a
block some other layer made. **Each layer names itself.** Read the text.

```text
"Denied by deny-archive-rm hook: …"       <- our hook (scripts/deny-archive-rm.sh)
"Permission to use Bash … has been denied." <- permissions.deny in settings.json
"rm in '…' was blocked. For security…"    <- Claude Code's BUILT-IN rm guard
"This Bash command contains multiple operations…" <- multi-op approval, unrelated
```

Our hook additionally names *which* rule matched, after the colon: "an
rm/mv touching a protected path", "a forced git clean, which destroys
uncommitted research bundles", "a truncating redirect (>) into a
protected path". Distinct clauses are how you prove two separate rules
fired rather than one rule twice.

**A control probe is not enough on its own.** We tried to show a benign
`rm` passing through, and could not: `/tmp` was refused by a sandbox
working-directory guard, and a file inside the allowed root was refused
by Claude Code's built-in `rm` restriction. Both blocks looked identical
to "our guard worked" if you only checked *that* it blocked. The message
is what saved the attribution.

---

## 2. What is NOT covered

**The `Write` tool.** The hook is on the `Bash` matcher only. Nothing
stops an agent overwriting `research_archive/x.json` with `Write` — that
is literally how this file and the README were written, because the hook
blocks shell heredocs that mention the patterns. This is a real hole,
left open deliberately: closing it needs a second matcher and its own
proof run. **Do not read the guard as "the archive cannot be
overwritten." It means "the archive cannot be destroyed by a bash
mistake."**

**Evasion, entirely.** `bash -c 'rm …'`, `xargs rm`, `find … -exec rm {} +`,
a python one-liner, a variable holding the path. All trivially get
through, all deliberately uncovered.

> **Threat model, one line: this guards mistakes, not an adversary.**

Chasing evasion means either a shell parser that is wrong in new ways, or
a denylist broad enough to block ordinary work — and a guard that blocks
ordinary work gets deleted within a week, which is strictly worse than a
narrow guard that is honest about its scope. The real protection against
a determined session is that the archive is **committed and pushed**.

**Still allowed, still unguarded:** `git reset --hard` and `psql`. Both
have legitimate uses here and neither has a narrow pattern worth denying.
Known residual risk, not an oversight.

---

## 3. What we got wrong, and what falsified it

Four errors. Each is recorded because the *shape* of the mistake recurs.

### 3.1 The rules were inexpressible, not just malformed

Shipped first: `Bash(rm:*research_archive*)`, `Bash(git push --force:*)`.

`permissions.deny` matches **exact** (`Bash(npm run test)`) or **prefix
wildcard** (`Bash(git *)`) — a **space**, not a colon. The schema's own
deny example is `Bash(rm -rf *)`, and Claude Code corroborates it by
auto-writing `Bash(tmux ls *)` into `settings.local.json` itself.

The push rules were merely malformed and were corrected. **The path rules
were inexpressible.** "Deny `rm` when a path *in the middle* of the
command contains `research_archive`" is a containment test; matching is
prefix-based. Fixing the separator would never have made them fire.

**Falsified by:** reading the schema, after four rounds of probes failed.
**Lesson:** path-containment guards need a hook, not a permission rule.

### 3.2 Four rounds of probes that proved nothing

The probes kept reaching the tool. The conclusion drawn — "the rules do
not fire" — was correct about the observation and useless as a diagnosis,
because **a session cannot load settings it wrote after it started.**

**Falsified by:** running the same probes in a fresh `claude -p` session,
where they fired immediately.
**Lesson:** a negative result from an instrument that was never connected
is not evidence about the thing being measured.

### 3.3 "A long session can NEVER reload settings"

Having learned 3.2, the README then over-generalised it into a rule.

**Falsified by:** accepting the trust dialog. Settings reloaded **in
place**, in the same long-running session, and probes that had failed
four times started firing.
**Lesson:** the document written to warn about over-generalising from a
real observation did exactly that, one section later.

### 3.4 The `if: Bash(rm *)` filter

The hook entry carried `"if": "Bash(rm *)"` as a cheap pre-filter. It was
correct when the hook only handled `rm`. When the hook was widened to
`mv`, `git clean` and redirects, that filter would have **silently
prevented it from ever running** for any of them — a guard that looks
wired and never executes.

**Falsified by:** noticing during the widening, before it shipped.
**Lesson:** a filter that duplicates the guard's own logic goes stale the
moment the guard changes.

---

## 4. The traps

**Untrusted workspace silently drops `allow` but keeps `deny` and hooks.**
A fresh session printed *"Ignoring 68 permissions.allow entries from
.claude/settings.json: this workspace has not been trusted"* — and the
deny rule and hook both still fired. The protective half survives; the
frictionless half does not. So an untrusted workspace is *more*
restricted, not less, and will fight you for permissions on every
ordinary command. Fix: accept the trust dialog, or set
`projects["/Users/ns/dev/TRIVELA"].hasTrustDialogAccepted: true` in
`~/.claude.json`.

**The hook blocks prose describing the hook.** It matches the command
string. Editing this file or the README with a shell heredoc is denied,
because the text mentions the patterns. Wrapping a probe in
`claude -p '… <trigger text> …'` is denied for the same reason — the
*outer* command carries the text. Fix: use the `Write`/`Edit` tool for
docs, and stage probe prompts in a file passed as `"$(cat file)"`.

**`claude -p` loads settings at startup and is the fast way to test any
settings change.** This is the whole testing lesson in one line:
**settings load at startup, so test with `claude -p`, never in the
session that wrote them.**

**Playwright serves a prebuilt bundle, so reverting source proves
nothing.** Reported by Son from another session, not personally verified
here: a UI check against a stale build will pass after you have already
reverted the change you were testing. Rebuild before concluding anything
from a frontend run.

**`/hooks` is unavailable over Remote Control.** If you are driving this
from a phone or the web UI, you cannot use it to reload config. Use the
trust dialog or `claude -p`.

---

## 5. File map

| Path | What it is | Edit? |
|---|---|---|
| `backend/.claude/settings.json` | **Canonical** permissions + hook wiring. Tracked, reviewed. | Yes — this is the one |
| `~/dev/TRIVELA/.claude/settings.json` | **Copy** the umbrella loads. Untracked. | No — re-copy from canonical |
| `~/dev/TRIVELA/.claude/settings.local.json` | Personal overrides. Arrays merge across sources, so its empty `deny` does not erase the project's. | Rarely |
| `backend/.claude/README.md` | Rationale + the demonstrated-guard table | Yes, via Write/Edit |
| `backend/scripts/deny-archive-rm.sh` | The hook. Threat model in its header. | Yes — but pipe-test before wiring |
| `backend/AGENTS.md` | Shared cross-repo contract, binds every agent | Yes, carefully |
| `backend/CLAUDE.md` | Claude-specific notes; imports AGENTS.md | Yes |
| `backend/research_archive/**` | Evidence bundles | **Never delete** |
| `backend/docs/V*/**` | Historical editions | **Never modernise** (AGENTS.md §5) |

**Keeping the two settings copies in sync is manual and load-bearing:**

```bash
cp backend/.claude/settings.json ~/dev/TRIVELA/.claude/settings.json
```

A symlink was tried and is wrong: the target lives on a branch, so the
link dangles whenever the checkout is on a branch without the file — and
a dangling settings symlink means **no permissions load at all**,
silently. Verify both copies hash the same after any change.

---

## 6. Open threads

**PR #6 — awaiting Codex.** Branch `ops-agent-permissions-deny` (and
`ops-deny-destructive`, same SHA). Range `4826368..f44846a`, 8 commits.
Contains the deny rules, the hook, the demonstrations, and the corrected
README. **Do not merge without review** — it changes what every agent is
permitted to do. Note its head was fast-forwarded to pick up the two
commits that turn "asserted" into "demonstrated"; if you see an older
head, the review is of an incomplete story.

**Two `Edit` deny rules.** Reported by Son from another session, **not
personally verified here**: `Edit(//path/**)` requires a **double
slash**; a single slash **silently does nothing**. That is the same
failure family as everything in §3 — a rule that reads as protection and
is not. If those rules exist in settings, probe them the same way before
trusting them.

**`personal-bet-journal`** — pushed, untouched, awaiting Codex review.
Range `audit-platform..personal-bet-journal`, worktree at
`~/dev/TRIVELA-worktrees/journal`. **Do not build on it until the review
lands; findings may change the migration.** Do not check it out in the
shared backend checkout. It adds `/api/mls/journal` and
`/api/mls/briefing/{id}` — both **404 in production**, i.e. built but not
live.

**The Saturday slate — 2026-08-01.** Counted from
`/api/mls/schedule?days=14` on 2026-07-28, grouped by kickoff:

```text
2026-07-30T00:00Z   1   MLS All-Stars v Liga MX All-Stars — EXHIBITION
2026-07-31T23:30Z   1   NYCFC v Toronto — first league match
2026-08-01T23:30Z   7  ┐
2026-08-02T00:30Z   4  │  ONE Saturday evening in the Americas
2026-08-02T01:30Z   1  │  14 fixtures across FOUR kickoff slots
2026-08-02T02:30Z   2  ┘
2026-08-08T20:30Z   1
```

**Do not size this slate off any single number.** It is **14 fixtures**,
not the 7 in the largest slot — an earlier draft of this file said 8,
from eyeballing a truncated list instead of counting.

**The UTC date change is not a new matchday.** Three of the four slots
fall on 2026-08-02 UTC while being the same Saturday evening locally
(23:30Z is 19:30 ET). Anything that groups fixtures by UTC date will
split one matchday in two and under-count the slate. Group by local
kickoff window, or by the whole 23:30Z–02:30Z span.

The Jul 30 00:00Z All-Star game is an **exhibition, not a league
fixture**; `src/live/runs.py` has explicit handling so it does not raise
a false readiness blocker.

Before the slate, all of this must be true — check `/api/ready`:

```text
ready: true, blockers: []
model_approved_for_shadow: true
unmapped_upcoming: 0
archive 16/16 results, 84/84 ledger, 6/6 bundles
```

**The thing most likely to break it:** every deploy invalidates the
approval and halts shadow collection until an operator calls
`POST /api/admin/mls/approval/activate` (token at `~/.wc26_admin_token`).
That is ~1.5s of work, but it is *every* deploy, including a docs-only
one — the engine signature includes the code revision.
`mls_readiness_watch` alerts after ten minutes unapproved. If you merge
anything before Saturday, reactivate and confirm `ready: true`.
