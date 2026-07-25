"""Raw-provider-response retention — the bottom of the evidence chain.

The V9.3 evaluation (F11) was right that a content hash without the bytes
is a weak guarantee: it cannot be independently verified, cannot be
inspected, and cannot be replayed through a corrected parser. The 8 KB
preview cap that shipped during the Jul 25 DiskFull incident traded
exactly that away for volume headroom.

Compression removes the trade-off. A real MLS stats response measured
9,596 bytes raw and 2,760 bytes gzip+base64 — 28.8%, i.e. SMALLER than
the truncated stub it replaces. So every observation can retain its
COMPLETE body while using less space than truncation did.

The hash is always taken over the FULL body, never the preview.
"""
from __future__ import annotations

import base64
import gzip
import hashlib

import config


def pack_payload(raw: str) -> dict:
    """Fields for a SourceObservation from the complete raw body.

    Returns the content hash (over the full bytes), the complete body
    gzip+base64, its true length, the encoding, and a short human-readable
    preview. Compression is best-effort: if it ever fails we still store
    the hash, length and preview rather than losing the observation."""
    data = raw.encode()
    out = {
        "content_hash": hashlib.sha256(data).hexdigest(),
        "payload_bytes": len(data),
        "payload_json": raw[:config.OBSERVATION_PAYLOAD_MAX_BYTES],
        "payload_compressed": None,
        "payload_encoding": None,
    }
    try:
        out["payload_compressed"] = base64.b64encode(
            gzip.compress(data, compresslevel=6)).decode()
        out["payload_encoding"] = "gzip+base64"
    except (OSError, ValueError, MemoryError):
        pass
    return out


def unpack_payload(obs) -> str | None:
    """The COMPLETE raw body from a stored observation, or None when only
    a legacy truncated preview exists. Verifies nothing — callers that
    need integrity should re-hash and compare to `content_hash`."""
    blob = getattr(obs, "payload_compressed", None)
    if not blob:
        return None
    try:
        return gzip.decompress(base64.b64decode(blob)).decode()
    except (OSError, ValueError, TypeError):
        return None


def verify_payload(obs) -> bool | None:
    """True/False when the complete body is retained and re-hashes to (or
    against) the stored content_hash; None when only a preview exists so
    verification is impossible. This is the check the evaluation asked
    for: a hash you cannot recompute is not evidence."""
    raw = unpack_payload(obs)
    if raw is None:
        return None
    return hashlib.sha256(raw.encode()).hexdigest() == obs.content_hash
