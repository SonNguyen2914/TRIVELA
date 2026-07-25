# Runbook — first MLS shadow slate (`mls-shadow-v1.5`)

Operational protocol for the first prospective MLS T-10 slate under the
V9.4 baseline. Record evidence at each stage; do not merely observe
informally. This supersedes the V9.2 runbook — **note the changed
acceptance values in §3**, which are different from v1.2.

## 0. Freeze
No change to: model · calibration · dispersion · approval policy · edge
thresholds · fee arithmetic · execution-readiness rules · market-family
policy · lock timing · sim count · scheduler · lineup rules · risk limits.
A further critical hotfix ⇒ new tag + disclosed deviation. **Hard deploy
freeze ≥ 90 min before the first T-10 lock** — V9.3 shipped 38 commits in
two days; that cadence is exactly what filled the database volume.

## 1. Live-book diagnostic (once, BEFORE any lock — do NOT create/reserve a lock)
Prove the production provider schema, parser, and fill engine agree:
1. fetch a raw Kalshi orderbook (`GET {KALSHI}/markets/{ticker}/orderbook`);
2. run it through `_depth_levels` + `yes_buy_ladder`;
3. assert per side: `raw best bid == persisted best bid == fill-engine top`
   and `raw best size == persisted exact size == first fillable size`.

**New in v1.4 — also assert execution-readiness is reachable.** Capture a
lock-grade snapshot on a mapped fixture and confirm:
```text
status                 = complete
freshness_basis        = capture_time      (NOT provider — see below)
execution_ready        = true
game_oldest_quote_age_seconds  <= 600
```
> Kalshi publishes **no** quote-update timestamp: `updated_time` tracks the
> market definition and runs ~30 h stale on active, two-sided markets. The
> v1 gate required it and could never pass, silently rejecting every paper
> signal. If `freshness_basis` ever reads `provider` again, something has
> regressed — see `AUDIT-FINDINGS.md` finding 4.

## 2. T-90 go/no-go (record each as evidence)
- **Scheduler** — last heartbeat, next T-10 job, scheduled fixture count,
  missed-job count, instance id. *No-go:* heartbeat out of tolerance ·
  missing/duplicate jobs · next-exec inconsistent with kickoff · restart loop.
- **Provider quota/connectivity** — ESPN/Kalshi/MLS-stats probes, response
  times, last discovery. *No-go:* schema mismatch · repeated auth or
  rate-limit failures · incomplete registry · unapproved mappings.
- **Storage headroom** — `GET /api/admin/mls/storage`. *No-go:* database
  above ~60 % of the volume. (New: the volume filled on Jul 25 and every
  prediction write failed silently.)
- **Data coverage** — `GET /api/mls/stats-coverage`: team and player stats
  complete, bridge minutes-weighted coverage ≥ 95 %.
- **Notifications** — one probe each: urgent Discord, detail Discord, ntfy.
- **Database backup** — backup id, creation time, db revision, release tag.

> Only the notifications probe is app-triggerable. Scheduler heartbeat and
> DB backup id are captured manually from Railway.

## 3. Lock-window acceptance (per fixture)
```text
artifact_schema            = model-input-v5
snapshot policy_version    = mls-lock-v2
paper policy_version       = paper-exec-v4       (was v3)
three_way_market_linked    = true                (NEW - F5, hard requirement)
registry sweep             complete, < 6h old    (NEW - F6, hard requirement)
freshness_basis            = capture_time
approval_decision_id       = 182  (unless explicitly superseded)
approval_decision_hash_valid = true    approval precedes run
engine_signature_present   = true      engine_signature_matches_current = true
input_artifact_retained    = true      lineup_snapshot_referenced = true
required_families_complete = true      execution_ready = true
exactly_one_canonical_lock = true      lock_before_kickoff / inside_window
```
A lock failing **any** of these is a defect, not a curiosity. `GET
/api/mls/audit` reports `all_pass` per lock; the slate must stay
`clean_slate: true`.

## 4. During the slate
- `GET /api/mls/slate` — every fixture must land in exactly one state.
  `EXECUTION_NOT_READY` across the board means the freshness regression
  is back; `INTEGRITY_FAILED` means a lock broke its contract.
- `GET /api/mls/paper` — signals, fills, rejects with reasons. **Zero fills
  with all rejects `NOT_EXECUTION_READY` is the v1 failure mode.** Read
  `execution_grade` P&L only; `estimate_only` is deliberately excluded.
  The reject reason `POST_FILL_EDGE_BELOW_THRESHOLD` is CORRECT behaviour
  (F3), not a fault — the depth walk moved the price past the policy.
- `GET /api/ready` — `real_money_signals` must remain **false**.

## 5. After settlement
- `settle_paper` runs from the window job. Verify: every fill leaves
  `status=open`, `outcome_hit` set, `pnl_dollars == payout − cost` exactly,
  and `unsettled_after_final == 0` in `observability.metrics()`.
- **This is the first real settlement.** Logic is verified for win/loss/draw
  with exact decimals in tests, but has never run on a resolved fixture —
  check the first few by hand against the actual scorelines.

## 6. Post-slate
- Publish the immutable corpus version (`corpus.publish_corpus`) — one
  label, written once, refused on re-publish.
- Re-run the ladder and record whether the prospective results move the
  edge interval off zero.
- Revisit **key-attacker availability** (+0.0034, CI spans zero) with the
  new matches.
- Consider a retention policy for `market_depth_level` (152 MB, unbounded;
  only lock-linked depth is needed by paper fills and the corpus).

## Emergency
```text
paper kill switches   PAPER_TRADING_ENABLED, paper_new_entries_allowed
model rollback        git checkout mls-shadow-v1.3   (re-disables paper execution)
volume pressure       GET /api/admin/mls/storage; payload_json has no reader
force regeneration    POST /api/admin/mls/sweep?force=true   (operator token)
```
Money stays locked in every scenario: `REAL_MONEY_SIGNALS_ENABLED=false`
and there is no order-placement code path in the repository.
