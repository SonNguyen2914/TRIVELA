"""The comparison nobody was making, and the three failures it catches.

On 2026-08-05 the journal and the operator's account disagreed in three
distinct ways in a single night, and nothing noticed:

  - FC Dallas: a $35 position returning $70.35 with NO journal row at
    all — the slate's best result, invisible to the record;
  - Vancouver: a $35 matchday-1 position unnoticed for two days, so the
    matchday-1 debrief was written assuming no position was held;
  - ids 22 and 23: rows reading `passed`, a word asserting the operator
    DECLINED, against fixtures carrying real money.

Each is asserted here as its own class, because they need different
responses: a missing row is an omission, a `passed` on a live position
is a false statement, and a size disagreement is a transcription error.
Collapsing them into one "mismatch" bucket would lose that.
"""
import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "reconcile_journal", _ROOT / "scripts" / "reconcile_journal.py")
rj = importlib.util.module_from_spec(_SPEC)
sys.modules["reconcile_journal"] = rj
_SPEC.loader.exec_module(rj)

DAL = "KXLEAGUESCUPGAME-26AUG05DALQUE-DAL"
MIA = "KXLEAGUESCUPGAME-26AUG05MIAASL-MIA"
MON = "KXLEAGUESCUPGAME-26AUG05MONORL-MON"


def _row(bet_id, ticker, status="taken", size="35", price="0.48",
         superseded=False, corrects=None):
    return {"id": bet_id, "market_ticker": ticker, "status": status,
            "stated_size": size, "stated_price_dollars": price,
            "superseded": superseded, "corrects_bet_id": corrects}


def _pos(ticker, cost=35.0, entry=0.48, paid=0.0):
    return {"market_ticker": ticker, "cost": cost,
            "entry_price": entry, "paid_out": paid}


class TestThePositionWithNoRow:
    """The FC Dallas class — the one that cost the most."""

    def test_real_money_on_a_market_with_no_row_is_named(self):
        out = rj.reconcile([], [_pos(DAL, cost=35.0, paid=70.35)])
        assert out["clean"] is False
        got = out["positions_with_no_journal_row"]
        assert len(got) == 1
        assert got[0]["market_ticker"] == DAL and got[0]["cost"] == 35.0
        assert "NO journal row" in got[0]["why"]

    def test_it_is_not_confused_with_a_value_mismatch(self):
        out = rj.reconcile([], [_pos(DAL)])
        assert out["value_mismatches"] == []
        assert out["status_contradictions"] == []

    def test_a_near_miss_ticker_does_not_rescue_it(self):
        """Matching is exact. A row on the Querétaro leg is not a row on
        the Dallas leg, and pretending otherwise would hide the gap."""
        que = DAL.replace("-DAL", "-QUE")
        out = rj.reconcile([_row(1, que)], [_pos(DAL)])
        assert len(out["positions_with_no_journal_row"]) == 1
        assert len(out["journal_rows_with_no_position"]) == 1


class TestTheStatusContradiction:
    """The ids 22/23 class — a row asserting a decision that never
    happened."""

    def test_passed_against_real_money_is_a_contradiction(self):
        out = rj.reconcile([_row(22, MON, status="passed")],
                           [_pos(MON, cost=35.0)])
        c = out["status_contradictions"]
        assert len(c) == 1 and c[0]["bet_id"] == 22
        assert "asserts no money was placed" in c[0]["why"]
        assert out["value_mismatches"] == []

    def test_considered_against_real_money_is_the_same_class(self):
        """`considered` also asserts no money — the operator was still
        deciding."""
        out = rj.reconcile([_row(9, MIA, status="considered")],
                           [_pos(MIA)])
        assert len(out["status_contradictions"]) == 1

    def test_taken_against_real_money_is_not_a_contradiction(self):
        out = rj.reconcile([_row(24, DAL, status="taken")], [_pos(DAL)])
        assert out["status_contradictions"] == []
        assert out["agreed"][0]["bet_id"] == 24

    def test_void_against_real_money_is_not_a_contradiction(self):
        """A void row is documentation of a real position recorded late
        — exactly what the repair path produces. Flagging it would make
        the tool cry wolf on every correction."""
        out = rj.reconcile([_row(26, DAL, status="void")], [_pos(DAL)])
        assert out["status_contradictions"] == []


