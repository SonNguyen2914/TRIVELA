"""The readiness watch: shadow collection stopping must not be silent.

Boot fails closed on approval by design. The gap that opens is a deploy
whose operator never follows through — the plane stops taking locks and
every existing alert stays quiet, because they all fire on MLS events.
Silence is indistinguishable from a quiet evening. The DiskFull incident
failed exactly this way.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from src.live.models import ModelVersion
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc


def _set_approved(s, approved: bool):
    from src.live import model_mls
    mv = s.query(ModelVersion).filter_by(name=model_mls.MODEL_NAME).first()
    if mv is None:
        mv = ModelVersion(name=model_mls.MODEL_NAME,
                          approved_for_shadow=approved)
        s.add(mv)
    else:
        mv.approved_for_shadow = approved
    s.commit()


class TestReadinessWatch:
    def _reset(self, sched):
        sched._unapproved_since = None
        sched._unapproved_alerted = False

    def test_no_alert_while_approved(self, live_session, monkeypatch):
        import jobs.scheduler as sched
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: sent.append(m))
        self._reset(sched)
        _set_approved(live_session, True)
        sched.mls_readiness_watch()
        assert sent == []

    def test_no_alert_during_a_normal_deploy_window(self, live_session,
                                                    monkeypatch):
        """A deploy plus reactivation takes ~2 minutes. Alerting on that
        would train Son to ignore the channel."""
        import jobs.scheduler as sched
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: sent.append(m))
        self._reset(sched)
        _set_approved(live_session, False)
        sched.mls_readiness_watch()          # first sighting, starts clock
        sched._unapproved_since = (datetime.now(UTC)
                                   - timedelta(seconds=120))
        sched.mls_readiness_watch()
        assert sent == [], "alerted inside a normal deploy window"

    def test_alerts_once_a_stall_exceeds_the_threshold(self, live_session,
                                                       monkeypatch):
        import jobs.scheduler as sched
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: sent.append(m))
        self._reset(sched)
        _set_approved(live_session, False)
        sched.mls_readiness_watch()
        sched._unapproved_since = (
            datetime.now(UTC)
            - timedelta(seconds=sched.UNAPPROVED_ALERT_AFTER_S + 60))
        sched.mls_readiness_watch()
        assert len(sent) == 1
        assert "SHADOW COLLECTION STOPPED" in sent[0]
        assert "approval/activate" in sent[0]
        # and it does NOT repeat every two minutes
        sched.mls_readiness_watch()
        sched.mls_readiness_watch()
        assert len(sent) == 1, "the watch spams once it has complained"

    def test_recovery_is_announced_then_the_clock_resets(self, live_session,
                                                         monkeypatch):
        import jobs.scheduler as sched
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: sent.append(m))
        self._reset(sched)
        _set_approved(live_session, False)
        sched.mls_readiness_watch()
        sched._unapproved_since = (
            datetime.now(UTC)
            - timedelta(seconds=sched.UNAPPROVED_ALERT_AFTER_S + 60))
        sched.mls_readiness_watch()
        assert len(sent) == 1
        _set_approved(live_session, True)
        sched.mls_readiness_watch()
        assert len(sent) == 2 and "RESUMED" in sent[1]
        assert sched._unapproved_since is None
        assert sched._unapproved_alerted is False

    def test_recovery_is_silent_if_it_never_complained(self, live_session,
                                                       monkeypatch):
        """An all-clear for an alarm nobody heard is noise."""
        import jobs.scheduler as sched
        import src.alerts as alerts
        sent = []
        monkeypatch.setattr(alerts, "send_alert",
                            lambda m, **kw: sent.append(m))
        self._reset(sched)
        _set_approved(live_session, False)
        sched.mls_readiness_watch()          # started the clock, no alert
        _set_approved(live_session, True)
        sched.mls_readiness_watch()
        assert sent == []
