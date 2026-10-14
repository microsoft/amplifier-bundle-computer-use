"""Archived integrity and unit guards for the separate strict release consumer.

Retained observations are checked AS RECORDED against independent expectations,
not against today's clock or packages. Unit guards invoke the actual release
consumer in a SYNTHETIC ENVIRONMENT UNIT CONTROL ONLY; these are not qualified
actual factories, profiles or acceptance. None refresh or grant qualification.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "release_checks"))
import neutral_qualification as nq
from evidence_integrity import _assert_artifact_references, assert_archived_evidence
from neutral_qualification import MAX_AGE_DAYS, load_json

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "wire-check.json"
QUALIFICATION_PATH = ROOT / "tests" / "fixtures" / "neutral-qualification.json"
EXPECTED_PATH = ROOT / "tests" / "fixtures" / "neutral-qualification-expected.json"
REQUIRED_PROVIDERS = ("anthropic", "openai")


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_exists_for_provider(provider):
    assert provider in load_json(FIXTURE_PATH), "missing historical attestation"


@pytest.mark.parametrize("provider", REQUIRED_PROVIDERS)
def test_wire_attestation_was_accepted(provider):
    # Historical acceptance only; no current/native/replay claim.
    assert load_json(FIXTURE_PATH)[provider]["http_status"] == 200


def test_archived_neutral_evidence_integrity():
    assert_archived_evidence(
        load_json(QUALIFICATION_PATH),
        load_json(EXPECTED_PATH),
        QUALIFICATION_PATH.parent,
    )


def test_max_age_days_is_documented_and_positive():
    assert MAX_AGE_DAYS == 30


@pytest.fixture
def release_consumer():
    # Keep the release test function off this module's collection namespace.
    spec = importlib.util.spec_from_file_location(
        "_neutral_release_consumer_unit_control",
        ROOT / "release_checks" / "test_neutral_release_qualification.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recorded_evidence():
    return (
        load_json(QUALIFICATION_PATH),
        load_json(EXPECTED_PATH),
        QUALIFICATION_PATH.parent,
    )


def _set_release_clock(monkeypatch, instant):
    class ControlClock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(nq, "datetime", ControlClock)


@pytest.fixture
def release_control(monkeypatch, release_consumer, recorded_evidence):
    """SYNTHETIC ENVIRONMENT UNIT CONTROL ONLY, not actual qualification."""
    expected = recorded_evidence[1]
    active_profile = "floor" if sys.version_info[:2] == (3, 11) else "current"
    identities = {
        (package["distribution"], package["module"]): copy.deepcopy(package)
        for binding in expected["bindings"]
        if binding["profile"] == active_profile
        for package in binding["identities"].values()
    }

    def production_manifest(_root):
        return copy.deepcopy(expected["source"])

    def script_manifest(_root):
        return copy.deepcopy(expected["producer"]["scripts"])

    def package_identity(distribution, module, *, installed=True):
        return copy.deepcopy(identities[(distribution, module)])

    monkeypatch.setattr(release_consumer, "production_manifest", production_manifest)
    monkeypatch.setattr(release_consumer, "script_manifest", script_manifest)
    monkeypatch.setattr(release_consumer, "package_identity", package_identity)
    return release_consumer


@pytest.fixture
def release_at_recording(monkeypatch, release_control, recorded_evidence):
    # Only unit drift/tamper controls use recorded time; direct release is real time.
    _set_release_clock(
        monkeypatch, datetime.fromisoformat(recorded_evidence[0]["created_at"])
    )
    return release_control


def test_archived_integrity_never_queries_current_environment(
    monkeypatch, release_consumer, recorded_evidence
):
    def forbidden(*args, **kwargs):
        raise AssertionError("archived integrity queried current environment")

    for name in ("production_manifest", "script_manifest", "package_identity"):
        monkeypatch.setattr(nq, name, forbidden)
        monkeypatch.setattr(release_consumer, name, forbidden)
    assert_archived_evidence(*recorded_evidence)


def test_release_consumer_baseline_in_synthetic_environment(release_at_recording):
    # Hypothetical matching environment at recording time, not release acceptance.
    release_at_recording.test_neutral_live_qualification_is_fresh()


def test_future_clock_preserves_archive_but_expires_release(
    monkeypatch, release_control, recorded_evidence
):
    document, expected, root = recorded_evidence
    future = datetime.fromisoformat(document["created_at"]) + timedelta(
        days=MAX_AGE_DAYS + 1
    )
    _set_release_clock(monkeypatch, future)
    assert_archived_evidence(document, expected, root)
    with pytest.raises(pytest.fail.Exception, match="future or stale evidence"):
        release_control.test_neutral_live_qualification_is_fresh()


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("production_manifest", "current production source manifest mismatch"),
        ("script_manifest", "current producer script manifest mismatch"),
    ],
)
def test_source_drift_preserves_archive_but_rejects_release(
    monkeypatch, release_at_recording, recorded_evidence, name, message
):
    original = getattr(release_at_recording, name)

    def drift(root):
        observed = original(root)
        observed[next(iter(observed))] = "0" * 64
        return observed

    monkeypatch.setattr(release_at_recording, name, drift)
    assert_archived_evidence(*recorded_evidence)
    with pytest.raises(AssertionError, match=message):
        release_at_recording.test_neutral_live_qualification_is_fresh()


def test_loaded_package_drift_preserves_archive_but_rejects_release(
    monkeypatch, release_at_recording, recorded_evidence
):
    original = release_at_recording.package_identity

    def drift(*args, **kwargs):
        # Alter only the hypothetical environment's stored package identity.
        return {**original(*args, **kwargs), "version": "unit-drift"}

    monkeypatch.setattr(release_at_recording, "package_identity", drift)
    assert_archived_evidence(*recorded_evidence)
    with pytest.raises(
        AssertionError, match="actually loaded package identity mismatch"
    ):
        release_at_recording.test_neutral_live_qualification_is_fresh()


@pytest.fixture
def copied_evidence(tmp_path, monkeypatch, release_at_recording, recorded_evidence):
    root = tmp_path / "fixtures"
    shutil.copytree(recorded_evidence[2], root)
    qualification = root / QUALIFICATION_PATH.name
    expected = root / EXPECTED_PATH.name
    monkeypatch.setattr(release_at_recording, "QUALIFICATION_PATH", qualification)
    monkeypatch.setattr(release_at_recording, "EXPECTED_PATH", expected)
    return release_at_recording, load_json(qualification), load_json(expected), root


def _save(path, value):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _assert_both_reject(copied_evidence, message):
    release, document, expected, root = copied_evidence
    _save(release.QUALIFICATION_PATH, document)
    with pytest.raises(nq.QualificationError, match=message):
        assert_archived_evidence(document, expected, root)
    with pytest.raises(pytest.fail.Exception, match=message):
        release.test_neutral_live_qualification_is_fresh()


def test_bad_receipt_hash_rejected_by_archive_and_release(copied_evidence):
    copied_evidence[1]["rows"][0]["artifacts"][0]["sha256"] = "0" * 64
    _assert_both_reject(copied_evidence, "artifact hash mismatch")


def _references(value):
    if isinstance(value, dict):
        if {"kind", "path", "sha256"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _rehash_copied_ancestors(document, root, bad_leaf):
    """Bind changed temporary parents, never repair the deliberately bad leaf."""
    visited = set()

    def walk(value):
        for ref in _references(value):
            path = root / ref["path"]
            if path == bad_leaf:
                continue
            if path.suffix == ".json" and path not in visited:
                visited.add(path)
                data = load_json(path)
                before = copy.deepcopy(data)
                walk(data)
                if data != before:
                    _save(path, data)
            ref["sha256"] = nq.sha256(path)

    walk(document)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("bytes", "artifact hash mismatch"),
        ("digest", "artifact hash mismatch"),
        ("json", "missing or malformed evidence JSON"),
    ],
)
def test_deep_native_body_rejected_by_archive_and_release(
    copied_evidence, corruption, message
):
    _, document, _, root = copied_evidence
    native_path = root / "evidence" / "anthropic" / "native-observation.json"
    native = load_json(native_path)
    ref = next(
        r for r in _references(native) if r["path"].endswith("/007-response.json")
    )
    body = root / ref["path"]
    if corruption == "bytes":
        body.write_bytes(body.read_bytes() + b"\n")
    else:
        if corruption == "json":
            body.write_bytes(b"{")
            ref["sha256"] = nq.sha256(body)
        else:
            ref["sha256"] = "0" * 64
        _save(native_path, native)
        _rehash_copied_ancestors(document, root, body)
    _assert_both_reject(copied_evidence, message)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("failed", "qualification did not pass"),
        ("missing_row", "missing required qualification"),
        ("missing_artifact", "missing or escaped artifact"),
        ("synthetic", "not genuine live/offline evidence"),
    ],
)
def test_failed_missing_or_synthetic_evidence_rejected(
    copied_evidence, corruption, message
):
    _, document, _, root = copied_evidence
    if corruption == "failed":
        document["status"] = "FAIL"
    elif corruption == "missing_row":
        document["rows"].pop()
    else:
        ref = document["rows"][0]["artifacts"][0]
        path = root / ref["path"]
        if corruption == "missing_artifact":
            path.unlink()
        else:
            receipt = load_json(path)
            receipt["evidence_class"] = "test-control"
            _save(path, receipt)
            ref["sha256"] = nq.sha256(path)
    _assert_both_reject(copied_evidence, message)


def test_missing_report_rejected_by_archive_and_release(copied_evidence):
    release, _, expected, root = copied_evidence
    release.QUALIFICATION_PATH.unlink()
    with pytest.raises(
        nq.QualificationError, match="missing or malformed evidence JSON"
    ):
        assert_archived_evidence(load_json(release.QUALIFICATION_PATH), expected, root)
    with pytest.raises(
        pytest.fail.Exception, match="missing or malformed evidence JSON"
    ):
        release.test_neutral_live_qualification_is_fresh()


@pytest.mark.parametrize(
    ("created_at", "message"),
    [
        (None, "malformed timestamp"),
        ("not-a-timestamp", "malformed timestamp"),
        ("naive", "timestamp lacks timezone"),
    ],
)
def test_archive_requires_valid_aware_timestamp(recorded_evidence, created_at, message):
    document, expected, root = recorded_evidence
    if created_at == "naive":
        created_at = (
            datetime.fromisoformat(document["created_at"])
            .replace(tzinfo=None)
            .isoformat()
        )
    document["created_at"] = created_at
    with pytest.raises(nq.QualificationError, match=message):
        assert_archived_evidence(document, expected, root)


@pytest.mark.parametrize("name", ["body.json", "body.log"])
def test_recursive_reference_checks_every_digest_and_closed_envelope(tmp_path, name):
    path = tmp_path / name
    leaf = tmp_path / "leaf.bin"
    leaf.write_bytes(b"unit-only")
    _save(
        path,
        {
            "path": "/v1/messages",
            "kind": "generation",
            "refs": [nq.artifact(tmp_path, leaf.name, "observation")],
        },
    )
    ref = nq.artifact(tmp_path, path.name, "observation")
    _assert_artifact_references([ref, dict(ref)], tmp_path)
    with pytest.raises(nq.QualificationError, match="artifact hash mismatch"):
        _assert_artifact_references([ref, {**ref, "sha256": "0" * 64}], tmp_path)
    with pytest.raises(nq.QualificationError, match="malformed artifact reference"):
        _assert_artifact_references({**ref, "extra": True}, tmp_path)
    leaf.write_bytes(b"unit-corruption")
    with pytest.raises(nq.QualificationError, match="artifact hash mismatch"):
        _assert_artifact_references(ref, tmp_path)
