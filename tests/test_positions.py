"""Position tracker: verdict math, key fallbacks, flip alerts, API."""
import config
from src import positions
from src.db import SessionLocal, TrackedPosition, init_db


class _Resp:
    """Stand-in for a `requests` response on the fake network."""
    status_code = 200


def setup_module(m):
    init_db()


def _wipe():
    with SessionLocal() as s:
        s.query(TrackedPosition).delete()
        s.commit()
    positions._state.clear()


class TestVerdict:
    # 100 contracts, cost $50 -> flip margin 0.05*50 = $2.50
    def test_exit_when_cashing_clearly_beats_holding(self):
        v, hold, cash = positions._verdict(0.30, 0.40, 100, 50.0)
        assert v == "EXIT" and hold == 30.0 and round(cash, 2) == 38.32

    def test_hold_when_settlement_ev_clearly_wins(self):
        v, _, _ = positions._verdict(0.50, 0.40, 100, 50.0)
        assert v == "HOLD"

    def test_close_call_inside_the_margin(self):
        v, _, _ = positions._verdict(0.40, 0.40, 100, 50.0)
        assert v == "CLOSE_CALL"


class TestEvaluate:
    def test_prefers_live_keys_falls_back_to_batch(self):
        _wipe()
        with SessionLocal() as s:
            s.add(TrackedPosition(match_id="FX", market_id="M-1",
                                  market_title="t", entry_price=0.40,
                                  contracts=100, cost=42.0))
            s.commit()
        live = {"M-1": {"live_model_probability": 0.20,
                        "market_probability": 0.50,
                        "market_yes_bid": 0.50}}
        (item,) = positions.evaluate_positions(live, "FX")
        assert item["verdict"] == "EXIT"          # cash 48.25 vs hold 20
        batch = {"M-1": {"model_probability": 0.80,
                         "market_yes_bid": 0.50}}
        (item,) = positions.evaluate_positions(batch, "FX")
        assert item["verdict"] == "HOLD"          # hold 80 vs cash 48.25
        no_bid = {"M-1": {"model_probability": 0.80,
                          "market_probability": 0.50}}
        (item,) = positions.evaluate_positions(no_bid, "FX")
        assert item["verdict"] == "NO_BID"        # ask never substitutes

    def test_flip_offers_exactly_one_alert_to_the_gate(self, monkeypatch):
        """Cooldown/flip logic, measured at the GATE rather than at the
        transport.

        This test used to patch `positions.send_alert` and assert that a
        cash-out message came out — which is what the old, ungated code
        did, and asserting it made the test an endorsement of the defect.
        A cash-out read is model-generated betting content aimed at the
        channel a consenting third party acts on; with
        REAL_MONEY_SIGNALS_ENABLED false it must not dispatch at all. So
        the behaviour under test is now "offers one dispatch per flip",
        and `test_gate_refuses_the_cash_out_dispatch` below asserts the
        refusal on the composed path."""
        _wipe()
        offered = []
        monkeypatch.setattr(positions, "send_alert",
                            lambda m, **k: offered.append((m, k)))
        with SessionLocal() as s:
            s.add(TrackedPosition(match_id="FX", market_id="M-2",
                                  market_title="Spain 2-1", entry_price=0.10,
                                  contracts=400, cost=42.0))
            s.commit()
        hold_rows = {"M-2": {"live_model_probability": 0.30,
                             "market_yes_bid": 0.12}}
        exit_rows = {"M-2": {"live_model_probability": 0.02,
                             "market_yes_bid": 0.10}}
        positions.evaluate_positions(hold_rows, "FX", 10, alert=True)
        assert offered == []                       # first sighting, HOLD
        pid = next(iter(positions._state))
        positions._state[pid]["ts"] -= 999         # age past cooldown
        positions.evaluate_positions(exit_rows, "FX", 55, alert=True)
        assert len(offered) == 1 and "CASH-OUT" in offered[0][0]
        # and the call site declares WHAT it is offering — the gate never
        # reads the text, so the classification has to be here
        from src import alerts
        assert offered[0][1]["dispatch_class"] == alerts.MODEL_SIGNAL
        positions.evaluate_positions(exit_rows, "FX", 56, alert=True)
        assert len(offered) == 1                   # same verdict: silent

    def test_gate_refuses_the_cash_out_dispatch(self, monkeypatch):
        """journal-P0 (round 4), composed path: REAL positions ->
        REAL send_alert -> fake network. With the money lock false, a
        cash-out read reaches ZERO transports, the verdict is still
        computed, and the refusal is recorded against this call site."""
        from src import alerts
        _wipe()
        alerts._reset_refusals_for_tests()
        posts = []
        monkeypatch.setattr(alerts.requests, "post",
                            lambda *a, **k: posts.append(a) or _Resp())
        monkeypatch.setattr(config, "DISCORD_ACTION_WEBHOOK_URL",
                            "https://discord.test/api/webhooks/1/SECRET")
        monkeypatch.setattr(config, "DISCORD_DETAIL_WEBHOOK_URL",
                            "https://discord.test/api/webhooks/2/SECRET")
        monkeypatch.setattr(config, "NTFY_TOPIC", "topic-secret")
        assert config.REAL_MONEY_SIGNALS_ENABLED is False
        with SessionLocal() as s:
            s.add(TrackedPosition(match_id="FX", market_id="M-3",
                                  market_title="Spain 2-1", entry_price=0.10,
                                  contracts=400, cost=42.0))
            s.commit()
        hold_rows = {"M-3": {"live_model_probability": 0.30,
                             "market_yes_bid": 0.12}}
        exit_rows = {"M-3": {"live_model_probability": 0.02,
                             "market_yes_bid": 0.10}}
        positions.evaluate_positions(hold_rows, "FX", 10, alert=True)
        pid = next(iter(positions._state))
        positions._state[pid]["ts"] -= 999
        out = positions.evaluate_positions(exit_rows, "FX", 55, alert=True)
        assert posts == [], "a cash-out read reached a transport"
        assert out[0]["verdict"] == "EXIT"          # still COMPUTED
        refusals = alerts.recent_refusals()
        assert refusals, "the refusal was silent"
        assert refusals[0]["dispatch_class"] == alerts.MODEL_SIGNAL
        assert refusals[0]["origin"].startswith("src.positions:")


class TestApi:
    def test_round_trip(self):
        _wipe()
        from fastapi.testclient import TestClient
        from api.main import app
        client = TestClient(app)
        r = client.post("/api/positions", json={"positions": [
            {"match_id": "FINAL", "market_id": "KXWCTOTAL-X-3",
             "market_title": "Over 2.5", "entry_price": 0.42,
             "contracts": 686}]})
        assert r.status_code == 200 and r.json()["added"] == 1
        pid = r.json()["ids"][0]
        with SessionLocal() as s:
            pos = s.get(TrackedPosition, pid)
            assert pos.cost == round(686 * (0.42 + 0.07*0.42*0.58), 2)
        assert client.get("/api/positions").status_code == 200
        assert client.delete(f"/api/positions/{pid}").json()["closed"] == pid
        with SessionLocal() as s:
            assert s.get(TrackedPosition, pid).closed_at is not None
