You are the primary implementation agent for **Trivela**.

**Read your memory first.** The project was renamed and restructured on
2026-07-26 — paths that look right from older context are dead. Then
read `AGENTS.md` (in the backend repo root), which is the shared contract between you
and the reviewer. It is a hand-written seed; your job includes improving
it, not restating it.

## This session's job

Establish a safe, repository-native **Claude Code + Codex review
workflow**. That is the whole deliverable.

Explicitly **not** this session: the platform-wide architecture map, the
WC26-assumption audit, the multi-league abstraction review, and the
modelling/leakage audit. Those are a full independent audit — they are
the *first task the new workflow should run*, reviewed through the
process you build here. Attempting them now produces a shallow pass at
both.

## Order of work

**1. Verify topology before changing anything.**
Whether `~/dev/TRIVELA/` is a repo or just an umbrella; whether
`backend/` and `frontend/` are independent repos; branches, cleanliness,
remotes, default branches, tags, existing worktrees, existing agent
instruction files, existing CI. Report what you found before editing.

Known starting points, all to be verified: `backend/` is
`github.com/SonNguyen2914/TRIVELA` (renamed; the old URL redirects, which
is why Railway kept working). `frontend/` is still `namson-dev` and also
serves Son's personal site — decide consciously how much Trivela-specific
instruction belongs in it. `frontend/` already has a 1-line `CLAUDE.md`
and a 5-line `AGENTS.md`; `backend/` has neither. Both repos have
`.github/workflows/ci.yml`. There is no `vercel.json` and no `railway.*`
file — deploy config is dashboard-side, so absence is not a gap.

**2. Write the smallest coherent instruction set.**
Improve `AGENTS.md` (in the backend repo root). Add a `CLAUDE.md` where one is
missing or too thin. Add a workflow document only if no current canonical
document already serves that purpose — do not create duplicates.

Decide and document: where the canonical instructions live given two
separate repos, and how they stay visible to an agent that starts inside
either one. Avoid hard-coded commit hashes, approval IDs, corpus
versions, fixture IDs or temporary branch names.

**3. Establish branch and worktree isolation.**
Never work on `main`. Capture the base commit per repo, branch, and
document the review-worktree pattern (`git worktree add --detach`) with
safe cleanup. Verify paths and worktree support before running anything.

**4. Determine Codex/MCP availability.**
Check what is actually installed — `command -v`, `--version`, `--help`
before assuming any subcommand exists. Inspect only safe configuration
metadata; never print tokens. Report exactly one of:
`CODEX_MCP_AVAILABLE_AND_TESTED`, `CODEX_MCP_CONFIGURED_BUT_UNVERIFIED`,
`CODEX_INSTALLED_NO_MCP`, `CODEX_NOT_INSTALLED`.

If MCP exists, confirm it cannot silently edit, push, merge, deploy or
reach production. The workflow must not *depend* on MCP.

**5. Define the separate-terminal fallback** — exact repo, base commit,
target commit, diff range, the reviewer's read-only behaviour, how the
report comes back, and how you independently verify each finding.

**6. Commit. Push only if the reviewer cannot read local commits.**
Without a commit there is no fixed target and the review cannot run
against prose. Commit on your implementation branch.

Then apply the single rule in AGENTS.md §4 — do not push reflexively:

- **Local reviewer** (a `codex` binary on this machine): it reads the
  object store directly. **No push.** The committed range is enough.
- **Cloud/GitHub-backed reviewer**: it cannot see unpushed commits, so
  the branch must be pushed or the review is starved of its diff. Get
  Son's confirmation of the Railway and Vercel deploy branches *first* —
  those are dashboard-side and unverifiable from the repo.

Establish which reviewer is running before deciding, and record it in
the handoff's `Pushed to origin` field. **Never push `main`** — that
deploys and trips the approval lock in AGENTS.md §4.

**7. Hand off, then verify.**
Produce the handoff (format in AGENTS.md §11). If Codex is reachable via
MCP, request a read-only review of the exact range; otherwise prepare the
copy-paste command and prompt for a separate terminal.

Reproduce every confirmed finding yourself. Reject unsupported ones with
evidence. **Where you and the reviewer disagree, surface the
disagreement to Son rather than resolving it silently.**

## Safety

Everything in `AGENTS.md` §3 and §4 binds you. In particular: no merge,
no deploy, no production migration, no approval activation, no corpus
publication, and nothing that weakens the real-money lock. Pushing is
governed by step 6 above and AGENTS.md §4 — conditional on the reviewer,
never to `main`. Do not modify `~/dev/wc26-predictor-archive/`. Do not
rename Railway services, production URLs, Vercel projects, domains or
GitHub repositories.

Before finishing, inspect your own diff for secrets, machine-specific
paths leaking into runtime code, generated files, database artifacts,
large binaries and unrelated changes.

## Final report

Verified topology and remotes · base commit and branch per repo · files
created/changed · responsibility model · branch/worktree workflow · the
handoff format · MCP result · the fallback · validation run and validation
*not* run with reasons · security and production-safety assessment ·
reviewer findings with your independent disposition of each ·
disagreements for Son to settle · remaining decisions · a recommended
commit message.

Do not deploy. Do not merge. Do not push `main`. Push an implementation
branch only under the condition in step 6.
