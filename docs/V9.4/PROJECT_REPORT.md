# V9.4 — Project Report

**Period:** July 25, 2026. **13 commits** past the V9.3 baseline.
**Trigger:** the independent V9.3 evaluation.

---

## What this cycle was

V9.3 was driven by the operator noticing his own model favouring a
9th-place side over the league leaders. V9.4 is driven by an outside
reader finding what that cycle *missed*.

The V9.3 evaluation's judgement was blunt and correct:

> The provenance and forecast-evidence system is substantially more mature
> than the execution-measurement system.

And its most uncomfortable detail: **all five of its P0 findings had
already been reported against V9.2 and shipped unfixed.** They survived a
whole version because nothing in the test suite exercised them. V9.3 ran
four self-audits and found four real defects — every one in the model,
data or operations layer, because that is where it looked. The open
findings list was never re-checked.

That is the lesson of this cycle, and it is a process lesson, not a
technical one.

---

## The arc

**1. Verify before accepting.** Every P0 was reproduced against the actual
source before a line changed. All five confirmed exactly as described —
no overstatement, no misreading.

**2. Fix the economics.** Depth ordered by exact price; fees charged on
real per-level allocations; the strategy policy re-applied to what a fill
actually achieved; depth-backed fills separated from top-of-book
estimates; a canonical lock required to prove its market legs and a
complete registry sweep.

**3. Fix the evidence.** Complete raw payloads restored (at *less* space
than the truncated stubs, via compression), the active price grid frozen
with each quote, the corpus extended to carry the research plane, and the
approval decision bound to the exact parameters and data cutoff it
approved — inside the hashed core.

**4. Measure the thing the evaluation doubted.** F9 asked whether the
reported metrics describe what actually ships. Now measured rather than
assumed: at the production simulation count the analytic ladder and the
deployed generator differ by **−0.0001**. The proxy is faithful.

**5. Say what is not fixed.** F8, F15, F16 and F21 are disclosed as
limitations, not quietly closed.

---

## Where the platform stands

```text
model        unchanged: provider xG ratings + calibrated 3-way
edge         +0.0331  CI [-0.0035, +0.0663]  n=162   NOT significant
execution    exact ordering · allocation fees · post-fill policy ·
             execution-grade separation
evidence     complete raw payloads · price grid · research corpus ·
             parameter-bound approval
tests        492 backend + 10 e2e   (4 of them hermetic)
money        LOCKED — no order-placement path exists in the repository
```

---

## Errors made during this response

Recorded, because the whole point of this cycle is that unexercised code
hides defects — including code written in the act of fixing defects:

- The research-corpus sections turned the public corpus preview into a
  **46-second read**. Caught in production verification; fixed by caching
  (25 s cold → 0.07 s warm).
- Rate-limiting `/api/research` would have **429'd a user opening a second
  match**: the limiter buckets per prefix, so only singleton routes belong
  in it. Caught by an existing test.
- The new deterministic frontend tests contained **two bugs of their own**
  — a click that closed an already-open section, and a body read before
  render. The no-model-run case turned out to be handled correctly
  already, so the test now pins the real behaviour rather than the
  assumption.

---

## Lessons carried forward

**1. An open findings list is a work item, not an archive.** Five P0s
survived a version because nobody re-ran them. Re-verification of prior
findings now belongs in every release, ahead of new feature work.

**2. A fix without a failing test is a claim.** Every correction here was
checked by reverting it and watching the guard fire. That is the only
evidence that a test tests anything.

**3. Fixing a defect can create one.** Three of our own mistakes this
cycle were introduced *by* the remediation. Verification has to run
against the change, not the intention.

**4. Disclose what you did not fix.** F8's in-sample selection is the
single largest caveat on the headline number, and the honest response was
to state it in the approval record rather than to rush a research
redesign hours before the slate.

---

## Open items

- **F8** in-sample hyperparameter selection — remedied only by prospective
  data.
- **F15** one modelled order type; **F16** capture-clock freshness;
  **F21** process-local scheduler.
- **Goal-rate bias** −0.174 goals/match, diagnosed, correction rejected.
- **Untested until kickoff**: settlement against a real resolved fixture,
  and the in-play path.
- **No corpus published yet**, so the approval is not yet corpus-bound —
  the first post-slate step.
