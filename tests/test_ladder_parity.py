"""THE CONTROL for the competition-keyed ladder refactor.

`src/live/model_eval.py` was MLS-only: it imported HALF_LIFE_DAYS,
MIN_GAMES and MODEL_NAME straight from `src.live.model_mls` and read
MLS's config constants inline. Making it competition-keyed touches the
exact code path that computes the number MLS's live shadow approval rests
on, and `model_mls.py` may not be modified at all (it is inside the MLS
engine signature — any source change invalidates the live approval).

So the refactor needs a control, not a claim. This module loads the
PRE-REFACTOR `model_eval.py` out of the git object store, side by side
with the working-tree version, feeds BOTH the identical synthetic MLS
fixture set, and requires their ladder reports to be byte-identical.

What makes this a real comparison rather than a tautology:
  - the old module is read from git (`BASE_REF`), so it cannot drift with
    the working tree;
  - `test_the_control_can_fail` proves the harness DETECTS a difference,
    by perturbing one MLS hyperparameter and requiring the comparison to
    go red. A parity test that cannot fail is not evidence.

Byte-identity is over the full report dict with only `generated_at`
removed (wall clock), serialized with sorted keys — so a changed metric,
a changed CI bound, a renamed rung or an ADDED key all fail.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

UTC = timezone.utc

# the commit the refactor started from — the ladder as MLS's live approval
# knows it. Read from git, never from the working tree.
BASE_REF = "origin/feat-liga-mx"
BASE_PATH = "src/live/model_eval.py"


def _load_base_module(tmp_path):
    """Import the pre-refactor model_eval under its own module name."""
    try:
        src = subprocess.run(
            ["git", "show", f"{BASE_REF}:{BASE_PATH}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    if src.returncode != 0:
        pytest.skip(f"base ref {BASE_REF} unavailable: {src.stderr.strip()}")
    path = tmp_path / "model_eval_base.py"
    path.write_text(src.stdout)
    spec = importlib.util.spec_from_file_location(
        "_model_eval_base", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_model_eval_base"] = mod
    spec.loader.exec_module(mod)
    return mod


def synthetic_mls_fixtures(n: int = 90, teams: int = 10):
    """A deterministic, DB-free fixture set with the attributes both
    ladder versions read: kickoff, the two team ids, the two goal counts,
    a row id and a provider event id.

    Deliberately NOT random: a fixed arithmetic pattern gives every team
    a distinct scoring profile, so the rungs that use team information
    (M1/M2/M2C/M3) genuinely diverge from M0 and the comparison exercises
    the ratings path rather than a degenerate one.
    """
    start = datetime(2026, 3, 1, tzinfo=UTC)
    rows = []
    for i in range(n):
        h = i % teams
        a = (i // teams + 1 + h) % teams
        if a == h:
            a = (h + 1) % teams
        rows.append(SimpleNamespace(
            id=1000 + i,
            espn_event_id=str(700000 + i),
            current_kickoff_utc=start + timedelta(days=2 * i),
            home_team_id=100 + h,
            away_team_id=100 + a,
            # a stable pattern: lower-indexed teams score more
            home_goals=(h + i) % 4,
            away_goals=(a + 1) % 3,
        ))
    return rows


def _canon(report: dict) -> str:
    """The comparable bytes: everything but the wall clock."""
    trimmed = {k: v for k, v in report.items() if k != "generated_at"}
    return json.dumps(trimmed, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _run(module, rows, monkeypatch, xg_map=None, **kwargs):
    """Run `module.evaluate_ladder` against `rows` with no database.

    Both versions read the plane through the same three seams — the
    module-level `plane_ready`, `model_mls._completed`, and
    `mls_stats.team_xg_map` — so patching those drives old and new
    identically without either one touching a database.
    """
    from src.live import mls_stats, model_mls
    monkeypatch.setattr(module, "plane_ready", lambda: True)
    monkeypatch.setattr(model_mls, "_completed", lambda s, before=None: rows)
    monkeypatch.setattr(mls_stats, "team_xg_map", lambda: xg_map or {})
    monkeypatch.setattr(module, "get_session", lambda: SimpleNamespace(
        close=lambda: None))
    return module.evaluate_ladder(n_boot=200, seed=4242, **kwargs)


class TestMlsLadderByteIdentity:
    """The refactor must not move MLS's number by one digit."""

    def test_mls_ladder_report_is_byte_identical_to_pre_refactor(
            self, tmp_path, monkeypatch):
        from src.live import model_eval as new_mod
        base_mod = _load_base_module(tmp_path)
        rows = synthetic_mls_fixtures()

        before = _run(base_mod, rows, monkeypatch)
        after = _run(new_mod, rows, monkeypatch)

        assert before.get("n_scored", 0) > 0, \
            "the control scored nothing — it would pass vacuously"
        assert _canon(before) == _canon(after)

    def test_byte_identical_with_provider_xg_supplied(
            self, tmp_path, monkeypatch):
        """The plain parity run supplies no xG, which leaves M3 numerically
        equal to M2C and the whole xG indirection untested — the refactor
        moved XG_SHRINK_GAMES behind `spec.xg_shrink_default()`, so it
        needs its own control with a populated map."""
        from src.live import model_eval as new_mod
        base_mod = _load_base_module(tmp_path)
        rows = synthetic_mls_fixtures()
        # a deterministic xG map keyed by row id, deliberately NOT equal to
        # the goal counts so the xG rung diverges from the goals rungs
        xg = {}
        for i, f in enumerate(rows):
            xg[f.id] = {
                "home": {"xg": 0.7 + (i % 5) * 0.31,
                         "xg_against": 0.5 + (i % 3) * 0.44},
                "away": {"xg": 0.5 + (i % 3) * 0.44,
                         "xg_against": 0.7 + (i % 5) * 0.31},
            }
        before = _run(base_mod, rows, monkeypatch, xg_map=xg)
        after = _run(new_mod, rows, monkeypatch, xg_map=xg)
        assert before.get("n_scored", 0) > 0
        # the map must actually have moved M3 off M2C, or this control is
        # testing nothing
        assert (before["variants"]["M3"]["log_loss"]
                != before["variants"]["M2C"]["log_loss"]), \
            "xG map did not reach the M3 rung — control is vacuous"
        assert _canon(before) == _canon(after)

    def test_default_spec_is_mls_when_nothing_is_passed(
            self, tmp_path, monkeypatch):
        """Calling the refactored ladder with an EXPLICIT MLS spec must
        equal calling it with no spec at all — that default is what keeps
        every existing MLS call site meaning what it meant."""
        from src.live import model_eval as new_mod
        rows = synthetic_mls_fixtures()
        implicit = _run(new_mod, rows, monkeypatch)
        explicit = _run(new_mod, rows, monkeypatch,
                        spec=new_mod.MLS_LADDER_SPEC)
        assert _canon(implicit) == _canon(explicit)

    def test_the_control_can_fail(self, tmp_path, monkeypatch):
        """PROOF the parity check is load-bearing.

        Perturb one MLS hyperparameter the ladder reads (the recency
        half-life) and the byte comparison must go RED. If this passes,
        every other assertion in this class is worthless.
        """
        from src.live import model_eval as new_mod
        from src.live import model_mls
        base_mod = _load_base_module(tmp_path)
        rows = synthetic_mls_fixtures()

        before = _run(base_mod, rows, monkeypatch)
        monkeypatch.setattr(model_mls, "HALF_LIFE_DAYS", 30.0)
        # the refactored ladder reads the half-life THROUGH the spec at
        # call time, so the patched value reaches it
        after = _run(new_mod, rows, monkeypatch)
        assert _canon(before) != _canon(after), \
            "the parity harness cannot detect a changed hyperparameter"


