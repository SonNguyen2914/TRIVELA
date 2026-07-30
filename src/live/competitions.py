"""The competition registry (EPL P0 remediation, 2026-07-29).

ONE place that answers "what is competition-specific about this slug?"
— the model module, the model version name, the provider league, the
master flag. Before this existed, the shared machinery reached for
`model_mls` and `"mls-2026"` directly from inside audit, replay, paper
and the corpus exporter, so a second competition's evidence was scored,
hashed and exported against the FIRST competition's model.

Two rules this module exists to enforce:

  1. **An unknown slug fails explicitly.** `spec()` raises rather than
     falling back to MLS. A silent default is exactly how EPL state
     ended up inside MLS outputs, and AGENTS.md §6 is unambiguous: an
     ambiguous identity match must fail, never silently pick.

  2. **The default is a call-site choice, never a lookup fallback.**
     Shared functions keep `competition_slug=DEFAULT_SLUG` in their
     signatures so every existing MLS caller behaves identically; what
     they must not do is accept an arbitrary slug and quietly serve MLS.
"""
from __future__ import annotations

import importlib

MLS_SLUG = "mls-2026"
EPL_SLUG = "epl-2026"
LALIGA_SLUG = "la-liga-2026"

# MLS is the default because every pre-existing call site meant MLS.
DEFAULT_SLUG = MLS_SLUG

_REGISTRY: dict[str, dict] = {
    MLS_SLUG: {
        "label": "MLS",
        "model_module": "src.live.model_mls",
        "espn_league": "usa.1",
        "enabled_flag": "MLS_SHADOW_ENABLED",
    },
    EPL_SLUG: {
        "label": "EPL",
        "model_module": "src.live.model_epl",
        "espn_league": "eng.1",
        "enabled_flag": "EPL_SHADOW_ENABLED",
    },
    # La Liga. `LALIGA_SHADOW_ENABLED` defaults FALSE (config.py), unlike
    # the other two — registering the slug does not start the plane, it
    # only stops the shared machinery from serving MLS when asked for
    # La Liga. laliga-2026-v0 is DARK: no approval decision exists, so
    # runs and locks refuse at the F3/F9 gates regardless of the flag.
    LALIGA_SLUG: {
        "label": "LALIGA",
        "model_module": "src.live.model_laliga",
        "espn_league": "esp.1",
        "enabled_flag": "LALIGA_SHADOW_ENABLED",
    },
}


def known() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def spec(competition_slug: str | None = None) -> dict:
    slug = competition_slug or DEFAULT_SLUG
    try:
        return _REGISTRY[slug]
    except KeyError:
        raise KeyError(
            f"unknown competition slug {slug!r} — register it in "
            f"src/live/competitions.py rather than letting it fall back "
            f"to {DEFAULT_SLUG!r} (known: {', '.join(known())})") from None


def model_module(competition_slug: str | None = None):
    return importlib.import_module(spec(competition_slug)["model_module"])


def model_name(competition_slug: str | None = None) -> str:
    return model_module(competition_slug).MODEL_NAME


def label(competition_slug: str | None = None) -> str:
    return spec(competition_slug)["label"]


def espn_league(competition_slug: str | None = None) -> str:
    return spec(competition_slug)["espn_league"]


def shadow_enabled(competition_slug: str | None = None) -> bool:
    """The competition's master switch, read at call time so an env flip
    needs no re-import."""
    import config
    return bool(getattr(config, spec(competition_slug)["enabled_flag"],
                        False))


def slug_for_model_name(name: str) -> str | None:
    """Which competition a model version belongs to. Returns None for an
    unrecognised name — the caller must then refuse, not guess."""
    for slug in _REGISTRY:
        try:
            if model_name(slug) == name:
                return slug
        except Exception:                 # a model module that won't load
            continue
    return None
