#!/usr/bin/env python3
"""Run the TASK-12 M3 quarantine-variant evaluation and archive it.

    .venv/bin/python scripts/run_xg_quarantine_variant.py \
        --out research_archive/xg_quarantine_variant_$(date -u +%F).json

A thin wrapper over `model_eval.evaluate_xg_quarantine_variant` — the
measurement lives in the eval module beside the machinery it uses, this
script only runs it against the live plane and writes the archive
document. It needs the live database (LIVE_DATABASE_URL), so it runs
where the plane runs; from anywhere else it reports the dormant state
rather than a fake result.

Nothing here changes MLS_XG_QUARANTINE_EXCLUDE, touches model_mls, or
alters the frozen production ladder. The output is the NUMBER (with its
bootstrap CI) the flag decision cites — the decision itself is the
operator's.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    from src.live import model_eval
    doc = model_eval.evaluate_xg_quarantine_variant(n_boot=a.n_boot,
                                                    seed=a.seed)
    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    print(text)
    return 0 if not doc.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
