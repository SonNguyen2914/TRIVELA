# MLS Shadow Plane — Operator Runbook (V8.1 eval Phase 10)

> **⚠️ SUPERSEDED.** This is the V9 edition (2026-07-23). The current
> edition is **V9.5** on branch `docs-v9.5` — see [`docs/EDITIONS.md`](../EDITIONS.md)
> for the full map. V9 predates the first prospective slate, the
> published corpus, and the paper-coverage / VARCHAR-truncation /
> engine-signature fixes. Kept for the record.

Procedures for the incidents the evaluation named. The overriding rule:
**the safest state is no new orders** — money is disabled, and every
kill switch below only ever *stops* activity.

## Observe first
- `GET /api/ready` — both planes' health, `shadow_ready`, `blockers`.
- `GET /api/mls/metrics` — data freshness, lock success rate, missed
  locks, failed snapshots, paper P&L, settlement lag.
- `GET /api/mls/risk` — policy, **active kill switches**, open exposure.
- `GET /api/mls/audit` — every lock's integrity checks + retained
  missed locks / failed snapshots.
- `GET /api/mls/model-eval` — the ladder + confidence intervals.

## Incidents

**Missed T-10 lock** (`metrics.locks.missed_locks > 0`). Expected when
a fixture's book never became execution-ready or a required family
failed to capture — the lock *correctly* did not happen. Confirm via
`audit.missed_locks[*].failed_snapshot_attempts` and
`audit.failed_snapshots[*].failure_reason`. No retroactive lock is ever
created; the miss is the evidence. If the cause is our side (mapping
gap, throttle), fix and let the next sweep retry before kickoff.

**Provider (Kalshi/ESPN) outage.** Sweeps catch `RequestException` and
no-op; counters are idempotent, so recovery is automatic on the next
cycle. If `metrics.data.latest_snapshot_quote_age_s` climbs past ~900s
near a kickoff, the book is stale — `execution_ready` will be false and
paper trading will reject `QUOTE_STALE`. Nothing to do but wait; if
sustained, set `GLOBAL_TRADING_DISABLED=true`.

**Kalshi schema change.** The parser prefers current `*_fp` /
`orderbook_fp` fields with legacy fallbacks. If sizes/depth read null
across the board (`metrics` shows fills with no depth), the schema
moved: update `src/live/markets.py` `_quote_row` / `_depth_levels` and
bump `PROVIDER_SCHEMA_VERSION`. Add a fixture test with the new shape.

**Fixture postponement.** `refresh_window` records the kickoff move as a
`FixtureChange` history row and updates `current_kickoff_utc`;
`original_kickoff_utc` is preserved. Locks key off the current kickoff.

**Wrong team mapping.** Only APPROVED aliases attach markets. Fix the
bridge in `src/live/identity.py::KALSHI_BRIDGES` (or add an approved
`TeamAlias`), redeploy; the next map sweep re-attaches.

**Failed migration.** The live plane disables itself and records
`LIVE_BOOT_ERROR` (surfaced by `/api/ready`) — the archive keeps
serving. Fix the migration, verify locally against PostgreSQL, redeploy.
CI runs empty→head and previous-release→head on real Postgres.

**Settlement mismatch / lag.** `metrics.paper.unsettled_after_final > 0`
means fills whose fixture is post haven't settled — the window job
settles on the next cycle. Settlement is idempotent (only touches
`status='open'`). If a provider *corrects* a result, re-settlement is
future work (results currently fill once).

**Backup / restore.** Railway Postgres has managed backups (verify in
the dashboard). Durability is proven by write→restart→read. A logical
export + restore rehearsal is P1 (not yet automated).

## Kill switches (env; safest state = all stopped)
- `GLOBAL_TRADING_DISABLED` — halts ALL new paper fills / orders.
- `COMPETITION_TRADING_DISABLED` — halts MLS specifically.
- `PAPER_TRADING_ENABLED=false` — stops paper trading (a switch, not a
  money gate).
- Data-driven, computed by the risk engine: `DAILY_LOSS_LIMIT`.

## Disable everything
Set `GLOBAL_TRADING_DISABLED=true` (stops new fills) and, to freeze the
whole shadow plane, `MLS_SHADOW_ENABLED=false`. `REAL_MONEY_SIGNALS_ENABLED`
and `AUTO_EXECUTION_ENABLED` are false and have no enabled code path;
leave them false.

## Credentials
Operator token at `~/.wc26_admin_token` (chmod 600). Alerts:
Discord two-channel + secret ntfy topic. Never place any secret in the
browser bundle or a URL.
