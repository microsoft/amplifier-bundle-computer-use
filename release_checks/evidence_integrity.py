"""Test-only archived integrity; never a current release qualification."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neutral_qualification as q


def _assert_artifact_references(value: object, root: Path) -> None:
    visited: set[Path] = set()

    def walk(node):
        if isinstance(node, dict):
            if {"kind", "path", "sha256"} <= node.keys():
                # Check every envelope, even a repeated path with another digest.
                path = q.reference_check(node, root)
                if path not in visited:
                    visited.add(path)
                    if (
                        path.suffix.lower() == ".json"
                        or path.read_bytes().lstrip().startswith((b"{", b"["))
                    ):
                        walk(q.load_json(path))
            else:
                for child in node.values():
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)


def assert_archived_evidence(document, expected, evidence_root):
    """Validate independent expectations and all references AS RECORDED only."""
    q.require(isinstance(document, dict), "malformed qualification")
    created_at = document.get("created_at")
    q.require(isinstance(created_at, str), "malformed timestamp")
    try:
        recorded_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise q.QualificationError("malformed timestamp") from exc
    q.timestamp(created_at, recorded_at)  # Includes the timezone requirement.
    q.validate(document, expected, now=recorded_at, evidence_root=evidence_root)
    _assert_artifact_references(document, evidence_root)
