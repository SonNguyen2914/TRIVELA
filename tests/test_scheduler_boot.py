"""Boot sequence: restore results -> resolve bracket -> prime predictions
-> prime odds, as ONE chained one-shot.

Regression for the boot race (prod 2026-07-12): restore_missing_results,
the bracket resolve and the prediction prime were four independent "date"
jobs, so the prime could run before the wiped QF results were re-frozen and
the SF slots filled — SF2 then served a symmetric default-stats prediction
(xg 1.398/1.398, advance ~0.50) for up to an hour after a deploy."""
import jobs.scheduler as sched


class TestBootSequence:
    def setup_method(self):
        self._restore = sched.live_state.restore_missing_results
        self._ledger = sched.bots.restore_from_archive
        self._bracket = sched.resolve_bracket_job
        self._hourly = sched.hourly_predictions
        self._poll = sched.poll_odds

    def teardown_method(self):
        sched.live_state.restore_missing_results = self._restore
        sched.bots.restore_from_archive = self._ledger
        sched.resolve_bracket_job = self._bracket
        sched.hourly_predictions = self._hourly
        sched.poll_odds = self._poll

    def _stub_all(self, calls, fail=()):
        def stub(name):
            def _f():
                calls.append(name)
                if name in fail:
                    raise RuntimeError(f"{name} exploded")
            return _f
        sched.live_state.restore_missing_results = stub("restore")
        sched.bots.restore_from_archive = stub("ledger")
        sched.resolve_bracket_job = stub("bracket")
        sched.hourly_predictions = stub("predictions")
        sched.poll_odds = stub("poll")

    def test_runs_in_dependency_order(self):
        # results BEFORE bracket (the resolver reads frozen MatchResults),
        # bracket BEFORE the prime (real names in the slots), predictions
        # BEFORE the odds prime (the poller needs model probs for edge).
        calls: list[str] = []
        self._stub_all(calls)
        sched.boot_sequence()
        assert calls == ["restore", "ledger", "bracket", "predictions", "poll"]

    def test_failing_step_never_skips_the_rest(self):
        # ESPN down during restore must still leave the bracket resolved
        # and the dashboard primed.
        calls: list[str] = []
        self._stub_all(calls, fail=("restore",))
        sched.boot_sequence()
        assert calls == ["restore", "ledger", "bracket", "predictions", "poll"]

    def test_every_step_is_isolated(self):
        calls: list[str] = []
        self._stub_all(calls, fail=("restore", "bracket", "predictions",
                                    "poll"))
        sched.boot_sequence()  # must not raise
        assert calls == ["restore", "ledger", "bracket", "predictions", "poll"]


class _RecordingScheduler:
    """Stands in for BackgroundScheduler: records add_job calls, runs
    nothing (a real one would fire the date jobs immediately)."""
    def __init__(self, timezone=None):
        self.jobs = []

    def add_job(self, func, trigger=None, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        pass


class TestBootRegistration:
    def test_single_chained_boot_job(self):
        # THE regression guard: the ARCHIVE boot is exactly one ordered
        # chain — re-adding a separate restore/prime/bracket "date" job
        # reintroduces the race. Each LIVE plane (MLS; EPL and La Liga
        # since 2026-07-28) gets its OWN isolated one-shot (plane
        # isolation: a live failure must never delay or break the
        # archive chain), and nothing else.
        orig = sched.BackgroundScheduler
        sched.BackgroundScheduler = _RecordingScheduler
        try:
            s = sched.start_scheduler()
        finally:
            sched.BackgroundScheduler = orig
        one_shots = [(func, kw.get("id")) for func, trigger, kw in s.jobs
                     if trigger == "date"]
        assert one_shots == [(sched.boot_sequence, "boot_sequence"),
                             (sched.mls_boot, "mls_boot"),
                             (sched.epl_boot, "epl_boot"),
                             (sched.laliga_boot, "laliga_boot")]

    def test_recurring_jobs_still_registered(self):
        # The chain must not have cannibalized the steady-state jobs.
        orig = sched.BackgroundScheduler
        sched.BackgroundScheduler = _RecordingScheduler
        try:
            s = sched.start_scheduler()
        finally:
            sched.BackgroundScheduler = orig
        ids = {kw.get("id") for _, trigger, kw in s.jobs if trigger != "date"}
        assert {"hourly", "final_lock", "odds_poll", "live_tick",
                "live_signals", "bracket"} <= ids
