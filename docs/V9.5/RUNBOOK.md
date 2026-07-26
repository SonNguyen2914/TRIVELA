# Runbook — ongoing MLS shadow slates (`mls-shadow-v1.6`)

Operational protocol for every slate **after** the first. The V9.4
runbook covered a one-off event nobody had rehearsed; this one covers a
repeating cycle that has now run once, end to end, successfully.

Record evidence at each stage. Do not merely observe informally.

---

## 0. Standing freeze — the most important rule in this document

**Do not tune on collected slates.** The 15 fixtures of 2026-07-25 are
now inside the scored sample (n=177). Any constant chosen with them in
view converts prospective evidence into another in-sample fit — exactly
how the M1 overfit and the "win% blend" happened.

No change without a measured walk-forward, to: model · calibration ·
dispersion · approval policy · edge thresholds · fee arithmetic ·
execution-readiness rules · market-family policy · lock timing · sim
count · risk limits.

**Hard deploy freeze ≥ 90 min before the first T-10 lock.** V9.3 shipped
38 commits in two days and filled the database volume doing it.

---

## 1. T-90 go/no-go

```bash
curl -s "$PROD/api/ready" | jq '{ready, readiness, shadow_blockers,
  real_money_signals, live:.live.shadow}'
```
Require: `ready: true` · `archive_ready: true` · `migrations_current:
true` · `shadow_blockers: []` · `paper_kill_switches: []` ·
`real_money_signals: false` · `unmapped_upcoming: 0`.

```bash
curl -s "$PROD/api/mls/metrics" | jq '{data, locks, runs, paper}'
```
Require: `fixture_obs_age_s` < 900 (the 15-min window job is alive) ·
`runs.failed: 0`.

```bash
curl -s "$PROD/api/admin/mls/storage" -H "X-Admin-Token: $T" | jq .
```
Volume headroom — the DiskFull incident failed **silently** behind
`{"created": 0}`.

Confirm the approval decision is bound:
```bash
curl -s "$PROD/api/mls/approval" | jq '{decision_id, corpus_version,
  corpus_manifest_hash, n_scored, edge_vs_baseline, edge_significant}'
```
`corpus_manifest_hash` must be **non-null**. A null means the approval is
not bound to any frozen corpus.

---

## 2. Live-book diagnostic (once, before any lock)

Do **not** create or reserve a lock. Prove provider schema, parser and
fill engine agree:

1. fetch a raw Kalshi orderbook (`GET {KALSHI}/markets/{ticker}/orderbook`);
2. run it through `_depth_levels` + `yes_buy_ladder`;
3. assert per side: `raw best bid == persisted best bid == fill-engine top`
   and `raw best size == persisted exact size == first fillable size`.

Kalshi's `no_dollars` array is **ascending** — the best bid is LAST.
Keeping the wrong end reported 0.84 where the truth was 0.47.

---

## 3. Lock-window acceptance (during the slate)

```bash
curl -s "$PROD/api/mls/slate" | jq '{summary, qualification, clean_slate}'
```
Require every fixture classified, `clean_slate: true`, no duplicate
canonical locks, no post-kickoff locks.

---

## 4. Post-slate — the 8 steps

Run **in order**. Steps 2 and 3 are new in V9.5 and are the ones that
would have caught the first slate's inverted headline.

### 1. Wait for settlement
`mls_window_job` runs every 15 minutes and calls `settle_paper()`.
```bash
curl -s "$PROD/api/mls/paper" | jq '{settled_fills, open_fills}'
curl -s "$PROD/api/mls/metrics" | jq '.paper.unsettled_after_final'
```
Require `open_fills: 0` and `unsettled_after_final: 0`.

### 2. ⚠️ CHECK PAPER COVERAGE BEFORE READING ANY PAPER NUMBER
```bash
curl -s "$PROD/api/mls/paper" | jq .coverage
```
Require `complete: true` and `legs_missing: 0`.

