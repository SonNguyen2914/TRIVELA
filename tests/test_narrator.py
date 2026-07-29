"""Two-channel routing + the narrator's event/interval behavior."""
import config
from src import alerts, narrator
from src.schedule_data import Match
from datetime import datetime, timezone


def _match():
    return Match("FINAL", "Spain", "Argentina", "F",
                 datetime(2026, 7, 19, 19, tzinfo=timezone.utc),
                 stage="knockout")


def _out(score="0-0", minute=30.0, phase="regulation", share=0.7):
    return {"live_state": {"score": score, "minutes_elapsed": minute,
                           "phase": phase, "red_home": 0, "red_away": 0},
            "levers": {"momentum": {"recent_share_home": share}},
            "markets": [{"outcome_key": "home_win",
                         "live_model_probability": 0.42,
                         "market_probability": 0.46}]}


class TestRouting:
    """Fan-out shape only. The transports are now PRIVATE to the gate
    module (`_post_discord` / `_post_ntfy`) — the rename is what makes
    the gate the single exported dispatch path, so these patch the
    private names. An OPERATIONAL class is used deliberately: this
    asserts ROUTING, and routing must be tested with a class the gate
    always authorizes, or the test would silently become a test of the
    money lock instead."""

    def test_action_goes_everywhere_detail_stays_home(self, monkeypatch):
        calls = []
        monkeypatch.setattr(alerts, "_post_discord",
                            lambda m, channel="action": calls.append(channel))
        monkeypatch.setattr(alerts, "_post_ntfy", lambda m, **k: calls.append("ntfy"))
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL", "https://a")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL", "https://d")
        alerts.send_alert("act", dispatch_class=alerts.OPERATIONAL)
        assert calls == ["action", "detail", "ntfy"]
        calls.clear()
        alerts.send_alert("brief", kind="detail",
                          dispatch_class=alerts.OPERATIONAL)
        assert calls == ["detail"]

    def test_single_webhook_setup_sends_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(alerts, "_post_discord",
                            lambda m, channel="action": calls.append(channel))
        monkeypatch.setattr(alerts, "_post_ntfy", lambda m, **k: None)
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL", "https://x")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL", "https://x")
        alerts.send_alert("act", dispatch_class=alerts.OPERATIONAL)
        assert calls == ["action"]          # no duplicate to the same channel


class TestNarrator:
    def setup_method(self):
        narrator._state.clear()

    def test_first_sight_briefs_then_respects_interval(self, monkeypatch):
        sent = []
        monkeypatch.setattr(narrator, "send_alert",
                            lambda m, **k: sent.append(m))
        m = _match()
        narrator.narrate(m, _out(minute=10), [])
        assert len(sent) == 1 and "LIVE BRIEF" in sent[0]
        narrator.narrate(m, _out(minute=11), [])
        assert len(sent) == 1               # inside the interval: silent

    def test_goal_fires_immediately_with_positions(self, monkeypatch):
        sent = []
        monkeypatch.setattr(narrator, "send_alert",
                            lambda m, **k: sent.append(m))
        m = _match()
        narrator.narrate(m, _out(score="0-0"), [])
        narrator.narrate(m, _out(score="1-0", minute=31), [
            {"market_title": "Spain advances", "hold_ev": 230.0,
             "cashout_now": 210.0, "verdict": "HOLD"}])
        assert len(sent) == 2
        assert "GOAL" in sent[1] and "1-0" in sent[1]
        assert "Your positions" in sent[1] and "HOLD" in sent[1]

    def test_phase_change_fires(self, monkeypatch):
        sent = []
        monkeypatch.setattr(narrator, "send_alert",
                            lambda m, **k: sent.append(m))
        m = _match()
        narrator.narrate(m, _out(phase="regulation", minute=89), [])
        narrator.narrate(m, _out(phase="et", minute=91), [])
        assert len(sent) == 2 and "ET" in sent[1]
