# Release — `mls-shadow-v1.4`

**The V9.3 slate-producing baseline.** Supersedes `mls-shadow-v1.3`
(which froze the xG deploy *before* the four audit rounds; 33 commits
separate them).

---

## Identity

```text
release            mls-shadow-v1.4
backend            8fd791f           main  (code; docs on docs-v9.3)
frontend           950b04c           namson-dev main
migration head     c8d9e0f1a2b3
date               2026-07-25
```

## Contract versions

```text
input artifact     model-input-v5        freezes the calibration term
lock policy        mls-lock-v2           capture-clock freshness
paper execution    paper-exec-v3         exact Decimal, centicent fees
evaluation         model-eval-v1
approval policy    shadow-approval-v1
audit              mls-lock-audit-v1
risk               risk-v1
```

## Model parameters

```text
MLS_XG_RATING_ALPHA      1.0     provider xG ratings
MLS_XG_SHRINK_GAMES      6.0     xG prior (goals keep k=24 as fallback)
MLS_CALIBRATION_ALPHA    0.25    3-way shrink toward uniform
MLS_GOAL_DISPERSION_CV   0.0     MLS-only; WC26 keeps 0.30
HALF_LIFE_DAYS           90
MIN_GAMES                5
```

## Approved-for-shadow decision (production)

```text
decision           182
edge vs baseline   +0.0332
CI 95%             [-0.0018, +0.0667]
n_scored           162
mode               shadow
significant        NO
```

**Shadow approval means "safe to collect prospective evidence". It does
not mean an edge is established.**

## Verification at cut

```text
tests              477 backend + 6 e2e + 3 page-health   (5 PG skipped)
ready              true, no blockers
migrations_current true
paper_execution    ready true, new entries allowed
data coverage      team 238/238 · player 238/238 · bridge 99.4% mins-wt
storage            377 MB of 5120 MB (7.4%)
REAL_MONEY_SIGNALS false  — no order-placement path exists in the codebase
```

## What changed since v1.3

| change | effect |
|---|---|
| xG ratings get their own prior (k=6) | the league's best team is no longer flattened to average |
| win% blend → explicit calibration | honest name; M3 1.0469 → 1.0443 |
| MLS-specific goal dispersion (0.0) | BTTS −10 pp bias corrected; 50 of 53 markets |
| lock policy v2 (capture clock) | **paper execution possible at all** |
| All-Star fixture excluded from readiness | prevents a false blocker on Jul 28 |
| observation payloads capped at 8 KB | stops the growth that filled the volume |
| sweep reports `no_prediction` / `failures` | failures are visible, not swallowed |
| `/api/admin/mls/storage` | volume visibility without a deploy |
| H2H parser reads `seasonseries` | restores the vanished section |
| scouting result derived from its own scores | defeats no longer render as wins |
| team-news section | announced XI, xG/90, absentees (display only) |

## Rollback

```bash
git checkout mls-shadow-v1.3      # pre-audit xG baseline
```
Note the trade: v1.3 carries the goals-prior shrinkage, the mislabelled
win% blend, WC26 dispersion on MLS props, and the lock gate that cannot
pass — i.e. **rolling back re-disables paper execution**. Prefer fixing
forward.

## Known limits at cut

- edge not statistically significant (n=162)
- all parameter sweeps are in-sample on those 162 matches
- goal-rate bias −0.174/match, open
- settlement and the in-play path untested against a real resolved fixture
- `market_depth_level` grows without retention
