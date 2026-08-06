#!/usr/bin/env python3
"""Does the cross-league read already know about hosting league?

    python scripts/measure_league_offset.py \
        --out research_archive/league_offset_$(date -u +%F).json

WHY THIS EXISTS, and why it is a MEASUREMENT and not a correction.
The three-edition history (research_archive/leagues_cup_history_*.json)
established from OUTCOMES ALONE that hosting matters asymmetrically in
this competition:

    MLS hosts      MLS points share .6159   CI [.5145, .7101]  EXCLUDES .5
    Liga MX hosts  MLS points share .5286   CI [.4357, .6286]  includes .5

Separately, matchday 1 of 2026 saw our read sit BELOW the market on all
four MLS home sides, and those sides then returned .750. Two
observations pointing the same way is a hypothesis — that the read
under-rates MLS hosts — and a hypothesis is not a constant. This script
measures the read's OWN signed error by hosting league so the
hypothesis becomes a number with an interval, or is refused.

Nothing here changes a constant. If a correction is ever fitted, this
document is what it cites.

THE LEAKAGE PROBLEM, stated before any number.
The ratings provider serves CURRENT ratings. A rating read today
already contains the outcomes of every historical match it would be
scored against, so a retrospective error is contaminated by
construction — it flatters the read, and the flattery is largest for
the oldest edition. Two consequences, both enforced here:

  RETROSPECTIVE (2025 only)  reported, labeled
                             `contaminated_retrospective`, and refused
                             outright for 2023/2024 where a two-to-
                             three-year-old outcome sits inside today's
                             number (AGENTS.md section 13).

  PROSPECTIVE (2026)         the clean arm: fixtures whose read was
                             observed BEFORE kickoff. There is no
                             leakage because the number existed first.
                             It is also tiny — this competition began
                             days ago — so the floor below applies and
                             a refusal is the expected honest output
                             for some time yet.

The prospective arm reads its pre-kickoff snapshots from a corpus file
the T-60 briefings write. Absent that file it says so by name rather
than falling back to the contaminated arm, because a contaminated
number wearing a clean label is the one outcome worse than no number.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

UTC = timezone.utc
BOOT = 2000
SEED = 12345
# Below this, a mean signed error by hosting league is one slate's mood.
MIN_PER_ARM = 20


def _fold(s: str) -> str:
    n = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in n if not unicodedata.combining(c)).lower()


def _toks(s: str) -> frozenset[str]:
    from src.friendlies import _identity_tokens
    return frozenset(t for t in _identity_tokens(_fold(s))
                     if len(t) > 1 and any(c.isalnum() for c in t))


def _get(base: str, path: str, params: dict | None = None):
    try:
        r = requests.get(f"{base.rstrip('/')}{path}", params=params,
                         timeout=60)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[offset] fetch failed {path}: {type(exc).__name__}",
              file=sys.stderr)
        return None


def _rosters(base: str):
    """League membership from our own standings surfaces — the same
    runtime read the history evaluation uses, never a typed list."""
    def names(doc):
        out = set()

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("name", "displayName", "team") \
                            and isinstance(v, str):
                        out.add(v)
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(doc or {})
        return out

    alias = {"lafc": frozenset({"los", "angeles"})}

    def entry(n):
        t = _toks(n)
        extra = alias.get(_fold(n).replace(" ", ""))
        return ((t | extra) if extra else t, n)

    return {"mls": [entry(n) for n in
                    sorted(names(_get(base, "/api/mls/standings")))],
            "ligamx": [entry(n) for n in
                       sorted(names(_get(base, "/api/ligamx/standings")))]}


def _league_of(name: str, rosters) -> str | None:
    t = _toks(name)
    if not t:
        return None
    best = {lg: max((len(t & rt) for rt, _ in roster), default=0)
            for lg, roster in rosters.items()}
    top = max(best.values())
    if top == 0:
        return None
    leaders = [lg for lg, v in best.items() if v == top]
    return leaders[0] if len(leaders) == 1 else None


def _ci(xs, rng):
    means = []
    for _ in range(BOOT):
        s = [xs[rng.randrange(len(xs))] for _ in range(len(xs))]
        means.append(sum(s) / len(s))
    means.sort()
    return [round(means[int(0.025 * BOOT)], 4),
            round(means[int(0.975 * BOOT)], 4)]


def _arm(rows, rosters, label):
    """Signed read error (read minus actual points share) split by which
    league hosted. Positive = the read was TOO HIGH on the home side."""
    rng = random.Random(SEED)
    buckets = {"mls_hosted": [], "ligamx_hosted": []}
    unresolved, no_read, same_league = [], 0, 0
    for r in rows:
        hn = (r.get("home") or {}).get("name") or ""
        an = (r.get("away") or {}).get("name") or ""
        hl, al = _league_of(hn, rosters), _league_of(an, rosters)
        if hl is None or al is None:
            unresolved.append(f"{hn} v {an}")
            continue
        if hl == al:
            same_league += 1
            continue
        e = r.get("_read")
        if e is None:
            no_read += 1
            continue
        g = r["goals"]
        actual = (1.0 if g["home"] > g["away"]
                  else 0.5 if g["home"] == g["away"] else 0.0)
        buckets["mls_hosted" if hl == "mls" else "ligamx_hosted"].append(
            e - actual)
    out = {"arm": label, "unresolved": len(unresolved),
           "unresolved_names": unresolved[:10],
           "same_league_excluded": same_league,
           "fixtures_without_a_read": no_read, "by_host": {}}
    for host, xs in buckets.items():
        if not xs:
            out["by_host"][host] = {
                "n": 0,
                "refused": "no scorable fixture in this bucket"}
            continue
        out["by_host"][host] = {
            "n": len(xs),
            "mean_signed_error": round(sum(xs) / len(xs), 4),
            "ci95": _ci(xs, rng) if len(xs) >= MIN_PER_ARM else None,
            "below_floor": len(xs) < MIN_PER_ARM,
        }
    a, b = buckets["mls_hosted"], buckets["ligamx_hosted"]
    if len(a) >= MIN_PER_ARM and len(b) >= MIN_PER_ARM:
        diffs = []
        for _ in range(BOOT):
            ma = sum(a[rng.randrange(len(a))] for _ in a) / len(a)
            mb = sum(b[rng.randrange(len(b))] for _ in b) / len(b)
            diffs.append(ma - mb)
        diffs.sort()
        lo = diffs[int(0.025 * BOOT)]
        hi = diffs[int(0.975 * BOOT)]
        point = sum(a) / len(a) - sum(b) / len(b)
        out["host_asymmetry"] = {
            "delta_mean_signed_error": round(point, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "significant": bool(lo > 0 or hi < 0),
            "means": ("the read's error on MLS-hosted fixtures minus its "
                      "error on Liga MX-hosted ones. A significant "
                      "non-zero value is what a hosting-league "
                      "correction would be fitted on; zero-crossing "
                      "means the read already handles hosting as well "
                      "as this sample can tell")}
    else:
        out["host_asymmetry"] = {
            "refused": (f"each bucket needs {MIN_PER_ARM}+ scorable "
                        f"fixtures; have "
                        f"{len(a)} MLS-hosted and {len(b)} Liga "
                        f"MX-hosted"),
            "why_a_floor": ("a mean signed error over a handful of "
                            "fixtures is a slate's mood, and this "
                            "number's only purpose is to justify or "
                            "refuse a constant")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--prospective-corpus", default=None,
                    help="JSON of pre-kickoff read snapshots (the clean "
                         "arm). Absent -> that arm is refused by name.")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if not a.api_base:
        print(json.dumps({"error": "no_api_base", "measured": False},
                         indent=2))
        return 2

    rosters = _rosters(a.api_base)
    now = datetime.now(UTC)
    doc = {
        "measured_at": now.date().isoformat(),
        "measured_at_utc": now.isoformat(),
        "question": ("does the cross-league strength read already "
                     "encode hosting league, or does it under-rate MLS "
                     "hosts as matchday 1 suggested?"),
        "what_this_is_NOT": ("a correction, a constant, or evidence of "
                             "an edge. It is the read's own signed "
                             "error, split by who hosted"),
        "prior_from_outcomes": {
            "source": "research_archive/leagues_cup_history_2026-08-05.json",
            "mls_hosted_points_share": 0.6159,
            "mls_hosted_ci95": [0.5145, 0.7101],
            "ligamx_hosted_points_share": 0.5286,
            "ligamx_hosted_ci95": [0.4357, 0.6286],
            "note": ("outcome-only and clean, but it says how the "
                     "MATCHES went, not what the READ said. This "
                     "document supplies the second half")},
        "leakage_policy": {
            "2023_2024": ("REFUSED. Today's ratings contain those "
                          "outcomes; a retrospective error there is "
                          "contaminated beyond use (AGENTS.md 13)"),
            "2025": "reported, labeled contaminated_retrospective",
            "2026": ("the clean arm — reads observed before kickoff, "
                     "so the number existed before the outcome")},
    }

    # --- contaminated arm: 2025, read served today ---------------------
    d = _get(a.api_base, "/api/comp/leagues-cup/fixtures",
             {"season": 2025, "include_finished": "true"})
    rows = []
    for f in (d or {}).get("fixtures") or []:
        if f.get("status") not in ("FT", "PEN") \
                or (f.get("goals") or {}).get("home") is None:
            continue
        st = f.get("strength") or {}
        eps = ((st.get("expected_points_share") or {}).get("home")
               if st.get("available") else None)
        rows.append({**f, "_read": eps})
    ret = _arm(rows, rosters, "retrospective_2025")
    ret["evidence_class"] = "contaminated_retrospective"
    ret["contamination"] = ("the read is CURRENT ratings scored against "
                            "2025 results those ratings already absorbed "
                            "— it flatters the read by an unknown amount "
                            "and cannot justify a constant on its own")
    doc["retrospective_2025"] = ret

    # --- clean arm: prospective, pre-kickoff snapshots ------------------
    if a.prospective_corpus and Path(a.prospective_corpus).exists():
        loaded = json.loads(Path(a.prospective_corpus).read_text())
        # A bare list of rows, or the document scripts/
        # build_prospective_corpus.py writes — which wraps the same rows
        # in the provenance that makes them auditable (what was rejected,
        # how far ahead of kickoff each read was captured, from which
        # file). Accept both rather than forcing the builder to throw
        # that away: iterating the document as if it were a list yields
        # its KEYS, which fails loudly here but would be a silent
        # miscount in any laxer reader.
        snaps = loaded["rows"] if isinstance(loaded, dict) else loaded
        if not isinstance(snaps, list):
            print(json.dumps({"error": "bad_prospective_corpus",
                              "means": "expected a list of rows, or an "
                                       "object with a 'rows' list"}))
            return 2
        doc["prospective_2026"] = _arm(snaps, rosters, "prospective_2026")
        doc["prospective_2026"]["evidence_class"] = "prospective_clean"
        if isinstance(loaded, dict):
            doc["prospective_2026"]["corpus_provenance"] = {
                k: loaded.get(k) for k in
                ("n_rows", "rejected_count", "admissibility",
                 "pending_not_yet_settled")}
    else:
        doc["prospective_2026"] = {
            "refused": ("no pre-kickoff read corpus supplied. The clean "
                        "arm needs reads OBSERVED BEFORE KICKOFF; "
                        "re-reading a fixture after the fact returns "
                        "today's rating and silently becomes the "
                        "contaminated arm wearing a clean label"),
            "what_would_be_needed": (
                "a JSON list of settled fixtures each carrying the "
                "read as it stood pre-kickoff (`_read`), plus home/away "
                "names and goals. The T-60 briefings on issue #68 "
                "freeze exactly these numbers per slate; ~2 slates "
                f"exist so far against a {MIN_PER_ARM}-per-bucket "
                "floor"),
            "evidence_class": "not_yet_collectable"}

    doc["conclusion"] = (
        "A hosting-league correction is JUSTIFIED only by the "
        "prospective arm clearing its floor with a CI excluding zero. "
        "Until then the read is left alone: matchday 1's four MLS "
        "hosts are an observation, not a coefficient")

    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
