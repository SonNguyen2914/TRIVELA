"""A deploy is not an engine change.

`engine_signature()` hashes the git revision, so every deploy moves it —
including a migration or a docs commit that cannot touch the model. Boot
compared that hash strictly, failed, and fell closed: MLS was disarmed
four times on 2026-08-02/03, once by a docs-only PR.

The replay path already distinguished the two cases. These tests pin the
approval path to the same distinction, and — more importantly — pin the
half that must NOT move: a real source or runtime change still fails.
"""
from datetime import datetime, timezone

import config
from src.live import model_mls
from src.live.models import ModelApprovalDecision, ModelVersion
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc

REPORT = {
    "eval_version": "model-eval-v1", "n_scored": 162,
    "variants": {}, "edges": {},
}


def _report(me):
    dv = me.deployed_variant()
    return {
        "eval_version": "model-eval-v1", "n_scored": 162,
        "variants": {dv: {"log_loss": 1.07, "brier": 0.647, "rps": 0.232}},
        "edges": {f"{dv}_vs_M0": {"delta_log_loss": 0.008,
                                  "ci95": [-0.012, 0.029],
                                  "significant": False}},
    }


def _seed_decision(me, monkeypatch, revision):
    """Mint a decision as though it shipped from `revision`."""
    monkeypatch.setattr(model_mls, "_GIT_REV", revision)
    monkeypatch.setattr(me, "evaluate_ladder", lambda **kw: _report(me))
    return me.ensure_approval_decision()


class TestTheRevisionIsRecorded:
    def test_a_new_decision_stores_the_revision_it_shipped_from(
            self, live_session, monkeypatch):
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        row = live_session.get(ModelApprovalDecision, dec["decision_id"])
        assert row.code_revision == "a" * 40

    def test_the_revision_is_not_inside_the_content_hash(
            self, live_session, monkeypatch):
        """Provenance about the decision, not part of what was decided."""
        import json as _json
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        row = live_session.get(ModelApprovalDecision, dec["decision_id"])
        doc = _json.loads(row.decision_document)
        assert "code_revision" not in doc


class TestRevisionOnlyDriftIsNotAnEngineChange:
    def test_a_deploy_no_longer_disarms_the_plane(
            self, live_session, monkeypatch):
        """The whole point. Same source, new revision, boot must LOAD."""
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        live_session.expire_all()

        # ---- a deploy: only the revision moves ----
        monkeypatch.setattr(model_mls, "_GIT_REV", "b" * 40)
        again = me.ensure_approval_decision(allow_create=False)

        assert again.get("loaded") is True
        assert again["decision_id"] == dec["decision_id"]
        assert again.get("approval_decision_missing") is not True
        live_session.expire_all()
        mv = live_session.query(ModelVersion).filter_by(
            name=model_mls.MODEL_NAME).one()
        assert mv.approved_for_shadow is True          # still armed

    def test_before_the_fix_this_is_exactly_what_disarmed_it(
            self, live_session, monkeypatch):
        """Control: with no recorded revision there is no second arm, so
        the old behaviour stands — fail closed. Missing evidence is not
        confidence."""
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        row = live_session.get(ModelApprovalDecision, dec["decision_id"])
        row.code_revision = None                       # a pre-migration row
        live_session.commit()

        monkeypatch.setattr(model_mls, "_GIT_REV", "b" * 40)
        again = me.ensure_approval_decision(allow_create=False)
        assert again.get("approval_decision_missing") is True
        assert again["approved"] is False
        assert again.get("fail_closed") is True
        live_session.expire_all()
        mv = live_session.query(ModelVersion).filter_by(
            name=model_mls.MODEL_NAME).one()
        assert mv.approved_for_shadow is False


class TestTheGuardStillHolds:
    """What must NOT have been weakened."""

    def test_a_real_source_change_still_fails_closed(
            self, live_session, monkeypatch):
        """A changed engine must refuse under BOTH arms — rehashing with
        the recorded revision cannot recover a hash that a real edit
        moved."""
        from src.live import model_eval as me
        _seed_decision(me, monkeypatch, "a" * 40)
        live_session.expire_all()

        # a genuine engine change: the signature no longer reproduces
        # under any revision
        monkeypatch.setattr(model_mls, "_GIT_REV", "b" * 40)
        monkeypatch.setattr(
            model_mls, "engine_matches",
            lambda stored, rev: (False, False))
        again = me.ensure_approval_decision(allow_create=False)
        assert again.get("approval_decision_missing") is True
        assert again.get("fail_closed") is True
        live_session.expire_all()
        mv = live_session.query(ModelVersion).filter_by(
            name=model_mls.MODEL_NAME).one()
        assert mv.approved_for_shadow is False

    def test_a_corpus_rebinding_still_falls_through(
            self, live_session, monkeypatch):
        """The corpus arm is independent and must be unaffected: a
        revision match cannot authorise a decision bound to a different
        corpus."""
        from src.live import corpus as live_corpus
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        row = live_session.get(ModelApprovalDecision, dec["decision_id"])
        assert row.corpus_version is None

        live_corpus.publish_corpus("test-corpus-v1")
        monkeypatch.setattr(model_mls, "_GIT_REV", "b" * 40)
        again = me.ensure_approval_decision(allow_create=False)
        # same engine (revision-only), but the corpus binding changed
        assert again.get("loaded") is not True
        assert again.get("approval_decision_missing") is True


class TestHealingAnOldRow:
    def test_a_null_revision_is_filled_on_the_next_activation(
            self, live_session, monkeypatch):
        """Dedup matched on content_hash, which covers engine_signature,
        which hashes the revision — so the running revision IS the one
        that produced the row. Fills a NULL; never overwrites."""
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        row = live_session.get(ModelApprovalDecision, dec["decision_id"])
        row.code_revision = None
        live_session.commit()

        again = me.ensure_approval_decision()      # same revision, dedupes
        assert again["decision_id"] == dec["decision_id"]
        live_session.expire_all()
        healed = live_session.get(ModelApprovalDecision, dec["decision_id"])
        assert healed.code_revision == "a" * 40

    def test_a_recorded_revision_is_never_overwritten(
            self, live_session, monkeypatch):
        from src.live import model_eval as me
        dec = _seed_decision(me, monkeypatch, "a" * 40)
        again = me.ensure_approval_decision()
        assert again["decision_id"] == dec["decision_id"]
        live_session.expire_all()
        row = live_session.get(ModelApprovalDecision, dec["decision_id"])
        assert row.code_revision == "a" * 40
