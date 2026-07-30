"""Central configuration. Everything overridable via environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Mode ---------------------------------------------------------------
# DEMO_MODE=true runs the whole system on realistic mock Kalshi/sports data
# so you can develop and demo without API keys. Flip to false when you have
# real Kalshi credentials and WC26 markets exist.
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"

# --- Kalshi -------------------------------------------------------------
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# --- Database -----------------------------------------------------------
# SQLite by default (zero setup). Point at Postgres when ready, e.g.:
#   DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/kalshi
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'suggester.db'}")

# --- Prediction engine --------------------------------------------------
N_SIMULATIONS = int(os.getenv("N_SIMULATIONS", "10000"))
PREDICTION_CACHE_TTL_SECONDS = int(os.getenv("PREDICTION_CACHE_TTL_SECONDS", "300"))  # 5 min
HOURLY_PREDICTION_WINDOW_HOURS = int(os.getenv("HOURLY_PREDICTION_WINDOW_HOURS", "6"))
FINAL_LOCK_MINUTES_BEFORE_KICKOFF = int(os.getenv("FINAL_LOCK_MINUTES", "10"))

# --- Suggestion filters (defaults; editable via /api/settings) ----------
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.05"))          # 5%
# 0.45 matches the value production always ran with (the old 0.60 default
# forced a manual settings re-POST after every deploy, since the SQLite
# settings row is wiped with the DB). Now a redeploy needs no manual step:
# the boot-time prime job repopulates predictions and this default is
# already the operating value.
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.45"))
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "10000"))

# --- Timing / ripeness alerts --------------------------------------------
ODDS_POLL_SECONDS = int(os.getenv("ODDS_POLL_SECONDS", "30"))
RIPENESS_ALERT_THRESHOLD = float(os.getenv("RIPENESS_ALERT_THRESHOLD", "75"))
RIPENESS_MIN_READINGS = int(os.getenv("RIPENESS_MIN_READINGS", "10"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))

# --- Alerts -------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# --- API ----------------------------------------------------------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000,https://namson.dev").split(",")

# --- Knockout goal damping -------------------------------------------------
# Knockout matches score fewer goals than group matches (well documented:
# e.g. WC2018 group stage averaged 2.54 goals/match with knockout 90-minute
# averages lower — teams protect leads with elimination on the line). The
# DIRECTION is sourced; the exact 0.85 per-team multiplier remains an
# estimate, kept configurable so it can be tuned against data without code.
KNOCKOUT_DAMPING = float(os.getenv("KNOCKOUT_DAMPING", "0.85"))

# --- Goal overdispersion ---------------------------------------------------
# Real goal counts are streakier than an independent Poisson allows —
# variance/mean ~1.1-1.25 in the literature, and this tournament's tails
# agree (four 3-0s, three 1-4s in 41 clean matches). A gamma-mixed Poisson
# (negative binomial): per-team-match rate multipliers Gamma(k, 1/k) with
# CV = this value; CV 0.30 at lambda 1.3 gives variance/mean ~1.12. What it
# DOES: fattens blowout tails and 0-0, better longshot/total calibration.
# What it DOESN'T: fix any 1-0-vs-1-1 ordering — dispersion slightly RAISES
# one-nil mass (zero-side convexity) and trims 1-1; the top-of-list order
# for even matchups is the calibrated answer either way. The tournament's
# apparent one-nil deficit (4 seen vs 7.6 expected) is p~0.13 — noted, not
# actionable. Set to 0 to recover pure Poisson.
GOAL_DISPERSION_CV = float(os.getenv("GOAL_DISPERSION_CV", "0.30"))

# MLS 3-way CALIBRATION: fraction of the final 3-way that is the uniform
# (1/3, 1/3, 1/3) anchor rather than the simulation. The model is
# measurably overconfident — its raw probabilities are too extreme — and
# shrinking them toward uniform corrects that.
#
# This REPLACED a "win% blend" that pulled toward the teams' win/draw/loss
# rates. The Jul 24 audit showed that term carried NO team information: a
# flat anchor at the same weight scored strictly better (1.0445 vs 1.0469),
# i.e. its whole benefit was damping, and the benefit did not survive
# fitting the weight walk-forward. Calling it calibration is what it is.
#
# The optimum is flat across 0.15-0.35 (1.0443-1.0454); 0.25 is its centre,
# so the exact value is not a knife-edge choice.
MLS_CALIBRATION_ALPHA = float(os.getenv("MLS_CALIBRATION_ALPHA", "0.25"))

# How much of a raw provider response SourceObservation keeps. The
# content_hash is the evidence anchor — it proves exactly what we
# received — and NOTHING in the codebase reads payload_json. Storing
# 200 KB per observation filled the production volume on Jul 25
# (source_observation reached 160 MB; every prediction write then failed
# with DiskFull), because the season-schedule ingest writes ~60
# observations on EVERY boot and the stats ingest 2 per match. An excerpt
# keeps the payload human-inspectable without unbounded growth.
OBSERVATION_PAYLOAD_MAX_BYTES = int(
    os.getenv("OBSERVATION_PAYLOAD_MAX_BYTES", "8192"))

# MLS goal-rate dispersion, SEPARATE from the WC26 GOAL_DISPERSION_CV
# below. Dispersion widens the per-match goal spread, which inflates
# P(0 goals) for each side and therefore suppresses BTTS and the overs.
# Inheriting WC26's 0.30 made the MLS props materially wrong (audit
# Jul 25: BTTS predicted 57.3% vs 66.0% actual; overs 4pp under). Swept
# on the 162-match walk-forward with the real simulator — prop log-loss
# improves MONOTONICALLY as dispersion falls (cv 0.3 -> 0.0 = +0.0277
# total, ~2x the entire xG gain) and the 3-way improves slightly too.
# WC26 keeps its own value: the archive must replay bit-for-bit.
MLS_GOAL_DISPERSION_CV = float(os.getenv("MLS_GOAL_DISPERSION_CV", "0.0"))
# deprecated alias — the old name, kept so an existing env override is not
# silently ignored. Remove once no deploy sets it.
MLS_WIN_BLEND_ALPHA = float(os.getenv("MLS_WIN_BLEND_ALPHA", "0.0"))

# MLS xG-based ratings: fraction of each team's attack/defence rating that
# comes from the provider's per-match expected goals (Sportec xG) rather
# than actual goals (0 = pure goals ratings == M2/M2W; 1 = pure xG). xG is
# the less-noisy signal over a half-season. Set to 1.0 after the walk-
# forward ladder MEASURED it beats the deployed model (M3 vs M2W): xG
# improves log-loss/Brier/RPS monotonically in alpha (real signal, not
# overfit), win% stays additive, total edge vs baseline ~3x the original.
# Still shadow evidence, NOT an established executable edge. Falls back to
# goals for any fixture the mls_stats ingestion hasn't populated with xG.
MLS_XG_RATING_ALPHA = float(os.getenv("MLS_XG_RATING_ALPHA", "1.0"))

# --- Model humility (market anchoring) -----------------------------------
# Final probability = MODEL_WEIGHT * model + (1-MODEL_WEIGHT) * market-implied.
# Liquid markets are usually right; only large, genuine disagreements should
# survive the edge filter. Raise toward 1.0 as the model earns trust.
MODEL_WEIGHT = float(os.getenv("MODEL_WEIGHT", "0.60"))
MAX_ODDS = float(os.getenv("MAX_ODDS", "8.0"))       # skip lottery-ticket longshots
MAX_SUGGESTIONS_PER_MATCH = int(os.getenv("MAX_SUGGESTIONS_PER_MATCH", "3"))

# --- Ranking board (likelihood-first) -------------------------------------
# The board shows bets MOST LIKELY TO HAPPEN that the user can then judge by
# edge/multiplier themselves. Likelihood is the gate and the sort key; edge
# is informational only (never a filter). Two-tier floor: if nothing clears
# the primary floor across all matches, retry once at the fallback floor,
# then show an honest empty state (no further lowering).
SUGGEST_PRIMARY_FLOOR = float(os.getenv("SUGGEST_PRIMARY_FLOOR", "0.49"))
SUGGEST_FALLBACK_FLOOR = float(os.getenv("SUGGEST_FALLBACK_FLOOR", "0.40"))

# Keep tracking a match through kickoff (live odds move on goals) and stop
# only once it's truly done: kickoff + 4h covers 90 min + ET + pens + Kalshi
# book-settling. Applies to the scheduler, the poller, and the board.
TRACK_HOURS_AFTER_KICKOFF = float(os.getenv("TRACK_HOURS_AFTER_KICKOFF", "4"))

# --- Live match feed (Layer 2: API-Football) ------------------------------
# Optional. When API_FOOTBALL_KEY is set, the /live endpoint can auto-fetch
# the real score/minute/red-cards instead of the user typing them. Free tier
# is 100 requests/day, so calls are budgeted: a hard daily cap (stop before
# the limit) plus a short cache so repeated reads of the same match don't
# each cost a request. World Cup is league id 1 in API-Football.
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FOOTBALL_BASE = os.getenv("API_FOOTBALL_BASE", "https://v3.football.api-sports.io")
API_FOOTBALL_LEAGUE_ID = int(os.getenv("API_FOOTBALL_LEAGUE_ID", "1"))
API_FOOTBALL_SEASON = int(os.getenv("API_FOOTBALL_SEASON", "2026"))
API_FOOTBALL_DAILY_CAP = int(os.getenv("API_FOOTBALL_DAILY_CAP", "90"))  # < 100
API_FOOTBALL_CACHE_SECONDS = int(os.getenv("API_FOOTBALL_CACHE_SECONDS", "20"))
# An EMPTY live=all answer (free-plan season blindness, or genuinely nothing
# live) is re-checked gently instead of every cache window — the dedicated
# live tick would otherwise burn the daily cap on calls that return nothing.
# ESPN carries the live read during the backoff.
LIVE_EMPTY_BACKOFF_SECONDS = int(os.getenv("LIVE_EMPTY_BACKOFF_SECONDS", "900"))
# The live-state snapshot tick — decoupled from the (slow, minutes-long)
# odds poll so the scoreboard tracks the real match closely.
LIVE_TICK_SECONDS = int(os.getenv("LIVE_TICK_SECONDS", "15"))

# In-play BUY/SELL signals on watched markets: fire when |live model −
# market price| clears the threshold; re-fire only on a side flip or when
# the divergence grows another 5 points, never inside the cooldown.
LIVE_SIGNAL_MIN_DIFF = float(os.getenv("LIVE_SIGNAL_MIN_DIFF", "0.08"))
LIVE_SIGNAL_COOLDOWN_SECONDS = int(os.getenv("LIVE_SIGNAL_COOLDOWN_SECONDS", "180"))
LIVE_SIGNAL_POLL_SECONDS = int(os.getenv("LIVE_SIGNAL_POLL_SECONDS", "30"))

# EASY-WIN alerts scan EVERY open in-play book (not just watched ones): the
# live model must call it near-certain, the price must still leave a real
# payout, and the gap must show the market hasn't fully caught up yet.
LIVE_EASYWIN_MIN_PROB = float(os.getenv("LIVE_EASYWIN_MIN_PROB", "0.85"))
LIVE_EASYWIN_MAX_PRICE = float(os.getenv("LIVE_EASYWIN_MAX_PRICE", "0.90"))
LIVE_EASYWIN_MIN_DIFF = float(os.getenv("LIVE_EASYWIN_MIN_DIFF", "0.05"))

# --- Live-state tracking (scoreboard robustness + finished-match handling) --
# A live match briefly disappears from API-Football's live=all during
# between-periods breaks (90'->ET, ET->penalties). The scoreboard holds a
# match through gaps up to this long before treating it as finished; must
# exceed the longest break (halftime-before-ET + ET->pens can be ~20 min).
LIVE_GAP_GRACE_MINUTES = int(os.getenv("LIVE_GAP_GRACE_MINUTES", "25"))
# How long a finished match stays on the live scoreboard as an FT card before
# dropping to the Past-matches section only.
LIVE_FT_WINDOW_MINUTES = int(os.getenv("LIVE_FT_WINDOW_MINUTES", "60"))
# How early before kickoff the live feed starts being polled for a match (the
# poll trails until TRACK_HOURS_AFTER_KICKOFF past kickoff). Kept tight so the
# daily API-Football budget is spent near/during matches, not days ahead on
# knockout fixtures that are "trackable" 96h out only for Kalshi market pricing.
LIVE_POLL_LEAD_MINUTES = int(os.getenv("LIVE_POLL_LEAD_MINUTES", "15"))

# --- Bracket auto-resolution ---------------------------------------------
# How often to check finished R16 (then QF, SF) results and fill the next
# round's placeholder slots. Low frequency by design: the bracket changes at
# most a handful of times all tournament, and the job self-skips (zero feed
# calls) once every slot is resolved, so this is nearly free.
BRACKET_RESOLVE_MINUTES = int(os.getenv("BRACKET_RESOLVE_MINUTES", "30"))

# --- Position tracker + alert fan-out ------------------------------------
# Cash-out-vs-hold verdicts flip when the better side wins by this fraction
# of the position's cost (hysteresis against book wobble).
POSITION_FLIP_MARGIN = float(os.getenv("POSITION_FLIP_MARGIN", "0.05"))
# ntfy.sh topic for instant phone pushes, independent of Remote Control and
# any open page. NO default: a topic committed to a public repo is a public
# channel (the tournament-weekend default was exactly that, by documented
# tradeoff — retired Jul 21). Set NTFY_TOPIC in the deployment environment
# and subscribe to the same topic in the ntfy app; unset, pushes no-op.
# .strip(): dashboard copy-paste loves smuggling trailing newlines into
# secrets — a whitespace-damaged topic failed silently on Jul 22.
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

# --- Post-tournament public lockdown (Jul 21 evaluation, P0) --------------
# FAIL CLOSED: read-only is the DEFAULT — an absent, misspelled, or lost
# variable leaves the public API read-only, never open. Development and
# tests opt out explicitly with PUBLIC_READ_ONLY=false. ADMIN_TOKEN
# (server-held, never shipped to a browser bundle) re-enables mutations
# for operator tooling via X-Admin-Token or Authorization: Bearer.
# RATE_LIMIT_SECONDS spaces calls to expensive recompute routes.
def _parse_read_only(raw: str | None) -> bool:
    """STRICT fail-closed boolean: only an exact, known 'off' value opens
    mutations. Unknown, misspelled, empty, or whitespace-damaged values
    all mean READ-ONLY — the V7 evaluation showed "true " and "treu"
    silently parsed as False under the old containment check, which is
    the opposite of fail-closed (same env-var-whitespace class as the
    Jul 22 ntfy newline)."""
    v = (raw or "").strip().lower()
    if v in ("false", "0", "no", "off"):
        return False
    if v not in ("", "true", "1", "yes", "on"):
        print(f"[config] PUBLIC_READ_ONLY={raw!r} not recognized — "
              "failing CLOSED (read-only)")
    return True


PUBLIC_READ_ONLY = _parse_read_only(os.getenv("PUBLIC_READ_ONLY", "true"))


# --- Competition operating modes (MLS launch decision, Jul 23) ------------
# The archive plane (WC26) and the live plane (MLS shadow) are SEPARATE
# concerns sharing one deployment. Every flag fails toward the safer
# state: an unknown value never enables anything. Real-money display and
# automated execution have NO enabling path in code yet — the manual
# money gate (implementation order #13) arrives only after the
# operational and model gates pass evidence review.
def _parse_flag(raw: str | None, default: bool, name: str) -> bool:
    """Strict allowlist boolean; unknown values -> the safer default,
    loudly."""
    v = (raw or "").strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    if v:
        print(f"[config] {name}={raw!r} not recognized — "
              f"using safe default {default}")
    return default


COMPETITION = os.getenv("COMPETITION", "mls-2026").strip()
# Shadow collection defaults ON: ingest, snapshot, lock, paper-trade.
MLS_SHADOW_ENABLED = _parse_flag(
    os.getenv("MLS_SHADOW_ENABLED"), True, "MLS_SHADOW_ENABLED")
# How old a cached market read may get before it is refused as a
# "current" price (journal-P0 F4). The TTL cache serves stale on a
# failed refresh — deliberately, for resilience — but past this age the
# briefing fails CLOSED: status `unavailable`, no price presented as
# current. Generous by default: ten minutes of staleness is clearly
# labelled fallback, beyond it is misinformation.
MLS_PRICE_MAX_AGE_SECONDS = int(
    os.getenv("MLS_PRICE_MAX_AGE_SECONDS", "600"))
# Money stays OFF by default and unknown-value-proof. Flipping this env
# var alone is NOT sufficient by design: the readiness endpoint must
# also report the model approved_for_real_money, which no code path
# sets in this phase.
REAL_MONEY_SIGNALS_ENABLED = _parse_flag(
    os.getenv("REAL_MONEY_SIGNALS_ENABLED"), False,
    "REAL_MONEY_SIGNALS_ENABLED")
# No auto-execution phase exists. The flag is declared so the invariant
# "it is false" is testable, not because anything reads it to act.
AUTO_EXECUTION_ENABLED = _parse_flag(
    os.getenv("AUTO_EXECUTION_ENABLED"), False, "AUTO_EXECUTION_ENABLED")
# Paper trading runs in SHADOW to build the execution-evidence base. It
# simulates fills against frozen books and NEVER places a real order —
# it has no coupling to REAL_MONEY_SIGNALS_ENABLED whatsoever. On by
# default in shadow; a kill switch, not a money gate.
PAPER_TRADING_ENABLED = _parse_flag(
    os.getenv("PAPER_TRADING_ENABLED"), True, "PAPER_TRADING_ENABLED")

# Risk-engine kill switches (V8.1 eval Phase 8). The SAFEST state is no
# new orders — each of these, when true, halts new fills/orders. They
# gate paper trading now and any future executor. Default false; the
# risk engine also computes data-driven switches (stale market data etc).
GLOBAL_TRADING_DISABLED = _parse_flag(
    os.getenv("GLOBAL_TRADING_DISABLED"), False, "GLOBAL_TRADING_DISABLED")
COMPETITION_TRADING_DISABLED = _parse_flag(
    os.getenv("COMPETITION_TRADING_DISABLED"), False,
    "COMPETITION_TRADING_DISABLED")

# --- Live data plane (PostgreSQL; the archive DB stays untouched) ---------
# Absent = the live plane is DORMANT: no engine is created, no MLS
# writes happen anywhere, shadow endpoints report not-ready. Set it to
# the Railway PostgreSQL connection string once provisioned (backups ON
# before first reliance — the launch decision's O2).
def _normalize_pg_url(url: str) -> str:
    """Railway (and most providers) hand out postgres:// or postgresql://
    connection strings. SQLAlchemy rejects the former outright and routes
    the latter to psycopg2 — we ship psycopg 3. Pin the driver in the
    scheme so the URL works exactly as provisioned."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