class TestCorrectionChains:
    def test_the_correction_is_judged_not_the_corrected_row(self):
        """id 22 says `passed` and is superseded by id 28 saying `void`
        at the real size. The chain is RESOLVED, so this must not
        re-report the original as a contradiction."""
        rows = [_row(22, MON, status="passed", superseded=True),
                _row(28, MON, status="void", size="35", corrects=22)]
        out = rj.reconcile(rows, [_pos(MON, cost=35.0)])
        assert out["status_contradictions"] == []
        assert out["agreed"][0]["bet_id"] == 28

    def test_a_superseded_row_is_never_reported_as_orphaned(self):
        rows = [_row(22, MON, status="passed", superseded=True),
                _row(28, MON, status="void", corrects=22)]
        out = rj.reconcile(rows, [])
        orphans = [r["bet_id"] for r in out["journal_rows_with_no_position"]]
        assert 22 not in orphans and 28 in orphans


class TestValueMismatches:
    def test_a_size_disagreement_is_reported_with_both_numbers(self):
        """The $33.54-vs-$35.00 class: a transcription error, not an
        omission and not a false statement."""
        out = rj.reconcile([_row(22, MON, size="33.54")],
                           [_pos(MON, cost=35.0)])
        m = out["value_mismatches"]
        assert m[0]["deltas"]["size"] == {"row": 33.54, "account": 35.0}

    def test_a_fee_sized_price_difference_is_NOT_a_mismatch(self):
        """A price derived from cost/payout is fee-inclusive and runs
        about a cent high. Flagging that would bury the real findings."""
        out = rj.reconcile([_row(1, MIA, price="0.79")],
                           [_pos(MIA, entry=0.78)])
        assert out["value_mismatches"] == [] and out["clean"] is True

    def test_a_real_price_gap_still_reports(self):
        out = rj.reconcile([_row(1, MON, price="0.43")],
                           [_pos(MON, entry=0.38)])
        assert out["value_mismatches"][0]["deltas"]["price"]["row"] == 0.43

    def test_a_missing_account_price_does_not_invent_a_mismatch(self):
        p = _pos(MON)
        del p["entry_price"]
        out = rj.reconcile([_row(1, MON, price="0.43")], [p])
        assert out["value_mismatches"] == []


class TestOrphanedRows:
    def test_a_recorded_row_with_no_position_is_named(self):
        out = rj.reconcile([_row(5, MIA, status="passed")], [])
        o = out["journal_rows_with_no_position"]
        assert o[0]["bet_id"] == 5
        assert "Expected for a genuine pass" in o[0]["why"]

    def test_clean_when_both_records_agree(self):
        out = rj.reconcile([_row(1, DAL, size="35", price="0.48")],
                           [_pos(DAL, cost=35.0, entry=0.48)])
        assert out["clean"] is True
        assert out["agreed"] and not out["journal_rows_with_no_position"]


class TestItNeverRepairs:
    def test_the_document_disclaims_repair_and_pnl(self):
        out = rj.reconcile([], [])
        assert "not a repair" in out["what_this_is_NOT"]
        assert "not P&L" in out["what_this_is_NOT"]
        assert "writes nothing" in out["what_this_is"]

    def test_matching_key_is_declared_and_is_the_ticker(self):
        out = rj.reconcile([], [])
        assert out["matching"]["key"] == "market_ticker"
        assert "name+date are" in out["matching"]["why"]


