"""`/api/ready` says WHICH REVISION answered.

Every other field on that endpoint reports what state the process is in;
none said which code produced it. So `model_approved_for_shadow: true`
read a minute after a merge was indistinguishable between "the new
revision re-armed" and "the old container is still serving" — and
AGENTS.md §4 invites precisely that reading.

The gap was not hypothetical. The coordinator polled `/api/ready` for a
running revision three separate times in one day, got nothing each time,
and substituted a CONTENT probe ("wait until the payload mentions
Mansfield, Texas") without recognising the substitution. A content probe
answers *has the behaviour I expect appeared*, which is usually the same
question and is not always: a cached response, a partial rollout or an
unrelated data change all break it.

Two properties matter and both are pinned here:

  SAME SOURCE   the revision reported must be the one the approval
                machinery hashes. Reporting it from a second lookup
                could drift, and a drifting answer to "which code is
                this" is worse than no answer.
  NEVER BLANK   null with a stated reason when the environment variable
                is absent, so a local process cannot be mistaken for a
                deployed one.
"""
import pytest
from fastapi.testclient import TestClient

import config
from src.db import init_db


@pytest.fixture()
def client(monkeypatch):
    # same shape as tests/test_archive_serving.py::client — /api/ready
    # counts real archive rows, so the schema has to exist
    monkeypatch.setattr(config, "DEMO_MODE", True)
    from api import main as api_main
    init_db()
    api_main._rate_last.clear()
    return TestClient(api_main.app)


class TestTheFieldExists:
    def test_ready_reports_a_code_revision_key_at_top_level(self, client):
        d = client.get("/api/ready").json()
        assert "code_revision" in d, (
            "the whole point is that a reader can quote the revision "
            "beside the verdict")

    def test_it_sits_beside_the_verdict_not_buried(self, client):
        """§4 tells a reader to read it FIRST. A field nested three
        levels down is one a hurried reader skips."""
        d = client.get("/api/ready").json()
        assert "code_revision" in d and "ready" in d


class TestItIsNeverSilentlyBlank:
    def test_absent_env_gives_null_WITH_a_named_reason(self, client,
                                                        monkeypatch):
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        import src.live.revision as rev
        monkeypatch.setattr(rev, "_GIT_REV", "")
        d = client.get("/api/ready").json()
        assert d["code_revision"] is None
        why = d["code_revision_unknown_because"]
        assert why and "RAILWAY_GIT_COMMIT_SHA" in why

    def test_a_local_process_cannot_look_deployed(self, monkeypatch):
        """Empty string is falsy but JSON-serialises to `""`, which reads
        as "a revision" at a glance. None does not."""
        import src.live.revision as rev
        monkeypatch.setattr(rev, "_GIT_REV", "")
        assert rev.running_code_revision() is None

    def test_a_present_revision_carries_no_excuse_field(self, client,
                                                         monkeypatch):
        import src.live.revision as rev
        monkeypatch.setattr(rev, "_GIT_REV", "a" * 40)
        d = client.get("/api/ready").json()
        assert d["code_revision"] == "a" * 40
        assert d["code_revision_unknown_because"] is None


class TestItIsTheSameRevisionTheApprovalMachineryUses:
    def test_accessor_agrees_with_the_engine_signature(self):
        """The load-bearing guard. If these two could disagree, the
        endpoint would answer "which code is running" from a different
        source than the two-arm approval check reads — which is the
        exact class of error the field was added to close."""
        import src.live.model_mls as mm
        import src.live.revision as rev
        assert rev.running_code_revision() == (
            mm.engine_signature()["code_revision"] or None)

    def test_they_still_agree_when_a_revision_is_set(self, monkeypatch):
        import src.live.model_mls as mm
        import src.live.revision as rev
        monkeypatch.setattr(rev, "_GIT_REV", "b" * 40)
        monkeypatch.setattr(mm, "_GIT_REV", "b" * 40)
        assert rev.running_code_revision() == "b" * 40
        assert mm.engine_signature()["code_revision"] == "b" * 40

    def test_the_endpoint_does_not_hash_the_engine_to_answer(
            self, client, monkeypatch):
        """`engine_signature()` opens and sha256s four module files.
        Readiness probes get polled, so the endpoint must not pay that
        cost — it reads the constant instead."""
        import src.live.model_mls as mm
        calls = []
        real = mm.engine_signature

        def counted(*a, **k):
            calls.append(1)
            return real(*a, **k)
        monkeypatch.setattr(mm, "engine_signature", counted)
        client.get("/api/ready")
        assert calls == [], "readiness hashed the engine to report a SHA"


