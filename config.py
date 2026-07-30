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

# --- Kalshi market hunter (observational scanner; shadow mode) -------------
# Always-on scan of Kalshi's soccer GAME-series for structurally mispriced
# books. OBSERVATIONAL ONLY: it records findings and never places, sizes,
# or recommends an order. Money stays locked (REAL_MONEY_SIGNALS_ENABLED).
#
# Every numeric HUNTER_* bound must be POSITIVE and parseable, enforced at
# import: a zero/negative cadence or threshold would silently disable a
# guard (0-minute poll = hammering the provider; 0 thin-book size = no
# book is ever thin), and this repo's rule is that misconfiguration fails
# loudly at boot, never quietly at runtime.
def _pos_int(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip() or default
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"[config] {name}={raw!r} is not an integer")
    if v <= 0:
        raise ValueError(f"[config] {name}={raw!r} must be > 0")
    return v


def _pos_decimal_str(name: str, default: str) -> str:
    """Positive decimal bound kept in its STRING form — the hunter's
    exact-Decimal consumers parse it themselves (never through float)."""
    from decimal import Decimal, InvalidOperation
    raw = os.getenv(name, default).strip() or default
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"[config] {name}={raw!r} is not a decimal")
    if v <= 0:
        raise ValueError(f"[config] {name}={raw!r} must be > 0")
    return raw


HUNTER_ENABLED = _parse_flag(os.getenv("HUNTER_ENABLED"), True,
                             "HUNTER_ENABLED")
# Scan cadence. Kalshi rate-limits hard; keep this modest.
HUNTER_POLL_MINUTES = _pos_int("HUNTER_POLL_MINUTES", "10")
# How often the series roster is re-discovered from the provider's series
# listing (tags=Soccer, ticker ends GAME). Between discoveries the scan
# only touches series that recently had open markets — a series going
# ACTIVE between discoveries is picked up with at most this much lag
# (≤6h at the default), which is a documented trade against hammering
# the provider's full taxonomy every cycle.
HUNTER_DISCOVERY_MINUTES = _pos_int("HUNTER_DISCOVERY_MINUTES", "360")
# Optional explicit roster override: comma-separated series tickers. When
# set, discovery is skipped and EXACTLY these series are scanned. The
# default roster comes from live discovery, never a hardcoded guess —
# the empirical taxonomy snapshot lives in
# research_archive/kalshi_soccer_taxonomy_2026-07-28.json.
HUNTER_SERIES = [t.strip().upper() for t in
                 os.getenv("HUNTER_SERIES", "").split(",") if t.strip()]
# Known non-match novelty series excluded from discovery (verified in the
# taxonomy snapshot): not per-fixture 3-way books.
HUNTER_SERIES_SKIP = [t.strip().upper() for t in os.getenv(
    "HUNTER_SERIES_SKIP",
    "KXWCGOALEVERYGAME,KXWCTEAMSINGAME,KXKXECULPGAME").split(",")
    if t.strip()]
# Liquidity-context thresholds (WIDE_SPREAD / THIN_BOOK are context
# flags, never wins and never alerts).
HUNTER_WIDE_SPREAD_DOLLARS = _pos_decimal_str(
    "HUNTER_WIDE_SPREAD_DOLLARS", "0.10")
HUNTER_THIN_BOOK_SIZE = _pos_int("HUNTER_THIN_BOOK_SIZE", "5")
# IN_PLAY_OVERREACTION: minimum mid-to-mid repricing (dollars) between
# two consecutive hunter captures of the same market, on a match dated
# today (ET), before the move is flagged. The move must ALSO exceed the
# wider of the two captures' spreads — a "move" inside quote noise is
# not a repricing. CONTEXT ONLY: a violent in-play move is usually
# conditioned on a real match event the hunter cannot observe, so this
# never claims mispricing and never alerts.
HUNTER_OVERREACTION_MIN_MOVE_DOLLARS = _pos_decimal_str(
    "HUNTER_OVERREACTION_MIN_MOVE_DOLLARS", "0.15")
# Net margin (dollars per contract, after exact fees) a structural
# finding must clear before it may ALERT. Findings below this are still
# recorded; they just stay quiet.
HUNTER_ALERT_MIN_MARGIN_DOLLARS = _pos_decimal_str(
    "HUNTER_ALERT_MIN_MARGIN_DOLLARS", "0.01")
