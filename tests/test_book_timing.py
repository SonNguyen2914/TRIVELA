"""Two questions about book timing, and the one that must refuse.

The operator asked for an alert naming the best moment to place a bet.
That request contains two questions wearing one costume:

  COST      how wide is the book when you cross it — observable, paid on
            every ticket win or lose, needs no forecast;
  DIRECTION where the price goes before kickoff — a forecast this
            project has no established ability to make.

The first look at DIRECTION found r=+0.488 at p=0.109 over 12 fixtures:
not significant, two clean counterexamples, and two observation windows
that were not even the same length. An alert built on that would be
noise shipped as a feature.

So these assert that the cost half reports, the direction half refuses,
and that no wording anywhere lets a refusal read like an answer.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


cap = _load("capture_market_book")
mbt = _load("measure_book_timing")

# derived, never pinned: every assertion below is a RELATIVE offset from
# this reference, so a literal would buy nothing and would rot the day
# after it was written — which is the failure tests/test_no_new_absolute
# _dates.py exists to ratchet against.
KO = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    minute=30, second=0, microsecond=0)


def _book(tmp_path, at, fixtures):
    """A witnessed book snapshot. `fixtures` is [(home, ask_h, ask_tie,
    ask_away)]."""
    events = []
    for i, (home, ah, at_, aa) in enumerate(fixtures):
        events.append({
            "event_ticker": f"EV{i}", "three_way": True,
            "home": home, "away": f"{home}-away",
            "kickoff_utc": KO.isoformat(),
            "sum_of_asks": round(ah + at_ + aa, 4),
            "vig": round(ah + at_ + aa - 1, 4),
            "legs": [{"label": home, "yes_ask": ah},
                     {"label": "Tie", "yes_ask": at_},
                     {"label": f"{home}-away", "yes_ask": aa}],
        })
    p = tmp_path / f"books-{at.strftime('%Y%m%dT%H%M')}.json"
    p.write_text(json.dumps({"captured_at": at.isoformat(),
                             "events": events}))
    return str(p)


class TestTheDirectionHalfRefuses:
    def test_below_the_floor_no_relationship_and_no_alert(self, tmp_path):
        a = _book(tmp_path, KO - timedelta(hours=40),
                  [(f"C{i}", .60, .20, .22) for i in range(5)])
        b = _book(tmp_path, KO - timedelta(hours=2),
                  [(f"C{i}", .62, .20, .20) for i in range(5)])
        d = mbt.drift_report(mbt.series(mbt.load([a, b])))
        assert d["n_fixtures"] == 5 and d["floor"] == mbt.MIN_FIXTURES
        assert "NO ALERT is issued" in d["refused"]
        assert "r" not in d and "established" not in d

    def test_the_floor_names_why_it_exists(self, tmp_path):
        a = _book(tmp_path, KO - timedelta(hours=40), [("A", .60, .20, .22)])
        b = _book(tmp_path, KO - timedelta(hours=2), [("A", .62, .20, .20)])
        d = mbt.drift_report(mbt.series(mbt.load([a, b])))
        assert "0.109" in d["why_a_floor"]
        assert "noise shipped as a feature" in d["why_a_floor"]

    def test_meeting_the_floor_on_noise_still_refuses(self, tmp_path, monkeypatch):
        """A big sample carrying a meaningless correlation must not
        produce an alert — n and significance are both required."""
        monkeypatch.setattr(mbt, "MIN_FIXTURES", 5)
        monkeypatch.setattr(mbt, "N_PERM", 2000)
        # drift deliberately unrelated to starting share
        early = [(f"C{i}", .40 + i * .04, .20, .40 - i * .04) for i in range(8)]
        late = [(f"C{i}", .40 + i * .04, .20, .40 - i * .04) for i in range(8)]
        late[3] = ("C3", .70, .20, .12)          # one arbitrary mover
        a = _book(tmp_path, KO - timedelta(hours=40), early)
        b = _book(tmp_path, KO - timedelta(hours=2), late)
        d = mbt.drift_report(mbt.series(mbt.load([a, b])))
        assert d["n_fixtures"] >= 5
        assert d["permutation_p"] > mbt.MAX_P
        assert "not established" in d["refused"]
        assert "established" not in d

    def test_a_real_relationship_at_scale_is_reported(self, tmp_path,
                                                       monkeypatch):
        """The refusal must be reachable — a gate that can never open is
        not a gate."""
        monkeypatch.setattr(mbt, "MIN_FIXTURES", 6)
        monkeypatch.setattr(mbt, "N_PERM", 2000)
        early = [(f"C{i}", .30 + i * .05, .20, .50 - i * .05) for i in range(9)]
        # drift proportional to starting share: a clean relationship
        late = [(f"C{i}", .30 + i * .05 + i * .01, .20, .50 - i * .05 - i * .01)
                for i in range(9)]
        a = _book(tmp_path, KO - timedelta(hours=40), early)
        b = _book(tmp_path, KO - timedelta(hours=2), late)
        d = mbt.drift_report(mbt.series(mbt.load([a, b])))
        assert "established" in d and "refused" not in d
        assert d["permutation_p"] <= mbt.MAX_P


class TestTheCostHalfReports:
    def test_the_cheapest_observed_moment_is_found(self, tmp_path):
        for h, v in ((40, .04), (20, .00), (2, .03)):
            _book(tmp_path, KO - timedelta(hours=h),
                  [("A", .60 + v, .20, .20)])
        r = mbt.cost_report(mbt.series(mbt.load(
            sorted(str(p) for p in tmp_path.glob("*.json")))))["fixtures"][0]
        assert r["vig_cheapest"] == 0.0
        assert r["cheapest_at_lead_minutes"] == 20 * 60
        assert r["cost_of_crossing_at_the_worst_observed_moment"] == 0.04

    def test_it_never_claims_more_than_it_observed(self, tmp_path):
        a = _book(tmp_path, KO - timedelta(hours=40), [("A", .64, .20, .20)])
        b = _book(tmp_path, KO - timedelta(hours=2), [("A", .60, .20, .20)])
        c = mbt.cost_report(mbt.series(mbt.load([a, b])))
        assert "Not interpolated" in c["means"]
        assert "not extrapolated" in c["means"]
        assert c["fixtures"][0]["observed_window_minutes"] == [120, 2400]

    def test_one_observation_is_named_not_silently_dropped(self, tmp_path):
        a = _book(tmp_path, KO - timedelta(hours=40),
                  [("A", .60, .20, .20), ("B", .60, .20, .20)])
        b = _book(tmp_path, KO - timedelta(hours=2), [("A", .62, .20, .20)])
        c = mbt.cost_report(mbt.series(mbt.load([a, b])))
        assert [f["home"] for f in c["fixtures"]] == ["A"]
        assert c["fixtures_with_one_observation"][0]["home"] == "B"
        assert "cannot show a trajectory" in \
            c["fixtures_with_one_observation"][0]["why"]


class TestOrderingAndAdmissibility:
    def test_the_series_is_ordered_by_captured_at_not_mtime(self, tmp_path):
        """Git does not preserve mtime; a committed series ordered by
        file time would be shuffled into checkout order."""
        early = _book(tmp_path, KO - timedelta(hours=40), [("A", .64, .20, .20)])
        late = _book(tmp_path, KO - timedelta(hours=2), [("A", .60, .20, .20)])
        os.utime(early, (9e8, 9e8))          # early file, LATEST mtime
        os.utime(late, (1e8, 1e8))
        pts = mbt.series(mbt.load([late, early]))["EV0"]["points"]
        assert [p["lead_seconds"] for p in pts] == [40 * 3600, 2 * 3600]

    def test_a_snapshot_without_a_witness_is_skipped(self, tmp_path):
        p = tmp_path / "nowitness.json"
        p.write_text(json.dumps({"events": []}))
        assert mbt.load([str(p)]) == []

    def test_post_kickoff_observations_never_enter(self, tmp_path):
        a = _book(tmp_path, KO - timedelta(hours=2), [("A", .60, .20, .20)])
        b = _book(tmp_path, KO + timedelta(minutes=30), [("A", .90, .05, .05)])
        pts = mbt.series(mbt.load([a, b]))["EV0"]["points"]
        assert len(pts) == 1 and pts[0]["lead_seconds"] == 7200

    def test_fewer_than_two_snapshots_is_an_error_not_a_result(
            self, tmp_path, monkeypatch, capsys):
        a = _book(tmp_path, KO - timedelta(hours=2), [("A", .60, .20, .20)])
        monkeypatch.setattr(sys, "argv", ["x", "--books", a])
        assert mbt.main() == 2
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "not_enough_snapshots"


class TestTheCaptureItself:
    def test_backfill_is_marked_as_a_claim_not_first_hand(self, tmp_path,
                                                           monkeypatch, capsys):
        mkp = tmp_path / "mk.json"
        mkp.write_text(json.dumps({"events": [{
            "event_ticker": "E1",
            "markets": [{"ticker": "E1-A", "no_sub_title": "A",
                         "yes_ask_dollars": "0.60"},
                        {"ticker": "E1-TIE", "no_sub_title": "Tie",
                         "yes_ask_dollars": "0.20"},
                        {"ticker": "E1-B", "no_sub_title": "B",
                         "yes_ask_dollars": "0.22"}]}]}))
        monkeypatch.setattr(sys, "argv", [
            "x", "--competition", "leagues-cup", "--from-markets", str(mkp),
            "--now", "2026-08-05T22:51:00+00:00", "--dir", str(tmp_path)])
        assert cap.main() == 0
        capsys.readouterr()
        doc = json.loads(next(tmp_path.glob("*books*.json")).read_text())
        assert doc["scratch_backfill"] is True
        assert "importer's claim" in doc["captured_at_provenance"]
        assert doc["captured_at"].startswith("2026-08-05T22:51")

    def test_backfill_without_a_time_refuses(self, tmp_path, monkeypatch,
                                              capsys):
        mkp = tmp_path / "mk.json"
        mkp.write_text(json.dumps({"events": []}))
        monkeypatch.setattr(sys, "argv", [
            "x", "--competition", "leagues-cup", "--from-markets", str(mkp),
            "--dir", str(tmp_path)])
        assert cap.main() == 2
        assert json.loads(capsys.readouterr().out)["error"] == \
            "backfill_needs_now"

    def test_a_capture_is_immutable(self, tmp_path, monkeypatch, capsys):
        mkp = tmp_path / "mk.json"
        mkp.write_text(json.dumps({"events": []}))
        argv = ["x", "--competition", "leagues-cup", "--from-markets",
                str(mkp), "--now", "2026-08-05T22:51:00+00:00",
                "--dir", str(tmp_path)]
        monkeypatch.setattr(sys, "argv", argv)
        cap.main()
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", argv)
        assert cap.main() == 2
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "capture_exists"
        assert "rewrite a point in the very series" in out["means"]

    def test_the_vig_is_derived_not_trusted(self, tmp_path):
        rows = cap.condense({"events": [{
            "event_ticker": "E1",
            "markets": [{"ticker": "a", "no_sub_title": "A",
                         "yes_ask_dollars": "0.60"},
                        {"ticker": "b", "no_sub_title": "Tie",
                         "yes_ask_dollars": "0.20"},
                        {"ticker": "c", "no_sub_title": "B",
                         "yes_ask_dollars": "0.25"}]}]},
            None, datetime.now(timezone.utc))
        assert rows[0]["vig"] == 0.05 and rows[0]["three_way"] is True

    def test_a_non_three_way_book_is_kept_and_flagged(self, tmp_path):
        rows = cap.condense({"events": [{
            "event_ticker": "E1",
            "markets": [{"ticker": "a", "no_sub_title": "A",
                         "yes_ask_dollars": "0.60"}]}]},
            None, datetime.now(timezone.utc))
        assert rows[0]["three_way"] is False and rows[0]["n_legs"] == 1


class TestItPromisesNothing:
    def test_the_document_separates_cost_from_recommendation(self, tmp_path):
        a = _book(tmp_path, KO - timedelta(hours=40), [("A", .64, .20, .20)])
        b = _book(tmp_path, KO - timedelta(hours=2), [("A", .60, .20, .20)])
        import sys as _s
        out = tmp_path / "o.json"
        _s.argv = ["x", "--books", a, b, "--out", str(out)]
        mbt.main()
        doc = json.loads(out.read_text())
        assert "not an edge claim" in doc["what_this_is_NOT"]
        assert "says nothing about whether crossing it is worthwhile" in \
            doc["what_this_is_NOT"]
        assert "refused" in doc["drift"]
