# Documentation editions — where each one lives

Editions V9.1 through V9.5 were each written on their own branch and
**never merged to `main`**, so that production stayed pinned to a release
tag while the docs moved. That is deliberate, but it makes `main` look
like V9 is current. It is not.

**Current edition: [V9.5](V9.5/PROJECT_DOC.md) — branch `docs-v9.5`.**

| edition | date | branch | theme | baseline |
|---|---|---|---|---|
| **V9.5** | 2026-07-26 | `docs-v9.5` | **the first prospective slate** | `mls-shadow-v1.6` |
| V9.4 | 2026-07-25 | `docs-v9.4` | execution measurement | `mls-shadow-v1.5` |
| V9.3 | 2026-07-25 | `docs-v9.3` | provider-xG model + 4 self-audits | `mls-shadow-v1.4` |
| V9.2 | 2026-07-24 | `docs-v9.2` | execution fidelity | `mls-shadow-v1.2` |
| V9.1 | 2026-07-24 | `docs-v9.1` | frozen pre-slate | `mls-shadow-v1.1` |
| V9 | 2026-07-23 | `main` | validation-ready | `mls-shadow-v1` |
| V8 / V7 / V6 | — | `main` | expansion snapshot / earlier | — |

To read an edition that is not on `main`:

```bash
git show docs-v9.4:docs/V9.4/PROJECT_DOC.md
# or
git checkout docs-v9.4 -- docs/V9.4/
```

## Reading order for V9.5

1. [`V9.5/PROJECT_DOC.md`](V9.5/PROJECT_DOC.md) — current state, baseline,
   contracts, endpoint surface
2. [`V9.5/SLATE-EVALUATION.md`](V9.5/SLATE-EVALUATION.md) — the
   whole-system evaluation of the first prospective slate; **the centre
   of this edition**
3. [`V9.5/DEFECT-ANALYSIS.md`](V9.5/DEFECT-ANALYSIS.md) — the four
   defects, finding by finding
4. [`V9.5/CALIBRATION.md`](V9.5/CALIBRATION.md) — model, ladder, and the
   updated edge estimate
5. [`V9.5/RELEASE-mls-shadow-v1.6.md`](V9.5/RELEASE-mls-shadow-v1.6.md) —
   the baseline manifest
6. [`V9.5/RUNBOOK.md`](V9.5/RUNBOOK.md) — the repeating slate cycle

## Standing verdicts (unchanged since V9)

```text
machinery        GO
profitability    NO-GO
real money       NO-GO      REAL_MONEY_SIGNALS_ENABLED=false
```
