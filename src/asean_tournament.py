"""ASEAN Championship tournament surface — groups, bracket, and a
champion FORECAST derived from eloratings.net.

WHAT KIND OF CLAIM THIS MAKES. No model of ours runs here (the registry
says so and why). This module runs a Monte-Carlo simulation of the
remaining tournament on top of an EXTERNAL published rating, so its
output is exactly as good as (a) eloratings.net's national Elo and
(b) the stated assumptions below — and no better. Every number it
serves is labelled a forecast from an external rating, never a model
probability, and nothing here prices a market or ranks a bet.

THE ASSUMPTIONS, STATED ONCE AND SERVED WITH EVERY PAYLOAD:

  DRAW ANCHOR (measured). At even ratings a group match draws at the
  competition's own historical rate: 15 draws in 92 group matches over
  the 2016-2024 editions = 0.163 (research_archive/
  asean_draw_rates_2026-08-04.json). The draw probability shrinks
  LINEARLY to zero as the Elo expectation goes extreme:
      p_draw(E) = 0.163 * (1 - |2E - 1|)
  The anchor is measured; the linear shape is a stated functional form,
  not a fitted curve — five editions cannot fit one.

  TWO-LEGGED TIES. P(advance) is taken as the single-match Elo
  expectation E. Two legs shrink variance and a level aggregate goes to
  extra time and penalties (near even), both of which fold the tie
  toward its expectation; the simplification is stated rather than
  modelled.

  TIEBREAK PROXY. Group ranking is points, then goal difference, then
  goals scored — and for SIMULATED matches no scoreline exists, so ties
  on points fall back to goal difference FROM PLAYED MATCHES and then
  to Elo. The regulations' exact tiebreak ladder (head-to-head clauses
  included) is not reproduced; the payload reports how often the proxy
  actually decided a qualification slot so a reader can size the
  distortion.

  HOME ADVANTAGE. Not applied, same stance as the strength read:
  eloratings.net's +100 applies to a TRUE home venue and the feed
  cannot distinguish real, nominal and neutral hosting.

FORMAT (2024 edition, carried forward): two groups, single round-robin,
top two advance; semi-finals A1 v B2 and B1 v A2 over two legs, final
over two legs. When the provider publishes the real knockout fixtures
they replace the projected pairings automatically.
"""
from __future__ import annotations

import random

DRAW_RATE_EVEN = 0.163          # measured: 15/92, editions 2016-2024
DRAW_PROVENANCE = ("group-stage draws 15/92 across the 2016-2024 "
                   "editions; research_archive/"
                   "asean_draw_rates_2026-08-04.json")
N_SIMS = 20000
SIM_SEED = 26           # fixed: two callers in one cache window must agree

FINISHED = ("FT", "AET", "PEN")


def _is_group(rnd: str) -> bool:
    rl = (rnd or "").lower()
    return "group" in rl and "qualif" not in rl


def outcome_probs(e: float) -> tuple[float, float, float]:
    """(p_home, p_draw, p_away) from an Elo expectation E.

    p_draw is anchored at the measured even-ratings rate and shrinks
    linearly to zero for total mismatches; the win legs split the rest
    so the three sum to one and the points expectation stays exactly E:
    p_home + p_draw/2 = E by construction.
    """
    p_draw = DRAW_RATE_EVEN * (1.0 - abs(2.0 * e - 1.0))
    return e - p_draw / 2.0, p_draw, 1.0 - e - p_draw / 2.0


def _teams(rows: list[dict]) -> dict[int, str]:
    out = {}
    for f in rows:
        if not _is_group(f.get("round") or ""):
            continue
        for side in ("home", "away"):
            t = f.get(side) or {}
            if t.get("apif_team_id"):
                out[t["apif_team_id"]] = t.get("name") or str(t["apif_team_id"])
    return out


def _groups(rows: list[dict]) -> list[list[int]]:
    """Connected components over who-plays-whom in group rounds. The
    provider's round strings carry no group letter, but the fixture
    graph does — two teams in one group are linked by a fixture, and
    the two groups never cross until the semis."""
    adj: dict[int, set] = {}
    for f in rows:
        if not _is_group(f.get("round") or ""):
            continue
        h = (f.get("home") or {}).get("apif_team_id")
        a = (f.get("away") or {}).get("apif_team_id")
        if not h or not a:
            continue
        adj.setdefault(h, set()).add(a)
        adj.setdefault(a, set()).add(h)
    seen: set = set()
    comps = []
    for start in adj:
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        comps.append(sorted(comp))
    comps.sort(key=lambda c: min(c))
    return comps


