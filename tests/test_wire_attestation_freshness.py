"""Offline fail-closed consumer of independent neutral live qualification.

Historical HTTP outcomes remain history, byte-for-byte; they cannot satisfy the
active 30-day release gate. Root supplies genuine final-source receipts/captures
and independently established expected package/profile bindings. Missing proof
is a failure, never a skip or an old-HTTP fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from neutral_qualification import (
    MAX_AGE_DAYS,
    QualificationError,
    load_json,
    package_identity,
    production_manifest,
    script_manifest,
    validate,
)

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "wire-check.json"
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


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_exists_for_provider(provider):
    assert provider in load_json(FIXTURE_PATH), "missing historical attestation"


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_was_accepted(provider):
    # Historical acceptance only; no current/native/replay claim.
    assert load_json(FIXTURE_PATH)[provider]["http_status"] == 200


def test_neutral_live_qualification_is_fresh():
    try:
        expected = load_json(EXPECTED_PATH)
        assert expected["source"] == production_manifest(ROOT)
        assert expected["producer"]["scripts"] == script_manifest(ROOT)
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
        validate(
            load_json(QUALIFICATION_PATH),
            expected,
            evidence_root=QUALIFICATION_PATH.parent,
        )
    except QualificationError as exc:
        pytest.fail(
            f"Neutral live release gate UNMET: {exc}. Root owns genuine final-source qualification; do not fabricate fixtures."
        )


def test_max_age_days_is_documented_and_positive():
    assert MAX_AGE_DAYS == 30
