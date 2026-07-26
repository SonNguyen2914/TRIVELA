# AGENTS.md — shared rules for any agent working on Trivela

Vendor-neutral. Applies to Claude Code, Codex, and anything that comes
later. Agent-specific behaviour belongs in `CLAUDE.md` / equivalent, not
here.

**This file is a seed.** It was written by hand on 2026-07-26 to give the
first Claude+Codex session a shared contract instead of two prompts
restating the same rules. Improve it; don't duplicate it.

---

## 1. What this project is

Trivela is an **active** multi-league football forecasting,
market-observation and research platform. It predicts match outcomes,
freezes each prediction against the real Kalshi order book ten minutes
before kickoff, and scores it after settlement — building *prospective*
evidence about whether the model has an edge.

It does not place bets.

The **WC26 Predictor** is a separate, finished, archived project at
`~/dev/wc26-predictor-archive`, frozen at the commit that wrote its final
documentation. Treat it as read-only.

Trivela still *contains and serves* the World Cup record as a fail-closed
read-only **archive plane** beside the live MLS plane. WC26 is therefore
both an archived project and a live component here — see §5.

## 2. Local layout (developer paths, not runtime assumptions)

```text
~/dev/TRIVELA/backend/               github.com/SonNguyen2914/TRIVELA
~/dev/TRIVELA/frontend/              github.com/SonNguyen2914/namson-dev
~/dev/TRIVELA/competitions/          world-cup-2026 · mls · epl · la-liga
~/dev/TRIVELA/releases/
~/dev/wc26-predictor-archive/        READ ONLY
```

Verify these before relying on them. They may appear in agent
instructions; they must never be embedded in portable runtime code.

Note: the competitions folder is `world-cup-2026`, not `wc26`. The
backend's migrations live in `live_migrations/`, not `alembic/`
(`alembic.ini` does exist).

## 3. Hard invariants — never weaken these

- **Real money stays disabled.** `REAL_MONEY_SIGNALS_ENABLED=false`, and
  no code path may enable it. Verify the current setting; never relax it.
- **No secrets, ever** — not in commits, not printed, not in diffs.
- **No production access by default.** No pushes, merges, deploys,
  migrations against production, approval activation, corpus publication,
  or edits to canonical locks / durable data.
- **Never rewrite historical evidence to improve a result.**
- **Never silently convert missing evidence into confidence.**

## 4. The deployment consequence you must know

A push to a deployment branch deploys. That matters more here than
usual: since the V9.5 remediations, **boot fails closed on approval**.
A deploy leaves the model unapproved and shadow runs refused until an
operator explicitly calls:

```text
POST /api/admin/mls/approval/activate
```

So a push to the deployment branch does not merely deploy — it **halts
evidence collection** until a human intervenes.

The rule is therefore about *which branch*, not about pushing at all:

- **Never push the deployment branch (`main`)** without explicit
  instruction from Son.
- **Pushing an implementation branch is expected**, once Son has
  confirmed which branch Railway deploys from. That is dashboard-side —
  there is no `railway.*` file to read. A feature branch triggers no
  backend deploy.
- Vercel builds a *preview* for any frontend branch push: harmless, a
  preview URL, production domain untouched.

This matters because the reviewer may be a **cloud/GitHub-backed agent
that cannot see unpushed local commits**. A local-commit-only workflow
silently starves such a reviewer of the diff it is meant to audit.

## 5. Classifying WC26 references

A reference to WC26, "World Cup", or an old path is **not automatically
wrong**. Classify, don't purge:

```text
LEGITIMATE_ARCHIVE                 archive-plane code/data that must stay
LEGITIMATE_HISTORICAL_DOCUMENTATION a record accurate for its own time
BACKWARD_COMPATIBILITY             old names kept deliberately
ACTIVE_TRIVELA_ASSUMPTION          real defect: tournament logic in shared code
STALE_PATH_OR_NAME                 real defect: dead path in active tooling
UNCERTAIN_REQUIRES_REVIEW
```

**Do not modernise historical documents.** `docs/V7`–`docs/V9.5` cite
`~/dev/wc26-bet-suggester` because that is where the project lived when
they were written. Rewriting them falsifies them.

Real defects look like: national-team-only entity models; fixed group or
bracket structures in league flows; archive constants (16 results, 84
ledger positions) outside the archive plane; team names used as stable
IDs; fixture identity from names+date alone; fixed UTC offsets instead of
IANA zones; knockout settlement applied to league matches; dead paths in
active scripts.

