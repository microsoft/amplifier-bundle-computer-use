"""Strict current-checkout, installed-package and real-clock release consumer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "release_checks"))
from evidence_integrity import _assert_artifact_references
from neutral_qualification import (
    QualificationError,
    load_json,
    package_identity,
    production_manifest,
    script_manifest,
    validate,
)

QUALIFICATION_PATH = ROOT / "tests" / "fixtures" / "neutral-qualification.json"
EXPECTED_PATH = ROOT / "tests" / "fixtures" / "neutral-qualification-expected.json"
REQUIRED_PROVIDERS = ("anthropic", "openai")
PROVIDER_REVISIONS = {
    "anthropic": "9f9c50c040a2d5f4f24e2f7b9d196d30bf54608b",
    "openai": "38fee172673d196ba54b5e99e434490b86290c5d",
}
SDK_PROFILES = {
    "floor": {"anthropic": "1.0.0", "openai": "2.9.0"},
    "current": {"anthropic": "1.13.0", "openai": "3.29.0"},
}


def test_neutral_live_qualification_is_fresh():
    try:
        expected = load_json(EXPECTED_PATH)
        assert expected["source"] == production_manifest(ROOT), (
            "current production source manifest mismatch"
        )
        assert expected["producer"]["scripts"] == script_manifest(ROOT), (
            "current producer script manifest mismatch"
        )
        assert {b["provider"] for b in expected["bindings"]} == set(REQUIRED_PROVIDERS)
        active_profile = "floor" if sys.version_info[:2] == (3, 11) else "current"
        for binding in expected["bindings"]:
            packages = binding["identities"]
            name = binding["provider"]
            assert packages["provider"]["revision"] == PROVIDER_REVISIONS[name]
            assert packages["sdk"]["version"] == SDK_PROFILES[binding["profile"]][name]
            assert (
                packages["loop"]["revision"]
                == "dd916499344671e0209d4951bc025777e3e891fd"
            )
            assert (
                packages["context"]["revision"]
                == "db641363013ff906b656f49498a87ab01f5c506f"
            )
            if binding["profile"] == active_profile:
                for role, package in packages.items():
                    observed = package_identity(
                        package["distribution"],
                        package["module"],
                        installed=role not in {"tool", "hook"},
                    )
                    assert observed == package, (
                        "actually loaded package identity mismatch"
                    )
        document = load_json(QUALIFICATION_PATH)
        validate(
            document,
            expected,
            evidence_root=QUALIFICATION_PATH.parent,
        )
        _assert_artifact_references(document, QUALIFICATION_PATH.parent)
    except QualificationError as exc:
        pytest.fail(
            f"Neutral live release gate UNMET: {exc}. Root owns genuine final-source qualification; do not fabricate fixtures."
        )