class TestTheCli:
    def _run(self, monkeypatch, tmp_path, entries, positions):
        def fake_open(url):
            class R:
                def read(self_inner):
                    return json.dumps({
                        "scopes_found": ["leagues-cup-2026"],
                        "scopes": {"leagues-cup-2026": {
                            "entries": entries}}}).encode()
            import io
            return io.BytesIO(R().read())
        monkeypatch.setattr(rj.urllib.request, "urlopen", fake_open)
        p = tmp_path / "pos.json"
        p.write_text(json.dumps(positions))
        out = tmp_path / "out.json"
        monkeypatch.setattr(sys, "argv", [
            "x", "--competition", "leagues-cup", "--positions", str(p),
            "--api-base", "https://example.test", "--out", str(out)])
        rc = rj.main()
        return rc, json.loads(out.read_text())

    def test_a_disagreement_exits_1_so_a_caller_can_branch(
            self, monkeypatch, tmp_path):
        rc, doc = self._run(monkeypatch, tmp_path, [], [_pos(DAL)])
        assert rc == 1 and doc["clean"] is False
        assert doc["competition"] == "leagues-cup"

    def test_agreement_exits_0(self, monkeypatch, tmp_path):
        rc, doc = self._run(monkeypatch, tmp_path,
                            [_row(1, DAL, size="35", price="0.48")],
                            [_pos(DAL)])
        assert rc == 0 and doc["clean"] is True

    def test_no_api_base_refuses(self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("TRIVELA_API_BASE", raising=False)
        p = tmp_path / "pos.json"
        p.write_text("[]")
        monkeypatch.setattr(sys, "argv", [
            "x", "--competition", "leagues-cup", "--positions", str(p)])
        assert rj.main() == 2
        assert json.loads(capsys.readouterr().out)["error"] == "no_api_base"


class TestWhatItCouldNotCheck:
    """A comparison that did not run must be named. Silence reads as
    agreement, which is the failure this tool exists to end."""

    def test_a_redacted_size_is_reported_as_a_check_not_run(self):
        row = _row(1, DAL)
        del row["stated_size"]              # as the public journal serves it
        out = rj.reconcile([row], [_pos(DAL, cost=35.0)])
        checks = {c["check"]: c for c in out["comparisons_not_run"]}
        assert "size" in checks
        assert "redacts it as staking" in checks["size"]["why"]
        assert "operator-credentialed" in checks["size"]["how_to_run_it"]

    def test_a_present_size_means_the_check_DID_run(self):
        out = rj.reconcile([_row(1, DAL, size="35")], [_pos(DAL)])
        assert [c["check"] for c in out["comparisons_not_run"]] == []

    def test_a_redacted_size_does_not_make_the_run_look_dirty(self):
        """The check being unavailable is not itself a disagreement —
        it must not flip `clean`, or every public run would cry wolf."""
        row = _row(1, DAL)
        del row["stated_size"]
        out = rj.reconcile([row], [_pos(DAL, cost=35.0, entry=0.48)])
        assert out["clean"] is True
        assert out["comparisons_not_run"]

    def test_no_entries_at_all_reports_no_phantom_checks(self):
        assert rj.reconcile([], [_pos(DAL)])["comparisons_not_run"] == []


class TestItRefusesRatherThanFabricate:
    """Run against a server that predates the row listing, an earlier
    version of this script reported all SEVEN of a real slate's
    positions as having no journal row. Six had one. A tool that turns a
    stale deployment into seven fabricated findings is worse than none.
    """

    def test_a_server_that_lists_no_rows_is_a_refusal(self):
        bad = rj.refusal({"total_recorded": 8, "entries_returned": 0,
                          "server_lists_rows": False})
        assert bad is not None
        assert "would be false" in bad["refused"]
        assert "predates the row listing" in bad["likely_cause"]

    def test_rows_recorded_but_none_returned_is_a_refusal(self):
        bad = rj.refusal({"total_recorded": 8, "entries_returned": 0,
                          "server_lists_rows": True})
        assert bad is not None and "returned none" in bad["refused"]

    def test_a_genuinely_empty_journal_is_NOT_a_refusal(self):
        """Nothing recorded and nothing returned is a real, honest
        state — every position legitimately has no row."""
        assert rj.refusal({"total_recorded": 0, "entries_returned": 0,
                           "server_lists_rows": True}) is None

    def test_a_healthy_listing_is_not_a_refusal(self):
        assert rj.refusal({"total_recorded": 8, "entries_returned": 8,
                           "server_lists_rows": True}) is None

    def test_fetch_detects_a_missing_entries_key(self, monkeypatch):
        import io
        body = {"scopes_found": ["leagues-cup-2026"],
                "scopes": {"leagues-cup-2026": {"total_recorded": 8}}}
        monkeypatch.setattr(rj.urllib.request, "urlopen",
                            lambda u: io.BytesIO(json.dumps(body).encode()))
        _rows, _scopes, health = rj.fetch_entries("https://x.test", "leagues-cup")
        assert health["server_lists_rows"] is False
        assert health["total_recorded"] == 8

    def test_the_cli_exits_2_on_a_refusal_and_writes_no_report(
            self, monkeypatch, tmp_path, capsys):
        import io
        body = {"scopes_found": ["leagues-cup-2026"],
                "scopes": {"leagues-cup-2026": {"total_recorded": 8}}}
        monkeypatch.setattr(rj.urllib.request, "urlopen",
                            lambda u: io.BytesIO(json.dumps(body).encode()))
        p = tmp_path / "pos.json"
        p.write_text(json.dumps([_pos(DAL)]))
        out = tmp_path / "out.json"
        monkeypatch.setattr(sys, "argv", [
            "x", "--competition", "leagues-cup", "--positions", str(p),
            "--api-base", "https://x.test", "--out", str(out)])
        assert rj.main() == 2
        assert not out.exists()
        assert json.loads(capsys.readouterr().out)["reconciled"] is False