LIVE_DATABASE_URL = _normalize_pg_url(os.getenv("LIVE_DATABASE_URL", ""))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "30"))

# --- Journal: interactive-session capability (journal-P0-G) ---------------
# The action channel reaches Son, whose consenting friend places REAL
# bets. `source="session"` used to be a CLAIM any holder of ADMIN_TOKEN
# could make over HTTP — the stack-frame check only ever bound in-process
# callers. Dispatch now requires a short-lived capability minted through
# a challenge/response handshake, so the generic operator token alone
# cannot produce one.
#
# This secret is the SECOND factor and must differ from ADMIN_TOKEN;
# unset means no interactive session can ever be opened, which fails
# closed (the relay simply cannot dispatch). It is never transmitted:
# the client proves possession by HMAC over a server-issued nonce.
JOURNAL_SESSION_SECRET = os.getenv("JOURNAL_SESSION_SECRET", "").strip()
# How long a minted capability may dispatch for, and how long an
# unanswered challenge stays open. Short by design — a capability is a
# live session's, not a deployment's.
JOURNAL_SESSION_TTL_SECONDS = int(
    os.getenv("JOURNAL_SESSION_TTL_SECONDS", "3600"))
JOURNAL_CHALLENGE_TTL_SECONDS = int(
    os.getenv("JOURNAL_CHALLENGE_TTL_SECONDS", "120"))