# Alert budget: max hunter alerts per rolling hour. A scanner that spams
# the channel gets muted by its human and then protects nothing.
HUNTER_ALERT_MAX_PER_HOUR = _pos_int("HUNTER_ALERT_MAX_PER_HOUR", "4")
# Minimum net edge for a MODEL_EDGE readout row (mirrors the paper
# execution policy's min_net_edge; observational, never alerts).
HUNTER_MODEL_EDGE_MIN = float(
    _pos_decimal_str("HUNTER_MODEL_EDGE_MIN", "0.03"))

# --- hunter in-play live statistics (API-Football) ------------------------
# Makes IN_PLAY_OVERREACTION's conditioning event OBSERVABLE where the
# provider's coverage allows. Purely OBSERVATIONAL: these readings reach
# HunterFinding.legs_json and nothing else. They must never touch a T-10
# lock, a PredictionRun, a paper signal, or anything the model is fitted
# or scored on — live xG carries information from AFTER a lock, and
# tests/test_hunter_live_stats.py fences that boundary statically and
# behaviourally.
#
# Enabled by default but INERT WITHOUT A KEY: with no key configured this
# spends zero requests and the detector reports exactly what it reports
# today (conditioning_unobserved_no_stats). Setting the key IS the opt-in,
# so deploying this changes nothing until an operator does that.
HUNTER_LIVE_STATS_ENABLED = _parse_flag(
    os.getenv("HUNTER_LIVE_STATS_ENABLED"), True,
    "HUNTER_LIVE_STATS_ENABLED")
# The PAID key, deliberately a DIFFERENT variable from the legacy
# API_FOOTBALL_KEY above: that one may still hold the free-tier key, and
# the free plan was measured season-blind for current seasons — reading it
# would produce confident answers about the wrong plan. Same rule as
# scripts/verify_apifootball.py.
APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "").strip()
APIFOOTBALL_BASE = os.getenv(
    "APIFOOTBALL_BASE", "https://v3.football.api-sports.io").strip()
# Per-CYCLE request ceiling. The cycle spends NOTHING unless an
# overreaction candidate actually exists; when one does it pays 1 request
# for the whole world's live fixtures plus 2 per distinct resolved
# fixture. PRO is 300/min and 7,500/day.
HUNTER_LIVE_STATS_MAX_REQUESTS_PER_CYCLE = _pos_int(
    "HUNTER_LIVE_STATS_MAX_REQUESTS_PER_CYCLE", "12")
# Whole-process daily ceiling, counted against OUR clock's UTC day. A
# second guard behind the per-cycle one: a pathological slate that
# produced candidates every cycle must still not eat the day's quota.
HUNTER_LIVE_STATS_MAX_REQUESTS_PER_DAY = _pos_int(
    "HUNTER_LIVE_STATS_MAX_REQUESTS_PER_DAY", "1200")
# How long a measured ABSENT coverage verdict is trusted before the
# competition is re-measured. Coverage drifts and this repo has been
# broken at HTTP 200 by exactly that; a cached absence must expire.
# A PRESENT verdict is never used in place of a fresh read — a finding's
# evidence is always the reading it was actually made from.
HUNTER_LIVE_STATS_COVERAGE_TTL_DAYS = _pos_int(
    "HUNTER_LIVE_STATS_COVERAGE_TTL_DAYS", "7")
# Whether IN_PLAY_OVERREACTION may ALERT now that its conditioning can be
# characterised. DEFAULT OFF, and deliberately so: promoting a
# context-class finding to the channel a consenting third party reads is
# Son's decision, not an implementer's. With this false the finding is
# recorded and served exactly as today and never dispatches. Turning it
# true routes it through the SAME AMBIENT_DETAIL / detail-channel path as
# the structural findings — never the act-now channel, never the phone.
HUNTER_IN_PLAY_ALERTS_ENABLED = _parse_flag(
    os.getenv("HUNTER_IN_PLAY_ALERTS_ENABLED"), False,
    "HUNTER_IN_PLAY_ALERTS_ENABLED")

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
