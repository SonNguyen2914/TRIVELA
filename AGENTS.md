# AGENTS.md — shared rules for any agent working on Trivela

Vendor-neutral. Applies to Claude Code, Codex, and anything that comes
later. Agent-specific behaviour belongs in `CLAUDE.md` / equivalent, not
here.

**This file is a seed.** It was written by hand on 2026-07-26 to give the
first Claude+Codex session a shared contract instead of two prompts
restating the same rules. Improve it; don't duplicate it.

### Where the rules live

Trivela is two independent repositories, so there is no single directory
an agent is guaranteed to start in. The rule is **one canonical file,
plus a local file that points at it**:

```text
TRIVELA/AGENTS.md         canonical, cross-repo: invariants, evidence
                          classes, WC26 classification, roles, branch
                          and review discipline, validation, handoff
TRIVELA/CLAUDE.md         Claude-specific backend notes; imports the above
namson-dev/AGENTS.md      frontend-only rules + a pointer back here
namson-dev/CLAUDE.md      imports namson-dev/AGENTS.md
```

A rule that binds both repos belongs **here and only here**. A rule that
is true of one repo belongs in that repo and must not be copied. If you
find yourself writing the same sentence in two files, the rule was
cross-repo and belongs in this one.

An agent starting in the frontend cannot import this file — it is in a
different repository, and a cloud agent may only have the one checkout.
It is told to say so explicitly rather than assume the shared rules do
not apply. **Silence about a missing contract is itself a finding.**

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

  > **Journal-relay carve-out — SIGNED OFF by Son, 2026-07-29, by explicit directive after plain-language explanation (recorded in the co-producer session).**
  > Operator-authenticated, session-sourced prose relayed to Son over
  > the broadcast channel is NOT a model signal; the real-money lock
  > governs model-generated signals. The boundary is mechanical, not
  > honor-system: `src/live/journal.py broadcast()` refuses dispatch
  > for any non-`session` source and for any call reached from a
  > scheduler or model code path (verified against the call stack, not
  > the caller's claim), a test asserts no scheduler or model module
  > references the broadcast path at all, and every action-channel
  > dispatch carries the standing-edge qualifier (estimate, CI,
  > significance) appended server-side — so no relayed message can
  > present a number without its uncertainty.
  >
  > **Strengthened 2026-07-29 (round-3 review, journal-P0-G) — the
  > mechanism above was one layer short of the boundary.** The stack
  > check binds IN-PROCESS callers only: over HTTP the stack is
  > uvicorn → fastapi → `api.main` and contains no scheduler or model
  > frame, so any client holding the generic `ADMIN_TOKEN` could claim
  > `source="session"` and dispatch to the channel Son's friend reads.
  > Dispatch now ALSO requires a short-lived capability minted by an
  > operator-authenticated challenge/response handshake against
  > `JOURNAL_SESSION_SECRET` — a second factor that must differ from
  > `ADMIN_TOKEN`, that never crosses the wire, and that the operator
  > token alone cannot produce. Unset secret = no capability = no
  > dispatch, which is the fail-closed state. The capability id the
  > server VERIFIED is stored on every broadcast row, beside the
  > `source` the caller merely claimed. This narrows the carve-out;
  > it does not widen it.
  >
  > **Strengthened again 2026-07-29 (round-4 review, journal-P0-J) —
  > the carve-out described a boundary only `broadcast()` implemented.**
  > `src/live_signals.py`, `src/positions.py` and `jobs/scheduler.py`
  > composed "NEW VALUE BET", "RIPE", "BUY/SELL", "EASY WIN", "FINAL
  > DECISION LOCKED" and cash-out reads and handed them straight to
  > `send_discord`/`send_ntfy`. No transport consulted the flag, so
  > setting it false prevented nothing; it was dormant only because
  > `load_schedule()` still points at a finished tournament. **All
  > dispatch now passes one gate**, `src/alerts.py send_alert()`. The
  > transports are private to that module (`_post_discord`,
  > `_post_ntfy`), the gate is the only exported dispatch path, and
  > `tests/test_alert_gate.py` asserts statically that no runtime module
  > outside the gate names a transport, a webhook setting or the ntfy
  > topic, and that every call site declares its class.
  >
  > **The line, drawn by the CALL SITE, never by the message text**
  > (a rule keyed on wording is a rule an unlucky wording defeats):
  >
  > ```text
  > OPERATIONAL     telemetry about the PLATFORM — readiness, storage
  >                 headroom, the channel probe, "the T-10 sweep took a
  >                 lock". No model output, no market view. NOT governed
  >                 by the money lock: silencing this class is how the
  >                 DiskFull incident hid behind {"created": 0}.
  > AMBIENT_DETAIL  the narrator's live briefs. Model numbers, so the
  >                 gate pins them to the DETAIL channel — never the
  >                 act-now channel, never the phone.
  > SESSION_RELAY   this carve-out. Requires a live session capability,
  >                 re-verified BY THE GATE, qualifier appended as before.
  > MODEL_SIGNAL    computed betting content — edges, recommendations,
  >                 BUY/SELL, ripeness, cash-out reads. Dispatches only
  >                 when REAL_MONEY_SIGNALS_ENABLED is true, which it is
  >                 not. Refusals are logged, counted, attributed to the
  >                 call site, and readable at
  >                 `GET /api/admin/alerts/refusals`.
  > ```
  >
  > One call site was SPLIT rather than classified whole: the T-10 lock
  > alert in `src/live/runs.py` used to relay the locked model's H/D/A
  > probabilities to the act-now channel under a "shadow — not advice"
  > label. The label is honour-system; three per-match probabilities on
  > the channel a consenting third party bets from are a market view. It
  > now sends an operational heartbeat naming the fixture and the model,
  > with no probabilities, and the numbers stay on the operator surfaces
  > and in the corpus.
- **No secrets, ever** — not in commits, not printed, not in diffs.
- **No production access by default.** No pushes, merges, deploys,
  migrations against production, approval activation, corpus publication,
  or edits to canonical locks / durable data.
- **Never rewrite historical evidence to improve a result.**
- **Never silently convert missing evidence into confidence.**

## 4. The deployment consequence you must know

A push to a deployment branch deploys. Boot still **fails closed on
approval** — that part of the V9.5 remediations is intact and is not
being weakened here. What changed is what counts as a reason to fail.

> **CORRECTED 2026-08-06.** This section used to say a deploy leaves the
> model unapproved and shadow runs refused until an operator calls
> `POST /api/admin/mls/approval/activate`, and that a push therefore
> **halts evidence collection** until a human intervenes.
>
> **That stopped being true on 2026-08-03 and the file was never
> updated.** It was three days stale, and it was read at every session
> start by every agent that touches this repo — which is how it came to
> be cited, in good faith, as a reason to hold a merge on 2026-08-06.
> A stale safety document is worse than a missing one: it produces
> false caution now and false confidence on the day the thing it
> describes actually changes.

**What is actually true.** An approval decision records the
`code_revision` it was made under. `engine_signature()` hashes the git
revision, so *every* deploy moves the hash — including a migration or a
docs commit that cannot touch the model. Strict hash equality therefore
failed on any deploy and disarmed the plane: four times on
2026-08-02/03, once from a **docs-only PR**. Boot now applies a two-arm
test, and re-arms itself when either arm holds:

```text
arm 1   the stored engine hash reproduces under the CURRENT revision
        -> nothing moved
arm 2   it reproduces under the revision THE DECISION RECORDED
        -> only the repo moved, not the engine
```

A genuine source, constant or runtime change fails **both** arms and the
plane stays dark. So this narrows nothing: it distinguishes *the repo
moved* from *the engine changed*, which strict equality could not.

Two implementations of the one rule, and they live in different places —
worth knowing before grepping for the wrong one:

```text
MLS          the two-arm check inside load_or_create_approval_decision
             (src/live/model_eval.py, "REVISION-ONLY DRIFT is not an
             engine change").  Landed in #59, 2026-08-03.
replay       boot_shadow_flag(), src/live/model_eval.py:1370, called
planes       from ligamx_plane.py and epl_plane.py.  Landed in #70,
             2026-08-04.  It is NOT on the MLS path.
```

**What still requires an operator activation.** The fail-closed cases
are unchanged, and any of these leaves the plane dark:

- **no active approval decision at all** — boot LOADS, and may never
  mint a replacement for itself (V9.5 eval H6);
- **a genuine engine change** — different model/simulator source,
  constant or runtime; fails both arms, which is the whole point;
- **a decision row with no recorded `code_revision`** (written before
  the column existed) — it has no second arm and falls through. Missing
  evidence stays missing evidence;
- **a change in the corpus binding** — publishing a corpus does not
  retroactively bind a decision recorded against `corpus_version=null`;
- **any error at all**, which prints `[boot-flag] … fail closed`.

**Verify, do not assume — in either direction.** This paragraph will
itself go stale one day. `GET /api/ready` answers it in one call, and
the answer is in `live.shadow`:

```text
model_approved_for_shadow · approval_decision_present · shadow_ready
blockers · shadow_blockers
```

Reproduced 2026-08-06 16:57Z, immediately after the #87 deploy and with
no activation call: `ready: true`, `model_approved_for_shadow: true`,
`approval_decision_present: true`, `shadow_ready: true`, both blocker
lists empty.

The rule is therefore about *which branch*, not about pushing at all:

- **Never push the deployment branch (`main`)** without explicit
  instruction from Son.
- **Pushing an implementation branch is expected.** Railway's deploy
  branch was confirmed dashboard-side on 2026-07-27: **source
  `SonNguyen2914/TRIVELA`, branch `main`, auto-deploy ON.** A feature
  branch therefore triggers no backend deploy. The deploy BRANCH is
  dashboard state, so re-confirm it if the service is ever
  reconfigured.
- **Part of the deploy config IS in the repo: `railway.json`.** An
  earlier revision of this file said no `railway.*` file existed, which
  sent an agent hunting dashboard-side on 2026-08-03 for settings that
  were committed all along. It declares:

  ```json
  {"restartPolicyType": "ON_FAILURE",
   "restartPolicyMaxRetries": 10, "numReplicas": 1}
  ```

  `numReplicas: 1` is load-bearing when diagnosing state that appears to
  flap: a single instance rules out "two replicas disagree" before
  anyone goes looking for one. Read the file first; check the dashboard
  only for what the file does not set.
- Vercel builds a *preview* for any frontend branch push: harmless, a
  preview URL, production domain untouched.

Railway's deploy-safety config, verified in the dashboard 2026-07-27:

```text
branch main · auto-deploy ON · Wait for CI ON
healthcheck NONE · teardown OFF · restart On Failure ×10
serverless OFF · volume 5 GB (735 MB used, 15%)
API_HOST unset · API_PORT 8000 · no PORT variable
```

> **Do not set `API_HOST=::`.** It makes uvicorn bind IPv6-only in this
> container, the public domain returns 502, and the app looks healthy the
> whole time — it logs `Uvicorn running on http://[::]:8000` and keeps
> its Postgres sessions. Cost a live outage on 2026-07-27. Leave
> `API_HOST` unset so it defaults to `0.0.0.0`.

> **The healthcheck path must stay empty.** Railway's probe can never
> connect: `service unavailable` on every attempt, in 300s and 900s
> windows, with an entirely empty HTTP network log. Isolated by
> controlled test — the same commit with no healthcheck deploys and
> serves `200`; re-adding *only* the path fails from attempt #1 while
> the previous container keeps serving `200` on the same route. Cause
> unknown. It WAS filed with Railway support 2026-07-28 (Hobby plan, so a
> public Central Station thread rather than a private ticket), and that
> thread is **no longer being pursued** — so treat the gap as PERMANENT,
> not pending. Both halves matter: the filing is why no one should re-open
> the investigation, and "not pending" is why no one should wait for an
> answer before designing around it. **A set path blocks every deploy**,
> so it costs the ability to ship a fix and buys nothing.
>
> Accept the consequence knowingly: with no healthcheck, Railway marks a
> deploy successful as soon as the container starts, **whether or not it
> can serve**. A broken deploy therefore replaces a working one. The only
> thing standing between that and an outage is teardown staying OFF, which
> is why that setting is load-bearing rather than incidental.

Two settings are deliberate and should not be "tidied":

- **Teardown stays OFF.** It would kill the old deployment before the new
  one proves healthy, so a bad deploy would take production down with no
  fallback. Off means a failed deploy simply never takes over. The cost
  is a brief overlap where two APScheduler instances run — `max_instances`
  and `coalesce` are per-process and do *not* span containers, so the
  real guard is the partial unique index on canonical locks (the one CI
  tests for concurrent creation).
- **`/api/health` is unconditional** — no DB, no approval check. Keep it
  that way. If Railway's probe is ever fixed, this is the right target
  precisely *because* it is unconditional: a boot that fails closed for
  one of the reasons listed above still reports healthy, so the deploy
  completes and an operator can reach `/approval/activate`. A health
  endpoint coupled to approval would make such a boot fail its own
  healthcheck — no way to ship the fix. (This bullet is unaffected by
  the 2026-08-06 correction: those cases still exist, they are simply
  no longer triggered by every deploy.)

**Volume alerts are unavailable on this plan** (Teams/Pro only). The
volume filled once already, silently, behind `{"created":0}`. Since the
platform cannot warn, headroom monitoring has to ride the app's existing
Discord/ntfy alert path — see `/api/admin/mls/storage`.

> **A GitHub repo rename silently severs Railway's source link.** After
> the rename to `TRIVELA`, Railway showed `GitHub Repo not found` where
> the branch should be, and could not deploy — while the service stayed
> Online and healthy, serving its last built image. **Running is not the
> same as deployable**, and nothing alerts on the difference.
>
> The fix was not on Railway's side: GitHub → Settings → Applications →
> Railway → **Save** the repository-access form (already set to "All
> repositories"; re-saving forces the installation to re-sync). Re-saving
> an unchanged form is the fix, which is why it is worth writing down.
> It resolved without triggering a deploy — approval and readiness were
> byte-identical before and after.

**Whether a push is needed at all depends on which reviewer runs**, and
that premise has already been wrong once. An earlier revision of this
file asserted there was no local `codex` binary and concluded the
reviewer must be cloud-backed. There is one — it ships inside the
ChatGPT desktop app rather than on `PATH`, which is why looking for
`codex` alone found nothing (see `agent-prompts/codex-review.md`).

So:

- **Local reviewer** (Codex CLI against a local worktree) reads the
  object store directly and needs **no push**. A committed local range
  is sufficient.
- **Cloud/GitHub-backed reviewer** cannot see unpushed commits, and a
  local-commit-only workflow silently starves it of the diff it exists
  to audit. That reviewer needs the branch pushed.

Establish which one is running *before* deciding, and record the answer
in the handoff's `Pushed to origin` field. Do not push reflexively: an
unnecessary push is a real, if small, outward action, and "the reviewer
might be cloud-backed" is an assumption, not a finding.

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
[−0.0050, +0.0596] — **not significant**.

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

**A branch push runs no CI.** Both repos' `.github/workflows/ci.yml`
trigger on `push` to `main` and on `pull_request`. An implementation
branch with no open PR gets no automated check at all, so local runs are
the only validation signal. Never describe such a push as "CI green".

### 8.1 Review worktree isolation

The two agents must never share a working tree — §7. Give the reviewer
its own checkout of the *fixed target commit*, detached so nothing it
does can move a branch:

```bash
cd ~/dev/TRIVELA/backend                  # or frontend — one per repo
git worktree add --detach /tmp/trivela-review-<repo> <TARGET_COMMIT>
```

Detached HEAD is the point: there is no branch to advance, so the review
range cannot drift underneath the review. Verify with
`git worktree list` that the reviewer's path and the implementer's are
distinct before starting.

**Cleanup must run from the repository that owns the worktree.** These
are two independent repositories: the backend's Git metadata knows
nothing about `/tmp/trivela-review-frontend`, so removing it from the
backend fails (safely, but it leaves the worktree registered).

```bash
git -C ~/dev/TRIVELA/backend  worktree remove /tmp/trivela-review-backend
git -C ~/dev/TRIVELA/frontend worktree remove /tmp/trivela-review-frontend
git -C ~/dev/TRIVELA/backend  worktree prune
git -C ~/dev/TRIVELA/frontend worktree prune
```

`scripts/launch-review.sh --clean` does exactly this and is the
preferred route. `git worktree remove` refuses to discard uncommitted
changes unless forced — that refusal is a feature; read what is there
before overriding it.

### 8.2 What the reviewer can and cannot validate

A read-only sandbox cannot run this project's test suites, and pretending
otherwise produces a review that quietly skips validation:

- backend `pytest` needs a writable temp directory (it fails **before
  collection** without one, and `conftest.py` creates a temporary SQLite
  database)
- the frontend needs `npm install`, `.next/` and Playwright output — a
  detached worktree has no `node_modules`

So the reviewer's default posture is: **audit the diff, read the
implementer's validation evidence critically, and mark execution
`SKIPPED_ENVIRONMENT`** — which is an honest status, not a failure.

When the reviewer must genuinely re-run tests independently, give it a
*disposable clone* rather than loosening the sandbox on tracked source:

```bash
git clone ~/dev/TRIVELA/backend /tmp/trivela-verify-backend
cd /tmp/trivela-verify-backend && git checkout --detach <TARGET_COMMIT>
```

Run it with `--sandbox workspace-write` scoped to that clone. The
acceptance check is `git status --short` coming back empty afterwards:
if validation mutated tracked files, the result is not trustworthy.
Delete the clone when done. Never point `PG_TEST_URL` at anything but a
throwaway database — §9.

> **A fresh worktree has no `.venv` and no `node_modules`** — both are
> ignored, so neither is copied. Backend tests still run by invoking the
> main checkout's interpreter against the worktree's sources:
>
> ```bash
> cd /tmp/trivela-review-backend
> ~/dev/TRIVELA/backend/.venv/bin/python -m pytest tests/ -q
> ```
>
> The frontend equivalent needs its own `npm install` in the worktree.

## 9. Validation (verify against the repo; these are current as of 2026-07-29)

Backend, from `~/dev/TRIVELA/backend`:

```bash
.venv/bin/python -m pytest tests/ -q          # expect the count the suite PRINTS — never a number written here; stale twice
PG_TEST_URL=postgresql+psycopg://<user>@localhost:5432/<throwaway> \
  .venv/bin/python -m pytest tests/ -q        # expect the count the suite PRINTS — never a number written here; stale twice
.venv/bin/python -m alembic -x "url=<throwaway>" upgrade head
```

> `tests/test_postgres_integration.py` **creates and drops schemas** on
> whatever `PG_TEST_URL` points at. A "read-only" reviewer that runs it
> against a real database is not read-only. Throwaway local DB only.

Frontend, from `~/dev/TRIVELA/frontend`:

```bash
npx tsc --noEmit
npm run build
npx playwright test        # expect the count the suite PRINTS — never a number written here; stale twice
```

The frontend figures still date from 2026-07-26 — they were not
re-measured when the backend ones were, so treat them as the older
claim they are.

> **`npx playwright test` talks to PRODUCTION by default.**
> `playwright.config.ts` falls back to the production Railway URL when
> `SUGGESTER_BACKEND_URL` is unset, and passes it to the dev server. The
> requests are read-only GETs against the public shadow API, so this
> does not write to production — but it does mean routine validation
> depends on a live service and pins evidence to volatile data, which
> is exactly the rot the frontend's own rules warn about.
>
> Point it somewhere else unless you specifically intend a live smoke
> test:
>
> ```bash
> SUGGESTER_BACKEND_URL=http://localhost:8000 npx playwright test
> ```
>
> `e2e/contract-deterministic.spec.ts` is the hermetic one — it replays
> recorded payloads and does not need a backend at all. The other four
> specs do. **Whether the config's default should change is an open
> decision for Son** (see §12), not something an agent should alter
> unilaterally: it would change the meaning of every existing run.

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
research_archive/v95_evaluation_remediation_2026-07-26.json
research_archive/agent_workflow_review_2026-07-26.json
```

Both are finding-by-finding records: what was claimed, how it was
verified, what was fixed, and **what was deliberately deferred**.

`v95_evaluation_remediation` covers the most recent external evaluation
of the platform. Two items there are open by choice — evaluating the
ladder from published corpus bytes, and a standalone M0–M3 evaluator.

`agent_workflow_review` covers the first run of this implementer/reviewer
workflow auditing its own establishing commits: eight confirmed
findings, each reproduced independently before being acted on, plus one
recorded disagreement about whether read-only GETs against the public
shadow API count as "production access" under §3.

Re-reporting anything settled in either file wastes a review.

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

## 12. Open decisions — Son's, not an agent's

These are known, deliberately unresolved. Re-reporting them as new
findings wastes a review; resolving them unilaterally is worse.

- **Playwright's production default.** `playwright.config.ts` falls back
  to the production backend (§9). Changing it would alter what every
  existing e2e run means. Options: make hermetic the default and put
  live behind an opt-in, or keep it and document it as intentional.
- **Vercel's production branch is unverified.** No `vercel.json` exists;
  the setting is dashboard-side. "A non-default branch push is only a
  preview" is therefore an *assumption*. Confirm before the first
  frontend branch push.
- **Multi-league abstraction is schema-only** (audit A2, 2026-07-27).
  `Competition.has_group_stage` / `has_knockout_stage` exist in the
  schema, migration and seed but have **zero runtime consumers**, while
  `model_mls.py` hardcodes `stage="group"` and `competition_slug=
  "mls-2026"` appears 30 times across `src/live/`. The honest description
  is *generic schema, MLS-specific implementation* — do not describe this
  platform as multi-league-ready.

  Not fixed deliberately. The fix touches `model_mls.py`, which is inside
  the engine signature, so it invalidates the approval and halts shadow
  collection until an operator reactivates. It also changes what the
  simulator does with stage semantics, which is a modelling change owed
  an evaluation, not a refactor. Sequence it: land it between slates,
  reactivate approval, and measure.

- **Frontend `CLAUDE.md` cannot import this file.** It lives in another
  repository, and a cloud agent may hold only one checkout. The frontend
  copy points here in prose instead. If that proves too weak, the
  alternative is duplicating the shared rules — which §"Where the rules
  live" exists to prevent.

## 13. Modelling safeguards

Cross-repo and binding, not merely a reviewer checklist:

- **No temporal leakage.** A feature must use only information available
  at the moment being predicted. The T-10 lock exists to make that
  mechanical — never reconstruct a "decision" from post-kickoff data and
  present it as contemporaneous (§6).
- **No target leakage.** Nothing derived from the outcome may reach a
  feature, directly or through a provider's post-hoc field.
- **Competition-aware, never tournament-shaped.** No group/bracket
  structure in shared league flows (§5).
- **Stable identity.** Fixtures and teams get provider-stable IDs, never
  names or name+date. An ambiguous match fails explicitly.
- **IANA time zones**, never fixed UTC offsets.
- **The four engine-signature modules take modelling changes only.**
  `src/live/model_mls.py`, `src/models/simulator.py`,
  `src/models/xg_model.py`, `src/models/features.py` are hashed by their
  RAW BYTES, so a helper, a docstring, a type hint or a bare comment
  darkens the plane exactly as a changed simulator would (§4). Put it in
  a sibling module and pin the two equal by test. The warning cannot be
  written *into* those files — that would be the edit — so it lives in
  `src/models/ENGINE-SIGNATURE.md` beside them, and in the assertion
  message of `tests/test_team_style.py`, which is where someone who has
  already made the edit will read it.