# --- Journal: quote capture-age ceilings (journal-P0-H) -------------------
# Kalshi publishes NO quote timestamp, so our capture clock is the only
# evidence of when a book was observed. Without a ceiling an arbitrarily
# old same-contract quote could be promoted into "the book at the time"
# and into the decomposed execution-fidelity metrics.
#
# Record-time ceiling: generous enough that the documented flow (cite the
# frozen T-10 book, which is at most ten minutes old before a view goes
# void) keeps working, tight enough that an hours-old quote can never
# become `observed_quote`.
JOURNAL_QUOTE_MAX_AGE_SECONDS = int(
    os.getenv("JOURNAL_QUOTE_MAX_AGE_SECONDS", "900"))
# Fill-time ceiling: the fill book must be CONTEMPORANEOUS with the fill
# it is claimed to describe, so this is tighter than the record ceiling.
JOURNAL_FILL_QUOTE_MAX_AGE_SECONDS = int(
    os.getenv("JOURNAL_FILL_QUOTE_MAX_AGE_SECONDS", "300"))
# Tolerance for operator/exchange clock skew on supplied timestamps. A
# moment slightly ahead of the server clock is skew; minutes ahead is a
# data-entry error and is refused.
JOURNAL_FUTURE_TOLERANCE_SECONDS = int(
    os.getenv("JOURNAL_FUTURE_TOLERANCE_SECONDS", "60"))