class TestReadinessStillWorks:
    def test_the_endpoint_answers_and_keeps_its_existing_fields(self,
                                                                 client):
        d = client.get("/api/ready").json()
        for k in ("ready", "mode", "readiness", "live", "shadow_blockers",
                  "real_money_signals", "time"):
            assert k in d

    def test_a_broken_accessor_cannot_take_readiness_down(self, client,
                                                           monkeypatch):
        """Readiness is the endpoint an operator reaches for when things
        are already wrong. A new display field must never be able to
        500 it."""
        import src.live.revision as rev

        def boom():
            raise RuntimeError("no")
        monkeypatch.setattr(rev, "running_code_revision", boom)
        r = client.get("/api/ready")
        assert r.status_code == 200
        d = r.json()
        assert d["code_revision"] is None
        assert "unavailable" in d["code_revision_unknown_because"]


class TestItStaysOutOfTheEngineSignature:
    """The reason `src/live/revision.py` exists as its own module.

    The first version of this feature put the accessor in
    `src/live/model_mls.py`, beside the constant it mirrors. That module
    is one of four whose SOURCE is sha256'd into `engine_signature()`, so
    three added lines moved the hash — which fails BOTH arms of the boot
    approval check and takes the plane dark until an operator
    reactivates. `tests/test_team_style.py` caught it and says in as many
    words: do not update these literals to make the test pass.

    The guard is right. It cannot tell an unrelated helper from a
    modelling change, and it must not try. What follows from that is the
    part worth pinning: **the engine-signature modules are closed to any
    edit that is not a deliberate modelling change.**
    """

    ENGINE_MODULES = ("src.live.model_mls", "src.models.simulator",
                      "src.models.xg_model", "src.models.features")

    def _imports(self, mod):
        """The module's ACTUAL imports, parsed — not a string search.

        A substring check would ban the docstrings that explain this
        rule, which is the documentation earning its place. Structural
        over textual is the same lesson as the #69 error classifier: a
        rule keyed on wording is a rule an unlucky wording defeats."""
        import ast
        import inspect
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(mod))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names.add(base)
                names.update(f"{base}.{a.name}" for a in node.names)
        return names

    def test_no_engine_module_imports_the_revision_helper(self):
        """Wiring model_mls to import this module would put it back
        inside the signature by the back door — the file's bytes change
        the moment the import line is added."""
        import importlib
        for name in self.ENGINE_MODULES:
            imported = self._imports(importlib.import_module(name))
            assert not any("revision" in i.split(".")[-1] or
                           i.endswith("live.revision") for i in imported), (
                f"{name} is inside the engine signature; importing the "
                f"revision helper there moves the hash and darkens the "
                f"plane")

    def test_the_helper_does_not_import_the_engine_either(self):
        """Symmetric: a shared constant would couple them and one edit
        would again reach the signature. Its docstring NAMES model_mls
        on purpose — that is the explanation, not a dependency."""
        from src.live import revision
        assert not any("model_mls" in i for i in self._imports(revision))
        assert "model_mls" in (revision.__doc__ or ""), (
            "the module must say why it is not in model_mls")

    def test_the_signature_modules_are_the_ones_this_test_names(self):
        """A guard listing the wrong modules protects nothing. Read the
        list back out of the engine itself."""
        from src.live import model_mls
        assert set(model_mls.engine_signature()["source_sha256"]) == \
            set(self.ENGINE_MODULES)