def _table(rows: list[dict], members: list[int],
           names: dict[int, str]) -> list[dict]:
    rec = {t: {"team": names.get(t, str(t)), "team_id": t, "played": 0,
               "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "gd": 0,
               "points": 0} for t in members}
    for f in rows:
        if not _is_group(f.get("round") or ""):
            continue
        if f.get("status") not in FINISHED:
            continue
        g = f.get("goals") or {}
        if g.get("home") is None:
            continue
        h = (f.get("home") or {}).get("apif_team_id")
        a = (f.get("away") or {}).get("apif_team_id")
        if h not in rec or a not in rec:
            continue
        gh, ga = g["home"], g["away"]
        rec[h]["played"] += 1; rec[a]["played"] += 1
        rec[h]["gf"] += gh; rec[h]["ga"] += ga
        rec[a]["gf"] += ga; rec[a]["ga"] += gh
        if gh > ga:
            rec[h]["w"] += 1; rec[a]["l"] += 1; rec[h]["points"] += 3
        elif gh < ga:
            rec[a]["w"] += 1; rec[h]["l"] += 1; rec[a]["points"] += 3
        else:
            rec[h]["d"] += 1; rec[a]["d"] += 1
            rec[h]["points"] += 1; rec[a]["points"] += 1
    for r in rec.values():
        r["gd"] = r["gf"] - r["ga"]
    return sorted(rec.values(),
                  key=lambda r: (-r["points"], -r["gd"], -r["gf"],
                                 r["team"]))


def _ratings(names: dict[int, str]) -> tuple[dict[int, int], list[str]]:
    from src.live import national_elo
    out, missing = {}, []
    for tid, name in names.items():
        r = national_elo.lookup(name)
        if r.get("rated"):
            out[tid] = r["rating"]
        else:
            missing.append(f"{name}: {r.get('reason')}")
    return out, missing


def _sim_once(rng, base: dict[int, dict], remaining: list[tuple],
              groups: list[list[int]], elo: dict[int, int]) -> tuple:
    """One tournament. Returns (champion, finalists, semifinalists,
    proxy_used) — proxy_used is True when a qualification slot was
    decided by the Elo fallback rather than points or played-GD."""
    from src.live.national_elo import expected_points_share as eps
    pts = {t: base[t]["points"] for t in base}
    for h, a in remaining:
        e = eps(elo[h], elo[a])
        ph, pd, _pa = outcome_probs(e)
        u = rng.random()
        if u < ph:
            pts[h] += 3
        elif u < ph + pd:
            pts[h] += 1; pts[a] += 1
        else:
            pts[a] += 3
    qualified, proxy_used = [], False
    for members in groups:
        def key(t):
            return (-pts[t], -base[t]["gd"], -base[t]["gf"], -elo[t])
        ranked = sorted(members, key=key)
        top2 = ranked[:2]
        # did the proxy (played-GD/Elo) decide who is IN, not just order?
        if len(ranked) > 2 and pts[ranked[1]] == pts[ranked[2]]:
            proxy_used = True
        qualified.append(top2)
    (a1, a2), (b1, b2) = qualified[0], qualified[1]
    sf = [(a1, b2), (b1, a2)]
    finalists = []
    for h, a in sf:
        finalists.append(h if rng.random() < eps(elo[h], elo[a]) else a)
    f1, f2 = finalists
    champion = f1 if rng.random() < eps(elo[f1], elo[f2]) else f2
    return champion, finalists, [t for pair in qualified for t in pair], \
        proxy_used


