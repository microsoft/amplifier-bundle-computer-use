"""Retained historical staleness signal; NOT neutral-v1 live acceptance.

Fresh live qualification belongs to providers and remains UNMET under Root.
Do not rewrite historical fixture dates/results or run the retired bundle
wire-check instrument to make these warnings green. Provider-owned fresh
declaration/result/replay checks must qualify the released combination.

Historical rationale: gate test for layer 3 of the anti-regression scheme
(docs/designs/multi-provider-design.md Sec 11.2) - the dated wire attestation
`scripts/wire_check.py` produces in `tests/fixtures/wire-check.json`.

The design doc originally specified this layer as advisory: "it records; it
does not assert" - CI prints the attestation's age and a human is expected to
notice. The six-lens review (docs/designs/phase2-plans.md, "Council items
still open") named this as reproducing, by a different name, the exact
failure the layer exists to catch: 393 green unit tests and a green 4-stage
chain test, all sitting on a wire nobody had actually exercised, while the
live API rejected every request. This test IS the fix - it turns "prints; a
human notices" into "the suite goes red."

Pure logic, no network: this test only inspects the JSON attestation file
`scripts/wire_check.py` writes. It runs the same in CI (no keys, no network,
`ubuntu-latest` with no display - see CONTRIBUTING.md) as anywhere else,
which is the whole point - a check that can only pass online is a check that
gets disabled the first time CI has no network. Refreshing the attestation
(running `scripts/wire_check.py` with real API keys) is a separate,
periodic, human/CI-with-secrets action, in the same family as
`scripts/verify_coexistence.py` (CONTRIBUTING.md, "The ship gate"); THIS test
enforces that someone actually did it recently enough to trust.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "wire-check.json"

#: How long a wire attestation may go unrefreshed before the suite treats it
#: as untrustworthy. Judgement call, not evidence - the task's own author
#: doesn't have a base rate for how often vendor schemas tighten. Rationale:
#: schema-tightening incidents on record for this bundle are rare (one, so
#: far) but had zero warning and 100% request failure once they happened; a
#: monthly cadence catches drift within one ordinary release cycle without
#: demanding a live vendor call on every commit (this bundle ships far more
#: often than vendors change their computer-use schema).
MAX_AGE_DAYS = 30

#: Retained historical provider attestations. Fresh wire responsibility has
#: transferred to providers; Root owns the UNMET release obligation. Gemini/Qwen are
#: deliberately out of scope (docs/designs/phase2-plans.md Sec "Gemini and
#: Qwen") - the deferral is a decision already made and dated, not a gap.
REQUIRED_PROVIDERS = ("anthropic", "openai")


def _load_attestation() -> dict:
    if not FIXTURE_PATH.is_file():
        pytest.fail(
            f"{FIXTURE_PATH} does not exist - this wire has NEVER been "
            "attested against a real API. A missing attestation is treated "
            "as maximally stale, not skipped or ignored. Fresh provider-owned "
            "wire qualification remains UNMET under Root before shipping."
        )
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_exists_for_provider(provider: str) -> None:
    attestation = _load_attestation()
    assert provider in attestation, (
        f"no wire attestation recorded for {provider!r} - never verified "
        "live. Provider-owned fresh wire qualification remains UNMET under Root."
    )


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_was_accepted(provider: str) -> None:
    """The LAST live call recorded for this provider must have been accepted
    (HTTP 200). A recorded rejection is real, current evidence the wire is
    broken RIGHT NOW - this must fail exactly as loud as a genuine
    schema-tightening incident, because that is precisely what it is.
    """
    attestation = _load_attestation()
    entry = attestation.get(provider)
    assert entry is not None, f"no wire attestation recorded for {provider!r}"
    assert entry.get("http_status") == 200, (
        f"the last live wire-check for {provider!r} was REJECTED "
        f"(http_status={entry.get('http_status')}, error={entry.get('error')!r}, "
        f"model={entry.get('model')}, tool_type={entry.get('tool_type')}) - "
        "this is exactly the upstream-schema-tightening failure this gate "
        "exists to catch, not a test infrastructure problem."
    )


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_is_not_stale(provider: str) -> None:
    attestation = _load_attestation()
    entry = attestation.get(provider)
    assert entry is not None, f"no wire attestation recorded for {provider!r}"
    checked_at = datetime.fromisoformat(entry["checked_at"])
    age = datetime.now(timezone.utc) - checked_at
    assert age <= timedelta(days=MAX_AGE_DAYS), (
        f"the wire attestation for {provider!r} is {age.days} day(s) old "
        f"(recorded {entry['checked_at']}), older than the {MAX_AGE_DAYS}-day "
        "limit (test_wire_attestation_freshness.MAX_AGE_DAYS). A green test "
        "suite next to a stale attestation is exactly the false confidence "
        "that let 393 green tests sit on a wire nobody had exercised. "
        "Historical evidence is unchanged. Fresh provider-owned wire qualification "
        "remains UNMET under Root; do not manufacture a new bundle attestation."
    )


def test_max_age_days_is_documented_and_positive() -> None:
    """Guards the constant itself against an accidental 0/negative value that
    would make every attestation permanently stale (or the check a no-op)."""
    assert MAX_AGE_DAYS > 0