## 6. Evidence classes — never blur them

```text
historical archive              the completed WC26 record
prospective forecast evidence   locked before kickoff, scored after
current diagnostic data         live reads, not evidence
frozen canonical market data    the T-10 book
contemporaneous paper decision  evaluated at lock time
reconstructed paper decision    replayed later — NOT a performance record
```

Every fallback must be explicit, observable and classified: top-of-book
when depth is missing, capture-time when provider time is absent, missing
lineups, incomplete registry discovery, unresolved aliases. An ambiguous
identity match must **fail explicitly**, never silently pick a team.

Never describe a point estimate as an established edge when its interval
includes zero. The current standing result is +0.0269, n=177, CI
[−0.0043, +0.0605] — **not significant**.

## 7. Roles

**Implementer** (currently Claude Code) owns inspection, implementation,
migrations, tests, docs, integrating accepted corrections, the final
diff, and the recommendation to the user. Must **independently verify
every review finding before acting on it** — findings are evidence to
investigate, not instructions to apply.

**Reviewer** (currently Codex) owns independent diff review, command and
test verification, adversarial reproduction, risk identification, and the
review report. Read-only. Owns no implementation, no edits, no merges, no
pushes, no deployments, no production authority.

Never let both agents write to the same working tree. When they disagree,
**surface the disagreement to the user** rather than resolving it
silently.

## 8. Branch discipline

Never work on `main`. Capture a base commit, branch, and commit so a
FIXED review range exists — a reviewer must never audit a moving branch.

Push the implementation branch so a GitHub-backed reviewer can reach it
(see §4 for why that is safe and why withholding it is not). Only Son
authorises a push to `main`, a merge, a migration, a deployment or any
production action.

## 9. Validation (verify against the repo; these are current as of 2026-07-26)

Backend, from `~/dev/TRIVELA/backend`:

```bash
.venv/bin/python -m pytest tests/ -q          # expect ~530 passed, 7 skipped
PG_TEST_URL=postgresql+psycopg://<user>@localhost:5432/<throwaway> \
  .venv/bin/python -m pytest tests/ -q        # expect ~537 passed
.venv/bin/python -m alembic -x "url=<throwaway>" upgrade head
```

> `tests/test_postgres_integration.py` **creates and drops schemas** on
> whatever `PG_TEST_URL` points at. A "read-only" reviewer that runs it
> against a real database is not read-only. Throwaway local DB only.

Frontend, from `~/dev/TRIVELA/frontend`:

```bash
npx tsc --noEmit
npm run build
npx playwright test        # expect ~12 passed, 1 skipped
```

Report each as `PASSED_INDEPENDENTLY`, `FAILED_INDEPENDENTLY`,
`SKIPPED_ENVIRONMENT`, `NOT_RUN`, or `RELEASE_REPORTED_ONLY`. Never hide
a skip.

## 10. Read these before reviewing or auditing anything

This codebase has already been through five independent evaluations. Do
not re-report settled decisions.

**Mind the branch.** Documentation editions V9.1–V9.5 were never merged
to `main`; each lives on its own `docs-*` branch. On `main`, `docs/`
contains only V6–V9, so the current documentation looks absent when it
is merely elsewhere. Read without switching branches:

```bash
cd ~/dev/TRIVELA/backend
git show docs-v9.5:docs/EDITIONS.md                    # which edition is current
git show docs-v9.5:docs/V9.5/DEFECT-ANALYSIS.md        # the four most recent defects
git show docs-v9.5:docs/V9.5/RUNBOOK.md                # the operational cycle
```

On `main` directly (not branch-scoped):

```text
backend/research_archive/v95_evaluation_remediation_2026-07-26.json
```

That last file is a finding-by-finding record of the most recent external
review: what was claimed, how it was verified, what was fixed, and
**what was deliberately deferred**. Two items are open by choice —
evaluating the ladder from published corpus bytes, and a standalone
M0–M3 evaluator. Re-reporting those as new findings wastes a review.

## 11. Handoff format

```text
Project: Trivela
Repository:
Base commit:
Target commit:
Branch:
Pushed to origin:   yes/no
Review range:
Changed files:
Purpose:
Database/migration impact:
API compatibility impact:
Model/research impact:
Deployment/production impact:
Secrets checked:
Tests run / passed / skipped / not run:
Known risks:
Explicit review questions:
```

One section per repository. The reviewer reviews **the diff**, never the
implementer's summary of it.