def build(rows: list[dict]) -> dict:
    """The whole tournament payload from one season fixture list."""
    names = _teams(rows)
    groups = _groups(rows)
    out: dict = {
        "competition": "asean",
        "format": ("two groups, single round-robin; top two per group "
                   "advance to two-legged semi-finals (A1 v B2, B1 v A2), "
                   "then a two-legged final"),
        "forecast_kind": (
            "EXTERNAL-RATING FORECAST — a Monte-Carlo simulation on "
            "eloratings.net's published national Elo. No model of ours "
            "runs on this competition and no number here is a model "
            "probability, an edge, or a recommendation."),
        "assumptions": {
            "draw": (f"p_draw(E) = {DRAW_RATE_EVEN} * (1 - |2E-1|); the "
                     f"anchor is MEASURED ({DRAW_PROVENANCE}), the linear "
                     f"shape is a stated form, not a fitted curve"),
            "two_leg_ties": ("P(advance) = single-match Elo expectation; "
                             "variance shrink and near-even penalties are "
                             "folded, not modelled"),
            "tiebreak": ("points, then goal difference, then goals — and "
                         "simulated matches carry no scoreline, so ties "
                         "fall back to played-matches GD then Elo. The "
                         "regulations' head-to-head clauses are NOT "
                         "reproduced; tiebreak_proxy_share reports how "
                         "often that mattered"),
            "home_advantage": "not applied (venue truth unknowable here)",
        },
    }
    if len(groups) != 2:
        out["available"] = False
        out["reason"] = f"expected 2 groups from the fixture graph, " \
                        f"found {len(groups)} — refusing to guess a format"
        return out
    tables = [_table(rows, g, names) for g in groups]
    out["groups"] = [{"name": f"Group {'AB'[i]}", "table": t}
                     for i, t in enumerate(tables)]

    elo, missing = _ratings(names)
    if missing:
        out["available"] = False
        out["reason"] = "unrated teams: " + "; ".join(missing)
        return out
    out["elo"] = {names[t]: elo[t] for t in sorted(elo, key=lambda x: -elo[x])}

    base = {r["team_id"]: r for t in tables for r in t}
    remaining = []
    for f in rows:
        if _is_group(f.get("round") or "") and f.get("status") not in FINISHED:
            h = (f.get("home") or {}).get("apif_team_id")
            a = (f.get("away") or {}).get("apif_team_id")
            if h in base and a in base:
                remaining.append((h, a))
    out["remaining_group_matches"] = len(remaining)

    # real knockout fixtures, once the provider publishes them
    ko = [f for f in rows
          if (f.get("round") or "") and not _is_group(f["round"])
          and "qualif" not in f["round"].lower()]
    out["knockout_fixtures_published"] = len(ko)

    rng = random.Random(SIM_SEED)
    champ: dict[int, int] = {}
    fin: dict[int, int] = {}
    semi: dict[int, int] = {}
    # per-SLOT occupancy — who lands A1/A2/B1/B2, and who wins each semi.
    # _sim_once returns the semifinalist list in fixed slot order and the
    # finalist list as [SF1 winner, SF2 winner]; both orders are pinned by
    # its construction (qualified[0] is groups[0]'s top two).
    slots: dict[str, dict[int, int]] = {k: {} for k in
                                        ("A1", "A2", "B1", "B2")}
    sfw: list[dict[int, int]] = [{}, {}]
    proxy_n = 0
    for _ in range(N_SIMS):
        c, fs, ss, proxy = _sim_once(rng, base, remaining, groups, elo)
        champ[c] = champ.get(c, 0) + 1
        for t in fs:
            fin[t] = fin.get(t, 0) + 1
        for t in ss:
            semi[t] = semi.get(t, 0) + 1
        for slot, t in zip(("A1", "A2", "B1", "B2"), ss):
            slots[slot][t] = slots[slot].get(t, 0) + 1
        for i, t in enumerate(fs):
            sfw[i][t] = sfw[i].get(t, 0) + 1
        proxy_n += proxy
    out["n_sims"] = N_SIMS
    out["sim_seed"] = SIM_SEED
    out["tiebreak_proxy_share"] = round(proxy_n / N_SIMS, 4)
    out["champion_forecast"] = [
        {"team": names[t],
         "p_champion": round(champ.get(t, 0) / N_SIMS, 4),
         "p_final": round(fin.get(t, 0) / N_SIMS, 4),
         "p_semis": round(semi.get(t, 0) / N_SIMS, 4)}
        for t in sorted(names,
                        key=lambda x: (-champ.get(x, 0), -fin.get(x, 0),
                                       -semi.get(x, 0)))]
    lead = out["champion_forecast"][0]
    out["champion"] = None          # crowned only by the real final
    out["champion_forecast_leader"] = {"team": lead["team"],
                                       "p": lead["p_champion"]}

    def dist(counter: dict[int, int], top: int = 3) -> list[dict]:
        return [{"team": names[t], "p": round(n / N_SIMS, 4)}
                for t, n in sorted(counter.items(),
                                   key=lambda kv: -kv[1])[:top]]

    if ko:
        out["bracket"] = _real_bracket(ko, names, elo)
    else:
        out["bracket"] = {
            "projected": True,
            "basis": ("slots filled by simulating the remaining group "
                      "matches from current standings — the pairings are "
                      "NOT drawn until the groups finish, and the real "
                      "knockout fixtures replace these the moment the "
                      "provider publishes them"),
            "semifinals": [
                {"name": "SF1",
                 "home_slot": {"label": "Group A winner",
                               "dist": dist(slots["A1"])},
                 "away_slot": {"label": "Group B runner-up",
                               "dist": dist(slots["B2"])},
                 "winner_dist": dist(sfw[0])},
                {"name": "SF2",
                 "home_slot": {"label": "Group B winner",
                               "dist": dist(slots["B1"])},
                 "away_slot": {"label": "Group A runner-up",
                               "dist": dist(slots["A2"])},
                 "winner_dist": dist(sfw[1])},
            ],
            "final": {
                "home_slot": {"label": "SF1 winner", "dist": dist(sfw[0])},
                "away_slot": {"label": "SF2 winner", "dist": dist(sfw[1])},
            },
        }
    out["available"] = True
    return out


