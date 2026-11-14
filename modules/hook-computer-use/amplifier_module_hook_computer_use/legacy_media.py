"""Read-only recognition of the exact historical screenshot marker.

The owning host supplies a decoded mapping from its allowlisted, bounded parser.
This adapter never parses arbitrary strings, opens screenshot paths, scans logs,
or reconstructs capture consent from content, filesystem time or event time.
"""

from __future__ import annotations

from typing import Any

_MARKER = "__amplifier_computer_use__"


def recognize_legacy_screenshot_marker(value: Any) -> dict[str, Any] | None:
    """Return a protected disposition for an exact known image marker.

    Historical writers persisted only text and screenshot paths. Their exact
    issuance/file-identity records were bounded memory-only state; no durable
    original capture timestamp or expiry receipt was emitted. Recognizing the
    schema is therefore NOT provenance validation or permission to read pixels.

    A host encountering None must preserve its ordinary unknown-format hold,
    not infer that the input is ordinary unrestricted media. It alone binds the
    source SHA/record/image index and renders this known-owner hold.
    """
    if (
        type(value) is not dict
        or value.keys() != {_MARKER, "text", "images"}
        or type(value[_MARKER]) is not int
        or value[_MARKER] != 1
        or type(value["text"]) is not str
        or type(value["images"]) is not list
        or not value["images"]
        or any(type(path) is not str or not path for path in value["images"])
    ):
        return None
    return {
        "owner": "computer-use",
        "format": "legacy-screenshot-marker-v1",
        "status": "held",
        "reason": "missing-original-capture-receipt",
        "origin": None,
        "image_count": len(value["images"]),
        "pixels_converted": 0,
    }


def legacy_screenshot_origin_for(snapshot: dict[str, Any]) -> None:
    """Historical ``CaptureMediaOwner.origin_for`` callback: no original receipt.

    The snapshot is a host-bound descriptor with source_sha256, record_index,
    path, image_index, sha256, bytes and session_id. None of those fields records
    original screenshot creation, expiry or authority. This owner deliberately
    has no positive legacy lookup: it must return None even if a supplied event
    date, screenshot path, digest, index or current file happens to look valid.
    Prospective structured refs use their existing media store, not this adapter.
    """
    return None