**A signal count is unreadable without this.** On the first slate the
ledger reported 27 signals against 45 eligible legs and nothing said so —
and because the missing rows were the *fill-producing* locks, the headline
read "the model agreed with the market" when the truth was the opposite.

If incomplete:
```bash
curl -s -X POST "$PROD/api/admin/mls/paper-backfill" -H "X-Admin-Token: $T" | jq .
```
Recovered rows are stamped `backfilled_at` and counted in
`backfilled_signals`. **Read any `error` in the results** — the backfill
reproducing a write failure is how the truncation bug was found.

### 3. Read the audit summary, both blocks
```bash
curl -s "$PROD/api/mls/audit" | jq .summary
```
- `locks_all_pass` == `canonical_locks`, `clean: true`
- `paper_coverage.complete: true`
- `engine_provenance` — `locks_engine_changed` is **informational**. It
  means the deployed code moved since those locks; the locks are still
  valid. Replay under the recorded revision remains the claim.

### 4. Score the slate against the baseline
Model log loss vs league base rates, with a **bootstrap CI** — never a
bare point estimate. If the CI is wider than the effect you are chasing,
say so explicitly; that is the finding.

### 5. Publish the immutable corpus
```bash
curl -s -X POST "$PROD/api/admin/mls/corpus/publish?version=<VERSION>" \
  -H "X-Admin-Token: $T" | jq .
```
Versions are immutable — re-publishing is refused; bump the version.
Takes ~80 s.

### 6. Download and verify independently
```bash
curl -s "$PROD/api/mls/corpus?version=<VERSION>&full=1" -o corpus.json
```
Recompute the manifest hash over `{files, counts, corpus_version,
schema_version}`, and re-hash every section with:
```python
json.dumps(section, sort_keys=True, ensure_ascii=False)
```
**`ensure_ascii=False` is mandatory.** The default produces six false
mismatches on the sections carrying player/team names and em dashes.

### 7. Bind the approval to it
```bash
curl -s -X POST "$PROD/api/admin/mls/approval/bind-corpus" \
  -H "X-Admin-Token: $T" | jq .
```
Then confirm `corpus_manifest_hash` is non-null on `/api/mls/approval`.

### 8. Archive locally, then stop
Gzip the corpus into `research_archive/corpus/` with a verification
receipt (the raw JSON is gitignored — it is ~12 MB). Archive the
evaluation bundle. **Do not touch the model.**

---

## 5. Invalidation conditions

Treat the slate's evidence as compromised if any hold:

- `paper_coverage.complete: false` and the backfill cannot close it;
- any lock fails `approval_hash_recomputed_ok` or
  `engine_signature_present`;
- a canonical lock exists after its kickoff, or two exist for one fixture;
- the sweep reports `failures` and prediction writes silently returned 0;
- a paper fill's `execution_class` is not `bounded_depth` while being
  counted in the headline P&L.

---

## 6. Reading results honestly — required framings

- **Never report a count without its denominator.** Signals without
  eligible legs; created without attempted; fills without coverage.
- **Never report an edge without n and its CI.** A point estimate with a
  CI spanning zero is not an edge.
- **Never let a small P&L imply a verdict.** Seven fills at 24¢: one more
  winner takes −40.9% to +18.1%. State what the sign turns on.
- **Distinguish recovered evidence from original.** `backfilled_at` rows
  are deterministic recomputations, not signals that existed at lock time.

---

## 7. Provider-drift standing checks

ESPN has broken us three times at HTTP 200 with well-formed payloads:
`headToHeadGames` → `seasonseries`, the winner-first `score` string, and
the standings child named "Eastern Conference" carrying all 30 clubs.

Before trusting any provider-derived display:
- derive the value shown from the same numbers displayed beside it;
- never render a provider composite string;
- treat a provider's **grouping** as data, not a guarantee — derive
  membership from the rows when you can;
- the team-roster endpoint is transfer-stale; use per-match summary
  participants.
