#!/usr/bin/env python
"""Analyze an exported MLS shadow corpus — WITHOUT a database.

The V8.1 evaluation Phase 3 acceptance test: a researcher downloads the
corpus, runs ONE command, and regenerates the report. This script reads
only the corpus files (it never opens the live database) and:

  1. verifies every file's sha256 and the manifest_hash;
  2. REPLAYS each prediction run from its stored input artifact and
     confirms it reproduces the stored contract probabilities;
  3. scores forecast quality (log loss, Brier) for runs whose fixture
     has a settled result;
  4. compares the model 3-way to the frozen lock book (implied prices);
  5. echoes the audit counts, including missed locks and failed
     snapshots (no survivorship bias).

Usage:
    python scripts/analyze_corpus.py <corpus_dir> [--json]

Model replay uses the committed model code (src.models), which is part
of the repository, not the production database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys

THREE_WAY = ("home_win", "draw", "away_win")


def _load(corpus_dir, name):
    with open(os.path.join(corpus_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


def verify_hashes(corpus_dir, manifest) -> list[str]:
    problems = []
    for name, meta in manifest["files"].items():
        data = _load(corpus_dir, name)
        body = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if hashlib.sha256(body.encode()).hexdigest() != meta["sha256"]:
            problems.append(f"file hash mismatch: {name}")
    # `competition_slug` joined the hashed core when the corpus became
    # competition-scoped (EPL P0-2): two leagues can export identical
    # counts and file hashes, and a manifest that did not commit to WHICH
    # competition it described could not tell them apart. Absent on
    # corpora published before the scoping, which still verify.
    core_fields = {"files": manifest["files"],
                   "counts": manifest["counts"],
                   "corpus_version": manifest["corpus_version"],
                   "schema_version": manifest["schema_version"]}
    if "competition_slug" in manifest:
        core_fields["competition_slug"] = manifest["competition_slug"]
    core = json.dumps(core_fields, sort_keys=True)
    if hashlib.sha256(core.encode()).hexdigest() != manifest["manifest_hash"]:
        problems.append("manifest_hash mismatch")
    return problems


def verify_provenance(corpus_dir) -> dict:
    """Verify the EVIDENCE CHAIN, not just the files we were handed.

    V9.5 eval H5. `verify_hashes` proves the supplied sections are
    internally manifest-consistent, and the replay proves current code
    can reproduce the stored probabilities from the supplied fields.
    Neither proves the chain is authentic: the evaluator built a corpus
    carrying a bogus engine signature, a bogus artifact content hash and
    a bogus run input_snapshot_hash, recomputed the section and manifest
    hashes consistently, and this script still reported
    `integrity_problems: []` with 309/309 reproduced and max_delta 0.0.

    Reported as its own dimension, because "the numbers reproduce" and
    "the provenance is intact" are different claims and collapsing them
    into one green tick is exactly how a tampered corpus passes.

    Two classes, kept apart for the same reason lock validity was
    separated from deployment state: FORGERY is a hash that does not
    recompute, and it fails the command. DRIFT is a historical fact —
    a legacy decision that predates content documents, or a run whose
    engine signature differs from its approval's because the repo moved
    (the signature includes code_revision, so every deploy changes it).
    Drift is reported, never treated as tampering; a check that is red
    on every honest corpus teaches people to ignore it."""
    problems: list[dict] = []
    observations: list[dict] = []

    def fail(code, detail):
        problems.append({"code": code, "detail": detail})

    def note(code, detail):
        observations.append({"code": code, "detail": detail})

    arts = {a["id"]: a for a in
            _load(corpus_dir, "model_input_artifacts.json")}
    runs = _load(corpus_dir, "prediction_runs.json")
    decisions = {d["id"]: d for d in
                 _load(corpus_dir, "model_approval_decisions.json")}
    manifest = _load(corpus_dir, "manifest.json")

    # 1. every artifact's content_hash must hash its own document
    for aid, a in arts.items():
        doc = a.get("document_json")
        if not doc:
            fail("ARTIFACT_DOCUMENT_MISSING", f"artifact {aid}")
            continue
        if hashlib.sha256(doc.encode()).hexdigest() != a.get("content_hash"):
            fail("ARTIFACT_CONTENT_HASH_MISMATCH", f"artifact {aid}")

    # 2. each run must point at the artifact its input_snapshot_hash names
    engine_seen: dict[str, int] = {}
    for r in runs:
        a = arts.get(r.get("model_input_artifact_id"))
        if a is None:
            continue
        if r.get("input_snapshot_hash") != a.get("content_hash"):
            fail("RUN_ARTIFACT_HASH_MISMATCH", f"run {r.get('id')}")
        try:
            eng = ((json.loads(a["document_json"]).get("engine") or {})
                   .get("signature_hash"))
        except (ValueError, TypeError):
            eng = None
        if eng:
            engine_seen[eng] = engine_seen.get(eng, 0) + 1

    # 3. an approval's content_hash must recompute from its document
    for did, d in decisions.items():
        doc = d.get("decision_document")
        if not doc:
            # pre-V9.1.2 rows stored the hash but not the document it
            # covers. Unverifiable, but historical — not forgery.
            note("APPROVAL_DOCUMENT_MISSING", f"decision {did}")
            continue
        if hashlib.sha256(doc.encode()).hexdigest() != d.get("content_hash"):
            fail("APPROVAL_HASH_MISMATCH", f"decision {did}")
            continue
        try:
            parsed = json.loads(doc)
        except (ValueError, TypeError):
            fail("APPROVAL_DOCUMENT_UNPARSEABLE", f"decision {did}")
            continue
        # 4. an approval that names a corpus must name THIS one
        cv = parsed.get("corpus_version")
        if cv and cv != manifest.get("corpus_version"):
            fail("APPROVAL_CORPUS_MISMATCH", f"decision {did} -> {cv}")
        ch = parsed.get("corpus_manifest_hash")
        if ch and ch != manifest.get("manifest_hash"):
            fail("APPROVAL_CORPUS_MISMATCH",
                 f"decision {did} manifest {str(ch)[:12]}")
        # 5. and must approve the model the runs actually used
        if d.get("model_version_name") and parsed.get("model_version") \
                and d["model_version_name"] != parsed["model_version"]:
            fail("APPROVAL_MODEL_MISMATCH", f"decision {did}")

    # 6. a run's artifact engine must match the decision that authorised it
    for r in runs:
        d = decisions.get(r.get("model_approval_decision_id"))
        a = arts.get(r.get("model_input_artifact_id"))
        if not (d and d.get("decision_document") and a
                and a.get("document_json")):
            continue
        try:
            want = json.loads(d["decision_document"]).get("engine_signature")
            got = ((json.loads(a["document_json"]).get("engine") or {})
                   .get("signature_hash"))
        except (ValueError, TypeError):
            continue
        if want and got and got != want:
            # the signature includes code_revision, so this fires
            # whenever the repo moved between the approval and the run.
            # Worth surfacing as governance drift; not evidence of a
            # forged chain.
            note("ENGINE_SIGNATURE_DRIFT",
                 f"run {r.get('id')}: artifact {str(got)[:12]} != "
                 f"approval {str(want)[:12]}")

    return {
        "artifacts_checked": len(arts),
        "runs_checked": len(runs),
        "decisions_checked": len(decisions),
        "distinct_engine_signatures": len(engine_seen),
        "problem_codes": sorted({p["code"] for p in problems}),
        "problems": problems[:50],
        "observation_codes": sorted({o["code"] for o in observations}),
        "observations": observations[:50],
        "chain_intact": not problems,
        "note": ("provenance is verified SEPARATELY from numerical "
                 "reproduction — a corpus can replay perfectly and still "
                 "carry a forged chain (V9.5 eval H5)"),
    }


def replay_report(corpus_dir) -> dict:
    from src.live.model_mls import replay_from_artifact
    runs = _load(corpus_dir, "prediction_runs.json")
    arts = {a["id"]: a for a in
            _load(corpus_dir, "model_input_artifacts.json")}
    contracts = _load(corpus_dir, "prediction_contracts.json")
    by_run: dict = {}
    for c in contracts:
        if c["outcome_key"] in THREE_WAY:
            by_run.setdefault(c["prediction_run_id"], {})[
                c["outcome_key"]] = c["raw_probability"]
    checked = reproduced = 0
    worst = 0.0
    for r in runs:
        aid = r.get("model_input_artifact_id")
        if aid is None or aid not in arts:
            continue
        doc = json.loads(arts[aid]["document_json"])
        out = replay_from_artifact(doc)
        if out is None:
            continue
        stored = by_run.get(r["id"], {})
        if set(stored) != set(THREE_WAY):
            continue
        delta = max(abs(out[k] - stored[k]) for k in THREE_WAY)
        checked += 1
        worst = max(worst, delta)
        if delta <= 1e-6:
            reproduced += 1
    return {"runs_checked": checked, "reproduced": reproduced,
            "max_delta": worst,
            "all_reproduced": checked > 0 and reproduced == checked}


def forecast_report(corpus_dir) -> dict:
    fixtures = {f["espn_event_id"]: f
                for f in _load(corpus_dir, "fixtures.json")}
    runs = _load(corpus_dir, "prediction_runs.json")
    fix_by_id = {f["id"]: f for f in fixtures.values()}
    contracts = _load(corpus_dir, "prediction_contracts.json")
    by_run: dict = {}
    for c in contracts:
        if c["outcome_key"] in THREE_WAY:
            by_run.setdefault(c["prediction_run_id"], {})[
                c["outcome_key"]] = c["raw_probability"]
    # one scored row per fixture: prefer the canonical t10 lock
    best: dict = {}
    for r in runs:
        f = fix_by_id.get(r["fixture_id"])
        if not f or f.get("status") != "post":
            continue
        if f.get("home_goals") is None or f.get("away_goals") is None:
            continue
        p = by_run.get(r["id"])
        if not p or set(p) != set(THREE_WAY):
            continue
        rank = 2 if (r["run_type"] == "t10" and r["canonical"]) else 1
        cur = best.get(r["fixture_id"])
        if cur is None or rank > cur[0]:
            best[r["fixture_id"]] = (rank, p, f)
    ll = brier = n = hits = 0.0
    for _rank, p, f in best.values():
        hg, ag = f["home_goals"], f["away_goals"]
        result = ("home_win" if hg > ag else
                  "away_win" if ag > hg else "draw")
        q = max(min(p[result], 1 - 1e-9), 1e-9)
        ll += -math.log(q)
        brier += sum((p[k] - (1.0 if k == result else 0.0)) ** 2
                     for k in THREE_WAY)
        hits += 1.0 if max(p, key=p.get) == result else 0.0
        n += 1
    if n == 0:
        return {"scored_fixtures": 0,
                "note": "no settled fixtures with runs yet"}
    return {"scored_fixtures": int(n),
            "log_loss": round(ll / n, 4),
            "brier": round(brier / n, 4),
            "winner_hit_rate": round(hits / n, 4)}


def market_report(corpus_dir) -> dict:
    """Model 3-way vs the frozen lock book (normalized yes-ask implied),
    per canonical lock — the prospective model-vs-executable comparison
    the whole platform exists to build."""
    runs = _load(corpus_dir, "prediction_runs.json")
    contracts = _load(corpus_dir, "prediction_contracts.json")
    quotes = {q["id"]: q for q in _load(corpus_dir,
                                        "market_quotes.json")}
    locks = [r for r in runs if r["run_type"] == "t10"
             and r["canonical"] and r["status"] == "complete"]
    lock_ids = {r["id"] for r in locks}
    rows = 0
    for c in contracts:
        if (c["prediction_run_id"] in lock_ids
                and c["outcome_key"] in THREE_WAY
                and c.get("market_quote_id") in quotes):
            rows += 1
    return {"canonical_locks": len(locks),
            "model_to_frozen_quote_links": rows,
            "note": ("market-vs-model comparison is computable from the "
                     "frozen quotes; full scoring lands with settlement")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus_dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    # make src importable when run from the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    manifest = _load(args.corpus_dir, "manifest.json")
    report = {
        "corpus_version": manifest["corpus_version"],
        "schema_version": manifest["schema_version"],
        "manifest_hash": manifest["manifest_hash"],
        "integrity_problems": verify_hashes(args.corpus_dir, manifest),
        # V9.5 eval H5: a SEPARATE dimension. File hashes prove the
        # bundle is self-consistent; this proves the chain behind it is
        # authentic. A forged corpus passes the first and fails this.
        "provenance": verify_provenance(args.corpus_dir),
        "counts": manifest["counts"],
        "reproducibility": replay_report(args.corpus_dir),
        "forecast": forecast_report(args.corpus_dir),
        "market": market_report(args.corpus_dir),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\n  Corpus {report['corpus_version']} "
              f"({report['schema_version']})")
        print(f"  manifest_hash {report['manifest_hash'][:16]}")
        print(f"  integrity: {'CLEAN' if not report['integrity_problems'] else report['integrity_problems']}")
        pv = report["provenance"]
        print(f"  provenance: {'CHAIN INTACT' if pv['chain_intact'] else pv['problem_codes']}"
              f"{'  drift: ' + str(pv['observation_codes']) if pv['observation_codes'] else ''}"
              f"  ({pv['artifacts_checked']} artifacts · "
              f"{pv['decisions_checked']} decisions)")
        c = report["counts"]
        print(f"  fixtures {c['fixtures']} · runs {c['prediction_runs']} "
              f"· canonical locks {c['canonical_locks']} · "
              f"missed {c['missed_locks']} · failed snaps "
              f"{c['failed_snapshots']}")
        rp = report["reproducibility"]
        print(f"  reproducibility: {rp['reproduced']}/{rp['runs_checked']} "
              f"runs replay exactly (max delta {rp['max_delta']})")
        print(f"  forecast: {report['forecast']}")
        print(f"  market: {report['market']}\n")
    # fail on EITHER dimension — file integrity or chain provenance
    return 1 if (report["integrity_problems"]
                 or not report["provenance"]["chain_intact"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
