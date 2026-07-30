"""Prior-season REPLAY ladders and their approval path (backlog S-5).

All offline: the archived research bytes are the input, so nothing here
touches the network. The archives are committed, which is what makes
these tests reproducible rather than dependent on ESPN staying up.

The guards that matter here are the ones about what an approval MAY NOT
do, and each was run against the code before the guard existed to confirm
it actually fails. A guard that passes both ways is a control, not
evidence, and is labelled as such.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from src.live import ladder_replay, model_eval

UTC = timezone.utc


class TestStage1Gate:
    """S-5's falsifier. Incomplete history must KILL the replay, and no
    count may be reported without its denominator."""

    def test_every_registered_league_has_complete_history(self):
        for slug, source in ladder_replay.REPLAY_SOURCES.items():
            doc, sha = ladder_replay.load_archive(source)
            gate = ladder_replay.completeness(doc, source)
            assert gate["gate"] == "PASS", f"{slug}: {gate}"
            for seg, v in gate["per_segment"].items():
                # the denominator is always present and the ratio derived
                # from it — never a bare count
                assert v["expected"] > 0
                assert v["ratio"] == round(v["retrieved"] / v["expected"], 4)
            assert len(sha) == 64

    def test_liga_mx_keeps_apertura_and_clausura_distinct(self):
        """Two tournaments, never one table. ESPN reports season.year 2025
        for BOTH, so a year-based split would silently merge them."""
        source = ladder_replay.REPLAY_SOURCES["liga-mx-2026"]
        doc, _ = ladder_replay.load_archive(source)
        split = ladder_replay.segments(doc, source)
        assert set(split["segments"]) == {"torneo-apertura",
                                          "torneo-clausura"}
        assert len(split["segments"]["torneo-apertura"]) == 153
        assert len(split["segments"]["torneo-clausura"]) == 153
        years = {e["season_year"] for seg in split["segments"].values()
                 for e in seg}
        # the year cannot be the discriminator — this is why the slug is
        assert years == {2025}

    def test_knockout_and_exhibition_events_are_excluded_and_counted(self):
        """Liguilla, MLS playoffs and the All-Star game are not league
        fixtures. They must be reported as exclusions, not dropped
        silently and not mixed into a league table."""
        for slug in ("liga-mx-2026", "mls-2026"):
            source = ladder_replay.REPLAY_SOURCES[slug]
            doc, _ = ladder_replay.load_archive(source)
            gate = ladder_replay.completeness(doc, source)
            assert gate["excluded_non_regular_season_total"] > 0
            assert gate["excluded_non_regular_season"]
        mls = ladder_replay.completeness(
            *ladder_replay.load_archive(
                ladder_replay.REPLAY_SOURCES["mls-2026"])[:1],
            ladder_replay.REPLAY_SOURCES["mls-2026"])
        assert "all-star-game" in mls["excluded_non_regular_season"]

    def test_an_incomplete_season_is_refused_not_fitted(self, monkeypatch):
        """THE GATE ITSELF. Truncate the history and the replay must refuse
        rather than fit a partial season."""
        source = ladder_replay.REPLAY_SOURCES["epl-2026"]
        doc, sha = ladder_replay.load_archive(source)
        half = {**doc, "events": doc["events"][:150]}
        monkeypatch.setattr(ladder_replay, "load_archive",
                            lambda s: (half, sha))
        gate = ladder_replay.completeness(half, source)
        assert gate["gate"] == "FAIL"
        assert gate["per_segment"][
            "2025-26-english-premier-league"]["ratio"] < 0.98
        out = ladder_replay.run_replay("epl-2026", n_boot=10)
        assert out["refused"] is True
        assert "ladders" not in out
        # and no approval may be computed from a killed replay
        res = ladder_replay.ensure_replay_approval(
            "epl-2026", n_boot=10, allow_create=True)
        assert res["approved"] is False and res["refused"] is True

    def test_winner_flag_disagreements_are_all_knockout_ties(self):
        """The ESPN semantic invariant (derive W/L/D from the numbers
        beside it). Disagreements are expected in knockout ties, where the
        flag means the TIE not the scoreline — but a disagreement inside a
        regular-season segment would mean the score parse is wrong."""
        for slug, source in ladder_replay.REPLAY_SOURCES.items():
            doc, _ = ladder_replay.load_archive(source)
            split = ladder_replay.segments(doc, source)
            for seg, rows in split["segments"].items():
                bad = [e for e in rows if not e["winner_flag_agrees"]]
                assert not bad, f"{slug}/{seg}: {len(bad)} disagreements"


class TestEvidenceClass:
    """REPLAYED must be impossible to omit, and impossible to confuse with
    a prospective result."""

    def test_every_replay_artifact_is_labelled_replayed(self):
        out = ladder_replay.run_replay("epl-2026", n_boot=10)
        assert out["evidence_class"] == "REPLAYED"
        assert "REPLAYED" in out["evidence_note"] or \
            "MEASURED" in out["evidence_note"]
        assert "prospective" in out["evidence_note"]

    def test_evidence_block_says_it_is_not_comparable(self):
        rep = ladder_replay.run_replay("epl-2026", n_boot=10)
        ev = ladder_replay.replay_evidence_block("epl-2026", rep)
        assert ev["evidence_class"] == "REPLAYED"
        assert ev["replay_scope"] == "prior_season"
        assert "not_comparable_to" in ev
        assert ev["archive_sha256"] and len(ev["archive_sha256"]) == 64

    def test_replay_decision_rung_is_never_the_xg_rung(self):
        """The archive carries no provider xG, so an M3-labelled replay
        decision would claim a measurement that was never made."""
        assert ladder_replay.REPLAY_DECISION_RUNG == "M2"
        # MLS's deployed variant IS M3 (xG alpha > 0), which is exactly the
        # mislabel this constant exists to prevent
        mls = model_eval.ladder_spec("mls-2026")
        assert model_eval.deployed_variant(mls) == "M3"
        prev = ladder_replay.approval_preview("mls-2026", n_boot=10)
        assert prev["evaluated_rung"] == "M2"


class TestReplayApprovalPolicy:
    """The replay bar is STRICTER than the live-plane bar, and a league
    that does not clear it stays dark."""

    def _report(self, delta, ci, significant=False, n=300):
        """A ladder report carrying the same edge under every rung key.

        The two policies read DIFFERENT rungs — the replay policy always
        reads M2, while the live policy reads MLS's deployed variant (M3,
        since MLS_XG_RATING_ALPHA > 0). Keying the edge under both is what
        makes the strictness comparison apples-to-apples rather than one
        policy simply failing to find its key.
        """
        edge = {"delta_log_loss": delta, "ci95": ci,
                "significant": significant}
        return {"n_scored": n,
                "variants": {r: {"log_loss": 1.0}
                             for r in ("M2", "M2C", "M3")},
                "edges": {f"{r}_vs_M0": dict(edge)
                          for r in ("M2", "M2C", "M3")}}

    def test_a_league_whose_m2_does_not_beat_m0_is_refused(self):
        spec = model_eval.ladder_spec("epl-2026")
        ok, why = model_eval.replay_approval_policy(
            self._report(-0.004, [-0.030, 0.021]), spec)
        assert ok is False
        assert "does not beat" in why and "dark" in why

    def test_exactly_zero_is_refused(self):
        spec = model_eval.ladder_spec("epl-2026")
        ok, _ = model_eval.replay_approval_policy(
            self._report(0.0, [-0.02, 0.02]), spec)
        assert ok is False

    def test_the_live_policy_would_have_APPROVED_what_replay_refuses(self):
        """The two policies must genuinely differ, or 'stricter' is a
        claim rather than a fact. A CI spanning zero with a slightly
        NEGATIVE point estimate is approvable for shadow on the live plane
        and refused on a replay."""
        borderline = self._report(-0.004, [-0.030, 0.021])
        spec = model_eval.ladder_spec("epl-2026")
        live_ok, _ = model_eval.shadow_approval_policy(borderline)
        replay_ok, _ = model_eval.replay_approval_policy(borderline, spec)
        assert live_ok is True
        assert replay_ok is False

    def test_a_positive_edge_is_approved_but_never_called_established(self):
        spec = model_eval.ladder_spec("epl-2026")
        ok, why = model_eval.replay_approval_policy(
            self._report(0.033, [0.020, 0.048], significant=True), spec)
        assert ok is True
        assert "NOT an established edge" in why
        assert "NOT" in why and "comparable" in why

    def test_a_small_sample_is_refused_regardless_of_edge(self):
        spec = model_eval.ladder_spec("epl-2026")
        ok, why = model_eval.replay_approval_policy(
            self._report(0.5, [0.4, 0.6], significant=True, n=12), spec)
        assert ok is False and "insufficient scored sample" in why


class TestApprovalIsFailClosed:
    """Nothing but an explicit operator call may create an approval."""

    def test_ensure_replay_approval_defaults_to_no_creation(self,
                                                            monkeypatch):
        """THE FAIL-CLOSED DEFAULT. Called without allow_create it must
        refuse and report the activation route, never mint a decision."""
        calls = []
        monkeypatch.setattr(
            model_eval, "plane_ready", lambda: True)
        monkeypatch.setattr(
            model_eval, "_active_decision", lambda spec=None: None)

        def _no_create(**kw):
            calls.append(kw)
            return None
        spec = model_eval.ladder_spec("epl-2026")
        monkeypatch.setattr(spec.model, "ensure_model_version",
                            lambda approved_for_shadow=False: calls.append(
                                {"approved": approved_for_shadow}))
        res = ladder_replay.ensure_replay_approval("epl-2026", n_boot=10)
        assert res["approved"] is False
        assert res["fail_closed"] is True
        assert "replay-approval/activate" in res["reason"]
        # the only model_version write was the DARK one
        assert calls and all(c.get("approved") is False for c in calls)

    def test_mls_can_never_get_a_replay_approval(self):
        """MLS's approval is earned on its LIVE plane. A prior-season
        replay must never mint a second decision under the same model
        name — that would file REPLAYED evidence beside a live decision."""
        assert "mls-2026" in ladder_replay.APPROVAL_FORBIDDEN
        res = ladder_replay.ensure_replay_approval(
            "mls-2026", n_boot=10, allow_create=True, force=True)
        assert res["approved"] is False
        assert res["forbidden"] is True

    def test_preview_persists_nothing(self):
        prev = ladder_replay.approval_preview("epl-2026", n_boot=10)
        assert prev["persisted"] is False
        assert "activation_route" in prev


class TestLigaMxSplitSeason:
    """The question its builder flagged, answered on evidence."""

    def test_verdict_rests_on_the_difference_in_differences(self):
        v = ladder_replay.split_season_verdict("liga-mx-2026", n_boot=300)
        assert "difference_in_differences" in v
        assert v["difference_in_differences"]["control_rung"] == "M0"
        assert "CONFOUNDED" in v["raw_comparison_caveat"]

    def test_the_comparison_is_paired_on_a_shared_fixture_set(self):
        """Spanning scores MORE fixtures than resetting (it can predict
        Clausura's opening rounds), so an unpaired comparison would
        confound the policy with a bigger sample."""
        v = ladder_replay.split_season_verdict("liga-mx-2026", n_boot=300)
        assert v["n_scored_span_only"] > v["n_scored_reset_only"]
        assert v["n_paired"] == v["n_scored_reset_only"]
        assert str(v["n_paired"]) in v["denominator_note"]
        assert str(v["n_scored_span_only"]) in v["denominator_note"]

    def test_a_single_segment_league_is_not_applicable(self):
        v = ladder_replay.split_season_verdict("epl-2026")
        assert v["not_applicable"] is True

    def test_indistinguishable_picks_the_simpler_policy(self):
        v = ladder_replay.split_season_verdict("liga-mx-2026", n_boot=300)
        if v["verdict"] == "INDISTINGUISHABLE":
            assert "SIMPLER" in v["verdict_meaning"]
            assert "spanning" in v["verdict_meaning"]

    def test_segment_reset_prior_never_crosses_the_boundary(self):
        """The reset policy's history slice is the load-bearing mechanism;
        prove it actually excludes the other tournament."""
        source = ladder_replay.REPLAY_SOURCES["liga-mx-2026"]
        doc, _ = ladder_replay.load_archive(source)
        split = ladder_replay.segments(doc, source)
        rows = ladder_replay._rows(
            [e for s in source.regular_slugs
             for e in split["segments"].get(s, [])])
        # a late Clausura fixture: spanning sees Apertura, resetting must not
        idx = len(rows) - 1
        assert rows[idx].segment == "torneo-clausura"
        spanning = rows[:idx]
        reset = ladder_replay._segment_prior_fn(rows, idx)
        assert any(f.segment == "torneo-apertura" for f in spanning)
        assert all(f.segment == "torneo-clausura" for f in reset)
        assert len(reset) < len(spanning)


class TestHyperparameterSweep:
    def test_sweep_reports_the_carried_value_beside_the_best(self):
        s = ladder_replay.sweep("epl-2026",
                                half_lives=(90.0,), shrinks=(6.0, 24.0))
        assert s["carried_from_mls"]["shrink_games"] == 24.0
        assert s["carried_result"] is not None
        assert "DIAGNOSTIC ONLY" in s["caveat"]

    def test_approval_does_not_use_swept_values(self):
        """The approval must be computed on the CARRIED constants, so no
        decision is inflated by a hyperparameter chosen on its own
        evidence (V9.3 F8)."""
        prev = ladder_replay.approval_preview("epl-2026", n_boot=10)
        spec = model_eval.ladder_spec("epl-2026")
        assert spec.ladder["M2"]["shrink"] == 24.0
        rep = ladder_replay.run_replay("epl-2026", n_boot=10,
                                       segment_mode="span")
        assert rep["hyperparameters"]["half_life_days"] == 90.0
        assert "NOT re-swept" in rep["hyperparameters"]["provenance"]
        assert prev["n_scored"] == rep["ladders"][
            "ALL_SEGMENTS_SPANNING"]["n_scored"]


class TestLaLigaDropsInWithARegistryEntry:
    """The abstraction's real test: La Liga lives on another branch, so its
    model module is absent here. It must be registered, skipped cleanly,
    and its history must already be archived and gated."""

    def test_la_liga_history_is_archived_and_passes_stage_1(self):
        source = ladder_replay.REPLAY_SOURCES["la-liga-2026"]
        doc, _ = ladder_replay.load_archive(source)
        gate = ladder_replay.completeness(doc, source)
        assert gate["gate"] == "PASS"
        assert gate["per_segment"]["2025-26-laliga"]["retrieved"] == 380

    def test_absent_model_module_is_skipped_not_fatal(self):
        assert "la-liga-2026" not in model_eval.ladder_specs()
        agg_leagues = ladder_replay.aggregate_report(n_boot=5)["leagues"]
        assert "skipped" in agg_leagues["la-liga-2026"]
        assert "not on this branch" in agg_leagues["la-liga-2026"]["skipped"]

    def test_the_registry_row_exists_so_only_a_module_is_missing(self):
        rows = {slug for slug, *_ in model_eval._DARK_LEAGUE_REGISTRY}
        assert "la-liga-2026" in rows


class TestReplayEndpoints:
    """The generalized per-competition surface. One activation route,
    operator-gated; the read paths persist nothing."""

    @pytest.fixture()
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        from api.main import app
        monkeypatch.setattr(config, "PUBLIC_READ_ONLY", False)
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 0)
        monkeypatch.setattr(config, "ADMIN_TOKEN", "test-operator-token")
        return TestClient(app)

    def test_activation_requires_operator_credentials(self, client):
        r = client.post("/api/admin/epl-2026/replay-approval/activate")
        assert r.status_code == 403

    def test_activation_refuses_mls_even_with_credentials(self, client):
        r = client.post("/api/admin/mls-2026/replay-approval/activate",
                        headers={"X-Admin-Token": "test-operator-token"})
        assert r.status_code == 409
        assert "LIVE-PLANE" in r.json()["detail"]

    def test_unknown_competition_is_404(self, client):
        assert client.get("/api/nonsense/replay-ladder").status_code == 404
        r = client.post("/api/admin/nonsense/replay-approval/activate",
                        headers={"X-Admin-Token": "test-operator-token"})
        assert r.status_code == 404

    def test_registered_league_without_a_module_says_so(self, client):
        """La Liga: history archived and gated, model module elsewhere.
        That is a different fact from 'this endpoint is broken', and the
        status code must not conflate them."""
        r = client.get("/api/la-liga-2026/replay-ladder",
                       params={"n_boot": 5})
        assert r.status_code == 409
        assert "no model module" in r.json()["detail"]

    def test_preview_is_public_and_labelled_replayed(self, client):
        r = client.get("/api/epl-2026/replay-approval/preview",
                       params={"n_boot": 20})
        assert r.status_code == 200
        body = r.json()
        assert body["evidence_class"] == "REPLAYED"
        assert body["persisted"] is False
        assert body["policy_version"] == "shadow-approval-replay-v1"

    def test_split_season_endpoint_reports_the_did(self, client):
        r = client.get("/api/liga-mx-2026/split-season",
                       params={"n_boot": 20})
        assert r.status_code == 200
        assert "difference_in_differences" in r.json()

    def test_mls_approval_surface_is_untouched(self, client):
        """The MLS endpoints must keep working exactly as before — the
        refactor's whole premise."""
        assert client.get("/api/mls/approval").status_code == 200

    def test_replay_routes_are_rate_limited(self, client, monkeypatch):
        """Same cost class as /api/mls/model-eval. The competition is a
        path parameter, so the prefix list cannot cover it — this proves
        the substring bucket actually fires."""
        monkeypatch.setattr(config, "RATE_LIMIT_SECONDS", 300)
        from api import main as api_main
        monkeypatch.setattr(api_main, "_rate_last", {})
        first = client.get("/api/epl-2026/replay-ladder",
                           params={"n_boot": 5})
        second = client.get("/api/epl-2026/replay-ladder",
                            params={"n_boot": 5})
        assert first.status_code == 200
        assert second.status_code == 429


class TestMlsControl:
    """MLS is the methodological control: the same replay machinery on the
    one league whose live-plane answer is already known."""

    def test_mls_prior_season_replay_runs_and_is_labelled_control(self):
        rep = ladder_replay.run_replay("mls-2026", n_boot=50,
                                       segment_mode="span")
        L = rep["ladders"]["ALL_SEGMENTS_SPANNING"]
        assert L["n_scored"] > 400
        assert rep["evidence_class"] == "REPLAYED"

    def test_mls_replay_m3_rung_carries_no_provider_xg(self):
        """The MLS ladder has an M3 rung, but the archive has no xG — so
        M3 must come out numerically identical to M2C. If it ever differs,
        xG leaked in from somewhere and the replay is not what it says."""
        rep = ladder_replay.run_replay("mls-2026", n_boot=10,
                                       segment_mode="span")
        v = rep["ladders"]["ALL_SEGMENTS_SPANNING"]["variants"]
        assert v["M3"]["log_loss"] == v["M2C"]["log_loss"]
        assert v["M3"]["brier"] == v["M2C"]["brier"]