class TestSpecRegistry:
    def test_mls_spec_reports_model_mls_constants_unmodified(self):
        """The MLS spec must READ model_mls, never restate it: a copied
        constant would silently diverge the day model_mls changed."""
        from src.live import model_eval, model_mls
        spec = model_eval.MLS_LADDER_SPEC
        assert spec.half_life_days() == model_mls.HALF_LIFE_DAYS
        assert spec.min_games() == model_mls.MIN_GAMES
        assert spec.result_shrink() == model_mls.RESULT_SHRINK
        assert spec.model_name == model_mls.MODEL_NAME

    def test_every_registered_spec_resolves(self):
        from src.live import model_eval
        specs = model_eval.ladder_specs()
        assert "mls-2026" in specs
        for slug, spec in specs.items():
            assert spec.slug == slug
            assert spec.model_name
            assert spec.ladder, f"{slug} has no rungs"
            assert "M0" in spec.ladder and "M2" in spec.ladder

    def test_dark_leagues_are_registered_and_goals_only(self):
        """EPL and Liga MX have no xG source in this stack, so their
        ladders must not carry an xG rung claiming a measurement that
        cannot exist."""
        from src.live import model_eval
        specs = model_eval.ladder_specs()
        for slug in ("epl-2026", "liga-mx-2026"):
            assert slug in specs, f"{slug} not registered"
            assert "M3" not in specs[slug].ladder
            assert specs[slug].xg_map_fn is None