# Ceiling on an inline public response body (V9.3 eval F20). The corpus
# PREVIEW is assembled from current state and therefore grows without
# bound as the database does; published versions are immutable stored
# bytes and remain the real download path.
MAX_PUBLIC_BODY_BYTES = int(
    os.getenv("MAX_PUBLIC_BODY_BYTES", str(8 * 1024 * 1024)))

# --- Two-channel Discord routing + the narrator ---------------------------
# ACTION channel: terse, act-now pings (signals, tracker flips, goals, T-10).
# DETAIL channel: the narrator's full live briefs + rich event analyses.
# Either falls back to the original DISCORD_WEBHOOK_URL so a single-channel
# setup keeps working untouched.
DISCORD_ACTION_WEBHOOK_URL = os.getenv(
    "DISCORD_ACTION_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
DISCORD_DETAIL_WEBHOOK_URL = os.getenv(
    "DISCORD_DETAIL_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
# Minutes between periodic in-play live briefs on the detail channel.
NARRATOR_INTERVAL_MINUTES = int(os.getenv("NARRATOR_INTERVAL_MINUTES", "5"))

# === EPL (epl-2026) — additive block, 2026-07-28 ==========================
# Premier League machinery parity. The MODEL IS DARK: nothing here can
# approve it, and no odds render until an approval decision is earned
# through the evaluation ladder on real 2026-27 data.

# Master switch for the EPL shadow-plane jobs (ingest, market discovery,
# quote capture, run sweeps). Same fail-safe parser as the MLS flag;
# with the model dark the run sweeps refuse regardless of this switch.
EPL_SHADOW_ENABLED = _parse_flag(
    os.getenv("EPL_SHADOW_ENABLED"), True, "EPL_SHADOW_ENABLED")

# The Kalshi game series is CONFIG, not fact: KXEPLGAME is verified to
# exist as a series (387 events in 25/26, research_archive/epl/) but had
# ZERO open 2026-27 events on 2026-07-28. The discovery probe
# (/api/epl/markets/discovery) reports its live status.
EPL_KALSHI_GAME_SERIES = os.getenv("EPL_KALSHI_GAME_SERIES",
                                   "KXEPLGAME").strip()

# EPL goal-rate dispersion. UNMEASURED for the EPL — 0.0 carries the
# closest measured precedent (MLS league play swept to 0.0 on 162
# fixtures; WC26's 0.30 was tournament data). Must be re-swept on real
# 2026-27 data before any approval evaluation; until then it only
# affects backtests/tests, since the dark model produces no runs.
EPL_GOAL_DISPERSION_CV = float(os.getenv("EPL_GOAL_DISPERSION_CV", "0.0"))

# 3-way calibration toward uniform. 0.0 = raw simulation: MLS's 0.25
# was MEASURED on MLS data and does not transfer by assumption. Swept
# alongside dispersion before any approval.
EPL_CALIBRATION_ALPHA = float(os.getenv("EPL_CALIBRATION_ALPHA", "0.0"))

# Kalshi discovery cadence for EPL. Slower than MLS's 10min while the
# series has no open events (11 family sweeps per pass; this repo has
# been burned by Kalshi 429s) — tighten once 26/27 listings appear.
EPL_MARKETS_JOB_MINUTES = int(os.getenv("EPL_MARKETS_JOB_MINUTES", "30"))
# === end EPL block =========================================================

# === Liga MX (liga-mx-2026) — additive block, 2026-07-29 ===================
# Mexican Liga BBVA MX machinery parity. IN SEASON (Apertura 2026) with
# OPEN Kalshi markets — but the MODEL IS DARK: nothing here can approve
# it, and no odds render until an approval decision is earned through
# the evaluation ladder.

# Master switch for the Liga MX shadow-plane jobs (ingest, market
# discovery, quote capture, run sweeps). DEFAULT FALSE — unlike the MLS
# and EPL flags — so shipping this build changes nothing at boot until
# an operator turns the plane on deliberately. With the model dark the
# run/lock sweeps refuse regardless of this switch.
LIGAMX_SHADOW_ENABLED = _parse_flag(
    os.getenv("LIGAMX_SHADOW_ENABLED"), False, "LIGAMX_SHADOW_ENABLED")

# The Kalshi game series, verified LIVE 2026-07-29: KXLIGAMXGAME exists
# with 9 open Apertura events and 221 historical events, exact KXMLSGAME
# grammar (research_archive/ligamx_kalshi_*_2026-07-29.json). Still
# config, and the discovery probe (/api/ligamx/markets/discovery)
# reports its live status.
LIGAMX_KALSHI_GAME_SERIES = os.getenv("LIGAMX_KALSHI_GAME_SERIES",
                                      "KXLIGAMXGAME").strip()

# Liga MX goal-rate dispersion. UNMEASURED — 0.0 carries the closest
# measured precedent (MLS league play swept to 0.0 on 162 fixtures).
# Must be re-swept on real Liga MX data before any approval evaluation;
# until then it only affects backtests/tests (the dark model produces
# no runs).
LIGAMX_GOAL_DISPERSION_CV = float(
    os.getenv("LIGAMX_GOAL_DISPERSION_CV", "0.0"))

# 3-way calibration toward uniform. 0.0 = raw simulation: MLS's 0.25
# was MEASURED on MLS data and does not transfer by assumption.
LIGAMX_CALIBRATION_ALPHA = float(
    os.getenv("LIGAMX_CALIBRATION_ALPHA", "0.0"))

# Kalshi discovery cadence. The listings are OPEN (unlike EPL's) but the
# plane is off by default and money is locked; 30min is plenty for
# discovery+capture while dark, and respects the 429 history (11 family
# sweeps per pass). Tighten deliberately if the plane is ever activated.
LIGAMX_MARKETS_JOB_MINUTES = int(
    os.getenv("LIGAMX_MARKETS_JOB_MINUTES", "30"))
# === end Liga MX block =====================================================

# --- live-plane volume headroom -------------------------------------------
# Railway's own volume alerts are Teams/Pro-only, so the platform CANNOT
# warn before the disk fills. It filled once (2026-07-25) and every
# prediction write failed silently behind {"created": 0}. The app has to
# watch its own headroom instead.
#
# Capacity is not discoverable from inside the container — Postgres knows
# its database size, not the size of the volume it sits on — so it is
# configured. Keep this in step with the Railway volume if it is resized.
LIVE_VOLUME_BYTES = int(os.getenv("LIVE_VOLUME_BYTES", str(5 * 1024**3)))
# Percent of the volume at which to start alerting. Postgres needs room
# above the database size for WAL, temp files and index rebuilds, so this
# deliberately fires well below full.
STORAGE_ALERT_PCT = float(os.getenv("STORAGE_ALERT_PCT", "70"))
# Re-alert cadence while still over threshold (minutes). A disk filling
# is not urgent-by-the-minute, and an hourly repeat is noise.
STORAGE_ALERT_COOLDOWN_MINUTES = int(
    os.getenv("STORAGE_ALERT_COOLDOWN_MINUTES", "360"))


# --- league-derived xG via API-Football ------------------------------------
# A paid API-Football key fills the xG gap for leagues with no free
# team-level xG source. MLS is deliberately NOT one of them: it already has
# real Sportec xG, free, with a measured effect, and it is inside the MLS
# engine signature — so MLS keeps Sportec and this provider is never allowed
# to supply it.
#
# The key is a SECRET: it is read from the environment, or from
# ~/.apifootball_key when that file is owner-only. It is never logged, never
# placed in a path or query string, and redacted out of provider error text.
APIFOOTBALL_BASE = os.getenv("APIFOOTBALL_BASE",
                             "https://v3.football.api-sports.io")
# Dark by default. Ingestion is an explicit operator action, exactly like the
# other league planes — a deploy must not silently start spending quota.
APIFOOTBALL_XG_ENABLED = os.getenv(
    "APIFOOTBALL_XG_ENABLED", "false").lower() == "true"
# Plan limits measured on the live key 2026-07-29: 300 requests/minute,
# 7500/day (Pro). The delay keeps a full-season ingest inside the per-minute
# limit with headroom; the budget is a per-process ceiling that aborts rather
# than spending someone else's quota.
APIFOOTBALL_REQUEST_DELAY_SECONDS = float(
    os.getenv("APIFOOTBALL_REQUEST_DELAY_SECONDS", "0.25"))
APIFOOTBALL_REQUEST_BUDGET = int(
    os.getenv("APIFOOTBALL_REQUEST_BUDGET", "3000"))
APIFOOTBALL_TIMEOUT_SECONDS = float(
    os.getenv("APIFOOTBALL_TIMEOUT_SECONDS", "20"))
# 429 backoff: the provider's per-minute window is 60s, so one full window is
# the honest wait. Two attempts, never a tight retry loop.
APIFOOTBALL_BACKOFF_SECONDS = float(
    os.getenv("APIFOOTBALL_BACKOFF_SECONDS", "60"))
APIFOOTBALL_MAX_RETRIES = int(os.getenv("APIFOOTBALL_MAX_RETRIES", "1"))
# Refresh cadence for the rolling xG top-up, in minutes. Config, not a
# constant, because the right cadence depends on how many leagues are on.
APIFOOTBALL_XG_JOB_MINUTES = int(
    os.getenv("APIFOOTBALL_XG_JOB_MINUTES", "720"))
# How long a measured coverage verdict stands before it is re-probed.
APIFOOTBALL_COVERAGE_TTL_DAYS = int(
    os.getenv("APIFOOTBALL_COVERAGE_TTL_DAYS", "30"))
# Fixtures sampled per league when measuring coverage, spread evenly across
# the season. A most-recent-N sample is BIASED: the newest completed fixtures
# on a split-year league are the promotion/relegation playoff block, which
# carries no xG, and that alone made three fully-covered leagues read
# 'partial' on 2026-07-29.
APIFOOTBALL_COVERAGE_SAMPLES = int(
    os.getenv("APIFOOTBALL_COVERAGE_SAMPLES", "6"))

# xG rating shrinkage for league-derived ratings. A STARTING POINT carried
# from MLS (model_mls.XG_SHRINK_GAMES, swept on 162 MLS fixtures to a clean
# interior optimum at k=4-6), NOT swept on any of these leagues' own data.
# Nothing prices off these ratings, so the parameter is a display choice
# until someone measures it — which is why the provenance is stated here
# rather than implied.
LEAGUE_XG_SHRINK_GAMES = float(os.getenv("LEAGUE_XG_SHRINK_GAMES", "6.0"))
# A club needs this many xG-carrying fixtures before it is rated at all.
# Below it the surface says 'not enough fixtures', never a number: mirrors
# model_mls.MIN_GAMES rather than inventing a second standard.
LEAGUE_XG_MIN_FIXTURES = int(os.getenv("LEAGUE_XG_MIN_FIXTURES", "5"))