def _real_bracket(ko: list[dict], names: dict[int, str],
                  elo: dict[int, int]) -> dict:
    """The knockout picture from PUBLISHED fixtures — real pairings, real
    kickoffs, legs and aggregates as they land. p_advance stays the Elo
    expectation (the stated two-leg simplification); no winner is ever
    derived from an aggregate here, because the away-goals and shootout
    clauses live in regulations this module does not reproduce."""
    from src.live.national_elo import expected_points_share as eps

    def ties_for(rnd_word: str) -> list[dict]:
        legs_by_pair: dict[frozenset, list[dict]] = {}
        for f in ko:
            if rnd_word not in (f.get("round") or "").lower():
                continue
            h = (f.get("home") or {}).get("apif_team_id")
            a = (f.get("away") or {}).get("apif_team_id")
            if h and a:
                legs_by_pair.setdefault(frozenset((h, a)), []).append(f)
        cards = []
        for pair, legs in legs_by_pair.items():
            legs.sort(key=lambda f: f.get("kickoff_utc") or "")
            t1, t2 = sorted(pair, key=lambda t: names.get(t, ""))
            agg = {t1: 0, t2: 0}
            settled = 0
            for f in legs:
                g = f.get("goals") or {}
                if f.get("status") in FINISHED and g.get("home") is not None:
                    settled += 1
                    hid = (f.get("home") or {}).get("apif_team_id")
                    agg[hid] += g["home"]
                    agg[t2 if hid == t1 else t1] += g["away"]
            e = eps(elo[t1], elo[t2]) if t1 in elo and t2 in elo else None
            cards.append({
                "teams": [
                    {"team": names[t1],
                     "p_advance": round(e, 4) if e is not None else None},
                    {"team": names[t2],
                     "p_advance": round(1 - e, 4) if e is not None else None},
                ],
                "legs": [{"kickoff_utc": f.get("kickoff_utc"),
                          "status": f.get("status"),
                          "home": (f.get("home") or {}).get("name"),
                          "away": (f.get("away") or {}).get("name"),
                          "goals": f.get("goals")} for f in legs],
                "aggregate": (f"{agg[t1]}-{agg[t2]}" if settled else None),
                "legs_settled": settled,
            })
        cards.sort(key=lambda c: (c["legs"][0]["kickoff_utc"] or ""))
        return cards

    return {
        "projected": False,
        "basis": ("published knockout fixtures. p_advance is the Elo "
                  "expectation under the stated two-leg simplification; "
                  "no winner is derived from an aggregate here"),
        "semifinals": ties_for("semi"),
        "final_ties": ties_for("final"),
    }
