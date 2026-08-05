"""The pick-loop CLI's refusals, viewer form and execution wrappers.

These test the CLIENT half of contracts the server also enforces —
the point is that a refusal happens BEFORE a request leaves the
machine, with the reason on the terminal, because a silent void or a
stated_only surprise reads exactly like success at 11pm.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "journal_pick", _ROOT / "scripts" / "journal_pick.py")
jp = importlib.util.module_from_spec(_SPEC)
sys.modules["journal_pick"] = jp
_SPEC.loader.exec_module(jp)

UTC = timezone.utc


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["journal_pick.py", *args])


def _future():
    return (datetime.now(UTC) + timedelta(hours=6)).isoformat()


VIEWER = ("record-viewer", "home_win",
          "--competition-slug", "leagues-cup-2026",
          "--provider-fixture-id", "1530116",
          "--home-team", "Monterrey", "--away-team", "Orlando City SC",
          "--market-ticker", "KXLEAGUESCUPGAME-26AUG05MONORL-MON")


class TestViewerRecord:
    def test_the_body_carries_the_full_viewer_shape(self, monkeypatch,
                                                    capsys):
        sent = {}
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: sent.update(path=p, body=b)
                            or {"bet": {"id": 26}})
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", _future(),
              "--stated-price", "0.43", "--stated-size", "35")
        assert jp.main() == 0
        assert sent["path"] == "/api/admin/mls/journal/view"
        b = sent["body"]
        assert b["competition_slug"] == "leagues-cup-2026"
        assert b["provider_fixture_id"] == "1530116"
        assert b["home_team"] == "Monterrey"
        assert b["stated_price"] == "0.43"
        assert b["stated_size"] == "35"
        assert "fixture_id" not in b

    def test_past_kickoff_refuses_before_any_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: called.append(p) or {})
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", past)
        with pytest.raises(SystemExit) as e:
            jp.main()
        assert "void" in str(e.value)
        assert called == [], "nothing may leave the machine"

    def test_allow_void_lets_documentation_through(self, monkeypatch):
        monkeypatch.setattr(jp, "_post", lambda p, b: {"bet": {"id": 9}})
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", past, "--allow-void")
        assert jp.main() == 0

    def test_a_naive_kickoff_is_refused(self, monkeypatch):
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", "2026-08-05T23:30:00")
        with pytest.raises(SystemExit) as e:
            jp.main()
        assert "timezone" in str(e.value)

    def test_garbage_kickoff_is_refused_with_the_format(self, monkeypatch):
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", "tonight")
        with pytest.raises(SystemExit) as e:
            jp.main()
        assert "ISO" in str(e.value)

    def test_the_stated_only_note_is_printed_after_a_write(
            self, monkeypatch, capsys):
        monkeypatch.setattr(jp, "_post", lambda p, b: {"bet": {"id": 9}})
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", _future())
        jp.main()
        assert "counted NOWHERE" in capsys.readouterr().err

    def test_dry_run_prints_the_body_and_posts_nothing(self, monkeypatch,
                                                       capsys):
        called = []
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: called.append(p) or {})
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", _future(), "--dry-run")
        assert jp.main() == 0
        assert called == []
        assert "DRY RUN" in capsys.readouterr().out

    def test_a_server_refusal_is_surfaced_verbatim(self, monkeypatch):
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: {"error": "season cannot be read"})
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", _future())
        with pytest.raises(SystemExit) as e:
            jp.main()
        assert "season cannot be read" in str(e.value)


class TestExecutionWrapper:
    def test_the_body_passes_operator_facts_through_untouched(
            self, monkeypatch):
        sent = {}
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: sent.update(path=p, body=b)
                            or {"execution": {"id": 1}})
        _argv(monkeypatch, "execution", "26", "operator-kalshi",
              "--consent-recorded-at", "2026-08-05T23:00:00+00:00",
              "--fill-price", "0.43", "--filled-contracts", "78.27",
              "--fee-paid", "1.34",
              "--filled-at", "2026-08-05T23:05:00+00:00")
        assert jp.main() == 0
        assert sent["path"] == "/api/admin/mls/journal/execution"
        b = sent["body"]
        assert b["bet_id"] == 26 and b["account_label"] == "operator-kalshi"
        assert b["status"] == "filled"
        assert b["fill_price"] == "0.43"
        assert b["filled_at"] == "2026-08-05T23:05:00+00:00"
        assert "not_filled_reason" not in b

    def test_not_filled_carries_its_reason_and_no_fill_facts(
            self, monkeypatch):
        sent = {}
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: sent.update(body=b) or {})
        _argv(monkeypatch, "execution", "26", "operator-kalshi",
              "--consent-recorded-at", "2026-08-05T23:00:00+00:00",
              "--status", "not_filled",
              "--not-filled-reason", "book moved before the order")
        jp.main()
        b = sent["body"]
        assert b["status"] == "not_filled"
        assert b["not_filled_reason"] == "book moved before the order"
        assert "fill_price" not in b

    def test_settlement_body(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(jp, "_post",
                            lambda p, b: sent.update(path=p, body=b) or {})
        _argv(monkeypatch, "settlement", "3", "78.27",
              "--settled-at", "2026-08-06T02:45:00+00:00",
              "--settled-outcome", "home_win")
        jp.main()
        assert sent["path"] == "/api/admin/mls/journal/settlement"
        assert sent["body"]["settlement_credit"] == "78.27"
        assert sent["body"]["settled_outcome"] == "home_win"


class TestNoTokenNoWrite:
    def test_an_unset_token_refuses_and_names_the_rule(self, monkeypatch):
        monkeypatch.setattr(jp, "TOKEN", "")
        _argv(monkeypatch, *VIEWER, "--kickoff-utc", _future())
        with pytest.raises(SystemExit) as e:
            jp.main()
        msg = str(e.value)
        assert "JOURNAL_TOKEN" in msg
        assert "ADMIN_TOKEN" in msg, "the never-substitute rule is the point"
