"""A RATCHET against time-rotted tests.

Three Liga MX discovery tests pinned kickoffs at 2026-08-01 and went red
on origin/main the day after — a red suite nobody caused, masking real
failures. A blanket ban is impossible (recorded payloads and historical
sentinels legitimately pin dates), so this ratchets: the count of
absolute `datetime(20xx, ...)` literals per test file may FALL but never
RISE. Adding one forces you here, to either derive it from now() or
consciously raise the baseline in this file with a reason.
"""
import pathlib
import re

PAT = re.compile(r"datetime\(\s*20\d\d\s*,")

# counts as of 2026-08-04. Lower is fine; higher fails.
BASELINE = {}
_here = pathlib.Path(__file__).parent
for p in sorted(_here.glob("test_*.py")):
    if p.name == "test_no_new_absolute_dates.py":
        continue
    n = len(PAT.findall(p.read_text(encoding="utf-8")))
    if n:
        BASELINE[p.name] = n


def test_absolute_date_count_never_rises():
    # regenerate and compare against the committed snapshot below
    assert BASELINE == SNAPSHOT, (
        "absolute-date literals changed. If a count ROSE: derive the "
        "date from now() instead (see _upcoming_cross_midnight in "
        "test_ligamx_plane.py), or consciously update SNAPSHOT with a "
        "comment saying why the pin cannot rot. If it FELL: update "
        f"SNAPSHOT downward. diff={ {k: (SNAPSHOT.get(k), BASELINE.get(k)) for k in set(SNAPSHOT) | set(BASELINE) if SNAPSHOT.get(k) != BASELINE.get(k)} }")


# FROZEN LITERAL — the first draft wrote `SNAPSHOT = dict(BASELINE)`,
# recomputed at import, equal by construction, unable to fail: the
# vacuous-test disease inside the guard built to cure it. Committed
# numbers or no guard.
SNAPSHOT = {
    "test_core.py": 1,
    "test_epl_plane.py": 2,
    "test_friendlies_apif.py": 11,
    "test_hunter.py": 1,
    "test_hunter_live_stats.py": 1,
    "test_ladder_parity.py": 1,
    "test_laliga.py": 2,
    "test_mls_shadow.py": 8,
    "test_mls_xg_guard.py": 2,
    "test_narrator.py": 1,
    "test_score_classification.py": 1,
    "test_team_style.py": 1,
    "test_verify_apifootball.py": 3,
    "test_xg_ratings.py": 7,
}
