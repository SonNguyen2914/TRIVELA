"""Which git revision THIS PROCESS is running.

One function, its own module, and the module is the point.

WHY NOT IN `model_mls.py`, where the constant it mirrors already lives.
`engine_signature()` sha256s the SOURCE of four modules, and
`src.live.model_mls` is one of them. Adding a three-line accessor there
changes that file's bytes, which changes the signature hash, which fails
BOTH arms of the boot approval check — the stored hash reproduces under
neither the current revision nor the one the decision recorded — and the
plane goes dark until an operator reactivates it.

That is the guard working exactly as designed: it cannot distinguish
"an unrelated helper was added" from "the model changed", and it must
not try. `tests/test_team_style.py` pins those hashes and says in as
many words: do not update these literals to make the test pass.

The practical consequence is worth stating plainly, because it is not
obvious and it cost a first attempt at this file: **the four
engine-signature modules are effectively closed to any edit that is not
a deliberate modelling change.** Anything else — a helper, a docstring,
a type hint — buys a dark plane and a manual reactivation. Put it
somewhere else.

So this lives outside the signature, and the duplication of one
`os.getenv` line is deliberate. `tests/test_ready_code_revision.py`
asserts this function and `engine_signature()["code_revision"]` return
the same value, which is what actually keeps them honest — a shared
import would couple the modules and reintroduce the problem.
"""
from __future__ import annotations

import os

# Read at import, which is the point: the revision the container STARTED
# with, not whatever the environment says later.
_GIT_REV = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:40]


def running_code_revision() -> str | None:
    """The revision this process is running, or None when unknown.

    None rather than "" when `RAILWAY_GIT_COMMIT_SHA` is unset: an empty
    string serialises to `""`, which reads as "a revision" at a glance,
    and a local process must never be able to look like a deployed one.
    """
    return _GIT_REV or None
